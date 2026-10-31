# Token Google fuori dal repo
import os as _os_tok
TOKEN_PATH = _os_tok.path.expanduser('~/.clio/token.pickle')
import os
import subprocess
import time
import webbrowser
import platform
import shutil
import glob
from pathlib import Path
import logging

# Logger dedicato alla visione: propaga al root logger configurato da clio_core/bot,
# così le righe "🖥️ Rilevati N monitor..." finiscono nei log di Clio.
logger = logging.getLogger("ClioVision")

# --- STRUMENTI AVANZATI PER "INFINITE CAPABILITIES" ---

def cerca_file(pattern: str, directory: str = ".") -> str:
    """Cerca file corrispettivi a un pattern (es. *.txt) in una directory. Utile per trovare documenti smarriti."""
    try:
        files = glob.glob(os.path.join(directory, pattern), recursive=True)
        if not files: return "Nessun file trovato."
        return "File trovato/i:\n" + "\n".join(files[:20])
    except Exception as e:
        return f"Errore ricerca: {e}"

def leggi_file_testo(percorso: str) -> str:
    """Legge il contenuto di un file di testo (log, sorgenti, note)."""
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return f.read(5000)
    except Exception as e:
        return f"Errore lettura: {e}"

def scrivi_file_testo(percorso: str, contenuto: str) -> str:
    """Crea o sovrascrive un file di testo con il contenuto fornito."""
    try:
        with open(percorso, "w", encoding="utf-8") as f:
            f.write(contenuto)
        return f"✅ File scritto: {percorso}"
    except Exception as e:
        return f"Errore scrittura: {e}"

def esegui_script_python(codice: str) -> str:
    """Esegue uno script Python fornito come stringa con controlli di sicurezza."""
    BLACKLIST = ["os.remove", "os.rmdir", "shutil.rmtree", "subprocess.Popen",
        "subprocess.call", "subprocess.run", "open(", "__import__",
        "eval(", "exec(", "os.system", "sys.exit", "socket", "urllib", "requests"]
    for cmd in BLACKLIST:
        if cmd in codice:
            return f"Bloccato: '{cmd}' non permesso per sicurezza."
            
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write(codice)
        tmp_path = f.name
    try:
        # Timeout ridotto a 15s e cattura output limitata
        res = subprocess.run(["python", tmp_path], capture_output=True, text=True, timeout=15)
        os.remove(tmp_path)
        output = res.stdout[:2000] # Tronca output a 2000 caratteri
        return f"Output:\n{output}\nErrori:\n{res.stderr[:500]}"
    except subprocess.TimeoutExpired:
        if os.path.exists(tmp_path): os.remove(tmp_path)
        return "Timeout: l'esecuzione ha superato il limite di 15 secondi."
    except Exception as e:
        if os.path.exists(tmp_path): os.remove(tmp_path)
        return f"Errore esecuzione script: {e}"

def finestre_gestione(azione: str, titolo_parziale: str = "") -> str:
    """Gestisce le finestre aperte. Azioni: 'lista' (elenca tutto), 'focus' (porta in primo piano), 'chiudi' (chiude)."""
    try:
        import pygetwindow as gw
        if azione == "lista":
            titoli = [w.title for w in gw.getAllWindows() if w.title]
            return "Finestre aperte:\n" + "\n".join(titoli)
        
        wins = gw.getWindowsWithTitle(titolo_parziale)
        if not wins: return f"Nessuna finestra trovata con '{titolo_parziale}'."
        
        target = wins[0]
        if azione == "focus":
            target.activate()
            return f"✅ Focus su: {target.title}"
        elif azione == "chiudi":
            target.close()
            return f"✅ Finestra chiusa: {target.title}"
        return "Azione non riconosciuta."
    except Exception as e:
        return f"Errore gestione finestre: {e}"

def ricerca_web_profonda(query: str) -> str:
    """Esegue una ricerca web, legge il primo risultato utile e ne fa una sintesi. Per domande complesse."""
    try:
        res_ricerca = leggi_motore_ricerca_web(query, max_risultati=1)
        if "Link: " not in res_ricerca: return res_ricerca
        
        import re
        link = re.search(r"Link: (https?://[^\s\n]+)", res_ricerca)
        if not link: return res_ricerca
        
        url = link.group(1)
        contenuto = leggi_sito_web(url)
        return f"Dati estratti da {url}:\n\n{contenuto}"
    except Exception as e:
        return f"Errore ricerca profonda: {e}"

def esegui_powershell_avanzato(script: str) -> str:
    """Esegue uno script PowerShell avanzato. Utile per configurazioni di sistema o automazioni Windows."""
    try:
        res = subprocess.run(["powershell", "-Command", script], capture_output=True, text=True, timeout=30)
        return f"Output:\n{res.stdout}\nErrori:\n{res.stderr}"
    except Exception as e:
        return f"Errore PowerShell: {e}"

# --- FINE STRUMENTI AVANZATI ---


# ── Routing siti web (FASE 3, 30/10/2026) ────────────────────────────────────
# "Apri youtube" non e' un'app desktop: cercare youtube.exe porta a
# "App non trovata". Se il nome e' un sito noto si apre nel browser.
# Chiavi in minuscolo e senza ".exe"; il valore e' l'URL da aprire.
SITI_NOTI = {
    "youtube": "https://www.youtube.com",
    "spotify": "https://open.spotify.com",
    "google":  "https://www.google.com",
    "gmail":   "https://mail.google.com",
    "netflix": "https://www.netflix.com",
    "telegram": "https://web.telegram.org",
    "discord": "https://discord.com/app",
    "whatsapp": "https://web.whatsapp.com",
}

