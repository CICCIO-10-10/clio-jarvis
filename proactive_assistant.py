"""proactive_assistant.py — R8: Proattività Quotidiana (privacy-first).

CLIO guarda SOLO la finestra in primo piano e, se sei su un browser o su un
editor, propone note del vault pertinenti con una notifica desktop discreta.

Privacy (regole ferree):
  * nessun log di cosa guardi: app e titolo non finiscono mai su file o logger;
  * lo stato (ultima notifica, indice del vault) vive solo in memoria;
  * in navigazione privata/incognito non viene prodotto alcun suggerimento;
  * nessuna cattura schermo, nessun tasto premuto, nessun file del vault toccato.

Rate limit:
  * al massimo UNA notifica ogni `RATE_LIMIT_SECONDS` (30 minuti).

Contratto pubblico:
    get_foreground_app() -> str                  # es. "chrome.exe"
    get_foreground_window() -> {"app","title"}
    check_triggers(app_name, vault_path, window_title=None) -> list[str]
    show_notification(suggestion) -> bool
    notify_if_allowed(suggestion, now=None) -> bool
    monitor_tick(vault_path="", window=None) -> dict
    attiva_proattivita(vault_path="", interval_seconds=60) -> str
    proattivita_attiva() -> bool
    ferma_proattivita() -> str

Azione CLIO (READ_ONLY in policy.py):
    attiva_proattivita(vault_path="", interval_seconds=60) -> str
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ClioProactive")

# ── Configurazione ───────────────────────────────────────────────────────────
RATE_LIMIT_SECONDS = 30 * 60          # max 1 notifica ogni 30 minuti
DEFAULT_INTERVAL_SECONDS = 60         # ogni quanto guardare la finestra attiva
MAX_SUGGESTIONS = 3                   # suggerimenti per singolo controllo
MAX_NOTIFICATION_CHARS = 180          # notifica discreta, non un report
INDEX_TTL_SECONDS = 600               # riusa l'indice del vault per 10 minuti

BROWSER_APPS = frozenset({
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "chromium.exe", "chromium",
})
EDITOR_APPS = frozenset({
    "code.exe", "notepad.exe", "notepad++.exe", "sublime_text.exe",
    "pycharm64.exe", "pycharm.exe", "antigravity.exe", "obsidian.exe",
    "wordpad.exe", "atom.exe", "zed.exe",
})

# Marcatori di navigazione privata (case-insensitive) sul titolo della finestra.
INCOGNITO_MARKERS = (
    "incognito", "in incognito", "navigazione privata", "modalita privata",
    "modalità privata", "private browsing", "private window", "finestra privata",
    "navigazione anonima", "seanless incognito", "inprivate",
)

BROWSER_LABELS = ("google chrome", "mozilla firefox", "microsoft edge", "brave",
                  "opera", "vivaldi", "chromium")

STOPWORDS = frozenset("""
    the and for with you your this that from are was were will can not non
    che del della delle degli dei con per una uno gli sul sulla sono come
    dove quando anche piu più meno solo tra fra quindi perche perché cosa
    home page search cerca cerca... nuovo nuova note nota appunti
""".split())

PROJECT_FOLDERS = ("10_progetti", "projects", "progetti", "10-progetti")


# ── Finestra in primo piano ──────────────────────────────────────────────────
def _window_text(hwnd: int) -> str:
    """Titolo della finestra via Win32 (stringa vuota se non disponibile)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value or ""
    except Exception:
        return ""


def get_foreground_window() -> Dict[str, str]:
    """App e titolo della finestra in primo piano, SOLO in memoria.

    Nessuna scrittura su file/log: il titolo può contenere dati personali.
    Ritorna `{"app": "", "title": ""}` se non è determinabile.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"app": "", "title": ""}
        title = _window_text(hwnd)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return {"app": "", "title": title}
        import psutil

        try:
            name = psutil.Process(pid.value).name()
        except Exception:
            name = ""
        return {"app": (name or "").strip(), "title": title}
    except Exception as exc:  # ambiente senza Win32/psutil: degradazione pulita
        logger.debug("Finestra in primo piano non leggibile: %s", type(exc).__name__)
        return {"app": "", "title": ""}


def get_foreground_app() -> str:
    """Nome dell'eseguibile in primo piano (es. "chrome.exe"), "" se ignoto."""
    return get_foreground_window().get("app", "")


