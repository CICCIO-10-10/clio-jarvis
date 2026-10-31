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
import subprocess
import webbrowser
import urllib.parse
import io
import contextlib
import re
from datetime import datetime
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
    "spotify": ["spotify:", os.path.expandvars(r"%APPDATA%\Spotify\Spotify.exe"), "spotify"],
    "chrome": ["chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "google-chrome"],
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

def open_app(app_name: str) -> str:
    """Apre un'applicazione di Windows (es. Spotify, Chrome, Notepad) usando subprocess o os.startfile."""
    app_clean = (app_name or "").strip().lower()
    if not app_clean:
        return "Capo, non hai specificato quale applicazione aprire."

    logger.info(f"🚀 [SKILLS] Richiesta apertura app: {app_name}")

    if "spotify" in app_clean:
        try:
            os.startfile("spotify:")
            return "Ho aperto Spotify per te, Capo."
        except Exception:
            pass

    candidati = KNOWN_APPS.get(app_clean, [app_name.strip()])
    for cmd in candidati:
        try:
            os.startfile(cmd)
            return f"Ho aperto {app_name} per te, Capo."
        except Exception:
            pass
        try:
            subprocess.Popen(f'start "" "{cmd}"', shell=True)
            return f"Ho aperto {app_name} per te, Capo."
        except Exception:
            pass

    try:
        subprocess.Popen(app_name, shell=True)
        return f"Ho aperto {app_name} per te, Capo."
    except Exception as e:
        logger.error(f"Impossibile aprire {app_name}: {e}")
        return f"Capo, non sono riuscita ad aprire {app_name}: {e}"


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

    if act_clean in ("play", "pause", "playpause", "play/pause", "play/pausa", "pausa", "riproduci", "stop"):
        _send_vk(VK_MEDIA_PLAY_PAUSE)
        return "Ho impostato Play/Pausa sulla riproduzione, Capo."
    elif act_clean in ("next", "avanti", "prossimo", "successivo", "nexttrack", "salta"):
        _send_vk(VK_MEDIA_NEXT_TRACK)
        return "Passata alla traccia successiva, Capo."
    elif act_clean in ("prev", "previous", "indietro", "precedente", "prevtrack"):
        _send_vk(VK_MEDIA_PREV_TRACK)
        return "Tornata alla traccia precedente, Capo."
    elif act_clean in ("volume_up", "vol_up", "volume su", "su", "alza", "alza volume", "volume_su"):
        _send_vk(VK_VOLUME_UP, repeat=5)
        return "Ho alzato il volume del PC, Capo."
    elif act_clean in ("volume_down", "vol_down", "volume giu", "volume giù", "giu", "giù", "abbassa", "abbassa volume", "volume_giu"):
        _send_vk(VK_VOLUME_DOWN, repeat=5)
        return "Ho abbassato il volume del PC, Capo."
    elif act_clean in ("mute", "muto", "muta", "silenzio", "unmute"):
        _send_vk(VK_VOLUME_MUTE)
        return "Ho attivato/disattivato il muto, Capo."
    else:
        _send_vk(VK_MEDIA_PLAY_PAUSE)
        return f"Ho inviato il comando multimediale '{action}', Capo."


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


# REGISTRO SKILLS GLOBALE PER CLIO
# ─────────────────────────────────────────────────────────────
SKILLS_MAP = {

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

    "run_python_code": run_python_code,
    "esegui_python": run_python_code,
    "esegui_codice": run_python_code,
}