APP_DESKTOP_PROFILES = {
    "spotify": {
        "exe": "Spotify.exe",
        "paths": (
            r"%APPDATA%\Spotify\Spotify.exe",
            r"%LOCALAPPDATA%\Programs\Spotify\Spotify.exe",
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",
        ),
        "uri": "spotify:",
    },
    "netflix": {
        "exe": "Netflix.exe",
        "paths": (
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Netflix.exe",
            r"%LOCALAPPDATA%\Programs\Netflix\Netflix.exe",
        ),
        "uri": "netflix:",
    },
    "telegram": {
        "exe": "Telegram.exe",
        "paths": (
            r"%APPDATA%\Telegram Desktop\Telegram.exe",
            r"%LOCALAPPDATA%\Programs\Telegram Desktop\Telegram.exe",
            r"%LOCALAPPDATA%\Telegram Desktop\Telegram.exe",
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Telegram.exe",
        ),
        "uri": "tg://",
    },
    "discord": {
        "exe": "Discord.exe",
        "paths": (
            r"%LOCALAPPDATA%\Discord\app-*\Discord.exe",
            r"%LOCALAPPDATA%\Programs\Discord\Discord.exe",
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\Discord.exe",
        ),
        "uri": "discord:",
    },
    "whatsapp": {
        "exe": "WhatsApp.exe",
        "paths": (
            r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe",
            r"%LOCALAPPDATA%\Programs\WhatsApp\WhatsApp.exe",
            r"%LOCALAPPDATA%\Microsoft\WindowsApps\WhatsApp.exe",
        ),
        "uri": "whatsapp:",
    },
}


def _apri_uri_applicazione(uri: str) -> bool:
    """Avvia un protocollo UWP/app registrato in Windows."""
    if not uri or platform.system() != "Windows":
        return False
    startfile = getattr(os, "startfile", None)
    if callable(startfile):
        try:
            startfile(uri)
            return True
        except OSError as exc:
            logger.debug("Protocollo app non disponibile (%s): %s", uri, exc)
            return False
    try:
        risultato = subprocess.run(
            ["cmd", "/c", "start", "", uri],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
        )
        return risultato.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Avvio protocollo app fallito (%s): %s", uri, exc)
        return False


def _apri_app_desktop(nome: str) -> tuple[bool, bool]:
    """Cerca l'eseguibile, poi UWP; ritorna (avviata, installazione_rilevata)."""
    profilo = APP_DESKTOP_PROFILES.get(nome)
    if not profilo:
        return False, False

    candidati = []
    for pattern in profilo["paths"]:
        espanso = os.path.expandvars(pattern)
        candidati.extend(glob.glob(espanso))
    nel_path = shutil.which(profilo["exe"]) or shutil.which(
        Path(profilo["exe"]).stem
    )
    if nel_path:
        candidati.insert(0, nel_path)

    installata = False
    visti = set()
    for candidato in candidati:
        candidato = os.path.abspath(candidato)
        chiave = os.path.normcase(candidato)
        if chiave in visti or not os.path.isfile(candidato):
            continue
        visti.add(chiave)
        installata = True
        if ("\\windowsapps\\" in chiave and profilo.get("uri")):
            if _apri_uri_applicazione(profilo["uri"]):
                return True, True
            continue
        try:
            startfile = getattr(os, "startfile", None)
            if callable(startfile):
                startfile(candidato)
            else:
                subprocess.Popen([candidato])
            return True, True
        except OSError as exc:
            logger.debug("Avvio app locale fallito (%s): %s", candidato, exc)

    # La URI è il fallback per un'app Microsoft Store/UWP installata.
    if _apri_uri_applicazione(profilo.get("uri")):
        return True, True
    return False, installata


def apri_applicazione(nome_app: str) -> str:
    """Apre prima l'app desktop, poi la URI UWP e infine il sito noto."""
    nome_lower = str(nome_app or "").lower().strip()
    nome_chiave = nome_lower[:-4] if nome_lower.endswith(".exe") else nome_lower

    if nome_chiave in APP_DESKTOP_PROFILES:
        avviata, installata = _apri_app_desktop(nome_chiave)
        if avviata:
            return f"✅ Ho aperto l'app desktop {nome_app}."
        if installata:
            return f"❌ {nome_app} è installata, ma non sono riuscito ad avviare l'app desktop."
        sito = SITI_NOTI.get(nome_chiave)
        if sito:
            logger.info("App desktop '%s' non installata: apro il sito %s", nome_chiave, sito)
            return apri_sito(sito)
        return f"App desktop '{nome_app}' non installata e nessun sito di fallback disponibile."

    # I servizi senza un profilo desktop (es. YouTube, Google, Gmail) restano siti.
    _sito = SITI_NOTI.get(nome_chiave)
    if _sito:
        return apri_sito(_sito)

    # Mappa di applicazioni comuni
    mappa_app = {
        "notepad": "notepad",
        "blocco note": "notepad",
        "chrome": "chrome",
        "google chrome": "chrome",
        "firefox": "firefox",
        "edge": "msedge",
        "microsort edge": "msedge",
        "esplora file": "explorer",
        "file explorer": "explorer",
        "explorer": "explorer",
        "calcolatrice": "calc",
        "calculator": "calc",
        # Accetta gli alias principali di Paint indipendentemente dal casing.
        "paint": "mspaint",
        "paint.exe": "mspaint",
        "mspaint": "mspaint",
        "mspaint.exe": "mspaint",
        "word": "winword",
        "excel": "excel",
        "powerpoint": "powerpnt",
        "vscode": "code",
        "visual studio code": "code",
        "task manager": "taskmgr",
        "gestore attività": "taskmgr",
        "cmd": "cmd",
        "terminale": "cmd",
        "powershell": "powershell",
        "spotify": "Spotify.exe",
        "netflix": "Netflix.exe",
        "telegram": "Telegram.exe",
        "discord": "Discord.exe",
        "whatsapp": "WhatsApp.exe",
        "discord": "discord",
        "telegram": "telegram",
        "whatsapp": "whatsapp",
        "vlc": "vlc",
        "steam": "steam",
    }
    
    # Cerca corrispondenza nella mappa
    eseguibile = mappa_app.get(nome_lower, nome_lower)
    
    try:
        import time
        import subprocess

        # FIX 3: Spotify è un'app UWP/Store. Aprire col protocollo "spotify:"
        # evita il popup "Impossibile trovare il file 'spotify'".
        if eseguibile == "spotify":
            try:
                import skills
                return skills.open_app("spotify")
            except Exception:
                try:
                    os.startfile("spotify:")
                    return "✅ Ho aperto Spotify."
                except OSError:
                    return "App non trovata, Capo."

        # Gestione speciale per WhatsApp Desktop (Store app)
        if eseguibile == "whatsapp":
            subprocess.Popen("cmd /c start whatsapp:", shell=True)
            return f"✅ Ho tentato di aprire WhatsApp Desktop."

        # Gestione speciale Office con switch da riga di comando per saltare la home screen
        if eseguibile == "winword":
            subprocess.Popen("cmd /c start winword /w /q", shell=True)
            time.sleep(6)
            return f"✅ Ho aperto Word direttamente su un foglio bianco."
        elif eseguibile == "powerpnt":
            subprocess.Popen("cmd /c start powerpnt /n", shell=True)
            time.sleep(6)
            return f"✅ Ho aperto PowerPoint su una diapositiva bianca."
        elif eseguibile == "excel":
            subprocess.Popen("cmd /c start excel", shell=True)
            time.sleep(6)
            try:
                import pyautogui
                pyautogui.hotkey('ctrl', 'n') 
            except: pass
            return f"✅ Ho aperto Excel."

        # Apertura standard per altre app
        if platform.system() == "Windows":
            # Usiamo 'start' per maggiore flessibilità (alias, store app, ecc)
            subprocess.Popen(f"cmd /c start {eseguibile}", shell=True)
        else:
            subprocess.Popen(["xdg-open", eseguibile])
            
        return f"✅ Ho aperto {nome_app}."
    except Exception as e:
        return f"❌ Non riesco ad aprire '{nome_app}'. Errore: {e}"


