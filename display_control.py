"""
CLIO - display_control.py
Effetto "Jarvis": risveglio del monitor + apertura/messa in primo piano
della Web UI quando viene rilevata la wake-word "Ehi Clio".

Espone tre funzioni, tutte idempotenti e a prova di errore (non sollevano MAI
eccezioni verso il chiamante: loggano e ritornano un bool):

    wake_screen()        -> riaccende il monitor dal risparmio energetico
                            (input sintetico "invisibile": tasto VK_F15, con
                            fallback a un micro-movimento del mouse)
    open_clio_ui()       -> apre la Web UI in Google Chrome in modalita' "app"
                            (senza barra indirizzi, sembra un'app nativa) o la
                            porta in primo piano se e' gia' aperta; fallback al
                            browser di default se Chrome non c'e'
    focus_clio_window()  -> porta in primo piano la finestra "C.L.I.O." esistente

Principio di design: nessuna dipendenza obbligatoria oltre alla stdlib.
pygetwindow e winreg sono usati se disponibili, con fallback via ctypes e
webbrowser. Ogni funzione e' silenziosa sui fallimenti: il wake-word loop non
deve mai bloccarsi perche' un monitor o Chrome non rispondono.
"""

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes

logger = logging.getLogger("DisplayControl")

CLIO_UI_URL = "http://127.0.0.1:5000"
CLIO_WINDOW_TITLE = "C.L.I.O."

_IS_WINDOWS = sys.platform == "win32"

# ──────────────────────────────────────────────────────────────
#  Strutture Win32 per SendInput (tasto sintetico)
# ──────────────────────────────────────────────────────────────
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_F15 = 0x7E          # tasto "fittizio": nessuna funzione di sistema, ma
                       # genera comunque un input che sveglia il monitor.

if _IS_WINDOWS:
    # ULONG_PTR: 8 byte su processi a 64 bit, 4 byte su 32 bit.
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("u", _INPUTUNION),
        ]


def _send_f15():
    """Invia una pressione+rlascio di VK_F15 via SendInput.

    Ritorna True se l'input e' stato consegnato. Il tasto F15 non ha alcuna
    funzione di sistema, quindi non produce effetti collaterali visibili: serve
    solo a marcare il sistema come "attivo" e far riaccendere il monitor.
    """
    if not _IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32
        inputs = (_INPUT * 2)()
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].u.ki = _KEYBDINPUT(wVk=VK_F15, wScan=0, dwFlags=0,
                                     time=0, dwExtraInfo=0)
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].u.ki = _KEYBDINPUT(wVk=VK_F15, wScan=0, dwFlags=KEYEVENTF_KEYUP,
                                     time=0, dwExtraInfo=0)
        inviati = user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))
        return inviati == 2
    except Exception as e:
        logger.debug("SendInput(F15) fallito: %s", e)
        return False


def _nudge_mouse():
    """Fallback: sposta il cursore di 1px e lo rimette a posto (impercettibile).

    Alcuni driver ignorano gli input da tastiera sintetica; un micro-movimento
    del mouse e' il modo piu' universale per generare attivita' e svegliare il
    monitor."""
    if not _IS_WINDOWS:
        return False
    try:
        user32 = ctypes.windll.user32

        class _POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        pt = _POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return False
        user32.SetCursorPos(pt.x + 1, pt.y)
        user32.SetCursorPos(pt.x, pt.y)
        return True
    except Exception as e:
        logger.debug("Micro-movimento mouse fallito: %s", e)
        return False


def wake_screen():
    """Riaccende il monitor se e' in risparmio energetico.

    Strategia (a cascata, la prima che riesce vince):
      1. SetThreadExecutionState(ES_DISPLAY_REQUIRED) — segnala l'esigenza di
         tenere acceso il display per il brevissimo tempo necessario.
      2. Input sintetico VK_F15 (invisibile, nessun side effect).
      3. Fallback: micro-movimento del cursore di 1px.

    Ritorna True se almeno una strategia ha avuto successo.
    """
    if not _IS_WINDOWS:
        return False

    ES_CONTINUOUS = 0x80000000
    ES_DISPLAY_REQUIRED = 0x00000002
    # SetThreadExecutionState vive in kernel32, non in user32.
    kernel32 = ctypes.windll.kernel32

    # 1) Segnala al sistema che il display serve (lo riaccende dal solo standby).
    try:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
    except Exception as e:
        logger.debug("SetThreadExecutionState fallito: %s", e)

    try:
        # 2) Input sintetico "invisibile".
        ok = _send_f15()
        # 3) Fallback: micro-movimento mouse.
        if not ok:
            ok = _nudge_mouse()
        return ok
    finally:
        # Rilascia il vincolo sul display (lascia che si rispenga da solo).
        try:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
