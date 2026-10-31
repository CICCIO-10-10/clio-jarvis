# -*- coding: utf-8 -*-
"""test_voice_reale.py — collaudo MANUALE della catena vocale REALE di CLIO.

NON è un test pytest (nessuna funzione `test_*`): è uno script che apre davvero
microfono e casse. Serve per l'unica cosa che i mock non possono coprire:

    microfono -> Whisper (Groq) -> CLIO -> edge-tts -> casse

Cosa fa, in ordine:
  1. annuncia a voce il test (dà il tempo di prepararsi);
  2. chiede conferma VOCALE con `policy._voice_confirm('esegui_coding_task', {})`
     (TTS -> ascolto 15 s -> Whisper -> 'yes' / 'no' / 'timeout');
  3. se 'yes'  -> `coding_agent.run_coding_task('aggiungi funzione divisione in main.py')`
     se 'no'   -> stampa "Annullato." e non esegue nulla
     altrimenti -> timeout: non esegue nulla (fail-safe del control-plane);
  4. riporta l'esito (diff, pytest) e ripristina la sandbox con `git reset --hard`.

Uso:
    python test_voice_reale.py                   # CODER simulato: < 30 s, deterministico
    python test_voice_reale.py --modello-reale    # agente reale (OpenCode / LLM locale)

La catena vocale è SEMPRE reale. Solo il passo di coding usa, per default, un
"CODER" deterministico: così il collaudo misura la voce, non la qualità o la
latenza del modello. La sandbox deve essere pulita all'avvio: se non lo è, lo
script si ferma senza toccare nulla.
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import coding_agent          # noqa: E402
import policy                # noqa: E402
from clio_voice import parla   # noqa: E402  (TTS reale; riconfigura anche stdout)

TASK = "aggiungi funzione divisione in main.py"

# Patch che il "CODER" deterministico restituisce: funzione + test (contratto R4).
MAIN_PATCH = '''"""Sandbox di coding per CLIO — codice di partenza volutamente minimale."""


def somma(a, b):
    """Ritorna la somma di due numeri."""
    return a + b


def divisione(a, b):
    """Ritorna a / b; solleva ZeroDivisionError se b è 0."""
    if b == 0:
        raise ZeroDivisionError("divisione per zero")
    return a / b


if __name__ == "__main__":
    print("1 + 2 =", somma(1, 2))
    print("6 / 3 =", divisione(6, 3))
'''

TEST_PATCH = '''import pytest

from main import divisione, somma


def test_somma_interi():
    assert somma(1, 2) == 3


def test_somma_negativi():
    assert somma(-2, 5) == 3


def test_somma_zero():
    assert somma(0, 0) == 0


def test_divisione():
    assert divisione(6, 3) == 2


def test_divisione_negativi():
    assert divisione(-6, 3) == -2


def test_divisione_per_zero():
    with pytest.raises(ZeroDivisionError):
        divisione(1, 0)
'''


class _CatturaLog(logging.Handler):
    """Tiene traccia delle righe di log: serve a mostrare cosa ha sentito CLIO."""

    def __init__(self):
        super().__init__()
        self.righe = []

    def emit(self, record):
        try:
            self.righe.append(record.getMessage())
        except Exception:
            pass


def _git(root, *args):
    """Esegue git e restituisce (returncode, stdout+stderr)."""
    proc = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))


def _usa_coder_simulato():
    """Sostituisce SOLO il modello: il resto di run_coding_task resta reale."""

    def finto_modello(messages, deadline):
        return (
            json.dumps({"files": [
                {"path": "main.py", "content": MAIN_PATCH},
                {"path": "test_main.py", "content": TEST_PATCH},
            ]}),
            "coder-simulato",
        )

    def agente(task, workspace=None, timeout=None):
        # OpenCode non viene invocato: si usa il CODER locale con il modello finto.
        return coding_agent._local_agent(
            task, workspace, timeout or coding_agent.TASK_TIMEOUT
        )

    coding_agent._default_agent = agente
    coding_agent._call_coder = finto_modello


def _ripristina_sandbox(sandbox):
    """`git reset --hard` + verifica: la sandbox torna esattamente a HEAD."""
    rc, out = _git(sandbox, "reset", "--hard")
    _, stato = _git(sandbox, "status", "--porcelain")
    pulita = rc == 0 and not stato.strip()
    print(f"🧹 Sandbox ripristinata: git reset --hard rc={rc}, "
          f"working tree {'PULITO' if pulita else 'ANCORA SPORCO'}")
    if not pulita:
        print(out.strip()[:500])
        print(stato.strip()[:500])
    return pulita


def main():
    parser = argparse.ArgumentParser(description="Collaudo vocale reale di CLIO")
    parser.add_argument(
        "--modello-reale", action="store_true",
        help="usa l'agente di coding reale (OpenCode / LLM locale) invece del CODER simulato",
    )
    args = parser.parse_args()

    cattura = _CatturaLog()
    cattura.setLevel(logging.INFO)
    logging.getLogger().addHandler(cattura)
    logging.getLogger().setLevel(logging.INFO)

    sandbox = Path(coding_agent.get_sandbox_root()).resolve()

    print("=" * 74)
    print("🎙️  COLLAUDO VOCALE REALE DI CLIO  (microfono + casse veri)")
    print("=" * 74)
    print(f"Sandbox  : {sandbox}")
    print(f"STT      : Groq Whisper (fallback Google)")
    print(f"TTS      : edge-tts")
    print(f"Canale   : {'Telegram' if policy._telegram_disponibile() else 'conferma VOCALE (Telegram non configurato)'}")
    print(f"Coder    : {'reale (OpenCode/LLM locale)' if args.modello_reale else 'simulato deterministico'}")
    print("=" * 74, flush=True)

    if not (sandbox / ".git").is_dir():
        print(f"❌ Sandbox non trovata o non è un repo git: {sandbox}")
        return 3

    _, stato_iniziale = _git(sandbox, "status", "--porcelain")
    if stato_iniziale.strip():
        print("❌ La sandbox ha modifiche non committate: mi fermo per non "
              "sovrascriverle. Fai commit o `git reset --hard` e riprova.")
        print(stato_iniziale.strip()[:500])
        return 3

    esito_codice = 1
    try:
        # 1. Annuncio vocale: dà il tempo di prepararsi alla risposta.
        parla("Test vocale reale. Tra un istante ti chiederò conferma. "
              "Rispondi sì oppure no, ad alta voce.")
        time.sleep(0.8)

        # 2. Conferma vocale REALE (TTS -> microfono -> Whisper -> decisione).
        t0 = time.time()
        decisione = policy._voice_confirm("esegui_coding_task", {})
        durata = time.time() - t0

        sentito = [r for r in cattura.righe if "risposta=" in r]
        print("\n" + "-" * 74)
        print(f"🗣️  CLIO ha sentito: {sentito[-1] if sentito else '(nessuna trascrizione)'}")
        print(f"🎯 Decisione: {decisione!r}  (ascolto + trascrizione: {durata:.1f}s)")
        print("-" * 74, flush=True)

        if decisione == "no":
            print("Annullato.")
            parla("Annullato, Capo. Nessuna modifica.")
            _ripristina_sandbox(sandbox)      # nulla da ripristinare, ma verifichiamo
            return 0

        if decisione != "yes":
            print("⌛ Timeout: nessuna conferma vocale valida, azione NON eseguita.")
            parla("Non ho sentito una conferma, Capo. Non ho toccato niente.")
            _ripristina_sandbox(sandbox)
            return 2

        # 3. Task di coding nella sandbox (diff git + pytest reali).
        if not args.modello_reale:
            _usa_coder_simulato()

        timeout = 120 if args.modello_reale else 60
        esito = coding_agent.run_coding_task(TASK, timeout=timeout)

        print("\n" + "-" * 74)
        print(f"🧑‍💻 success={esito.get('success')} timed_out={esito.get('timed_out')} "
              f"tests_touched={esito.get('tests_touched')}")
        print(f"   errore: {esito.get('error')!r}")
        print("--- diff ---")
        print((esito.get("diff") or "").strip()[:2000] or "(nessuna modifica)")
        print("--- pytest ---")
        print((esito.get("test_output") or "").strip()[:1200] or "(pytest non eseguito)")
        print("-" * 74, flush=True)

        main_py = sandbox / "main.py"
        contenuto = main_py.read_text(encoding="utf-8", errors="replace")
        ha_divisione = "def divisione" in contenuto
        print(f"🔎 `def divisione` presente in main.py: {ha_divisione}")

        if esito.get("success") and ha_divisione:
            print("\n✅ Test vocale reale: PASS")
            parla("Fatto, Capo. Funzione divisione aggiunta e test superati.")
            esito_codice = 0
        else:
            print("\n❌ Test vocale reale: FAIL (il task di coding non è andato a buon fine)")
            parla("Non sono riuscita a completare il task, Capo.")
            esito_codice = 1
    finally:
        # 4. Pulizia: la sandbox torna al commit iniziale, sempre.
        if not _ripristina_sandbox(sandbox):
            esito_codice = esito_codice or 4

    return esito_codice


if __name__ == "__main__":
    sys.exit(main())