def apri_sito(url: str) -> str:
    """Apre un URL nel browser predefinito."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"✅ Ho aperto {url} nel browser."
    except Exception as e:
        return f"❌ Errore nell'aprire {url}: {e}"


def cerca_su_google(query: str) -> str:
    """Cerca qualcosa su Google nel browser."""
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    webbrowser.open(url)
    return f"✅ Sto cercando '{query}' su Google."


def cerca_su_youtube(query: str) -> str:
    """Cerca qualcosa su YouTube nel browser."""
    url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
    webbrowser.open(url)
    return f"✅ Sto cercando '{query}' su YouTube."


def spegni_pc(minuti: int = 0) -> str:
    """Spegne il PC, con un ritardo opzionale in minuti."""
    secondi = minuti * 60
    if platform.system() == "Windows":
        os.system(f"shutdown /s /t {secondi}")
        if minuti > 0:
            return f"✅ Il PC si spegnerà tra {minuti} minuti."
        return "✅ Il PC si sta spegnendo."
    else:
        os.system(f"shutdown -h +{minuti}")
        return f"✅ Spegnimento programmato tra {minuti} minuti."


def annulla_spegnimento() -> str:
    """Annulla uno spegnimento programmato."""
    if platform.system() == "Windows":
        os.system("shutdown /a")
        return "✅ Spegnimento annullato."
    else:
        os.system("shutdown -c")
        return "✅ Spegnimento annullato."


def riavvia_pc() -> str:
    """Riavvia il PC."""
    if platform.system() == "Windows":
        os.system("shutdown /r /t 10")
        return "✅ Il PC si riavvierà tra 10 secondi."
    else:
        os.system("reboot")
        return "✅ Riavvio in corso."


def volume_su() -> str:
    """Alza il volume al massimo (Windows)."""
    try:
        import subprocess
        # Usa nircmd se disponibile, altrimenti PowerShell
        result = subprocess.run(
            ["powershell", "-c", "[System.Media.SystemSounds]::Beep.Play()"],
            capture_output=True
        )
        # Alza volume con tasti simulati
        import ctypes
        for _ in range(5):
            ctypes.windll.user32.keybd_event(0xAF, 0, 0, 0)  # VK_VOLUME_UP
            ctypes.windll.user32.keybd_event(0xAF, 0, 2, 0)
        return "✅ Volume alzato."
    except Exception as e:
        return f"⚠️ Non riesco a gestire il volume: {e}"


def muta_volume() -> str:
    """Muta o de-muta il volume (Windows)."""
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)  # VK_VOLUME_MUTE
        ctypes.windll.user32.keybd_event(0xAD, 0, 2, 0)
        return "✅ Volume mutato/de-mutato."
    except Exception as e:
        return f"⚠️ Errore: {e}"

def media_play_pausa() -> str:
    """Riproduce o mette in pausa la musica o i video (Media Key)."""
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(0xB3, 0, 0, 0) # VK_MEDIA_PLAY_PAUSE
        ctypes.windll.user32.keybd_event(0xB3, 0, 2, 0)
        return "✅ Play/Pausa inviato."
    except: return "❌ Errore media key."

def media_prossimo() -> str:
    """Passa al brano o video successivo."""
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(0xB0, 0, 0, 0) # VK_MEDIA_NEXT_TRACK
        ctypes.windll.user32.keybd_event(0xB0, 0, 2, 0)
        return "✅ Prossimo brano."
    except: return "❌ Errore media key."

def media_precedente() -> str:
    """Torna al brano o video precedente."""
    try:
        import ctypes
        ctypes.windll.user32.keybd_event(0xB1, 0, 0, 0) # VK_MEDIA_PREV_TRACK
        ctypes.windll.user32.keybd_event(0xB1, 0, 2, 0)
        return "✅ Brano precedente."
    except: return "❌ Errore media key."

def chiudi_finestra_attiva() -> str:
    """Chiude la finestra attualmente in primo piano (ALT+F4)."""
    try:
        import pyautogui
        pyautogui.hotkey('alt', 'f4')
        return "✅ Finestra chiusa."
    except: return "❌ Errore chiusura finestra."

def minimizza_tutto() -> str:
    """Minimizza tutte le finestre aperte per mostrare il desktop (WIN+D)."""
    try:
        import pyautogui
        pyautogui.hotkey('win', 'd')
        return "✅ Desktop mostrato."
    except: return "❌ Errore minimizzazione."

def blocca_pc() -> str:
    """Blocca immediatamente il PC (Schermata di login)."""
    try:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return "✅ PC Bloccato."
    except: return "❌ Errore blocco PC."

def svuota_cestino() -> str:
    """Svuota il cestino di Windows senza chiedere conferma."""
    try:
        import ctypes
        # SHEmptyRecycleBinW
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 1 | 2 | 4)
        return "✅ Cestino svuotato."
    except: return "❌ Errore svuotamento cestino."

def rinomina_file(vecchio: str, nuovo: str) -> str:
    """Rinomina un file o una cartella."""
    try:
        os.rename(vecchio, nuovo)
        return f"✅ Rinomina completata: da {vecchio} a {nuovo}."
    except Exception as e: return f"❌ Errore: {e}"

def elimina_file(percorso: str) -> str:
    """Elimina definitivamente un file o una cartella."""
    try:
        if os.path.isdir(percorso):
            shutil.rmtree(percorso)
        else:
            os.remove(percorso)
        return f"✅ Eliminato: {percorso}."
    except Exception as e: return f"❌ Errore: {e}"

def lista_file_cartella(percorso: str = ".") -> str:
    """Elenca i file presenti in una cartella specifica."""
    try:
        files = os.listdir(percorso)
        testo = "\n".join([f"- {f}" for f in files[:50]]) # Limitato a 50
        return f"✅ File in {percorso}:\n{testo}"
    except Exception as e: return f"❌ Errore: {e}"

def stato_batteria() -> str:
    """Controlla la percentuale della batteria e se il PC è in carica."""
    try:
        import psutil
        batt = psutil.sensors_battery()
        if not batt: return "❌ Batteria non trovata."
        stato = "In carica" if batt.power_plugged else "In scarica"
        return f"🔋 Batteria: {batt.percent}% ({stato})."
    except: return "❌ Errore batteria."

def ottieni_telemetria_hardware() -> dict:
    """Restituisce dati tecnici grezzi per il monitoraggio proattivo."""
    try:
        import psutil
        cpu_load = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory()
        batt = psutil.sensors_battery()
        
        return {
            "cpu_percent": cpu_load,
            "ram_percent": ram.percent,
            "battery_percent": batt.percent if batt else 100,
            "power_plugged": batt.power_plugged if batt else True,
            "network_io": psutil.net_io_counters()._asdict()
        }
    except:
        return {}

def monitora_connessioni_sospette() -> str:
    """Controlla se ci sono connessioni outbound verso IP sospetti o porte non comuni."""
    try:
        import psutil
        conns = psutil.net_connections(kind='inet')
        sospette = []
        for c in conns:
            if c.status == 'ESTABLISHED' and c.remote_address:
                r_ip, r_port = c.remote_address
                # Esempio: segnala se la porta remota è insolita (semplificazione per demo)
                if r_port in [4444, 6666, 1337]:
                    sospette.append(f"IP: {r_ip} Port: {r_port}")
        
        if sospette:
            return f"⚠️ [SECURITY] Rilevate connessioni sospette: {', '.join(sospette)}"
        return "✅ Nessuna connessione sospetta rilevata."
    except Exception as e:
        return f"❌ Errore monitoraggio rete: {e}"

def monitora_processi_critici() -> str:
    """Controlla se ci sono processi con nomi sospetti o consumo anomalo di risorse."""
    try:
        import psutil
        sospetti = []
        for proc in psutil.process_iter(['name', 'cpu_percent', 'username']):
            try:
                # Esempio: segnale processi che simulano nomi di sistema o script sospetti
                name = proc.info['name'].lower()
                if any(x in name for x in ["miner", "cryptonight", "reverse_shell"]):
                    sospetti.append(name)
            except: continue
        
        if sospetti:
            return f"⚠️ [SECURITY] Rilevati processi sospetti in esecuzione: {', '.join(sospetti)}"
        return "✅ Processi di sistema stabili."
    except Exception as e:
        return f"❌ Errore monitoraggio processi: {e}"


def chi_sei() -> str:
    """Restituisce l'identità e lo stato di installazione attuale di Clio nell'ecosistema."""
    return (
        "🤖 **Identità:** C.L.I.O. (Cognitive Logic Intent Optimization)\n"
        "🌐 **Stato:** Installata in Simbiosi Cognitiva su questo dispositivo.\n"
        "🔗 **Ecosistema:** Sincronizzata via PC-Core, Hub Desktop, Mobile PWA e Telegram."
    )

def uccidi_processo(nome_processo: str) -> str:
    """Termina forzatamente un'applicazione o un processo (es: chrome.exe, notepad)."""
    try:
        import os
        # Assicura estensione .exe su Windows se manca
        if not nome_processo.endswith(".exe") and os.name == "nt":
            nome_processo += ".exe"
        os.system(f"taskkill /F /IM {nome_processo}")
        return f"✅ Ho terminato il processo: {nome_processo}."
    except Exception as e:
        return f"❌ Errore terminazione processo: {e}"