# ── Utilità di testo ─────────────────────────────────────────────────────────
def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tokens(text: str) -> set:
    """Parole significative (>=4 caratteri), minuscole, senza punteggiatura."""
    return {word for word in re.findall(r"[^\W_]{4,}", (text or "").casefold())
            if word not in STOPWORDS}


def _is_private(title: str) -> bool:
    """True se il titolo indica navigazione privata/incognito."""
    lowered = (title or "").casefold()
    return any(marker in lowered for marker in INCOGNITO_MARKERS)


def _clean_title(title: str) -> str:
    """Toglie dal titolo i nomi delle app ("… - Google Chrome")."""
    parti = re.split(r"\s+[-\u2013\u2014|]\s+", (title or "").strip())
    tenute = [p.strip() for p in parti
              if p.strip() and not any(label in p.casefold() for label in BROWSER_LABELS)
              and not p.casefold().endswith(".exe")]
    return " - ".join(tenute) if tenute else (title or "").strip()


def _topic(title: str) -> str:
    """Argomento leggibile ricavato dal titolo della finestra."""
    return _clip(_clean_title(title), 60)


# ── Indice del vault (solo memoria) ──────────────────────────────────────────
_INDEX_CACHE: Dict[str, Any] = {}
_INDEX_LOCK = threading.Lock()


def _vault_notes(vault_path: str, use_cache: bool = True) -> List[Dict[str, Any]]:
    """Note del vault con i token precalcolati (cache in memoria, TTL)."""
    import knowledge_curator as kc

    vault = str(Path(vault_path).expanduser().resolve())
    now = time.time()
    with _INDEX_LOCK:
        cached = _INDEX_CACHE.get(vault)
        if use_cache and cached and now - cached[0] < INDEX_TTL_SECONDS:
            return cached[1]

    data = kc.scan_vault(vault)
    notes: List[Dict[str, Any]] = []
    for nota in data["notes"]:
        notes.append({
            "path": nota.get("path", ""),
            "title": nota.get("title", ""),
            "title_tokens": _tokens(nota.get("title", "")),
            "path_tokens": _tokens(nota.get("path", "")),
            "content_tokens": _tokens(nota.get("content", "")),
            "frontmatter": nota.get("frontmatter") or {},
        })
    with _INDEX_LOCK:
        _INDEX_CACHE[vault] = (now, notes)
    return notes


def clear_index_cache() -> None:
    """Svuota l'indice in memoria (utile dopo modifiche al vault)."""
    with _INDEX_LOCK:
        _INDEX_CACHE.clear()


def _is_project_note(note: Dict[str, Any]) -> bool:
    path = str(note.get("path", "")).casefold()
    if any(path.startswith(cartella + "/") for cartella in PROJECT_FOLDERS):
        return True
    if "/projects/" in path:
        return True
    tags = note.get("frontmatter", {}).get("tags")
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(tags, (list, tuple, set)):
        for tag in tags:
            if str(tag).strip("# ").casefold() in {"progetto", "progetti", "project"}:
                return True
    return False


def _score(note: Dict[str, Any], tokens: set, include_content: bool) -> int:
    """Rilevanza: titolo pesa 3, percorso 2, contenuto al massimo 2."""
    if not tokens:
        return 0
    score = 3 * len(tokens & note["title_tokens"])
    score += 2 * len(tokens & note["path_tokens"])
    if include_content:
        score += min(2, len(tokens & note["content_tokens"]))
    return score


