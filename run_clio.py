"""
run_clio.py — Entrypoint UNICO di Clio (STEP 5: processo unico + control-plane centralizzato).

Un solo processo Python:
  - Flask/core (clio_core) avviato in thread NON bloccante (wakeword + proattivo + API :5000);
  - Bot Telegram (bot.main) sul thread principale, che possiede l'event loop asyncio.

Cosi' policy._BOT / policy._LOOP (impostati da bot.main via policy.bind) sono nello STESSO
processo di tutte le superfici (Flask, voce, proattivo, Jarvis): la conferma irreversibile
cross-superficie funziona. Nessun IPC.

STEP 5B: apertura automatica della Web UI locale (comportamento storico dell'HQ), con
guardia anti-doppia-istanza — se la porta 5000 e' gia' occupata NON avvia un secondo
runtime (niente doppio Flask / doppio polling Telegram) ma apre solo la UI esistente.

STEP 6 (fix apertura Web UI): l'apertura del browser ora
  - attende che Flask risponda davvero su :5000 (niente tab su "connessione rifiutata");
  - verifica l'ESITO di webbrowser.open (che ritorna False in silenzio se fallisce);
  - ha fallback nativi Windows: os.startfile e `cmd /c start`;
  - logga chiaramente ogni tentativo e ogni fallimento;
  - puo' essere disattivata con l'argomento --no-browser (loggata).
"""
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser

# Logging visibile anche PRIMA di importare clio_core (es. ramo "Clio gia' in esecuzione").
# Idempotente: se clio_core chiama basicConfig piu' tardi, non fa nulla (root gia' configurato).
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RunClio")

WEB_UI_URL = "http://localhost:5000/"
WEB_UI_HOST = "127.0.0.1"
WEB_UI_PORT = 5000


def _porta_occupata(host=WEB_UI_HOST, port=WEB_UI_PORT):
    """True se qualcosa e' gia' in ascolto sulla porta (Clio gia' avviata)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            return s.connect_ex((host, port)) == 0
        except OSError:
            return False


def _attendi_flask(timeout=20.0, host=WEB_UI_HOST, port=WEB_UI_PORT):
    """Attende che Flask sia davvero in ascolto. Ritorna True se pronto entro il timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _porta_occupata(host, port):
            return True
        time.sleep(0.25)
    return False


def _apri_browser(url=WEB_UI_URL):
    """Apre la Web UI nel browser predefinito con fallback nativi Windows.

    Ritorna True se ALMENO un metodo ha riferito successo. Ogni tentativo e'
    loggato: questo rende diagnosticabile il caso "il browser non si apre".
    """
    # 1) webbrowser: rispetta il browser predefinito dell'utente.
    try:
        esito = webbrowser.open(url)
        logger.info(f"🌐 [WEB UI] webbrowser.open('{url}') -> {esito}")
        if esito:
            return True
    except Exception as e:
        logger.warning(f"🌐 [WEB UI] webbrowser.open fallito: {e}")

    # 2) os.startfile: associazione predefinita di Windows (spesso piu' affidabile
    #    di webbrowser quando il browser non e' registrato in webbrowser._tryorder).
    try:
        os.startfile(url)  # type: ignore[attr-defined]  # solo Windows
        logger.info(f"🌐 [WEB UI] os.startfile('{url}') eseguito (fallback 1).")
        return True
    except Exception as e:
        logger.warning(f"🌐 [WEB UI] os.startfile fallito: {e}")

    # 3) Ultimo fallback: shell "start" (usa l'associazione di Windows ShellExecute).
    try:
        subprocess.Popen(f'start "" "{url}"', shell=True)
        logger.info(f"🌐 [WEB UI] fallback 'start {url}' eseguito (fallback 2).")
        return True
    except Exception as e:
        logger.error(f"🌐 [WEB UI] impossibile aprire il browser: {e}")
    return False


def _saluto_avvio(parla=True):
    """Saluto d'avvio con numeri REALI letti a runtime (mai inventati).

    Fonti: registry.AZIONI_MAP, note del vault, .pipeline_status.json.
    Se una fonte manca, la frase lo dice invece di inventare un valore.
    """
    try:
        from startup_summary import build_greeting, get_startup_summary

        riepilogo = get_startup_summary()
        testo = build_greeting(riepilogo)
        logger.info(f"👋 [SALUTO] {testo}")
        logger.info(f"👋 [SALUTO][dati] {riepilogo}")
        if parla:
            import clio_core
            clio_core.clio_parla(testo)
    except Exception as e:  # il saluto non deve mai impedire l'avvio
        logger.warning(f"⚠️ Saluto d'avvio non disponibile: {e}")


def main():
    no_browser = "--no-browser" in sys.argv[1:]
    no_greeting = "--no-greeting" in sys.argv[1:]

    # Guardia anti-doppia-istanza: se Clio e' gia' in esecuzione, apri solo la Web UI ed esci.
    if _porta_occupata():
        logger.info("Clio risulta gia' in esecuzione (porta 5000 occupata): apro solo la Web UI.")
        if no_browser:
            logger.info("🌐 [WEB UI] apertura browser disattivata (--no-browser).")
        else:
            _apri_browser()
        return

    # 1) Core + Flask in thread (non blocca)
    import clio_core
    clio_core.start_core_services(run_flask_blocking=False)
    logger.info("Core services avviati (Flask in thread).")

    # 2) Apertura automatica della Web UI quando Flask e' su (comportamento storico)
    def _apri_web_ui():
        if no_browser:
            logger.info("🌐 [WEB UI] apertura browser disattivata (--no-browser).")
            return
        logger.info(f"🌐 [WEB UI] attendo che Flask risponda su {WEB_UI_HOST}:{WEB_UI_PORT}...")
        if _attendi_flask():
            logger.info("🌐 [WEB UI] Flask pronto: apro il browser.")
        else:
            logger.warning("🌐 [WEB UI] Flask non risponde entro il timeout: apro comunque il browser.")
        _apri_browser()

    threading.Timer(2.5, _apri_web_ui).start()

    # 3) Saluto vocale con numeri veri, dopo che il core e' su (mai bloccante).
    if no_greeting:
        logger.info("👋 [SALUTO] disattivato (--no-greeting).")
    else:
        threading.Timer(4.0, _saluto_avvio).start()

    # 4) Bot Telegram sul thread principale (owner del loop asyncio + control-plane)
    import bot
    logger.info("Avvio bot Telegram (run_polling) sul thread principale...")
    bot.main()


if __name__ == "__main__":
    main()