def imposta_volume(percentuale: int) -> str:
    """Imposta il volume di sistema a una percentuale specifica (0-100)."""
    try:
        # Usiamo PowerShell per impostare il volume preciso (richiede AudioDeviceCmdlets o trucco via CoreAudio)
        # Più semplice: nircmd se presente, o script PS inline
        cmd = f"(new-object -com wscript.shell).SendKeys([char]175*50); (new-object -com wscript.shell).SendKeys([char]174*50); (new-object -com wscript.shell).SendKeys([char]175*{(percentuale//2)})"
        subprocess.run(["powershell", "-Command", cmd], capture_output=True)
        return f"✅ Volume impostato al {percentuale}%."
    except Exception as e:
        return f"❌ Errore volume: {e}"


def ottieni_info_sistema() -> str:
    """Restituisce info sul sistema (CPU, RAM, disco)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        ram = psutil.virtual_memory()
        disco = psutil.disk_usage('/')
        return (
            f"💻 **Sistema:**\n"
            f"• CPU: {cpu}%\n"
            f"• RAM: {ram.used // (1024**2)} MB usati / {ram.total // (1024**2)} MB totali ({ram.percent}%)\n"
            f"• Disco C: {disco.used // (1024**3):.1f} GB usati / {disco.total // (1024**3):.1f} GB totali"
        )
    except ImportError:
        return "⚠️ Installa psutil per le info di sistema: pip install psutil"
    except Exception as e:
        return f"❌ Errore: {e}"


def esegui_comando_shell(comando: str) -> str:
    """Esegue un comando shell e restituisce l'output (uso avanzato)."""
    try:
        result = subprocess.run(
            comando, shell=True, capture_output=True, text=True, timeout=15
        )
        output = result.stdout.strip() or result.stderr.strip() or "Comando eseguito (nessun output)."
        return f"```\n{output[:1000]}\n```"
    except subprocess.TimeoutExpired:
        return "⚠️ Il comando ha impiegato troppo tempo."
    except Exception as e:
        return f"❌ Errore nell'esecuzione: {e}"