# ── Trigger ──────────────────────────────────────────────────────────────────
def _resolve_vault(vault_path: str = "") -> str:
    """Vault esplicito, poi CLIO_VAULT_PATH, poi la config di The Scribe."""
    raw = (vault_path or "").strip() or (os.getenv("CLIO_VAULT_PATH") or "").strip()
    if not raw:
        try:
            from scribe_control import load_config, scribe_config_path
            config = load_config(scribe_config_path())
            raw = str((config.get("obsidian") or {}).get("vault") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raise ValueError(
            "vault non specificato: passa vault_path o imposta CLIO_VAULT_PATH")
    vault = Path(raw).expanduser()
    if not vault.is_dir():
        raise ValueError(f"vault non trovato o non e' una cartella: {vault}")
    return str(vault.resolve())


def check_triggers(app_name: str, vault_path: str,
                   window_title: Optional[str] = None) -> List[str]:
    """Suggerimenti pertinenti per l'app in primo piano.

    * browser  -> note che parlano dell'argomento del titolo della finestra;
    * editor   -> note di progetto collegate al file/progetto aperto;
    * incognito o app non riconosciuta -> lista vuota (nessun suggerimento).
    """
    app = (app_name or "").strip().casefold()
    if not app or (app not in BROWSER_APPS and app not in EDITOR_APPS):
        return []

    if window_title is None:
        corrente = get_foreground_window()
        window_title = (corrente.get("title") or ""
                        if (corrente.get("app") or "").casefold() == app else "")
    if _is_private(window_title):
        return []      # privacy: navigazione privata -> nessun suggerimento

    argomento = _topic(window_title)
    tokens = _tokens(argomento)
    if not tokens:
        return []

    try:
        vault = _resolve_vault(vault_path)
        notes = _vault_notes(vault)
    except Exception as exc:
        logger.warning("Vault non leggibile per i trigger: %s", type(exc).__name__)
        return []

    if app in BROWSER_APPS:
        soglia, prefisso = 3, "Nota sul tema"
        candidati = notes
    else:  # editor: solo note di progetto
        soglia, prefisso = 2, "Nota di progetto"
        candidati = [nota for nota in notes if _is_project_note(nota)]

    punteggi = []
    for nota in candidati:
        score = _score(nota, tokens, include_content=True)
        if score >= soglia:
            punteggi.append((score, nota))
    punteggi.sort(key=lambda coppia: (-coppia[0], coppia[1]["path"]))

    suggerimenti: List[str] = []
    for _score_valore, nota in punteggi[:MAX_SUGGESTIONS]:
        suggerimenti.append(
            f"{prefisso} «{argomento}»: {nota['title']} ({nota['path']})")
    return suggerimenti


# ── Notifiche + rate limit ───────────────────────────────────────────────────
def show_notification(suggestion: str, title: str = "CLIO") -> bool:
    """Notifica desktop discreta via plyer. Non solleva mai eccezioni."""
    message = _clip(suggestion, MAX_NOTIFICATION_CHARS)
    if not message:
        return False
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="CLIO",
                            timeout=8)
        return True
    except Exception as exc:
        # Solo il tipo di errore: mai il contenuto del suggerimento.
        logger.warning("Notifica desktop non disponibile: %s", type(exc).__name__)
        return False


_last_notification: Optional[float] = None
_rate_lock = threading.Lock()


def _clock() -> float:
    return time.monotonic()


def reset_rate_limit() -> None:
    """Azzera il rate limit (solo in memoria; utile nei test)."""
    global _last_notification
    with _rate_lock:
        _last_notification = None


def should_notify(now: Optional[float] = None) -> bool:
    """True se è trascorso il periodo di silenzio (default 30 minuti)."""
    momento = _clock() if now is None else float(now)
    with _rate_lock:
        if _last_notification is None:
            return True
        return (momento - _last_notification) >= RATE_LIMIT_SECONDS


def seconds_until_next_notification(now: Optional[float] = None) -> float:
    """Secondi mancanti prima che una nuova notifica sia ammessa (0 se libero)."""
    momento = _clock() if now is None else float(now)
    with _rate_lock:
        if _last_notification is None:
            return 0.0
        return max(0.0, RATE_LIMIT_SECONDS - (momento - _last_notification))


