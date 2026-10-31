"""
CLIO - skills.py (Modulo PC Agent & Competenze di Sistema)
Fornisce funzioni per il controllo completo del PC:
1. Controllo App: open_app
2. Controllo Multimediale: control_media
3. Data e Ora: get_system_time
4. Web & YouTube: search_youtube
5. Visione Schermo: take_screenshot, see_screen
6. Controllo Mouse & Tastiera: click_coordinates, type_text, press_key
7. Esecuzione Codice Python: run_python_code
8. Control-Plane Sicurezza: check_safety_action
"""

import os
import sys
import time
import ctypes
import shutil
import subprocess
import webbrowser
import urllib.parse
import io
import contextlib
import re
from datetime import datetime
from typing import NamedTuple, Optional
import logging
from PIL import Image

logger = logging.getLogger("ClioSkills")

# Directory base per file generati
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOT_PATH = os.path.join(BASE_DIR, "screenshot.png")

# ─────────────────────────────────────────────────────────────
# 1. CONTROLLO APPLICAZIONI (open_app)
# ─────────────────────────────────────────────────────────────
KNOWN_APPS = {
    "spotify": ["spotify:"],
    "chrome": ["chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "google-chrome"],
    "paint": ["mspaint.exe", "mspaint"],
    "mspaint": ["mspaint.exe", "mspaint"],
    "mspaint.exe": ["mspaint.exe", "mspaint"],
    "notepad": ["notepad.exe", "notepad"],
    "blocco note": ["notepad.exe", "notepad"],
    "calcolatrice": ["calc.exe", "calc"],
    "calc": ["calc.exe", "calc"],
    "esplora risorse": ["explorer.exe", "explorer"],
    "esplora file": ["explorer.exe", "explorer"],
    "explorer": ["explorer.exe", "explorer"],
    "terminale": ["wt.exe", "cmd.exe", "powershell.exe"],
    "cmd": ["cmd.exe"],
    "powershell": ["powershell.exe"],
    "vscode": ["code", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe")],
    "code": ["code", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe")],
    "discord": [os.path.expandvars(r"%LOCALAPPDATA%\Discord\Update.exe --processStart Discord.exe"), "discord:"],
    "edge": ["msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
    "obsidian": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\obsidian\Obsidian.exe"), "obsidian:"],
}

# Percorsi noti di Spotify Desktop (installer classico) + alias Store/UWP.
SPOTIFY_PATHS = (
    os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe"),
)


def _lancia_spotify() -> bool:
    """FIX 3: apre Spotify (desktop o UWP) senza il popup di errore di Windows.

    Ordine dei tentativi:
      1. protocollo registrato "spotify:" (funziona sia per l'app Store sia per quella desktop);
      2. eseguibile Spotify Desktop nei percorsi noti.
    Ritorna True solo se un tentativo è andato a buon fine: nessun `start spotify`
    (era quello a far comparire "Impossibile trovare il file 'spotify'").
    """
    # 1) Protocollo URI: è il modo corretto per un'app UWP del Microsoft Store.
    try:
        os.startfile("spotify:")
        return True
    except OSError:
        pass

    # 2) Installazione desktop classica.
    for exe in SPOTIFY_PATHS:
        if exe and os.path.exists(exe):
            try:
                os.startfile(exe)
                return True
            except OSError:
                pass

    return False


def _avvia_candidato(cmd: str) -> bool:
    """Tenta di avviare un singolo candidato SENZA far comparire popup di Windows."""
    cmd = (cmd or "").strip()
    if not cmd:
        return False

    espanso = os.path.expandvars(cmd)

    # Comando con argomenti (es. "...\Update.exe --processStart Discord.exe")
    parti = espanso.split(" ", 1)
    if len(parti) == 2 and os.path.exists(parti[0]):
        try:
            subprocess.Popen(
                [parti[0]] + parti[1].split(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError:
            pass

    # Percorso assoluto / file esistente sul disco.
    if os.path.exists(espanso):
        try:
            os.startfile(espanso)
            return True
        except OSError:
            pass

    # Eseguibile nel PATH (notepad.exe, wt.exe, code.cmd...).
    risolto = shutil.which(espanso) or shutil.which(espanso + ".exe")
    if risolto:
        try:
            subprocess.Popen(
                [risolto],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError:
            pass

    # Protocollo/URI registrato (es. "discord:", "obsidian:").
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:$", espanso):
        try:
            os.startfile(espanso)
            return True
        except OSError:
            pass

    return False


def open_app(app_name: str) -> str:
    """Apre un'applicazione di Windows (es. Spotify, Chrome, Notepad).

    FIX 3: niente più `start spotify` (che faceva apparire "Impossibile trovare
    il file 'spotify'"). Spotify passa dal protocollo "spotify:" e, se l'app non
    esiste, si risponde con un messaggio pulito senza errori di Windows.
    """
    app_clean = (app_name or "").strip().lower()
    if not app_clean:
        return "Capo, non hai specificato quale applicazione aprire."

    logger.info(f"🚀 [SKILLS] Richiesta apertura app: {app_name}")

    # I nomi che possono essere sia app sia siti usano un solo routing:
    # eseguibile locale -> protocollo Store/UWP -> fallback al sito.
    app_key = app_clean[:-4] if app_clean.endswith(".exe") else app_clean
    if app_key in {
            "spotify", "netflix", "telegram", "discord", "whatsapp",
            "youtube", "google", "gmail"}:
        try:
            from actions import apri_applicazione
            return apri_applicazione(app_key)
        except Exception as exc:
            logger.exception("Routing desktop/web fallito per %s", app_key)
            return f"Capo, non sono riuscito ad aprire {app_key}: {exc}"

    candidati = KNOWN_APPS.get(app_clean, [app_name.strip()])
    for cmd in candidati:
        if _avvia_candidato(cmd):
            return f"Ho aperto {app_name} per te, Capo."

    logger.warning(f"App non trovata: {app_name}")
    return "App non trovata, Capo."


# ─────────────────────────────────────────────────────────────
# 2. CONTROLLO MULTIMEDIALE (control_media)
# ─────────────────────────────────────────────────────────────
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE      = 0xAD
VK_VOLUME_DOWN      = 0xAE
VK_VOLUME_UP        = 0xAF
KEYEVENTF_KEYUP     = 0x0002

def _send_vk(vk_code: int, repeat: int = 1):
    try:
        user32 = ctypes.windll.user32
        for _ in range(repeat):
            user32.keybd_event(vk_code, 0, 0, 0)
            user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception as e:
        logger.warning(f"keybd_event error: {e}")
        return False

def control_media(action: str) -> str:
    """Controlla la riproduzione multimediale (Play/Pausa, Next, Prev, Volume Su/Giù, Mute)."""
    act_clean = (action or "").strip().lower()
    if not act_clean:
        return "Capo, specifica un comando multimediale (es: play, pause, next, prev, volume_up, volume_down)."

    logger.info(f"🎵 [SKILLS] Comando multimediale: {action}")

    play_pause = {
        "play", "pause", "playpause", "play_pause", "play pause", "play/pause", "play/pausa", "pausa",
        "metti in pausa", "metti in pausa la musica", "pausa la musica",
        "metti pausa", "metti pausa musica", "riprendi", "riprendi la musica",
        "riprendi musica", "riproduci", "riproduci musica", "stop", "ferma la musica",
        "metti play", "metti play alla musica", "metti play sulla musica",
    }
    next_track = {
        "next", "avanti", "vai avanti", "prossimo", "successivo", "nexttrack",
        "next track", "salta", "salta canzone", "canzone successiva",
        "prossima canzone", "brano successivo", "prossimo brano",
        "traccia successiva", "prossima traccia", "vai alla prossima canzone",
        "vai al brano successivo",
    }
    previous_track = {
        "prev", "previous", "indietro", "torna indietro", "vai indietro",
        "precedente", "prevtrack", "previous track", "canzone precedente",
        "brano precedente", "traccia precedente", "torna alla canzone precedente",
        "torna al brano precedente", "canzone prima", "brano prima",
    }

    if act_clean in play_pause:
        if not _send_vk(VK_MEDIA_PLAY_PAUSE):
            return "Capo, non sono riuscita a inviare il tasto multimediale Play/Pausa di Windows."
        return "Comando multimediale Play/Pausa inviato, Capo."
    elif act_clean in next_track:
        if not _send_vk(VK_MEDIA_NEXT_TRACK):
            return "Capo, non sono riuscita a inviare il tasto multimediale Traccia successiva di Windows."
        return "Comando multimediale Traccia successiva inviato, Capo."
    elif act_clean in previous_track:
        if not _send_vk(VK_MEDIA_PREV_TRACK):
            return "Capo, non sono riuscita a inviare il tasto multimediale Traccia precedente di Windows."
        return "Comando multimediale Traccia precedente inviato, Capo."
    elif act_clean in ("volume_up", "vol_up", "volume su", "su", "alza", "alza volume", "volume_su"):
        if not _send_vk(VK_VOLUME_UP, repeat=5):
            return "Capo, non sono riuscita a inviare il tasto Volume Su di Windows."
        return "Ho alzato il volume del PC, Capo."
    elif act_clean in ("volume_down", "vol_down", "volume giu", "volume giù", "giu", "giù", "abbassa", "abbassa volume", "volume_giu"):
        if not _send_vk(VK_VOLUME_DOWN, repeat=5):
            return "Capo, non sono riuscita a inviare il tasto Volume Giù di Windows."
        return "Ho abbassato il volume del PC, Capo."
    elif act_clean in ("mute", "muto", "muta", "silenzio", "unmute"):
        if not _send_vk(VK_VOLUME_MUTE):
            return "Capo, non sono riuscita a inviare il tasto Mute di Windows."
        return "Ho attivato/disattivato il muto, Capo."
    else:
        return f"Comando multimediale non riconosciuto: '{action}'. Non ho inviato tasti."


# ─────────────────────────────────────────────────────────────
# 3. DATA E ORA DI SISTEMA (get_system_time)
# ─────────────────────────────────────────────────────────────
GIORNI_IT = {
    0: "lunedì", 1: "martedì", 2: "mercoledì", 3: "giovedì",
    4: "venerdì", 5: "sabato", 6: "domenica"
}

MESI_IT = {
    1: "gennaio", 2: "febbraio", 3: "marzo", 4: "aprile",
    5: "maggio", 6: "giugno", 7: "luglio", 8: "agosto",
    9: "settembre", 10: "ottobre", 11: "novembre", 12: "dicembre"
}

def get_system_time() -> str:
    """Restituisce l'ora e la data attuali formattate in italiano naturale."""
    now = datetime.now()
    giorno_settimana = GIORNI_IT.get(now.weekday(), "")
    giorno = now.day
    mese = MESI_IT.get(now.month, "")
    anno = now.year
    ora = now.strftime("%H:%M")
    msg = f"Capo, oggi è {giorno_settimana} {giorno} {mese} {anno} e sono le {ora}."
    logger.info(f"⏰ [SKILLS] System time: {msg}")
    return msg


# ─────────────────────────────────────────────────────────────
# 4. RICERCA YOUTUBE (search_youtube)
# ─────────────────────────────────────────────────────────────
def search_youtube(query: str) -> str:
    """Apre il browser predefinito ed effettua una ricerca su YouTube."""
    q_clean = (query or "").strip()
    if not q_clean:
        return "Capo, cosa vuoi che cerchi su YouTube?"

    logger.info(f"📺 [SKILLS] Ricerca YouTube: {q_clean}")
    encoded = urllib.parse.quote_plus(q_clean)
    url = f"https://www.youtube.com/results?search_query={encoded}"
    try:
        webbrowser.open(url)
        return f"Ho cercato '{q_clean}' su YouTube per te, Capo."
    except Exception as e:
        logger.error(f"Errore apertura YouTube: {e}")
        return f"Capo, c'è stato un problema aprendo YouTube: {e}"


# ─────────────────────────────────────────────────────────────
# 5. CATTURA SCHERMO & VISIONE (take_screenshot, see_screen)
# ─────────────────────────────────────────────────────────────
def take_screenshot(filename: str = None) -> str:
    """Cattura lo schermo corrente e lo salva come file immagine (screenshot.png)."""
    target = filename or SCREENSHOT_PATH
    logger.info(f"📸 [SKILLS] Cattura screenshot in: {target}")

    # 1. Prova mss (ultra-veloce e supporta monitor multipli su Windows)
    try:
        import mss
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            img.save(target)
            return os.path.abspath(target)
    except Exception as e1:
        logger.debug(f"mss screenshot non riuscito: {e1}")

    # 2. Fallback pyautogui
    try:
        import pyautogui
        img = pyautogui.screenshot()
        img.save(target)
        return os.path.abspath(target)
    except Exception as e2:
        logger.debug(f"pyautogui screenshot non riuscito: {e2}")

    # 3. Fallback PIL ImageGrab
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=True)
        img.save(target)
        return os.path.abspath(target)
    except Exception as e3:
        logger.error(f"Tutti i metodi di cattura schermo sono falliti: {e3}")
        return f"Errore cattura screenshot: {e3}"


def see_screen(prompt: str = None) -> str:
    """Cattura lo schermo e lo analizza tramite l'API multimodale di Gemini 3.8 Flash,

    individuando gli elementi a video e le coordinate (x, y) per cliccarli.
    """
    shot_path = take_screenshot()
    if not os.path.exists(shot_path):
        return "Capo, non sono riuscita a catturare lo schermo per analizzarlo."

    try:
        import config
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        img = Image.open(shot_path)
        w, h = img.size

        vision_prompt = prompt or (
            f"Sei il modulo visivo di Clio. Risoluzione schermo: {w}x{h}. "
            "Descrivi in modo chiaro e sintetico cosa c'è a video. "
            "Individua le finestre aperte, i campi di testo, i bottoni e gli elementi interattivi. "
            "Per ciascun elemento cliccabile rilevante, indica le coordinate pixel esatte (x, y) "
            "così che Clio possa cliccarlo con la funzione click_coordinates(x, y)."
        )

        resp = client.models.generate_content(
            model=getattr(config, "GEMINI_VISION_MODEL", "models/gemini-3.8-flash"),
            contents=[img, vision_prompt]
        )
        testo_visione = (resp.text or "").strip()
        logger.info(f"👁️ [SKILLS] Visione Gemini completata ({len(testo_visione)} chars)")
        return testo_visione if testo_visione else "Ho visto lo schermo, ma non sono riuscita a descrivere il contenuto."
    except Exception as e:
        logger.error(f"Errore Gemini Vision: {e}")
        return f"Capo, ho riscontrato un errore durante l'analisi visiva dello schermo: {e}"


# ─────────────────────────────────────────────────────────────
# 6. CONTROLLO MOUSE E TASTIERA (click, type, press)
# ─────────────────────────────────────────────────────────────
def click_coordinates(x: int, y: int) -> str:
    """Muove il mouse e clicca alle coordinate specificate (x, y)."""
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        screen_w, screen_h = pyautogui.size()
        
        # Supporta sia coordinate pixel (es. 500, 300) sia percentuali (es. 0.5, 0.5)
        fx, fy = float(x), float(y)
        if 0.0 < fx <= 1.0 and 0.0 < fy <= 1.0:
            target_x = int(fx * screen_w)
            target_y = int(fy * screen_h)
        else:
            target_x = int(fx)
            target_y = int(fy)

        target_x = max(0, min(screen_w - 1, target_x))
        target_y = max(0, min(screen_h - 1, target_y))

        logger.info(f"🖱️ [SKILLS] Click su coordinate ({target_x}, {target_y})")
        pyautogui.moveTo(target_x, target_y, duration=0.25)
        pyautogui.click(target_x, target_y)
        return f"Ho cliccato alle coordinate ({target_x}, {target_y}), Capo."
    except Exception as e:
        logger.error(f"Errore click coordinate ({x}, {y}): {e}")
        return f"Capo, errore durante il click a ({x}, {y}): {e}"


def type_text(text: str) -> str:
    """Digita il testo specificato nella finestra attualmente attiva."""
    if text is None:
        return "Capo, nessun testo fornito da digitare."

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        logger.info(f"⌨️ [SKILLS] Digitazione testo: '{text[:50]}'")

        # Metodo Windows Clip: gestisce caratteri speciali, emoji e accenti senza errori di codifica
        try:
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(str(text).encode("utf-16"))
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.write(str(text), interval=0.01)

        return f"Ho digitato il testo per te, Capo."
    except Exception as e:
        logger.error(f"Errore digitazione testo: {e}")
        return f"Capo, errore durante la digitazione: {e}"


def press_key(key: str) -> str:
    """Preme un tasto specifico (es. 'enter', 'esc', 'tab', 'backspace', o combinazioni come 'ctrl+c')."""
    k_raw = (key or "").strip().lower()
    if not k_raw:
        return "Capo, specifica quale tasto premere."

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        logger.info(f"⌨️ [SKILLS] Pressione tasto: '{k_raw}'")

        if "+" in k_raw:
            parts = [p.strip() for p in k_raw.split("+")]
            pyautogui.hotkey(*parts)
        else:
            alias = {
                "invio": "enter", "return": "enter",
                "canc": "delete", "cancella": "delete",
                "esc": "escape",
                "spazio": "space",
                "freccia_su": "up", "su": "up",
                "freccia_giu": "down", "giu": "down",
                "freccia_destra": "right", "destra": "right",
                "freccia_sinistra": "left", "sinistra": "left",
            }
            mapped = alias.get(k_raw, k_raw)
            pyautogui.press(mapped)

        return f"Ho premuto il tasto '{key}', Capo."
    except Exception as e:
        logger.error(f"Errore pressione tasto '{key}': {e}")
        return f"Capo, errore nella pressione del tasto '{key}': {e}"


# ─────────────────────────────────────────────────────────────
# 6b. ARTE CREATIVA: PAINT FISICO + GENERAZIONE IMMAGINI
# ─────────────────────────────────────────────────────────────
SOGLIA_BIANCO = 238            # un pixel più chiaro di così è "foglio"
LARGHEZZA_MIN_CANVAS = 240     # sotto questa soglia il quadrato da 200 px non entra
ALTEZZA_MIN_CANVAS = 220
FRAZIONE_MIN_RIGHE = 0.10      # il foglio deve coprire almeno il 10% delle righe
FRAZIONE_SOLIDITA = 0.85       # dentro il rettangolo trovato quasi tutto deve essere bianco
BLOCCHE_PX = 8                 # lato del blocco usato per il "massimo rettangolo"
SOGLIA_BLOCCO_BIANCO = 250     # media del blocco: 255 solo se è quasi tutto bianco
LATO_QUADRATO = 200
ENV_FALLBACK_CIECO = "CLIO_PAINT_BLIND_FALLBACK"
ENV_MODELLO_VISION_PAINT = "CLIO_PAINT_VISION_MODEL"


class CanvasPaint(NamedTuple):
    """Rettangolo del foglio bianco, in pixel dell'immagine catturata."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def larghezza(self) -> int:
        return self.right - self.left

    @property
    def altezza(self) -> int:
        return self.bottom - self.top


def _trova_canvas(immagine) -> Optional[CanvasPaint]:
    """Trova il foglio bianco di Paint. Funzione PURA: nessun I/O, testabile offline.

    Strategia (deterministica, nessun modello):
      1. maschera dei pixel quasi bianchi;
      2. la maschera viene ridotta a blocchi di 8x8 px: un blocco vale "foglio"
         solo se è quasi tutto bianco (icone, pannelli scuri e righelli non
         contano come foglio);
      3. si cerca il MASSIMO RETTANGOLO di blocchi bianchi (maximal rectangle
         su istogrammi): è il foglio, e non risente di overlay che spezzano le
         righe (es. il pannello "dimensione pennello" a sinistra del canvas);
      4. il rettangolo deve essere abbastanza grande, alto e solido.

    Nota: la prima versione raggruppava le righe per estremi arrotondati e su
    Paint reale sceglieva rettangoli sbagliati quando un pannello scuro spezzava
    le righe bianche (il quadrato finiva nella barra strumenti).
    """
    if immagine is None:
        return None

    maschera = immagine.convert("L").point(
        [0 if valore < SOGLIA_BIANCO else 255 for valore in range(256)])
    larghezza, altezza = maschera.size
    if larghezza < LARGHEZZA_MIN_CANVAS or altezza < ALTEZZA_MIN_CANVAS:
        return None

    blocchi = maschera.resize(
        (max(1, larghezza // BLOCCHE_PX), max(1, altezza // BLOCCHE_PX)),
        Image.BOX)
    larg_p, alt_p = blocchi.size
    bianco = [1 if valore >= SOGLIA_BLOCCO_BIANCO else 0
              for valore in blocchi.getdata()]

    altezze = [0] * larg_p
    migliore = (0, 0, 0, 0, 0)  # area, left, top, larghezza, altezza (in blocchi)
    for y in range(alt_p):
        base = y * larg_p
        for x in range(larg_p):
            altezze[x] = altezze[x] + 1 if bianco[base + x] else 0
        pila = []
        for x in range(larg_p + 1):
            corrente = altezze[x] if x < larg_p else 0
            inizio = x
            while pila and pila[-1][1] >= corrente:
                inizio_pila, alt = pila.pop()
                area = alt * (x - inizio_pila)
                if area > migliore[0]:
                    migliore = (area, inizio_pila, y - alt + 1,
                                x - inizio_pila, alt)
                inizio = inizio_pila
            pila.append((inizio, corrente))

    if migliore[0] <= 0:
        return None
    _, bl, bt, bw, bh = migliore
    canvas = CanvasPaint(bl * BLOCCHE_PX, bt * BLOCCHE_PX,
                         (bl + bw) * BLOCCHE_PX, (bt + bh) * BLOCCHE_PX)
    if canvas.larghezza < LARGHEZZA_MIN_CANVAS or canvas.altezza < ALTEZZA_MIN_CANVAS:
        return None
    # Una striscia bianca alta poche righe (barra strumenti) non è un foglio.
    if canvas.altezza < altezza * FRAZIONE_MIN_RIGHE:
        return None

    interno = maschera.crop((canvas.left, canvas.top, canvas.right, canvas.bottom))
    px_bianchi = interno.histogram()[255]
    if px_bianchi / float(interno.width * interno.height) < FRAZIONE_SOLIDITA:
        return None
    return canvas


def _cattura_finestra_paint(finestra):
    """Cattura SOLO la finestra Paint: (immagine, rect_px, scala_x, scala_y) o None.

    Preferisce mss (nessun PNG su disco, nessun pixel di altre finestre). Se mss
    non è disponibile ricade sullo screenshot completo già usato da CLIO, ma
    ritaglia subito il rettangolo della finestra.
    """
    try:
        left, top = int(finestra.left), int(finestra.top)
        larghezza, altezza = int(finestra.width), int(finestra.height)
    except Exception:
        return None
    if larghezza <= 0 or altezza <= 0:
        return None

    import pyautogui
    from PIL import Image

    # 1) Solo finestra, via mss. La scala DPI converte le coordinate logiche di
    #    pygetwindow in pixel fisici (1.0 su schermi al 100%).
    try:
        import mss
        dimensione_logica = pyautogui.size()
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            scala_x = monitor["width"] / float(dimensione_logica.width)
            scala_y = monitor["height"] / float(dimensione_logica.height)
            regione = {
                "left": round(left * scala_x),
                "top": round(top * scala_y),
                "width": max(1, round(larghezza * scala_x)),
                "height": max(1, round(altezza * scala_y)),
            }
            scatto = sct.grab(regione)
        immagine = Image.frombytes("RGB", scatto.size, scatto.bgra, "raw", "BGRX")
        rect_px = (regione["left"], regione["top"],
                   regione["left"] + regione["width"],
                   regione["top"] + regione["height"])
        return immagine, rect_px, scala_x, scala_y
    except Exception as exc:
        logger.info("Cattura finestra con mss non disponibile (%s): uso lo screenshot", exc)

    # 2) Fallback: screenshot completo, ritagliato sulla finestra.
    try:
        schermo = pyautogui.screenshot()
        logica = pyautogui.size()
        scala_x = schermo.width / float(logica.width)
        scala_y = schermo.height / float(logica.height)
        left_px = max(0, round(left * scala_x))
        top_px = max(0, round(top * scala_y))
        right_px = min(schermo.width, round((left + larghezza) * scala_x))
        bottom_px = min(schermo.height, round((top + altezza) * scala_y))
        if right_px <= left_px or bottom_px <= top_px:
            return None
        return (schermo.crop((left_px, top_px, right_px, bottom_px)),
                (left_px, top_px, right_px, bottom_px), scala_x, scala_y)
    except Exception as exc:
        logger.warning("Cattura Paint fallita: %s", exc)
        return None


def _vision_conferma_foglio(immagine) -> bool:
    """Chiede a un modello VISION LOCALE se nella finestra c'è un foglio bianco.

    Usa Ollama (llava di default): gratis, offline, nessuna immagine inviata in
    rete. Il modello risponde SOLO sì/no: le coordinate restano all'euristica,
    perché i bounding box dei modelli piccoli non sono affidabili.
    """
    if immagine is None:
        return False
    try:
        import base64
        import requests
        from config import OLLAMA_URL

        modello = os.getenv(ENV_MODELLO_VISION_PAINT, "llava:latest").strip()
        if not modello:
            return False
        copia = immagine.copy()
        copia.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        copia.convert("RGB").save(buffer, "JPEG", quality=70)
        risposta = requests.post(
            f"{str(OLLAMA_URL).rstrip('/')}/api/generate",
            json={
                "model": modello,
                "prompt": ("Questa è una finestra di Microsoft Paint. C'è un ampio "
                           "foglio rettangolare bianco visibile? Rispondi solo SI o NO."),
                "images": [base64.b64encode(buffer.getvalue()).decode("ascii")],
                "stream": False,
                "options": {"temperature": 0, "num_predict": 8},
            },
            timeout=20,
        )
        testo = (risposta.json().get("response") or "").strip().lower()
        esito = testo.startswith("si") or testo.startswith("sì")
        logger.info("Gate vision Paint (%s): %s -> %s", modello, testo[:20], esito)
        return esito
    except Exception as exc:
        logger.warning("Gate vision Paint non disponibile: %s: %s", type(exc).__name__, exc)
        return False


def _fallback_cieco_attivo() -> bool:
    """Il disegno alla cieca è un fallback esplicito, spento di default."""
    return os.getenv(ENV_FALLBACK_CIECO, "0").strip().lower() in {"1", "true", "si", "sì", "yes", "on"}


def _geometria(finestra) -> tuple:
    """(left, top, larghezza, altezza) correnti della finestra."""
    return (int(finestra.left), int(finestra.top),
            int(finestra.width), int(finestra.height))


def _attendi_geometria_stabile(finestra, timeout: float = 3.0) -> tuple:
    """Aspetta che la finestra smetta di muoversi/ridimensionarsi.

    Una finestra appena aperta può essere ancora in animazione: catturare le
    coordinate prima della fine produce tratti fuori bersaglio (verificato nel
    test reale: il quadrato finiva ~90 px a monte del punto calcolato).
    Ritorna l'ultima geometria osservata, anche in caso di timeout.
    """
    scadenza = time.monotonic() + timeout
    precedente = _geometria(finestra)
    conferme = 0
    while time.monotonic() < scadenza:
        time.sleep(0.15)
        attuale = _geometria(finestra)
        if attuale == precedente:
            conferme += 1
            if conferme >= 2:
                return attuale
        else:
            precedente, conferme = attuale, 0
    return precedente


def _tratto_lento(pyautogui, destinazione_px, scala_x, scala_y, passi: int = 6) -> None:
    """Porta il puntatore a destinazione in piccoli passi.

    Paint disegna ciò che campiona: con un solo `moveTo` lungo la penna unisce
    i due estremi in una diagonale. I passi intermedi rendono il lato continuo.
    """
    x0, y0 = pyautogui.position()
    x1, y1 = destinazione_px[0] / scala_x, destinazione_px[1] / scala_y
    for indice in range(1, passi + 1):
        pyautogui.moveTo(x0 + (x1 - x0) * indice / passi,
                         y0 + (y1 - y0) * indice / passi,
                         duration=0.03)


def _disegna_alla_cieca(finestra) -> Optional[str]:
    """Ultima risorsa dichiarata: quadrato al centro finestra, SENZA verifica."""
    try:
        import pyautogui
        centro_x = int(finestra.left) + int(finestra.width) // 2
        centro_y = int(finestra.top) + int(finestra.height) // 2
        meta = LATO_QUADRATO // 2
        pyautogui.moveTo(centro_x - meta, centro_y - meta, duration=0.2)
        pyautogui.mouseDown()
        try:
            for dx, dy in ((LATO_QUADRATO, 0), (0, LATO_QUADRATO),
                           (-LATO_QUADRATO, 0), (0, -LATO_QUADRATO)):
                pyautogui.moveRel(dx, dy, duration=0.1)
        finally:
            pyautogui.mouseUp()
        logger.warning("Paint: disegno alla cieca eseguito (fallback esplicito)")
        return ("Capo, ho disegnato alla cieca come fallback esplicito: non ho potuto "
                "verificare il risultato. Metti CLIO_PAINT_BLIND_FALLBACK=0 per avere "
                "un fallimento trasparente senza tratti casuali.")
    except Exception as exc:
        logger.warning("Fallback cieco fallito: %s", exc)
        return None


def disegna_su_paint(coordinate=None, confirmed: bool = False, origin: str = None) -> str:
    """Disegna e VERIFICA un quadrato al centro del foglio bianco di Paint.

    coordinate è mantenuto per compatibilità con i vecchi tool payload, ma
    viene ignorato: le coordinate nascono dal rilevamento locale del foglio
    (`_trova_canvas`), non dal modello. Se i pixel non bastano si chiede un
    parere sì/no al modello VISION locale (llava via Ollama).
    Il disegno alla cieca è un fallback ESPLICITO, spento di default
    (`CLIO_PAINT_BLIND_FALLBACK=0`): in caso di dubbio la skill fallisce
    dicendolo, invece di tracciare quadrati a caso.
    Il fail-safe PyAutoGUI (mouse nell'angolo alto-sinistro) resta attivo.
    """
    if os.name != "nt":
        return "Capo, il disegno diretto su Paint è disponibile solo su Windows."

    try:
        import pyautogui
        import pygetwindow as gw
        import desktop_agent

        def _trova_paint():
            finestre = gw.getWindowsWithTitle("Paint")
            return next((w for w in finestre
                         if w.title and "paint" in w.title.lower()), None)

        finestra = _trova_paint()
        if finestra is None:
            # La policy inietta confirmed=True solo dopo una conferma esplicita.
            if not desktop_agent.open_app(
                    "paint", confirmed=confirmed, origin=origin):
                return "Capo, Paint non è aperto e non sono riuscito ad avviarlo."
            scadenza = time.monotonic() + 8
            while time.monotonic() < scadenza and finestra is None:
                time.sleep(0.2)
                finestra = _trova_paint()
        if finestra is None:
            return "Capo, non trovo la finestra di Paint."

        if finestra.isMinimized:
            finestra.restore()
        # Usa un click fisico sulla barra del titolo: Window.activate() di
        # pygetwindow può sollevare PyGetWindowException su Windows (code 0).
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        pyautogui.click(int(finestra.left) + 20, int(finestra.top) + 10)
        time.sleep(0.5)
        scadenza = time.monotonic() + 3
        attiva = None
        while time.monotonic() < scadenza:
            attiva = gw.getActiveWindow()
            if attiva and getattr(attiva, "_hWnd", None) == getattr(finestra, "_hWnd", None):
                break
            time.sleep(0.1)
        if not attiva or getattr(attiva, "_hWnd", None) != getattr(finestra, "_hWnd", None):
            return "Capo, Paint non è in primo piano: non ho disegnato."

        # Non modificare un documento preesistente o non salvato.
        titolo = (finestra.title or "").strip().lower()
        if not any(parola in titolo for parola in ("senza titolo", "untitled")):
            return ("Capo, Paint mostra già un documento. Apri un nuovo canvas vuoto "
                    "per evitare di modificare un disegno esistente.")

        # 1) OCCHI: cattura della SOLA finestra Paint, in RAM (nessun PNG su disco).
        #    Prima si aspetta che la finestra sia ferma: se è ancora in animazione
        #    le coordinate calcolate sarebbero già vecchie.
        _attendi_geometria_stabile(finestra)
        cattura = _cattura_finestra_paint(finestra)
        if cattura is None:
            return "Capo, non riesco a catturare la finestra di Paint: non ho disegnato."
        immagine, rect_px, scala_x, scala_y = cattura

        # 2) RILEVAMENTO deterministico del foglio bianco (nessun modello).
        canvas = _trova_canvas(immagine)

        # 2b) GATE VISION locale (llava): solo se i pixel non bastano. Il modello
        #     risponde sì/no, non fornisce coordinate: serve a distinguere
        #     "foglio non riconosciuto" da "foglio assente".
        if canvas is None:
            if _vision_conferma_foglio(immagine):
                motivo = ("Capo, la visione locale vede il foglio di Paint ma non riesco "
                          "a calcolarne i bordi con i pixel: preferisco non disegnare a caso.")
            else:
                motivo = ("Capo, non riconosco un foglio bianco in Paint (pixel più "
                          "visione locale): non ho disegnato.")
            esito_cieco = _disegna_alla_cieca(finestra) if _fallback_cieco_attivo() else None
            if esito_cieco:
                return esito_cieco
            if _fallback_cieco_attivo():
                return motivo
            return (motivo + " Fallback cieco disattivato "
                    f"({ENV_FALLBACK_CIECO}=0): nessun tratto casuale.")

        # 3) Conversioni: pixel immagine -> pixel schermo -> coordinate mouse.
        canvas_left_px = rect_px[0] + canvas.left
        canvas_top_px = rect_px[1] + canvas.top
        canvas_right_px = rect_px[0] + canvas.right
        canvas_bottom_px = rect_px[1] + canvas.bottom

        centro_locale_x = (canvas.left + canvas.right) // 2
        centro_locale_y = (canvas.top + canvas.bottom) // 2
        centro_x_px = rect_px[0] + centro_locale_x
        centro_y_px = rect_px[1] + centro_locale_y
        raggio = LATO_QUADRATO // 2
        margine = 8

        if (centro_x_px - raggio < canvas_left_px + margine
                or centro_x_px + raggio > canvas_right_px - margine
                or centro_y_px - raggio < canvas_top_px + margine
                or centro_y_px + raggio > canvas_bottom_px - margine):
            return "Capo, il quadrato calcolato non rientra nel foglio di Paint."

        tratti = [[
            (centro_x_px - raggio, centro_y_px - raggio),
            (centro_x_px + raggio, centro_y_px - raggio),
            (centro_x_px + raggio, centro_y_px + raggio),
            (centro_x_px - raggio, centro_y_px + raggio),
            (centro_x_px - raggio, centro_y_px - raggio),
        ]]

        # Zona di verifica, in coordinate LOCALI all'immagine catturata.
        zona_locale = (
            centro_locale_x - raggio - margine,
            centro_locale_y - raggio - margine,
            centro_locale_x + raggio + margine + 1,
            centro_locale_y + raggio + margine + 1,
        )
        immagine_prima = immagine.crop(zona_locale).convert("RGB")

        # Paint scarta i tratti se il CANVAS non ha il focus (verificato: dopo il
        # click sulla barra del titolo il primo tratto va perso). Un click
        # singolo nell'angolo del foglio, fuori dalla zona di verifica, attiva
        # il canvas. Lascia al massimo un punto nell'angolo, non un disegno.
        pyautogui.click(round((canvas_left_px + 12) / scala_x),
                        round((canvas_top_px + 12) / scala_y))
        time.sleep(0.25)

        # 4 tratti SEPARATI, uno per lato. Sulla nuova Paint un unico tratto con
        # cambi di direzione viene disegnato solo nel primo segmento (verificato
        # sul campo): qui ogni lato è un mouseDown/mouseUp autonomo.
        lati = list(zip(tratti[0][:-1], tratti[0][1:]))
        for (x1, y1), (x2, y2) in lati:
            pyautogui.moveTo(round(x1 / scala_x), round(y1 / scala_y), duration=0.1)
            pyautogui.mouseDown()
            time.sleep(0.05)
            try:
                _tratto_lento(pyautogui, (x2, y2), scala_x, scala_y)
            finally:
                pyautogui.mouseUp()
            time.sleep(0.05)

        # Non dichiarare successo solo perché il mouse si è mosso: ricattura la
        # finestra (mai upload) e confronta i pixel della zona del quadrato.
        # Il puntatore viene spostato fuori dalla zona per non falsare il test.
        pyautogui.moveTo(round((canvas_left_px + 12) / scala_x),
                         round((canvas_top_px + 12) / scala_y), duration=0.1)
        time.sleep(0.2)
        cattura_dopo = _cattura_finestra_paint(finestra)
        if cattura_dopo is None:
            return ("Capo, ho disegnato ma non riesco a ricatturare Paint per "
                    "verificare il tratto: non posso confermarlo.")
        immagine_dopo = cattura_dopo[0].crop(zona_locale).convert("RGB")

        from PIL import ImageChops
        differenza = ImageChops.difference(immagine_prima, immagine_dopo).convert("L")
        pixel_cambiati = differenza.point(
            [0 if valore < 40 else 255 for valore in range(256)]
        ).histogram()[255]
        if pixel_cambiati < 40:
            return ("Capo, ho inviato il tratto ma non vedo modifiche verificabili "
                    "sul canvas; non posso confermare che il quadrato sia stato disegnato.")

        centro_x = round(centro_x_px / scala_x)
        centro_y = round(centro_y_px / scala_y)
        logger.info(
            "Quadrato Paint verificato localmente: canvas=%sx%s, centro=(%s,%s), "
            "lato=%s px, pixel_variati=%s",
            canvas.larghezza, canvas.altezza, centro_x, centro_y,
            LATO_QUADRATO, pixel_cambiati,
        )
        return (f"Capo, ho disegnato e verificato localmente un quadrato da "
                f"{LATO_QUADRATO}x{LATO_QUADRATO} pixel in Paint.")
    except Exception as exc:
        logger.exception("Errore nella skill disegna_su_paint")
        return f"Capo, non sono riuscito a disegnare su Paint: {type(exc).__name__}: {exc}"


def genera_immagine_pollinations(prompt: str, apri: bool = True) -> str:
    """Genera un'immagine raster via Pollinations e la salva in modo atomico.

    Il prompt viene inviato al servizio remoto: non passare screenshot o dati
    personali.
    """
    from io import BytesIO
    from pathlib import Path
    from urllib.parse import quote, urlsplit
    import uuid
    import webbrowser
    import requests

    testo = str(prompt or "").strip()
    if not testo or len(testo) > 1500:
        return "Capo, il prompt deve contenere da 1 a 1500 caratteri."
    if re.search(
            r"\b(?:realistic|realistica|realistico|photorealistic|"
            r"fotorealistica|fotorealistico)\b", testo, re.IGNORECASE):
        testo = f"{testo}, highly detailed, 8k, photorealistic"

    endpoint = os.getenv(
        "POLLINATIONS_IMAGE_ENDPOINT",
        "https://image.pollinations.ai/prompt",
    ).strip().rstrip("/")
    if urlsplit(endpoint).scheme != "https" or not urlsplit(endpoint).netloc:
        return "Capo, endpoint Pollinations non valido: è richiesto un URL HTTPS."
    url = f"{endpoint}/{quote(testo, safe='')}"
    headers = {}
    api_key = os.getenv("POLLINATIONS_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    params = {
        "model": os.getenv("POLLINATIONS_IMAGE_MODEL", "flux-realism"),
        "width": 1024,
        "height": 1024,
        "enhance": "true",
        "nologo": "true",
    }
    max_bytes = 20 * 1024 * 1024

    try:
        with requests.get(url, params=params, headers=headers, stream=True,
                          timeout=(10, 120), allow_redirects=True) as response:
            response.raise_for_status()
            if urlsplit(response.url).scheme != "https":
                return "Capo, Pollinations ha reindirizzato a una connessione non sicura."
            content_type = response.headers.get("Content-Type", "").lower()
            if not content_type.startswith("image/"):
                return ("Capo, Pollinations non ha restituito un'immagine "
                        f"({content_type or 'tipo sconosciuto'}).")
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                return "Capo, l'immagine supera il limite di 20 MB."
            contenuto = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    contenuto.extend(chunk)
                    if len(contenuto) > max_bytes:
                        return "Capo, l'immagine supera il limite di 20 MB."

        immagine = Image.open(BytesIO(contenuto))
        if (immagine.width < 1 or immagine.height < 1
                or immagine.width * immagine.height > 16_777_216):
            return "Capo, le dimensioni dell'immagine ricevuta non sono valide."
        formato = (immagine.format or "").upper()
        immagine.verify()
        estensione = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(formato)
        if not estensione:
            return ("Capo, il formato immagine ricevuto non è supportato: "
                    f"{formato or 'sconosciuto'}.")

        cartella = Path(__file__).resolve().parent / "data" / "generated_images"
        cartella.mkdir(parents=True, exist_ok=True)
        percorso = cartella / f"pollinations_{uuid.uuid4().hex}{estensione}"
        temporaneo = percorso.with_name(percorso.name + ".part")
        temporaneo.write_bytes(contenuto)
        temporaneo.replace(percorso)
        if apri:
            try:
                if hasattr(os, "startfile"):
                    os.startfile(str(percorso))
                else:
                    webbrowser.open(percorso.as_uri())
            except OSError as exc:
                return (f"Capo, immagine salvata in {percorso}, ma non sono riuscito "
                        f"ad aprirla: {exc}")
        return f"Capo, immagine generata e salvata in {percorso} ({len(contenuto)} byte)."
    except requests.Timeout:
        return "Capo, Pollinations non ha risposto entro 120 secondi. Nessuna immagine è stata salvata."
    except requests.RequestException as exc:
        return f"Capo, Pollinations non è raggiungibile o ha rifiutato la richiesta: {exc}"
    except Exception as exc:
        logger.exception("Errore durante la generazione Pollinations")
        return ("Capo, la risposta di Pollinations non è un'immagine valida: "
                f"{type(exc).__name__}: {exc}")


# ─────────────────────────────────────────────────────────────
# 7. ESECUZIONE CODICE PYTHON (run_python_code) & SICUREZZA
# ─────────────────────────────────────────────────────────────
DANGEROUS_PATTERNS = [
    r"\bshutil\.rmtree\b",
    r"\bos\.remove\b",
    r"\bos\.unlink\b",
    r"\bos\.rmdir\b",
    r"\btaskkill\b",
    r"\bkill\b",
    r"\bformat\b\s+[a-zA-Z]:",
    r"\bdel\s+/[fFqQsS]",
    r"\brmdir\s+/[sSqQ]",
    r"\bsubprocess\..*(?:del|rmdir|rm\s+-rf)",
]

def check_safety_action(action_name: str, params: dict = None) -> tuple[bool, str]:
    """Verifica se un'azione è critica/distruttiva e richiede conferma preventiva.

    Restituisce (is_dangerous, descrizione_avviso).
    """
    act = (action_name or "").lower().strip()
    params = params or {}

    if act in ("elimina_file", "svuota_cestino", "uccidi_processo", "spegni_pc", "riavvia_pc"):
        return True, f"l'operazione di sistema irreversibile '{act}'"

    if act in ("organize_folder", "organizza_cartella"):
        target_f = str(params.get("folder_path") or params.get("cartella") or "Desktop")
        return True, f"la riorganizzazione e lo spostamento dei file in '{target_f}'"

    if act in ("move_file", "sposta_file"):
        src = str(params.get("source") or params.get("sorgente") or "")
        dst = str(params.get("destination") or params.get("destinazione") or "")
        return True, f"lo spostamento del file da '{src}' a '{dst}'"

    if act == "run_python_code":
        code = str(params.get("code") or params.get("code_string") or "")
        for pat in DANGEROUS_PATTERNS:
            if re.search(pat, code, re.IGNORECASE):
                return True, f"l'esecuzione di codice Python con operazione critica ({pat.strip(r'\\b')})"

    return False, ""


def run_python_code(code_string: str, confirmed: bool = False, timeout: int = 5) -> str:
    """Esegue uno snippet di codice Python in sandbox subprocess con timeout di sicurezza e restituisce output o errore."""
    code = (code_string or "").strip()
    if not code:
        return "Capo, nessun codice Python fornito da eseguire."

    # Controllo di sicurezza
    is_danger, motivo = check_safety_action("run_python_code", {"code": code})
    if is_danger and not confirmed:
        logger.warning(f"🛡️ [SICUREZZA] Bloccata esecuzione codice distruttivo senza conferma: {code[:100]}")
        return f"Sto per fare {motivo}. Sei sicuro? [S/N]"

    logger.info(f"🐍 [SKILLS] Esecuzione codice Python ({len(code)} caratteri, timeout={timeout}s)")
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()

        risultato = []
        if out:
            risultato.append(out)
        if err:
            risultato.append(f"[STDERR]: {err}")

        if not risultato:
            return "Codice Python eseguito con successo (nessun output a terminale)."
        return "\n".join(risultato)
    except subprocess.TimeoutExpired:
        logger.warning(f"⚠️ [SKILLS] Timeout superato ({timeout}s) durante esecuzione codice Python.")
        return f"Capo, l'esecuzione del codice Python è andata in timeout: superato il limite di sicurezza di {timeout} secondi."
    except Exception as e:
        logger.error(f"Errore esecuzione Python: {e}")
        return f"Errore durante l'esecuzione del codice Python: {type(e).__name__}: {e}"


# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# 9. DEEP WEB RESEARCH (deep_web_research)
# ─────────────────────────────────────────────────────────────
def deep_web_research(query: str) -> str:
    """Cerca sul web con browser headless Playwright, legge i primi 3 link e sintetizza con Gemini."""
    q_clean = (query or "").strip()
    if not q_clean:
        return "Capo, specifica cosa vuoi che cerchi sul web."

    logger.info(f"🌐 [DEEP WEB RESEARCH] Avvio ricerca per: '{q_clean}'")
    from playwright.sync_api import sync_playwright

    links = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            # 1. Bing search (molto reattivo e stabile con Playwright headless)
            try:
                page.goto(f"https://www.bing.com/search?q={urllib.parse.quote_plus(q_clean)}", timeout=12000)
                page.wait_for_timeout(1000)
                for a in page.locator("li.b_algo h2 a").all():
                    h = a.get_attribute("href")
                    if h and h.startswith("http") and "bing.com" not in h and "microsoft.com" not in h:
                        if h not in links:
                            links.append(h)
                    elif h and "bing.com/ck/a" in h and h not in links:
                        links.append(h)
            except Exception as eb:
                logger.warning(f"Bing search notice: {eb}")

            # 2. Se servono più link, integra con Google
            if len(links) < 3:
                try:
                    page.goto("https://www.google.com", timeout=10000)
                    try:
                        page.locator("button:has-text('Rifiuta tutto'), button:has-text('Accetta tutto')").first.click(timeout=2000)
                    except Exception:
                        pass
                    inp = page.locator("textarea[name='q'], input[name='q']").first
                    inp.fill(q_clean)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(2000)
                    for a in page.locator("a[href^='http'], a[href*='/url?q=']").all():
                        href = a.get_attribute("href") or ""
                        if "/url?q=" in href:
                            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("q")
                            if parsed and parsed[0].startswith("http"):
                                target = parsed[0]
                                if "google." not in target and target not in links:
                                    links.append(target)
                        elif href.startswith("http") and "google." not in href and "youtube.com" not in href:
                            if href not in links:
                                links.append(href)
                except Exception as eg:
                    logger.warning(f"Google search notice: {eg}")

            top_links = links[:3]
            logger.info(f"🌐 [DEEP WEB RESEARCH] Trovati {len(links)} link, estraggo i top {len(top_links)}...")

            contents = []
            for i, u in enumerate(top_links, 1):
                try:
                    p_read = context.new_page()
                    p_read.goto(u, timeout=9000, wait_until="domcontentloaded")
                    p_read.wait_for_timeout(1000)
                    raw_text = p_read.evaluate("() => document.body ? document.body.innerText : ''")
                    clean = " ".join(raw_text.split())[:3000]
                    contents.append(f"=== FONTE {i}: {u} ===\n{clean}\n")
                    p_read.close()
                except Exception as ep:
                    logger.warning(f"Errore lettura pagina {u}: {ep}")
                    contents.append(f"=== FONTE {i}: {u} ===\n(Impossibile leggere la pagina: {ep})\n")

            browser.close()
    except Exception as e_pw:
        logger.error(f"Errore browser Playwright: {e_pw}")
        return f"Capo, c'è stato un problema durante la ricerca web con Playwright: {e_pw}"

    if not contents:
        return f"Capo, non ho trovato pagine utili sul web per '{q_clean}'."

    aggregated_text = "\n".join(contents)
    try:
        from config import GEMINI_API_KEY
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt_gemini = f"""Sei Clio, l'assistente AI e PC Agent del Capo.
Hai effettuato una ricerca approfondita sul web e letto i contenuti dei primi 3 siti per la richiesta: "{q_clean}".

Ecco i contenuti estratti dalle pagine web:
{aggregated_text}

ISTRUZIONI:
- Fornisci un riassunto dettagliato, chiaro e approfondito in italiano, formattato in Markdown.
- Evidenzia le notizie e informazioni più recenti e rilevanti.
- Cita le fonti principali o siti consultati.
- Mantieni il tono professionale, amichevole e diretto di Clio ("Capo, ecco cosa ho trovato...").
"""
        resp = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt_gemini
        )
        if resp and resp.text:
            return resp.text.strip()
    except Exception as e_llm:
        logger.warning(f"Errore sintesi Gemini: {e_llm}")

    return f"Capo, ecco le informazioni trovate sul web per '{q_clean}':\n\n" + aggregated_text[:1500]


# ─────────────────────────────────────────────────────────────
# 10. FILE MANAGER AUTONOMO
# ─────────────────────────────────────────────────────────────
def find_files(extension: str, folder_path: str = None) -> str:
    """Cerca file per estensione in una cartella specifica (o sul Desktop se non specificata)."""
    ext = (extension or "").strip().lower().lstrip(".")
    if not ext:
        return "Capo, specifica quale estensione cercare (es. 'pdf', 'png', 'txt')."

    target_dir = folder_path.strip() if folder_path else os.path.expandvars(r"%USERPROFILE%\Desktop")
    if not os.path.exists(target_dir):
        return f"Capo, la cartella specificata non esiste: {target_dir}"

    logger.info(f"🔍 [FILE MANAGER] Ricerca file *.{ext} in: {target_dir}")
    trovati = []
    try:
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("_trash", "node_modules", ".git")]
            for f in files:
                if f.lower().endswith(f".{ext}"):
                    full = os.path.join(root, f)
                    try:
                        sz_kb = os.path.getsize(full) / 1024
                        trovati.append((f, sz_kb, full))
                    except Exception:
                        trovati.append((f, 0, full))
    except Exception as e:
        return f"Capo, errore durante la ricerca: {e}"

    if not trovati:
        return f"Capo, non ho trovato nessun file *.{ext} in: {target_dir}"

    righe = [f"Capo, ho trovato {len(trovati)} file con estensione '.{ext}' in `{target_dir}`:"]
    for nome, sz, full in trovati[:25]:
        righe.append(f"- **{nome}** ({sz:.1f} KB) - `{full}`")
    if len(trovati) > 25:
        righe.append(f"... e altri {len(trovati) - 25} file.")
    return "\n".join(righe)


def move_file(source: str, destination: str, confirmed: bool = False) -> str:
    """Sposta un file da sorgente a destinazione con controllo di sicurezza."""
    import shutil
    src = os.path.abspath((source or "").strip().strip('"'))
    dst = os.path.abspath((destination or "").strip().strip('"'))

    if not src or not os.path.exists(src):
        return f"Capo, il file sorgente non esiste: {source}"

    is_danger, motivo = check_safety_action("move_file", {"source": src, "destination": dst})
    if is_danger and not confirmed:
        return f"Sto per fare {motivo}. Sei sicuro? [S/N]"

    try:
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)

        shutil.move(src, dst)
        logger.info(f"📦 [FILE MANAGER] Spostato: {src} -> {dst}")
        return f"File spostato con successo, Capo:\nDa: `{src}`\nA: `{dst}`"
    except Exception as e:
        logger.error(f"Errore spostamento file: {e}")
        return f"Capo, errore nello spostamento del file: {e}"


def organize_folder(folder_path: str = None, confirmed: bool = False) -> str:
    """Organizza i file di una cartella spostandoli in sottocartelle per tipo (Immagini, Documenti, Video, ecc.)."""
    import shutil
    target_dir = os.path.abspath((folder_path or "").strip().strip('"')) if folder_path else os.path.expandvars(r"%USERPROFILE%\Desktop")

    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        return f"Capo, la cartella specificata non esiste: {target_dir}"

    is_danger, motivo = check_safety_action("organize_folder", {"folder_path": target_dir})
    if is_danger and not confirmed:
        return f"Sto per fare {motivo}. Sei sicuro? [S/N]"

    logger.info(f"📁 [FILE MANAGER] Avvio riorganizzazione cartella: {target_dir}")

    CATEGORIE = {
        "Immagini": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff"},
        "Documenti": {".pdf", ".docx", ".doc", ".txt", ".xlsx", ".xls", ".pptx", ".csv", ".md", ".rtf"},
        "Video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv"},
        "Audio": {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a", ".wma"},
        "Archivi": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
        "Codice_e_Script": {".py", ".js", ".ts", ".html", ".css", ".json", ".bat", ".cmd", ".ps1", ".sh"},
        "Installatori": {".exe", ".msi", ".iso"},
    }

    IGNORE_FILES = {"desktop.ini", ".gitignore", "thumbs.db", "CLIO_CURRENT_STATE.md"}
    spostati = {cat: 0 for cat in CATEGORIE}

    try:
        entries = os.listdir(target_dir)
    except Exception as e:
        return f"Capo, impossibile leggere la cartella: {e}"

    for item in entries:
        full_p = os.path.join(target_dir, item)
        if os.path.isdir(full_p) or item.lower().endswith(".lnk") or item.startswith(".") or item.lower() in IGNORE_FILES:
            continue

        ext = os.path.splitext(item)[1].lower()
        dest_cat = None
        for cat, estensioni in CATEGORIE.items():
            if ext in estensioni:
                dest_cat = cat
                break

        if dest_cat:
            cat_folder = os.path.join(target_dir, dest_cat)
            os.makedirs(cat_folder, exist_ok=True)
            dst_file = os.path.join(cat_folder, item)
            if os.path.exists(dst_file):
                base, e = os.path.splitext(item)
                dst_file = os.path.join(cat_folder, f"{base}_{int(time.time())}{e}")
            try:
                shutil.move(full_p, dst_file)
                spostati[dest_cat] += 1
            except Exception as e_move:
                logger.warning(f"Errore spostamento file {item}: {e_move}")

    tot_spostati = sum(spostati.values())
    if tot_spostati == 0:
        return f"Capo, non c'erano file da organizzare nella cartella: `{target_dir}`."

    dettagli = [f"**{cat}**: {cnt}" for cat, cnt in spostati.items() if cnt > 0]
    return f"Capo, ho riorganizzato la cartella `{target_dir}`:\nSpostati con successo {tot_spostati} file in:\n- " + "\n- ".join(dettagli)


# ─────────────────────────────────────────────────────────────
# 11. GESTIONE EMAIL GMAIL (check_emails)
# ─────────────────────────────────────────────────────────────
def check_emails() -> str:
    """Controlla le ultime email non lette su Gmail e ne genera un riassunto vocale tramite Gemini."""
    import imaplib
    import email
    from email.header import decode_header

    user_email = os.getenv("GMAIL_USER")
    app_password = os.getenv("GMAIL_APP_PASSWORD")

    try:
        import config
        if not user_email:
            user_email = getattr(config, "GMAIL_USER", None)
        if not app_password:
            app_password = getattr(config, "GMAIL_APP_PASSWORD", None)
    except Exception:
        pass

    if not user_email or not app_password or "tuo_indirizzo" in user_email or "tua_password" in app_password:
        return (
            "Capo, per leggere le email di Gmail serve configurare le credenziali.\n"
            "Aggiungi GMAIL_USER e GMAIL_APP_PASSWORD nel file .env "
            "(puoi creare una Password per le App da myaccount.google.com -> Sicurezza -> Password per le app)."
        )

    logger.info(f"📧 [EMAIL] Connessione a imap.gmail.com per {user_email}...")
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user_email, app_password)
        mail.select("INBOX")

        status, messages = mail.search(None, "UNSEEN")
        ids = messages[0].split() if status == "OK" and messages[0] else []
        is_unread_search = True

        if not ids:
            status, messages = mail.search(None, "ALL")
            ids = messages[0].split() if status == "OK" and messages[0] else []
            is_unread_search = False

        if not ids:
            mail.logout()
            return "Capo, non ci sono email nella tua casella di posta in arrivo."

        last_ids = ids[-5:]
        email_data_list = []

        for mail_id in reversed(last_ids):
            res, msg_data = mail.fetch(mail_id, "(RFC822)")
            if res != "OK":
                continue
            for part in msg_data:
                if isinstance(part, tuple):
                    msg = email.message_from_bytes(part[1])
                    subject, encoding = decode_header(msg.get("Subject", "(Nessun Oggetto)"))[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding if encoding else "utf-8", errors="ignore")

                    from_hdr, encoding = decode_header(msg.get("From", "(Sconosciuto)"))[0]
                    if isinstance(from_hdr, bytes):
                        from_hdr = from_hdr.decode(encoding if encoding else "utf-8", errors="ignore")

                    body_text = ""
                    if msg.is_multipart():
                        for p in msg.walk():
                            if p.get_content_type() == "text/plain":
                                try:
                                    body_text = p.get_payload(decode=True).decode("utf-8", errors="ignore")
                                    break
                                except Exception:
                                    pass
                    else:
                        try:
                            body_text = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                        except Exception:
                            pass

                    clean_snippet = " ".join(body_text.split())[:300]
                    email_data_list.append(f"Mittente: {from_hdr} | Oggetto: {subject} | Testo: {clean_snippet}")

        mail.logout()
    except Exception as e:
        logger.error(f"Errore connessione IMAP Gmail: {e}")
        return f"Capo, c'è stato un problema durante l'accesso a Gmail: {e}"

    if not email_data_list:
        return "Capo, non sono riuscita ad estrarre il testo delle ultime email."

    emails_context = "\n".join(email_data_list)
    try:
        from config import GEMINI_API_KEY
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        status_label = "non lette" if is_unread_search else "recenti"
        prompt = f"""Sei Clio, l'assistente vocale del Capo.
Ecco le ultime email {status_label} arrivate su Gmail:
{emails_context}

ISTRUZIONI:
- Riassumi in modo naturale e discorsivo in italiano, perfetto per essere pronunciato a voce.
- Sintetizza mittenti e scopi principali (es: "Capo, hai 2 email importanti da Mario e una notifica da Whop riguardo a...").
- Mantieni la risposta concisa (3-4 frasi al massimo).
"""
        resp = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        )
        if resp and resp.text:
            return resp.text.strip()
    except Exception as e_gem:
        logger.warning(f"Errore Gemini email summary: {e_gem}")

    return "Capo, ecco le ultime email:\n" + "\n".join([f"- {item}" for item in email_data_list[:3]])


# ─────────────────────────────────────────────────────────────
# 15. OCR LOCALE E UI AUTOMATION (FASE 1 - Tris test)
# ─────────────────────────────────────────────────────────────
def leggi_schermo_locale(app_name: str = "notepad.exe") -> str:
    """Legge il testo visibile della finestra specificata usando OCR locale (rapidocr_onnxruntime).

    Niente cloud: tutto in locale, niente upload. Restituisce il testo riconosciuto
    con le confidenze. Utile per leggere griglie (es. Tris), menu, stati di gioco.

    Args:
        app_name: Nome dell'eseguibile (es. "notepad.exe", "mspaint.exe").

    Returns:
        Stringa con il testo rilevato e le confidenze, o messaggio di errore.
    """
    try:
        import pygetwindow as gw
    except Exception as exc:
        return f"Capo, pygetwindow non disponibile: {type(exc).__name__}: {exc}"

    # Trova la finestra
    finestre = gw.getWindowsWithTitle("")
    target = None
    for w in finestre:
        if w.title and app_name.lower().replace(".exe", "") in w.title.lower():
            target = w
            break
    if target is None:
        # Fallback: finestra attiva
        try:
            target = gw.getActiveWindow()
            if target is None:
                return f"Capo, nessuna finestra '{app_name}' trovata e nessuna finestra attiva."
        except Exception:
            return f"Capo, nessuna finestra '{app_name}' trovata."

    # Cattura la finestra con mss (stesso approccio di vision_agent)
    try:
        import mss
        from PIL import Image
    except Exception as exc:
        return f"Capo, mss o PIL non disponibili: {type(exc).__name__}: {exc}"

    left, top, right, bottom = target.left, target.top, target.right, target.bottom
    if right - left < 64 or bottom - top < 64:
        return "Capo, la finestra è troppo piccola per l'OCR."

    try:
        with mss.mss() as sct:
            regione = {"left": left, "top": top, "width": right - left, "height": bottom - top}
            shot = sct.grab(regione)
            immagine = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception as exc:
        return f"Capo, cattura finestra fallita: {type(exc).__name__}: {exc}"

    # OCR con rapidocr_onnxruntime
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as exc:
        return f"Capo, rapidocr_onnxruntime non disponibile: {type(exc).__name__}: {exc}"

    try:
        ocr = RapidOCR(lang="it")
        result = ocr(immagine, use_textline_orientation=True)
    except Exception as exc:
        return f"Capo, OCR fallito: {type(exc).__name__}: {exc}"

    # rapidocr_onnxruntime ritorna una tupla (detections, timings)
    # detections = lista di [box_coords, text, confidence]
    if not result or not result[0]:
        return "Capo, nessun testo rilevato nella finestra."

    detections = result[0]

    # Formatta output: testo + confidenza
    linee = []
    for det in detections:
        if len(det) >= 3:
            box, text, conf = det[0], det[1], det[2]
            linee.append(f"  [{conf:.2f}] {text}")

    if not linee:
        return "Capo, nessun testo rilevato nella finestra."

    return f"Testo rilevato in '{target.title}' ({len(linee)} blocchi):\n" + "\n".join(linee)


def clicca_elemento(app_name: str, element_name: str) -> str:
    """Clicca un elemento (pulsante, menu, cella) nell'app specificata via UI Automation.

    Usa computer_agent.click_element che usa pywinauto/uiautomation.
    L'elemento viene cercato per nome (title/automation_id). Se ambiguo o assente,
    non clicca e restituisce errore.

    Args:
        app_name: Eseguibile consentito (es. "notepad.exe", "mspaint.exe").
        element_name: Nome esatto dell'elemento da cliccare.

    Returns:
        Messaggio di conferma o errore.
    """
    try:
        from computer_agent import click_element
    except Exception as exc:
        return f"Capo, computer_agent non disponibile: {type(exc).__name__}: {exc}"

    try:
        ok = click_element(app_name, element_name)
    except Exception as exc:
        return f"Capo, click fallito: {type(exc).__name__}: {exc}"

    if ok:
        return f"Capo, ho cliccato '{element_name}' in {app_name}."
    else:
        return (f"Capo, non sono riuscito a cliccare '{element_name}' in {app_name}. "
                f"L'elemento non esiste, è ambiguo o l'app non è consentita/attiva.")


# REGISTRO SKILLS GLOBALE PER CLIO
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# 14. COMPATIBILITÀ OPENCODE (delegata al Coding Agent locale)
# ─────────────────────────────────────────────────────────────
def run_opencode_task(prompt: str, cwd: str = None, confirmed: bool = False) -> str:
    """Compatibilità per il vecchio tool: delega al Coding Agent locale sandboxato.

    Conserva il gate di conferma e il nome storico, ma non avvia più OpenCode,
    Gemini o comandi su workspace arbitrarie. `cwd`, se indicato, viene validato
    dal Coding Agent e deve rimanere dentro la sua sandbox.
    """

    if not confirmed:
        logger.warning("🧑‍💻 [CODING] richiesta legacy senza conferma: rifiutata.")
        return ("⚠️ Il task di coding modifica file nella sandbox e richiede una "
                "conferma esplicita. Usa 'coding task: ...' oppure conferma l'azione.")

    logger.info("🧑‍💻 [CODING] richiesta legacy delegata al provider locale, cwd=%r", cwd)
    try:
        from coding_agent import run_coding_task
        result = run_coding_task(prompt, workspace=cwd, timeout=120)
        test_output = str(result.get("test_output") or "").strip()
        agent_output = str(result.get("agent_output") or result.get("error") or "").strip()
        if result.get("success"):
            return ("✅ Coding Agent locale: task completato nella sandbox; "
                    f"test verificati.\n{test_output}")
        detail = agent_output or "nessun dettaglio fornito"
        return f"❌ Coding Agent locale: task NON completato.\n{detail}\n{test_output}".strip()
    except Exception as e:
        logger.error("🧑‍💻 [CODING] errore nella delega legacy: %s", e)
        return f"❌ Errore nel Coding Agent locale: {e}"


SKILLS_MAP = {
    "run_opencode_task": run_opencode_task,
    "opencode_task": run_opencode_task,
    "esegui_opencode": run_opencode_task,
    "chiedi_a_opencode": run_opencode_task,


    "deep_web_research": deep_web_research,
    "ricerca_web": deep_web_research,
    "cerca_web": deep_web_research,
    "cerca_su_internet": deep_web_research,

    "find_files": find_files,
    "cerca_file": find_files,
    "trova_file": find_files,

    "move_file": move_file,
    "sposta_file": move_file,

    "organize_folder": organize_folder,
    "organizza_cartella": organize_folder,
    "riordina_cartella": organize_folder,

    "check_emails": check_emails,
    "controlla_email": check_emails,
    "leggi_email": check_emails,
    "open_app": open_app,
    "apri_app": open_app,
    "apri_applicazione": open_app,

    "control_media": control_media,
    "media_control": control_media,
    "controllo_media": control_media,

    "get_system_time": get_system_time,
    "ora_sistema": get_system_time,
    "tempo_sistema": get_system_time,

    "search_youtube": search_youtube,
    "cerca_su_youtube": search_youtube,

    "take_screenshot": take_screenshot,
    "fai_screenshot": take_screenshot,
    "cattura_schermo": take_screenshot,

    "see_screen": see_screen,
    "analizza_schermo": see_screen,
    "guarda_schermo": see_screen,

    "click_coordinates": click_coordinates,
    "clicca": click_coordinates,
    "clicca_coordinate": click_coordinates,

    "type_text": type_text,
    "digita": type_text,
    "digita_testo": type_text,

    "press_key": press_key,
    "premi_tasto": press_key,

    "disegna_su_paint": disegna_su_paint,
    "genera_immagine_pollinations": genera_immagine_pollinations,

    "leggi_schermo_locale": leggi_schermo_locale,
    "clicca_elemento": clicca_elemento,

    "run_python_code": run_python_code,
    "esegui_python": run_python_code,
    "esegui_codice": run_python_code,
}

# ── R1: Memoria come servizio (The Scribe) ───────────────────────────────────
# Registrate qui per il routing delle skills; l'esecuzione passa comunque da
# policy.guarded_call (READ_ONLY in policy.py). Import protetto: se il pacchetto
# the_scribe manca, CLIO parte lo stesso e la skill risponde "offline".
try:
    from scribe_control import (
        get_scribe_resume,
        get_scribe_status,
        recupera_resume_progetto,
        stato_sessioni_scribe,
    )

    SKILLS_MAP.update({
        "recupera_resume_progetto": recupera_resume_progetto,
        "get_scribe_resume": get_scribe_resume,
        "stato_sessioni_scribe": stato_sessioni_scribe,
        "get_scribe_status": get_scribe_status,
    })
except Exception as _e_scribe:  # pragma: no cover - dipende dall'ambiente
    logger.warning(f"Skill The Scribe non disponibili: {_e_scribe}")