def crea_cartella(percorso: str) -> str:
    """Crea una cartella nel percorso specificato."""
    try:
        Path(percorso).mkdir(parents=True, exist_ok=True)
        return f"✅ Cartella creata: {percorso}"
    except Exception as e:
        return f"❌ Errore: {e}"


def apri_cartella(percorso: str) -> str:
    """Apre una cartella in Esplora File."""
    percorso = os.path.expanduser(percorso)
    try:
        if platform.system() == "Windows":
            os.startfile(percorso)
        else:
            subprocess.Popen(["xdg-open", percorso])
        return f"✅ Aperta la cartella: {percorso}"
    except Exception as e:
        return f"❌ Errore: {e}"

def digita_su_tastiera_fisica(testo: str) -> str:
    """Invia le sequenze di tasti all'applicazione attualmente in primo piano sul sistema operativo locale. Usa il copia-incolla per affidabilità."""
    try:
        import pyautogui
        import pyperclip
        import time
        # Leggero delay per dare il tempo di cambiare finestra
        time.sleep(1.5)
        
        # Salviamo gli appunti vecchi
        vecchi_appunti = pyperclip.paste()
        
        # Mettiamo il nuovo testo negli appunti
        pyperclip.copy(testo)
        time.sleep(0.1)
        
        # Simuliamo Incolla (CTRL+V)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.3)
        
        # Ripristiniamo gli appunti vecchi per trasparenza
        pyperclip.copy(vecchi_appunti)
        
        return f"✅ Inserimento dati completato con successo (Incollato): '{testo[:30]}...'"
    except ImportError:
         return "⚠️ Libreria pyautogui o pyperclip mancante."
    except Exception as e:
        return f"❌ Errore I/O tastiera: {e}"


def leggi_motore_ricerca_web(query: str, max_risultati: int = 3) -> str:
    """Interroga un motore di ricerca semantico per recuperare metadati web testuali (SEO, snippet, query analysis)."""
    try:
        from duckduckgo_search import DDGS
        risultati = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_risultati):
                risultati.append(f"- Titolo: {r.get('title')}\n  Snippet: {r.get('body')}\n  Link: {r.get('href')}")
        
        testo_risultati = "\n\n".join(risultati)
        return f"✅ Dati estratti dall'indice web per '{query}':\n\n{testo_risultati}"
    except ImportError:
         return "⚠️ Libreria duckduckgo-search mancante."
    except Exception as e:
        return f"❌ Errore connessione web: {e}"


def leggi_appunti() -> str:
    """Legge cosa c'è copiato negli appunti (clipboard) del PC."""
    try:
        import pyperclip
        testo = pyperclip.paste()
        if not testo:
            return "❌ Gli appunti sono vuoti."
        return f"✅ Questo è il contenuto attuale dei tuoi appunti:\n{testo[:2000]}"
    except ImportError:
         return "⚠️ Installa pyperclip: pip install pyperclip"
    except Exception as e:
        return f"❌ Errore durante la lettura degli appunti: {e}"


def scrivi_appunti(testo: str) -> str:
    """Scrive un testo negli appunti (clipboard) del PC in modo che l'utente possa incollarlo."""
    try:
        import pyperclip
        pyperclip.copy(testo)
        return "✅ Ho copiato il testo nei tuoi appunti. Ora puoi fare Incolla (CTRL+V)."
    except ImportError:
         return "⚠️ Installa pyperclip: pip install pyperclip"
    except Exception as e:
        return f"❌ Errore durante la scrittura negli appunti: {e}"





def leggi_sito_web(url: str) -> str:
    """Estrae e legge il testo completo di un articolo, blog o pagina web saltando le pubblicità."""
    try:
        import urllib.request
        jina_url = f"https://r.jina.ai/{url}"
        
        req = urllib.request.Request(
            jina_url, 
            headers={'Accept': 'text/plain', 'User-Agent': 'Mozilla/5.0'}
        )
        
        with urllib.request.urlopen(req, timeout=15) as response:
            testo = response.read().decode('utf-8')
            
        if not testo or len(testo) < 50:
            return "⚠️ Impossibile estrarre testo utile da questa pagina."
            
        # Limitiamo il testo per non sfondare il context window
        return f"✅ Contenuto estratto da {url}:\n\n{testo[:6000]}...\n[Testo troncato]"
    except Exception as e:
        return f"❌ Errore durante la lettura del sito web: {e}"


