"""desktop_agent.py — FASE 2 · C1: Desktop Agent (apri app + porta in primo piano).

Il primo mattone del "Partner Creativo": CLIO deve poter aprire lo strumento di
lavoro dell'utente (Paint, Blocco Note, VS Code...) e portarlo davanti, cosi'
l'utente e l'agente lavorano sulla STESSA finestra.

Contratto pubblico (usato dal registro AZIONI_MAP e dal LLM):
    open_app("mspaint")                 -> bool
    focus_window("notepad")             -> bool
    apri_app_e_inizia_idea(app_name, obiettivo, progetto) -> str

Sicurezza (difesa in profondita', come computer_agent.py):
  1. WHITELIST di eseguibili (`DEFAULT_ALLOWED_APPS`, override con
     CLIO_DESKTOP_ALLOWED_APPS="notepad.exe,mspaint.exe"). Un'eseguibile fuori
     whitelist non parte: ritorna False con un messaggio pulito;
  2. la policy tratta `apri_app_e_inizia_idea` come SENSITIVE; solo la
     richiesta esplicita dalla Web UI ha un'eccezione di auto-approvazione.
     L'app e' comunque limitata alla whitelist,
     `porta_finestra_in_primo_piano` e' REVERSIBLE (nessun effetto persistente);
  3. ogni esecuzione passa da `policy.guarded_call`: conferma + audit in
     logs/security.log. Una chiamata diretta (fuori dal control-plane) entra
     comunque nel guard: se la conferma non arriva, l'app NON parte;
  4. nessuna eccezione raggiunge CLIO: app inesistente, eseguibile non trovato,
     nessuna finestra corrispondente -> messaggio pulito / False.

Nessun import pesante a livello di modulo: ctypes viene importato dentro
`_enumerate_windows()` / `_activate_window()`, cosi' i test girano con mock e
CLIO parte anche senza un desktop Windows.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import policy

logger = logging.getLogger("ClioDesktop")

# ── Nomi delle azioni nel control-plane ───────────────────────────────────────
LAUNCH_ACTION = "apri_app_e_inizia_idea"          # SENSITIVE; UI allowlist dedicata
FOCUS_ACTION = "porta_finestra_in_primo_piano"    # REVERSIBLE in policy.py

# ── Whitelist ──────────────────────────────────────────────────────────────────
DEFAULT_ALLOWED_APPS = (
    "notepad.exe",       # Blocco Note
    "mspaint.exe",       # Paint
    "calc.exe",          # Calcolatrice
    "code.exe",          # Visual Studio Code
    "explorer.exe",      # Esplora file
    "antigravity.exe",   # IDE già consentito al Computer Use
)
ENV_ALLOWED_APPS = "CLIO_DESKTOP_ALLOWED_APPS"

# Alias accettati in ingresso: il nome canonico resta l'eseguibile della whitelist.
APP_ALIASES = {
    "notepad": "notepad.exe",
    "blocco note": "notepad.exe",
    "notepad.exe": "notepad.exe",
    "paint": "mspaint.exe",
    "mspaint": "mspaint.exe",
    "mspaint.exe": "mspaint.exe",
    "calculator": "calc.exe",
    "calcolatrice": "calc.exe",
    "calc": "calc.exe",
    "calc.exe": "calc.exe",
    "vscode": "code.exe",
    "visual studio code": "code.exe",
    "code": "code.exe",
    "code.exe": "code.exe",
    "esplora file": "explorer.exe",
    "explorer": "explorer.exe",
    "explorer.exe": "explorer.exe",
    "antigravity": "antigravity.exe",
    "antigravity.exe": "antigravity.exe",
}

# Hint sui titoli delle finestre (Windows 11: il titolo non contiene l'exe).
TITLE_HINTS = {
    "notepad.exe": (r"blocco note", r"notepad"),
    "mspaint.exe": (r"paint", r"pittura"),
    "calc.exe": (r"calcolatrice", r"calculator"),
    "code.exe": (r"visual studio code",),
    "explorer.exe": (r"esplora file", r"file explorer"),
    "antigravity.exe": (r"antigravity",),
}

# Limiti anti-abuso (il goal finisce in un file di stato e nel prompt del LLM).
MAX_GOAL_CHARS = 500
MAX_APP_NAME_CHARS = 120
DEFAULT_PROJECT_ID = "clio"


class DesktopAgentError(RuntimeError):
    """Errore prevedibile del Desktop Agent (messaggio già pronto per l'utente)."""


# ── Normalizzazione e whitelist ───────────────────────────────────────────────
def normalize_app_name(app_name: str) -> str:
    """Normalizza un nome app nell'eseguibile canonico.

    Accetta "Paint", "mspaint.exe", "C:/Windows/System32/mspaint.exe",
    "blocco note", "calcolatrice", "vscode".
    """
    if not isinstance(app_name, str) or not app_name.strip():
        raise DesktopAgentError(
            "app_name mancante: indica l'app da aprire (es. mspaint, notepad)")
    name = app_name.strip().strip('"').strip("'")
    if len(name) > MAX_APP_NAME_CHARS:
        raise DesktopAgentError(f"app_name troppo lungo ({len(name)} caratteri)")
    base = Path(name).name.lower()
    if not base:
        raise DesktopAgentError(f"app_name non valido: {app_name!r}")
    if base in APP_ALIASES:
        return APP_ALIASES[base]
    if "." not in base:
        base += ".exe"
    return APP_ALIASES.get(base, base)


def allowed_apps() -> frozenset:
    """Whitelist effettiva: default, oppure CLIO_DESKTOP_ALLOWED_APPS se impostata."""
    raw = (os.environ.get(ENV_ALLOWED_APPS) or "").strip()
    if raw:
        apps = set()
        for parte in raw.split(","):
            parte = parte.strip()
            if not parte:
                continue
            try:
                apps.add(normalize_app_name(parte))
            except DesktopAgentError:
                logger.warning(f"[DESKTOP] voce di whitelist ignorata: {parte!r}")
        if apps:
            return frozenset(apps)
    return frozenset(DEFAULT_ALLOWED_APPS)


def is_allowed_app(app_name: str) -> bool:
    """True se l'app (normalizzata) è nella whitelist attiva."""
    try:
        return normalize_app_name(app_name) in allowed_apps()
    except DesktopAgentError:
        return False


def _require_allowed(app_name: str) -> str:
    """Normalizza e verifica la whitelist. Solleva DesktopAgentError se non va bene."""
    exe = normalize_app_name(app_name)
    consentite = allowed_apps()
    if exe not in consentite:
        raise DesktopAgentError(
            f"app '{exe}' non consentita dal Desktop Agent. Consentite: "
            f"{', '.join(sorted(consentite))} "
            f"(override con la variabile {ENV_ALLOWED_APPS})")
    return exe


# ── Avvio del processo ────────────────────────────────────────────────────────
def _launch_process(exe: str) -> None:
    """Avvia davvero l'eseguibile.

    Prima ShellExecute (`os.startfile`), che risolve anche le App Paths e le app
    UWP; se non è disponibile si prova il PATH con subprocess. Un eseguibile
    inesistente solleva OSError: la gestione pulita è in `_launch`.
    """
    if os.name != "nt":
        raise DesktopAgentError("Desktop Agent disponibile solo su Windows")
    trovato = shutil.which(exe)
    if trovato:
        startfile = getattr(os, "startfile", None)
        if callable(startfile):
            # Windows 11 espone Paint come execution alias in WindowsApps;
            # ShellExecute sul file alias può fallire con WinError 1920.
            if (exe == "mspaint.exe" and "\\windowsapps\\" in trovato.lower()):
                try:
                    startfile("ms-paint:")
                    return
                except OSError as protocol_error:
                    # Alcuni ambienti Windows rifiutano il protocollo URI
                    # (WinError 5): riprova l'execution alias già risolto.
                    logger.warning(
                        "[DESKTOP] ms-paint: non disponibile (%s); provo l'alias locale",
                        protocol_error,
                    )
                    startfile(trovato)
                    return
            startfile(trovato)
            return
        subprocess.Popen([trovato], close_fds=True)
        return
    raise DesktopAgentError(f"eseguibile '{exe}' non trovato nel PATH del sistema")


def _launch(exe: str) -> bool:
    """Avvia l'app: True se il processo è partito, False con log pulito altrimenti."""
    try:
        _launch_process(exe)
        logger.info(f"[DESKTOP] app avviata: {exe}")
        return True
    except Exception as e:  # app inesistente, permessi, ambiente non Windows...
        logger.warning(f"[DESKTOP] impossibile aprire {exe}: {type(e).__name__}: {e}")
        return False


def _launch_after_confirm(app_name: str) -> bool:
    """Esecutore interno usato da policy.guarded_call dopo il 'yes'."""
    return _launch(app_name)


def open_app(app_name: str, confirmed: bool = False, confirm=None,
             origin: str = None) -> bool:
    """Apre un'app consentita. False (mai un'eccezione) se non è possibile.

    `confirmed=True` lo inietta il control-plane dopo la conferma dell'utente
    (policy._invoke): in quel caso non si chiede una seconda conferma.
    Una chiamata diretta passa comunque da policy.guarded_call.

    `origin` serve alla policy per distinguere la UI locale (flask/flask_ui)
    dalle altre superfici: senza, la conferma finirebbe su Telegram.
    """
    try:
        exe = _require_allowed(app_name)
    except DesktopAgentError as e:
        logger.warning(f"[DESKTOP] apertura rifiutata: {e}")
        return False

    if not confirmed:
        esito = policy.guarded_call(
            LAUNCH_ACTION, _launch_after_confirm, {"app_name": exe},
            confirm=confirm, origin=origin)
        return esito is True
    return _launch(exe)


# ── Finestra in primo piano ───────────────────────────────────────────────────
def _title_patterns(exe: str) -> List[str]:
    """Regex dei titoli che identificano la finestra di `exe`."""
    stem = Path(exe).stem.lower()
    patterns = [re.escape(stem)]
    patterns.extend(TITLE_HINTS.get(exe, ()))
    return patterns


def _process_name(pid: Any) -> str:
    """Nome dell'eseguibile del processo `pid` (minuscolo). '' se non leggibile."""
    try:
        pid_num = int(pid)
    except (TypeError, ValueError):
        return ""
    if pid_num <= 0:
        return ""
    try:
        import psutil

        return (psutil.Process(pid_num).name() or "").strip().lower()
    except Exception:  # psutil assente, processo chiuso, permessi
        return ""


def _enumerate_windows() -> List[Dict[str, Any]]:
    """Finestre visibili con titolo: [{"hwnd", "title", "pid"}]. [] se non è Windows."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception as e:  # pragma: no cover - dipende dall'ambiente
        logger.warning(f"[DESKTOP] ctypes non disponibile: {type(e).__name__}: {e}")
        return []

    finestre: List[Dict[str, Any]] = []
    protocollo = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            lunghezza = user32.GetWindowTextLengthW(hwnd)
            if lunghezza <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(lunghezza + 1)
            user32.GetWindowTextW(hwnd, buffer, lunghezza + 1)
            titolo = (buffer.value or "").strip()
            if titolo:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                finestre.append(
                    {"hwnd": int(hwnd), "title": titolo, "pid": int(pid.value)})
        except Exception:  # una finestra anomala non deve fermare l'enumerazione
            pass
        return True

    try:
        user32.EnumWindows(protocollo(_callback), 0)
    except Exception as e:  # pragma: no cover - dipende dall'ambiente
        logger.warning(f"[DESKTOP] EnumWindows fallita: {type(e).__name__}: {e}")
        return []
    return finestre


def _activate_window(hwnd: int) -> bool:
    """Ripristina e porta in primo piano la finestra `hwnd`."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception as e:  # pragma: no cover - dipende dall'ambiente
        logger.warning(f"[DESKTOP] focus fallito su {hwnd}: {type(e).__name__}: {e}")
        return False


def _find_window(exe: str, windows: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """Finestra di `exe`: prima per PROCESSO, poi (fallback) per titolo.

    Il match per solo titolo è troppo lascivo ("paint" comparirebbe anche in una
    scheda del browser): il nome del processo è la prova forte. psutil è già una
    dipendenza di CLIO; se manca si usa solo il match per titolo.
    """
    candidate = windows if windows is not None else _enumerate_windows()

    for finestra in candidate:
        if _process_name(finestra.get("pid")) == exe:
            return finestra

    patterns = _title_patterns(exe)
    for finestra in candidate:
        titolo = str(finestra.get("title") or "").lower()
        if not titolo:
            continue
        if any(re.search(p, titolo, re.IGNORECASE) for p in patterns):
            return finestra
    return None


def find_window(app_name: str) -> Optional[Dict[str, Any]]:
    """Finestra visibile dell'app (`{"hwnd","title","pid"}`) o None. Nessun side effect.

    Usata anche dal Vision Agent per sapere QUALE rettangolo catturare: nessuna
    app viene aperta o portata in primo piano.
    """
    try:
        exe = normalize_app_name(app_name)
    except DesktopAgentError:
        return None
    return _find_window(exe)


def _focus(app_name: str) -> bool:
    exe = normalize_app_name(app_name)
    finestra = _find_window(exe)
    if not finestra:
        logger.info(f"[DESKTOP] nessuna finestra per {exe}")
        return False
    ok = _activate_window(finestra["hwnd"])
    logger.info(f"[DESKTOP] focus su '{finestra.get('title')}' (hwnd={finestra['hwnd']}): {ok}")
    return ok


def focus_window(app_name: str) -> bool:
    """Porta in primo piano la finestra dell'app. False se non c'è o non si attiva.

    REVERSIBLE in policy.py: passa dal guard per l'audit, senza conferma.
    """
    if not isinstance(app_name, str) or not app_name.strip():
        logger.warning("[DESKTOP] focus_window senza app_name")
        return False
    return policy.guarded_call(FOCUS_ACTION, _focus, {"app_name": app_name}) is True


# ── Azione composta: apri lo strumento e apri l'idea ─────────────────────────
def _clean_goal(obiettivo: str) -> str:
    goal = (obiettivo or "").strip()
    if not goal:
        raise DesktopAgentError("obiettivo mancante: dimmi cosa vogliamo realizzare")
    if len(goal) > MAX_GOAL_CHARS:
        raise DesktopAgentError(
            f"obiettivo troppo lungo ({len(goal)} caratteri, massimo {MAX_GOAL_CHARS})")
    return goal


def _apri_e_inizia(app_name: str, obiettivo: str, progetto: str) -> str:
    """Esecutore interno (già autorizzato): apre l'app e crea la IdeaSession."""
    import idea_session as idea

    exe = _require_allowed(app_name)
    goal = _clean_goal(obiettivo)
    project = idea.normalize_project_id(progetto or DEFAULT_PROJECT_ID)

    if not _launch(exe):
        return (f"❌ Non riesco ad aprire «{exe}». Verifica che sia installata "
                f"o aggiungila a {ENV_ALLOWED_APPS}.")

    sessione = idea.create_idea_session(goal=goal, project_id=project, active_app=exe)
    logger.info(f"[DESKTOP] idea {sessione.idea_id} avviata su {exe} (progetto {project})")
    return (f"✅ {exe} aperto. Idea {sessione.idea_id[:8]} avviata per il progetto "
            f"«{project}»: {sessione.user_goal}")


def apri_app_e_inizia_idea(app_name: str, obiettivo: str, progetto: str = DEFAULT_PROJECT_ID,
                           confirmed: bool = False, confirm=None,
                           origin: str = None) -> str:
    """Apre lo strumento di lavoro e apre una IdeaSession legata all'obiettivo.

    SENSITIVE in policy.py; la Web UI ha una allowlist dedicata per questa
    sola azione. Le altre origini richiedono conferma esplicita. Se l'app non parte, NON viene creata
    nessuna sessione (niente stato fantasma).

    `origin` va propagato: senza, la conferma finisce su Telegram e dalla Web UI
    locale diventa "Conferma scaduta" (stesso bug di open_app, 30/10/2026).
    """
    if not confirmed:
        esito = policy.guarded_call(
            LAUNCH_ACTION, _apri_e_inizia,
            {"app_name": app_name, "obiettivo": obiettivo, "progetto": progetto or DEFAULT_PROJECT_ID},
            confirm=confirm, origin=origin)
        return esito if isinstance(esito, str) else (
            "❌ Azione non eseguita: apertura app non confermata.")
    try:
        return _apri_e_inizia(app_name, obiettivo, progetto)
    except Exception as e:  # obiettivo/app non validi: messaggio pulito, nessuna eccezione
        logger.warning(f"[DESKTOP] apri_app_e_inizia_idea fallita: {type(e).__name__}: {e}")
        return f"❌ {e}"


def describe_desktop_agent() -> str:
    """Riepilogo leggibile dell'agente (usato a voce e nei report)."""
    return (
        "Desktop Agent (C1): apre un'app consentita e crea una IdeaSession. "
        f"App: {', '.join(sorted(allowed_apps()))}. "
        "Apertura = SENSITIVE (allowlist esplicita UI), focus = REVERSIBLE."
    )