def notify_if_allowed(suggestion: str,
                      now: Optional[float] = None,
                      notifier: Optional[Any] = None) -> bool:
    """Mostra la notifica solo se il rate limit lo consente. True se inviata.

    Il timestamp viene "prenotato" prima di inviare (evita raffiche da thread
    concorrenti) e ripristinato se l'invio fallisce.
    """
    global _last_notification
    if not (suggestion or "").strip():
        return False
    notifier = notifier or show_notification
    momento = _clock() if now is None else float(now)

    with _rate_lock:
        if _last_notification is not None and \
                (momento - _last_notification) < RATE_LIMIT_SECONDS:
            return False
        precedente = _last_notification
        _last_notification = momento

    try:
        inviata = bool(notifier(suggestion))
    except Exception as exc:
        logger.warning("Invio notifica fallito: %s", type(exc).__name__)
        inviata = False
    if not inviata:
        with _rate_lock:
            _last_notification = precedente
    return inviata


# ── Monitor in background ────────────────────────────────────────────────────
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()
_monitor_lock = threading.Lock()


def monitor_tick(vault_path: str = "",
                 window: Optional[Dict[str, str]] = None,
                 notifier: Optional[Any] = None) -> Dict[str, Any]:
    """Un singolo controllo: suggerimenti trovati e notifica (rate-limited)."""
    finestra = window if window is not None else get_foreground_window()
    app = (finestra.get("app") or "").strip()
    titolo = finestra.get("title") or ""
    suggerimenti = check_triggers(app, vault_path, titolo)
    notificata = False
    if suggerimenti:
        notificata = notify_if_allowed(suggerimenti[0], notifier=notifier)
    return {"app": app, "suggestions": suggerimenti, "notified": notificata}


def _monitor_loop(vault_path: str, interval: float) -> None:
    """Ciclo di monitoraggio: nessun log di app o titolo (privacy)."""
    while not _monitor_stop.wait(interval):
        try:
            monitor_tick(vault_path)
        except Exception as exc:  # il monitor non deve mai morire
            logger.warning("Ciclo proattivo fallito: %s", type(exc).__name__)


def attiva_proattivita(vault_path: str = "",
                       interval_seconds: float = DEFAULT_INTERVAL_SECONDS) -> str:
    """Azione CLIO: avvia (una sola volta) il monitor in background."""
    global _monitor_thread
    try:
        vault = _resolve_vault(vault_path)
    except Exception as exc:
        return f"ERRORE: proattività non avviata: {exc}"
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError):
        return "ERRORE: interval_seconds non valido (atteso un numero di secondi)"
    if interval < 1:
        return "ERRORE: interval_seconds deve essere almeno 1 secondo"

    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return "Proattività già attiva: nessun secondo monitor avviato."
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_monitor_loop, args=(str(vault), interval),
            name="clio-proattivita", daemon=True)
        _monitor_thread.start()

    return (f"Proattività attiva su {vault}: controllo della finestra ogni "
            f"{int(interval)}s, massimo 1 notifica ogni "
            f"{RATE_LIMIT_SECONDS // 60} minuti. Privacy: nessun log di app o "
            "titoli, nessun suggerimento in navigazione privata.")


def proattivita_attiva() -> bool:
    """True se il thread di monitoraggio è in esecuzione."""
    with _monitor_lock:
        return _monitor_thread is not None and _monitor_thread.is_alive()


def ferma_proattivita(timeout: float = 2.0) -> str:
    """Ferma il monitor in background (idempotente)."""
    global _monitor_thread
    with _monitor_lock:
        thread = _monitor_thread
        if thread is None or not thread.is_alive():
            _monitor_thread = None
            return "Proattività non attiva."
        _monitor_stop.set()

    thread.join(timeout)

    with _monitor_lock:
        if thread.is_alive():
            return "Proattività: stop richiesto, il thread termina al prossimo ciclo."
        _monitor_thread = None
        _monitor_stop.clear()
    return "Proattività fermata."