def leggi_youtube(url: str) -> str:
    """Estrae i sottotitoli completi di un video YouTube per poterne fare il riassunto o studiarli."""
    try:
        if "youtube.com" not in url and "youtu.be" not in url:
             return "❌ Errore: l'URL fornito non sembra essere un video di YouTube."
             
        import urllib.request
        jina_url = f"https://r.jina.ai/{url}"
        
        req = urllib.request.Request(
            jina_url, 
            headers={'Accept': 'text/plain', 'User-Agent': 'Mozilla/5.0'}
        )
        
        with urllib.request.urlopen(req, timeout=15) as response:
            testo = response.read().decode('utf-8')
            
        if not testo or len(testo) < 50:
            return "⚠️ Il video non ha sottotitoli disponibili oppure non è stato possibile estrarli."
            
        return f"✅ Trascrizione YouTube estratta con successo:\n\n{testo[:6000]}...\n[Testo troncato]"
    except Exception as e:
        return f"❌ Errore durante l'estrazione dei sottotitoli YouTube: {e}"


def esegui_comando_adb(comando: str) -> str:
    """Helper interno per eseguire comandi ADB sul telefono collegato."""
    try:
        import subprocess
        # Prova a importare da config o usa fallback
        try:
            from config import ADB_EXEC as adb_path
        except:
            adb_path = r"C:\Users\User\AppData\Local\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
            
        full_cmd = f'"{adb_path}" {comando}'
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"❌ Errore ADB: {result.stderr.strip()}"
    except Exception as e:
        return f"❌ Errore esecuzione ADB: {e}"

def telefono_clicca(x: int, y: int) -> str:
    """Simula un tocco (click) sulle coordinate X e Y dello schermo del telefono."""
    res = esegui_comando_adb(f"shell input tap {x} {y}")
    return f"✅ Tocco effettuato a {x},{y} sul telefono." if "❌" not in res else res

def telefono_digita(testo: str) -> str:
    """Scrive del testo o numeri sul telefono. Supporta l'invio automatico se il testo finisce con \\n."""
    import time
    invio = False
    if testo.endswith("\n"):
        invio = True
        testo = testo.strip()
        
    # Sostituiamo spazi con %s per ADB
    testo_adb = testo.replace(" ", "%s")
    # Escaping per caratteri speciali shell
    testo_adb = testo_adb.replace("'", "'\"'\"'")
    
    res = esegui_comando_adb(f"shell input text '{testo_adb}'")
    if invio:
        time.sleep(0.5)
        esegui_comando_adb("shell input keyevent 66") # 66 è l'invio (Enter)
        
    return "✅ Testo/Numeri digitati sul telefono." if "❌" not in res else res

def telefono_apri_app(nome_pacchetto: str) -> str:
    """Apre un'app specifica sul telefono (es: com.whatsapp, com.instagram.android)."""
    import time
    res = esegui_comando_adb(f"shell monkey -p {nome_pacchetto} -c android.intent.category.LAUNCHER 1")
    time.sleep(1.5) # Attesa caricamento app
    return f"✅ Ho provato ad aprire l'app {nome_pacchetto}." if "❌" not in res else res

def telefono_home() -> str:
    """Torna alla schermata Home del telefono."""
    import time
    res = esegui_comando_adb("shell input keyevent 3")
    time.sleep(0.8)
    return "✅ Tornato alla Home del telefono." if "❌" not in res else res

def telefono_indietro() -> str:
    """Simula la pressione del tasto 'Indietro' del telefono."""
    import time
    res = esegui_comando_adb("shell input keyevent 4")
    time.sleep(0.8) # Attesa animazione chiusura/ritorno
    return "✅ Tasto Indietro premuto." if "❌" not in res else res

def telefono_swipe(x1: int, y1: int, x2: int, y2: int, durata: int = 500) -> str:
    """Simula uno scorrimento (swipe) da una coordinata all'altra. Utile per scrollare liste o feed."""
    res = esegui_comando_adb(f"shell input swipe {x1} {y1} {x2} {y2} {durata}")
    return f"✅ Swipe effettuato da {x1},{y1} a {x2},{y2}." if "❌" not in res else res

def telefono_screenshot() -> str:
    """Scatta uno screenshot del telefono e lo salva sul PC come 'telefono_screenshot.png'."""
    res = esegui_comando_adb("shell screencap -p /sdcard/screen.png")
    if "❌" in res: return res
    # Tira il file sul PC
    try:
        from clio_core import ADB_EXEC as adb_path
    except:
        adb_path = r"C:\Users\User\AppData\Local\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
        
    import subprocess
    subprocess.run(f'"{adb_path}" pull /sdcard/screen.png telefono_screenshot.png', shell=True)
    return "✅ Screenshot del telefono salvato come 'telefono_screenshot.png'."

def _coerce_monitor(monitor):
    """Normalizza il parametro monitor in un intero (0 = tutti gli schermi)."""
    try:
        return int(monitor)
    except (TypeError, ValueError):
        return 0

def _cattura_schermo(monitor: int = 0):
    """Cattura lo schermo con mss e restituisce (PIL.Image, n_monitor, tipo, (w, h)).

    monitor <= 0  -> spazio virtuale completo (tutti gli schermi affiancati)
    monitor 1..N  -> solo il monitor indicato (1 = primario, 2 = secondario, ...)
    """
    import mss
    monitor = _coerce_monitor(monitor)
    from PIL import Image
    with mss.mss() as sct:
        monitors = list(sct.monitors)
        n_monitor = len(monitors) - 1  # l'indice 0 è lo spazio virtuale, non un monitor reale
        if not monitor or monitor <= 0:
            region = monitors[0]       # tutti gli schermi in un'unica immagine
            tipo = "combinato"
        else:
            idx = min(monitor, n_monitor)
            if idx != monitor:
                logger.warning(f"⚠️ Monitor {monitor} non disponibile (rilevati {n_monitor}): uso il monitor {idx}.")
            region = monitors[idx]
            tipo = f"monitor {idx}"
        shot = sct.grab(region)
        # mss restituisce BGRA: convertiamo in RGB per PIL/Gemini
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, n_monitor, tipo, (region["width"], region["height"])