#  Finestre
# ──────────────────────────────────────────────────────────────
def _find_clio_windows():
    """Ritorna la lista delle finestre il cui titolo contiene "C.L.I.O.".

    Usa pygetwindow (match per sottostringa sul titolo). Lista vuota se la
    libreria non e' disponibile o nessuna finestra corrisponde."""
    try:
        import pygetwindow as gw
    except Exception as e:
        logger.debug("pygetwindow non disponibile: %s", e)
        return []
    try:
        return list(gw.getWindowsWithTitle(CLIO_WINDOW_TITLE))
    except Exception as e:
        logger.debug("Ricerca finestra '%s' fallita: %s", CLIO_WINDOW_TITLE, e)
        return []


def _force_foreground(hwnd):
    """Porta in primo piano una finestra Win32 aggirando il 'foreground lock'.

    Windows vieta a un processo in background di rubare il focus: il trucco
    standard e' inviare una pressione di ALT subito prima di SetForegroundWindow."""
    try:
        user32 = ctypes.windll.user32
        VK_MENU = 0x12
        SW_RESTORE = 9

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        return bool(user32.GetForegroundWindow() == hwnd)
    except Exception as e:
        logger.debug("SetForegroundWindow fallito (hwnd=%s): %s", hwnd, e)
        return False


def focus_clio_window():
    """Porta in primo piano la finestra "C.L.I.O." se esiste.

    Ritorna True se una finestra e' stata trovata E attivata, False altrimenti
    (nessuna finestra aperta o attivazione fallita)."""
    if not _IS_WINDOWS:
        return False

    finestre = _find_clio_windows()
    if not finestre:
        logger.debug("[JARVIS] Nessuna finestra '%s' aperta.", CLIO_WINDOW_TITLE)
        return False

    win = finestre[0]

    # 1) Via pygetwindow (restore se minimizzata + activate).
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
        logger.debug("[JARVIS] Finestra Clio in primo piano (pygetwindow).")
        return True
    except Exception as e:
        logger.debug("pygetwindow.activate() fallito: %s", e)

    # 2) Fallback via ctypes sull'handle nativo.
    hwnd = getattr(win, "_hWnd", None)
    if hwnd and _force_foreground(hwnd):
        logger.debug("[JARVIS] Finestra Clio in primo piano (ctypes).")
        return True
    return False


# ──────────────────────────────────────────────────────────────
#  Apertura Web UI
# ──────────────────────────────────────────────────────────────
def _find_chrome():
    """Trova chrome.exe: PATH -> percorsi standard -> registro di Windows."""
    for nome in ("chrome.exe", "chrome"):
        p = shutil.which(nome)
        if p:
            return p

    candidati = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LocalAppData", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LocalAppData", ""),
                     "Chromium", "Application", "chrome.exe"),
    ]
    for c in candidati:
        if c and os.path.isfile(c):
            return c

    # Registro: App Paths (copre installazioni non standard / per-utente).
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ) as k:
                    return winreg.QueryValue(k, None)
            except OSError:
                continue
    except Exception:
        pass
    return None


def open_clio_ui():
    """Apre la Web UI di Clio in modalita' "app", o la porta in primo piano.

    Comportamento:
      1. Se una finestra "C.L.I.O." esiste gia' -> NON apre un duplicato,
         chiama focus_clio_window().
      2. Altrimenti lancia Chrome con --app=URL (finestra senza barra
         indirizzi, massimizzata: sembra un'app nativa).
      3. Se Chrome non c'e' -> apre l'URL nel browser di default.

    Ritorna True se la UI e' stata aperta o portata in primo piano.
    """
    # 1) Gia' aperta? -> evita i duplicati, porta in primo piano.
    if _find_clio_windows():
        logger.info("[JARVIS] UI Clio gia' aperta: la porto in primo piano.")
        return focus_clio_window()

    # 2) Apri in Chrome in modalita' app (senza barra degli indirizzi).
    chrome = _find_chrome()
    if chrome:
        try:
            subprocess.Popen(
                [chrome, "--app=" + CLIO_UI_URL, "--start-maximized"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.info("[JARVIS] UI Clio aperta in Chrome (modalita' app).")
            return True
        except Exception as e:
            logger.warning("[JARVIS] Avvio Chrome fallito (%s): uso il browser di default.", e)

    # 3) Fallback: browser di default.
    try:
        import webbrowser
        webbrowser.open(CLIO_UI_URL)
        logger.info("[JARVIS] UI Clio aperta nel browser di default.")
        return True
    except Exception as e:
        logger.error("[JARVIS] Impossibile aprire la UI Clio: %s", e)
        return False


__all__ = ["wake_screen", "open_clio_ui", "focus_clio_window",
           "CLIO_UI_URL", "CLIO_WINDOW_TITLE"]