def fai_screenshot_pc(monitor: int = 0) -> str:
    """Scatta uno screenshot del PC e lo salva come 'clio_screenshot.png'.

    Con più monitor collegati cattura l'intero spazio virtuale: tutti gli schermi
    affiancati in un'unica immagine. Passa monitor=1..N per catturare solo uno
    specifico schermo (1 = primario, 2 = secondario, ...). monitor=0 (default) = tutti.
    """
    try:
        img, n_monitor, tipo, (w, h) = _cattura_schermo(monitor)
        img.save("clio_screenshot.png")
        if tipo == "combinato":
            msg = f"🖥️ Rilevati {n_monitor} monitor, screenshot combinato: {w}x{h}"
        else:
            msg = f"🖥️ Rilevati {n_monitor} monitor, screenshot {tipo}: {w}x{h}"
        logger.info(msg)
        return f"✅ Screenshot salvato come 'clio_screenshot.png'. {msg}."
    except Exception as e:
        # Fallback: PIL ImageGrab con all_screens=True (tutti gli schermi su Windows)
        logger.error(f"❌ Screenshot mss fallito ({e}). Provo il fallback PIL ImageGrab...")
        try:
            from PIL import ImageGrab
            img = ImageGrab.grab(all_screens=True)
            img.save("clio_screenshot.png")
            msg = f"🖥️ Screenshot fallback PIL (tutti gli schermi): {img.width}x{img.height}"
            logger.info(msg)
            return f"✅ Screenshot salvato come 'clio_screenshot.png'. {msg}."
        except Exception as e2:
            logger.error(f"❌ Errore screenshot definitivo: {e2}")
            return f"❌ Errore screenshot: {e} / fallback: {e2}"

def analizza_schermo_pc(monitor: int = 0) -> str:
    """Scatta uno screenshot del PC e lo analizza immediatamente per descrivere cosa c'è a video. Utile per avere contesto visivo.

    Con più monitor collegati analizza tutti gli schermi insieme (spazio virtuale).
    Passa monitor=1..N per analizzare solo un monitor specifico (es. "analizza schermo 2" -> monitor=2).
    """
    monitor = _coerce_monitor(monitor)
    res = fai_screenshot_pc(monitor)
    if "✅" not in res:
        return res
    try:
        # Import lazy: evita l'import circolare actions <-> clio_core all'avvio.
        from clio_core import analizza_visione
        if monitor and monitor > 0:
            prompt = (f"Questa immagine è il monitor {monitor}. Descrivi in italiano e in modo "
                      f"breve (max 120 parole) cosa c'è a video: app aperte, testi ed elementi principali.")
        else:
            prompt = ("Questa immagine è lo spazio virtuale completo del PC e può contenere più "
                      "monitor affiancati (es. 2). Descrivi in italiano e in modo breve (max 150 "
                      "parole) cosa c'è a video su CIASCUNO schermo, distinguendo quello a sinistra "
                      "da quello a destra, con app aperte ed elementi principali.")
        descrizione = analizza_visione("clio_screenshot.png", prompt)
    except Exception as e:
        logger.error(f"Errore analisi schermo: {e}")
        return "✅ Screenshot acquisito, ma non riesco ad analizzarlo ora."
    return descrizione or "✅ Screenshot acquisito. Non rilevo contenuti significativi."

def prepara_immagine_visione(immagine, max_width: int = 1280, quality: int = 85) -> bytes:
    """Prepara un'immagine per il modello di visione Gemini.

    Ridimensiona a `max_width` px di larghezza (proporzioni mantenute) e la
    restituisce come bytes JPEG (`quality`). Ridurre la risoluzione prima
    dell'invio accorcia drasticamente upload e tempo di analisi.

    Accetta sia un percorso di file sia un oggetto PIL.Image già aperto."""
    from PIL import Image
    import io
    if isinstance(immagine, Image.Image):
        img = immagine.convert("RGB")
    else:
        with Image.open(immagine) as im:
            img = im.convert("RGB")
    if img.width > max_width:
        new_height = round(img.height * (max_width / img.width))
        img = img.resize((max_width, new_height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()

def leggi_gmail(max_risultati: int = 5) -> str:
    """Legge le ultime email non lette da Gmail. Richiede credentials.json autorizzato."""
    import os, pickle
    from googleapiclient.discovery import build
    try:
        if not os.path.exists(TOKEN_PATH):
            return "❌ Errore: Autenticazione Gmail mancante. Esegui prima 'setup_google_api.py'."
        
        with open(TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)
            
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().messages().list(userId='me', labelIds=['UNREAD'], maxResults=max_risultati).execute()
        messages = results.get('messages', [])
        
        if not messages: return "Nessuna nuova email non letta."
        
        res_list = []
        for msg in messages:
            m = service.users().messages().get(userId='me', id=msg['id'], format='metadata').execute()
            headers = m.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), "Senza Oggetto")
            sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), "Sconosciuto")
            snippet = m.get('snippet', '')
            res_list.append(f"📧 Da: {sender}\n📌 Oggetto: {subject}\n📝 Snippet: {snippet}\n---")
            
        return "\n\n".join(res_list)
    except Exception as e:
        return f"❌ Errore lettura Gmail: {e}"

def leggi_calendario(giorni: int = 1) -> str:
    """Legge gli eventi del calendario di Google per oggi e domani. Richiede credentials.json autorizzato."""
    import os, pickle, datetime
    from googleapiclient.discovery import build
    try:
        if not os.path.exists(TOKEN_PATH):
            return "❌ Errore: Autenticazione Calendar mancante. Esegui prima 'setup_google_api.py'."
            
        with open(TOKEN_PATH, 'rb') as token:
            creds = pickle.load(token)
            
        service = build('calendar', 'v3', credentials=creds)
        
        now = datetime.datetime.utcnow().isoformat() + 'Z' 
        tomorrow = (datetime.datetime.utcnow() + datetime.timedelta(days=giorni)).isoformat() + 'Z'
        
        events_result = service.events().list(calendarId='primary', timeMin=now, timeMax=tomorrow,
                                              singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        
        if not events: return "Nessun evento in programma."
        
        res_list = []
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            summary = event.get('summary', 'Senza Titolo')
            res_list.append(f"📅 [{start}] - {summary}")
            
        return "\n".join(res_list)
    except Exception as e:
        return f"❌ Errore lettura Calendario: {e}"

def ottieni_meteo(citta: str = "Catania") -> str:
    """Ottiene il meteo attuale per una città. Usa wttr.in per velocità e assenza di chiavi."""
    import urllib.request
    try:
        url = f"https://wttr.in/{citta}?format=3"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            return response.read().decode('utf-8').strip()
    except Exception as e:
        return f"❌ Errore meteo per {citta}: {e}"

def ottieni_notizie() -> str:
    """Ottiene le top 3 notizie dall'RSS dell'ANSA."""
    import urllib.request
    import xml.etree.ElementTree as ET
    try:
        url = "https://www.ansa.it/sito/ansait_rss.xml"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()
            
        root = ET.fromstring(xml_data)
        # Cerchiamo tutti i <item> all'interno del <channel>
        items = root.findall('.//item')[:5]
        
        if not items: return "📰 Al momento non ci sono notizie flash disponibili dall'ANSA."
        
        res_list = []
        for i, item in enumerate(items, 1):
            title = item.find('title').text if item.find('title') is not None else "Senza Titolo"
            link = item.find('link').text if item.find('link') is not None else ""
            res_list.append(f"{i}. 📰 {title.strip()}")
            
        return "Notizie Flash (ANSA):\n" + "\n".join(res_list)
    except Exception as e:
        logger.error(f"Errore lettura notizie RSS: {e}")
        return f"❌ Errore nel recupero delle notizie. Forse l'ANSA è giù?"

def accendi_luce(entita_id: str) -> str:
    """Accende una luce o dispositivo in Home Assistant."""
    try:
        from config import HASS_URL, HASS_TOKEN
        import requests
        if not HASS_URL or not HASS_TOKEN: return "❌ Home Assistant non configurato in config.py."
        
        url = f"{HASS_URL}/api/services/light/turn_on"
        headers = {"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"}
        res = requests.post(url, json={"entity_id": entita_id}, headers=headers)
        if res.status_code in [200, 201]: return f"✅ Comando accendi inviato a {entita_id}."
        return f"❌ Errore Home Assistant: {res.status_code}"
    except Exception as e:
        return f"❌ Errore Domotica: {e}"

def spegni_luce(entita_id: str) -> str:
    """Spegne una luce o dispositivo in Home Assistant."""
    try:
        from config import HASS_URL, HASS_TOKEN
        import requests
        if not HASS_URL or not HASS_TOKEN: return "❌ Home Assistant non configurato in config.py."
        
        url = f"{HASS_URL}/api/services/light/turn_off"
        headers = {"Authorization": f"Bearer {HASS_TOKEN}", "Content-Type": "application/json"}
        res = requests.post(url, json={"entity_id": entita_id}, headers=headers)
        if res.status_code in [200, 201]: return f"✅ Comando spegni inviato a {entita_id}."
        return f"❌ Errore Home Assistant: {res.status_code}"
    except Exception as e:
        return f"❌ Errore Domotica: {e}"

def registra_episodio(titolo: str, descrizione: str = "") -> str:
    """Registra un evento storico, traguardo o ricordo nella memoria di Clio."""
    try:
        from episodic_memory import episodic_memory
        return episodic_memory.save_episode(titolo, descrizione)
    except Exception as e:
        return f"❌ Errore salvataggio episodio: {e}"

AZIONI_DISPONIBILI = {
    "registra_episodio": registra_episodio,
    "accendi_luce": accendi_luce,
    "spegni_luce": spegni_luce,
    "ottieni_meteo": ottieni_meteo,
    "ottieni_notizie": ottieni_notizie,
    "leggi_gmail": leggi_gmail,
    "leggi_calendario": leggi_calendario,
    "apri_applicazione": apri_applicazione,
    "apri_sito": apri_sito,
    "cerca_su_google": cerca_su_google,
    "cerca_su_youtube": cerca_su_youtube,
    "spegni_pc": spegni_pc,
    "annulla_spegnimento": annulla_spegnimento,
    "riavvia_pc": riavvia_pc,
    "ottieni_info_sistema": ottieni_info_sistema,
    "muta_volume": muta_volume,
    "media_play_pausa": media_play_pausa,
    "media_prossimo": media_prossimo,
    "media_precedente": media_precedente,
    "chiudi_finestra_attiva": chiudi_finestra_attiva,
    "minimizza_tutto": minimizza_tutto,
    "blocca_pc": blocca_pc,
    "svuota_cestino": svuota_cestino,
    "rinomina_file": rinomina_file,
    "elimina_file": elimina_file,
    "lista_file_cartella": lista_file_cartella,
    "stato_batteria": stato_batteria,
    "uccidi_processo": uccidi_processo,
    "imposta_volume": imposta_volume,
    "chi_sei": chi_sei,
    "esegui_comando_shell": esegui_comando_shell,
    "crea_cartella": crea_cartella,
    "apri_cartella": apri_cartella,
    "digita_su_tastiera_fisica": digita_su_tastiera_fisica,
    "leggi_motore_ricerca_web": leggi_motore_ricerca_web,
    "leggi_sito_web": leggi_sito_web,
    "leggi_youtube": leggi_youtube,
    "leggi_appunti": leggi_appunti,
    "scrivi_appunti": scrivi_appunti,
    "fai_screenshot_pc": fai_screenshot_pc,
    "analizza_schermo_pc": analizza_schermo_pc,
    "telefono_clicca": telefono_clicca,
    "telefono_digita": telefono_digita,
    "telefono_apri_app": telefono_apri_app,
    "telefono_screenshot": telefono_screenshot,
    "telefono_home": telefono_home,
    "telefono_indietro": telefono_indietro,
    "telefono_swipe": telefono_swipe,
    "monitora_connessioni_sospette": monitora_connessioni_sospette,
    "monitora_processi_critici": monitora_processi_critici
}
