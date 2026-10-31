"""computer_agent.py — R6: Computer Use cross-app in sola UI Automation.

Permette a CLIO di guardare e azionare le finestre di applicazioni desktop
*esplicitamente consentite*: leggere i controlli visibili, cliccare un elemento
e digitare testo. Nessuna cattura schermo, nessun video, nessun OCR: solo
l'albero dei controlli esposto da UI Automation (pywinauto, backend "uia").

Sicurezza (difesa in profondita'):
  1. whitelist di eseguibili (`DEFAULT_ALLOWED_APPS`, override con
     CLIO_COMPUTER_ALLOWED_APPS="notepad.exe,Code.exe");
  2. le azioni sono classificate in policy.py: `ispeziona_finestra` e'
     SENSITIVE, `clicca_elemento`/`scrivi_testo_app` sono IRREVERSIBLE. Ogni
     azione passa da `policy.guarded_call`, che richiede sempre conferma;
  3. click solo su un elemento UNIVOCO: se il nome e' ambiguo non clicca;
  4. digitazione senza caratteri di controllo (niente Invio/Tab impliciti) e
     con lunghezza massima;
  5. ogni errore (app non avviata, elemento assente, pywinauto mancante,
     finestra non raggiungibile) torna come messaggio pulito / False: nessuna
     eccezione raggiunge CLIO.

Contratto pubblico (usato dal registro AZIONI_MAP e dal LLM):
    inspect_window("notepad.exe")            -> str
    click_element("notepad.exe", "File")     -> bool
    type_text("notepad.exe", "Ciao Clio")    -> bool

Ricerca della finestra (robusta su Windows 11 / app UWP):
  1. processi psutil con nome eseguibile (o stem, senza .exe);
  2. se psutil non trova nulla o il processo non ha una finestra UIA,
     fallback per titolo con pywinauto (`connect(title_re=...)`) usando gli
     hint per app (es. "Blocco Note|Notepad", "Calcolatrice|Calculator").
La whitelist accetta alias: "notepad", "calculator"/"calcolatrice" -> "calc.exe",
"vscode" -> "code.exe".

Il modulo non importa pywinauto/psutil all'import: gli import avvengono dentro
`_backend()` / `_candidate_pids()`, cosi' i test girano con mock e CLIO parte
anche su una macchina senza pywinauto.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ClioComputer")

# ── Whitelist ──────────────────────────────────────────────────────────────────
DEFAULT_ALLOWED_APPS = ("code.exe", "notepad.exe", "antigravity.exe")
ENV_ALLOWED_APPS = "CLIO_COMPUTER_ALLOWED_APPS"

# ── Limiti (anti-abuso / anti-blocco) ─────────────────────────────────────────
MAX_TEXT_LENGTH = 5000          # caratteri massimi digitabili in una azione
MAX_ELEMENT_NAME = 200          # lunghezza massima del nome elemento da cercare
MAX_CONTROLS = 150              # controlli riportati da inspect_window
MAX_SCAN = 1500                 # controlli esaminati per trovare un elemento
INSPECT_MAX_CHARS = 6000        # limite del testo restituito al LLM
CONNECT_TIMEOUT = 5             # secondi per connettersi alla finestra

try:  # redazione dei secret, se il modulo di logging avanzato e' disponibile
    from logging_setup import _redact as _redact_secrets
except Exception:  # pragma: no cover - dipende dall'ambiente
    def _redact_secrets(text):
        return text


class ComputerUseError(RuntimeError):
    """Errore prevedibile di Computer Use (messaggio gia' pronto per l'utente)."""


# ── Whitelist ──────────────────────────────────────────────────────────────────
# Alias accettati in ingresso (e in CLIO_COMPUTER_ALLOWED_APPS): il nome
# canonico resta l'eseguibile usato per psutil e per la policy.
APP_ALIASES = {
    "notepad": "notepad.exe",
    "notepad.exe": "notepad.exe",
    "windowsapps.notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calculator.exe": "calc.exe",
    "calcolatrice": "calc.exe",
    "calcolatrice.exe": "calc.exe",
    "calc": "calc.exe",
    "calc.exe": "calc.exe",
    "vscode": "code.exe",
    "visual studio code": "code.exe",
    "code": "code.exe",
    "antigravity": "antigravity.exe",
}

# Hint sul titolo della finestra per il fallback UIA (Windows 11 / app UWP).
# I pattern sono regex case-insensitive compilabili da pywinauto.
APP_TITLE_HINTS = {
    "notepad.exe": (r"(?i).*Blocco Note.*", r"(?i).*Notepad.*"),
    "calc.exe": (r"(?i).*Calcolatrice.*", r"(?i).*Calculator.*"),
    "code.exe": (r"(?i).*Visual Studio Code.*",),
    "antigravity.exe": (r"(?i).*Antigravity.*",),
}


def normalize_app_name(app_name: str) -> str:
    """Normalizza un eseguibile: basename, minuscolo, alias noti, estensione .exe.

    Accetta "Notepad", "notepad.EXE", "C:/Windows/System32/notepad.exe",
    "calculator"/"calcolatrice" (-> calc.exe), "vscode" (-> code.exe).
    """
    if not isinstance(app_name, str) or not app_name.strip():
        raise ComputerUseError(
            "app_name mancante: indica l'eseguibile consentito (es. notepad.exe)")
    name = Path(app_name.strip().strip('"').strip("'")).name.lower()
    if not name:
        raise ComputerUseError(f"app_name non valido: {app_name!r}")
    if name in APP_ALIASES:
        return APP_ALIASES[name]
    if "." not in name:
        name += ".exe"
    return APP_ALIASES.get(name, name)


def _title_patterns(exe_name: str) -> List[str]:
    """Regex dei titoli da provare nel fallback UIA, specifici + generico."""
    patterns = list(APP_TITLE_HINTS.get(exe_name, ()))
    stem = Path(exe_name).stem
    generico = r"(?i).*" + re.escape(stem) + r".*"
    if generico not in patterns:
        patterns.append(generico)
    return patterns


def allowed_apps() -> frozenset[str]:
    """Whitelist effettiva: env CLIO_COMPUTER_ALLOWED_APPS o default sicuro."""
    raw = (os.getenv(ENV_ALLOWED_APPS) or "").strip()
    source = raw.split(",") if raw else list(DEFAULT_ALLOWED_APPS)
    out = set()
    for item in source:
        try:
            out.add(normalize_app_name(item))
        except ComputerUseError:
            continue
    return frozenset(out)


def is_allowed_app(app_name: str) -> bool:
    """True solo se l'eseguibile e' in whitelist. Non solleva mai."""
    try:
        return normalize_app_name(app_name) in allowed_apps()
    except ComputerUseError:
        return False


def _require_allowed(app_name: str) -> str:
    """Valida l'app contro la whitelist e restituisce il nome normalizzato."""
    name = normalize_app_name(app_name)
    if name not in allowed_apps():
        consentite = ", ".join(sorted(allowed_apps())) or "(nessuna)"
        raise ComputerUseError(
            f"app non consentita: {name}. Whitelist attuale: {consentite}")
    return name


# ── Backend pywinauto (import pigro e sostituibile nei test) ──────────────────
def _backend():
    """Ritorna pywinauto.Application; errore pulito se non disponibile."""
    try:
        from pywinauto import Application
    except Exception as exc:  # ImportError o dipendenze Windows mancanti
        raise ComputerUseError(
            "pywinauto non disponibile: installa pywinauto (pip install pywinauto) "
            f"su Windows. Dettaglio: {type(exc).__name__}: {exc}") from exc
    return Application


def _candidate_pids(exe_name: str) -> List[int]:
    """PID dei processi con quell'eseguibile, dal piu' recente al piu' vecchio.

    Tollerante: confronta anche lo stem senza ".exe" (alcune build riportano
    "Notepad") e non solleva se psutil manca o non trova nulla: in quel caso
    `_open_window` prova il fallback per titolo.
    """
    try:
        import psutil
    except Exception as exc:
        logger.warning("psutil non disponibile (%s): fallback per titolo", exc)
        return []

    wanted = {exe_name.lower(), Path(exe_name).stem.lower()}
    found: List[tuple] = []
    try:
        for proc in psutil.process_iter(["name", "create_time"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in wanted:
                    found.append((proc.info.get("create_time") or 0, proc.pid))
            except Exception:
                continue
    except Exception as exc:  # psutil instabile: meglio tentare il titolo
        logger.warning("psutil.process_iter fallito (%s): fallback per titolo", exc)
        return []
    found.sort(reverse=True)
    return [pid for _created, pid in found]


def _resolve_window(app, pattern: Optional[str] = None):
    """Finestra del processo connesso: per titolo se indicato, poi top_window."""
    errori: List[str] = []
    if pattern:
        try:
            candidate = app.window(title_re=pattern)
            if candidate.exists(timeout=CONNECT_TIMEOUT):
                return candidate
        except Exception as exc:
            errori.append(f"window(title_re): {type(exc).__name__}: {exc}")
    try:
        candidate = app.top_window()
        if candidate.exists(timeout=CONNECT_TIMEOUT):
            return candidate
    except Exception as exc:
        errori.append(f"top_window: {type(exc).__name__}: {exc}")
    raise ComputerUseError(
        "nessuna finestra corrispondente" + (f" ({'; '.join(errori[-2:])})" if errori else ""))


def _open_window(app_name: str) -> "_WindowAdapter":
    """Connette all'app consentita e restituisce l'adapter della finestra.

    Prima prova i PID trovati da psutil (alcuni eseguibili hanno processi figli
    senza finestra), poi ripiega sul titolo della finestra: e' il percorso
    affidabile per le app UWP di Windows 11 (Blocco Note/Calcolatrice), dove il
    nome del processo puo' non corrispondere all'eseguibile atteso.
    Nessuna cattura schermo.
    """
    exe = _require_allowed(app_name)
    Application = _backend()
    errori: List[str] = []

    for pid in _candidate_pids(exe):
        try:
            app = Application(backend="uia").connect(process=pid, timeout=CONNECT_TIMEOUT)
            return _WindowAdapter(_resolve_window(app), exe)
        except Exception as exc:
            errori.append(f"pid {pid}: {type(exc).__name__}: {exc}")

    # Fallback per titolo: gestisce UWP, alias e processi invisibili a psutil.
    for pattern in _title_patterns(exe):
        try:
            app = Application(backend="uia").connect(
                title_re=pattern, timeout=CONNECT_TIMEOUT)
            return _WindowAdapter(_resolve_window(app, pattern), exe)
        except Exception as exc:
            errori.append(f"titolo {pattern!r}: {type(exc).__name__}: {exc}")

    dettaglio = "; ".join(errori[-4:]) if errori else "nessun processo utilizzabile"
    raise ComputerUseError(
        f"finestra di {exe} non raggiungibile: avvia l'app o verifica il titolo "
        f"({dettaglio})")


# ── Adapter della finestra (unico punto che parla con pywinauto) ──────────────
# Caratteri riservati dal parser di type_keys: vanno inviati tra graffe.
_TYPE_KEY_ESCAPES = {
    "{": "{{}", "}": "{}}", "+": "{+}", "^": "{^}", "%": "{%}",
    "~": "{~}", "(": "{(}", ")": "{)}", "[": "{[}", "]": "{]}",
}


def _escape_for_keys(text: str) -> str:
    """Neutralizza i metacaratteri di pywinauto.type_keys."""
    return "".join(_TYPE_KEY_ESCAPES.get(ch, ch) for ch in text)


def _clean_name(value: Any, limit: int = MAX_ELEMENT_NAME) -> str:
    """Testo di un controllo: stringa, redatta e troncata."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = str(_redact_secrets(text))
    return text[:limit]


class _WindowAdapter:
    """Wrapper minimale su una WindowSpecification pywinauto."""

    def __init__(self, window, app_name: str):
        self._window = window
        self.app_name = app_name

    # ── lettura ──
    def describe(self) -> Dict[str, Any]:
        """Snapshot dei controlli con testo visibile (nessuna immagine)."""
        try:
            title = _clean_name(self._window.window_text(), 300)
        except Exception:
            title = ""
        controls: List[Dict[str, str]] = []
        seen = set()
        truncated = False
        scanned = 0
        try:
            for element in self._window.descendants():
                scanned += 1
                if scanned > MAX_SCAN:
                    truncated = True
                    break
                name = _clean_name(getattr(getattr(element, "element_info", None), "name", ""))
                if not name:
                    continue
                info = getattr(element, "element_info", None)
                control_type = _clean_name(getattr(info, "control_type", ""), 60)
                automation_id = _clean_name(getattr(info, "automation_id", ""), 120)
                key = (name.casefold(), control_type, automation_id)
                if key in seen:
                    continue
                seen.add(key)
                if len(controls) >= MAX_CONTROLS:
                    truncated = True
                    break
                controls.append({"name": name, "control_type": control_type,
                                 "automation_id": automation_id})
        except Exception as exc:
            raise ComputerUseError(
                f"lettura dei controlli di {self.app_name} fallita: "
                f"{type(exc).__name__}: {exc}") from exc
        return {"app": self.app_name, "title": title,
                "controls": controls, "truncated": truncated}

    # ── ricerca elemento ──
    def _matches(self, element_name: str) -> List[Any]:
        wanted = element_name.strip().casefold()
        matches: List[Any] = []
        scanned = 0
        try:
            for element in self._window.descendants():
                scanned += 1
                if scanned > MAX_SCAN:
                    break
                info = getattr(element, "element_info", None)
                name = _clean_name(getattr(info, "name", ""))
                auto_id = _clean_name(getattr(info, "automation_id", ""))
                if (name and name.casefold() == wanted) or (auto_id and auto_id.casefold() == wanted):
                    matches.append(element)
                    if len(matches) > 1:
                        break  # basta sapere che e' ambiguo
        except Exception as exc:
            raise ComputerUseError(
                f"ricerca di '{element_name}' in {self.app_name} fallita: "
                f"{type(exc).__name__}: {exc}") from exc
        return matches

    # ── azioni ──
    def click_named(self, element_name: str) -> None:
        """Clicca l'unico controllo con quel nome; ambiguo o assente -> errore."""
        matches = self._matches(element_name)
        if not matches:
            raise ComputerUseError(
                f"elemento '{element_name}' non trovato in {self.app_name}")
        if len(matches) > 1:
            raise ComputerUseError(
                f"elemento '{element_name}' ambiguo in {self.app_name}: "
                f"{len(matches)}+ controlli corrispondono, nessun click eseguito")
        try:
            self._window.set_focus()
        except Exception as exc:  # il focus e' best-effort, non blocca il click
            logger.warning("Focus non forzato su %s: %s: %s",
                           self.app_name, type(exc).__name__, exc)
        try:
            matches[0].click_input()
        except Exception as exc:
            raise ComputerUseError(
                f"click su '{element_name}' in {self.app_name} fallito: "
                f"{type(exc).__name__}: {exc}") from exc

    def type_text(self, text: str) -> None:
        """Digita il testo nella finestra, senza tasti speciali impliciti."""
        try:
            self._window.set_focus()
        except Exception as exc:  # il focus e' best-effort
            logger.warning("Focus non forzato su %s: %s: %s",
                           self.app_name, type(exc).__name__, exc)
        try:
            self._window.type_keys(_escape_for_keys(text), with_spaces=True)
        except Exception as exc:
            raise ComputerUseError(
                f"digitazione in {self.app_name} fallita: "
                f"{type(exc).__name__}: {exc}") from exc


# ── Validazione input ─────────────────────────────────────────────────────────
def _validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ComputerUseError("testo mancante: niente da digitare")
    if len(text) > MAX_TEXT_LENGTH:
        raise ComputerUseError(
            f"testo troppo lungo ({len(text)} caratteri, massimo {MAX_TEXT_LENGTH})")
    # Niente Invio/Tab/controlli: evita invii o azioni implicite nell'app.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ComputerUseError(
            "il testo contiene caratteri di controllo (Invio/Tab): "
            "non vengono digitati per sicurezza")
    return text


def _validate_element_name(element_name: str) -> str:
    if not isinstance(element_name, str) or not element_name.strip():
        raise ComputerUseError("element_name mancante: indica il nome del controllo")
    if len(element_name) > MAX_ELEMENT_NAME:
        raise ComputerUseError(
            f"element_name troppo lungo ({len(element_name)} caratteri, "
            f"massimo {MAX_ELEMENT_NAME})")
    return element_name.strip()


# ── Operazioni interne (sollevano ComputerUseError) ───────────────────────────
def _inspect(app_name: str) -> Dict[str, Any]:
    _require_allowed(app_name)
    return _open_window(app_name).describe()


def _click(app_name: str, element_name: str) -> Dict[str, Any]:
    _require_allowed(app_name)
    name = _validate_element_name(element_name)
    window = _open_window(app_name)
    window.click_named(name)
    return {"app": window.app_name, "element": name, "clicked": True}


def _type(app_name: str, text: str) -> Dict[str, Any]:
    _require_allowed(app_name)
    validated = _validate_text(text)
    window = _open_window(app_name)
    window.type_text(validated)
    return {"app": window.app_name, "characters": len(validated), "typed": True}


# ── Rendering leggibile per il LLM ────────────────────────────────────────────
def _render_inspection(data: Dict[str, Any]) -> str:
    title = data.get("title") or "(senza titolo)"
    righe = [f"App: {data.get('app')}", f"Finestra: {title}"]
    controls = data.get("controls") or []
    if controls:
        suffisso = "+" if data.get("truncated") else ""
        righe.append(f"Controlli con testo ({len(controls)}{suffisso}):")
        for control in controls:
            tipo = f" [{control['control_type']}]" if control.get("control_type") else ""
            auto_id = f" #{control['automation_id']}" if control.get("automation_id") else ""
            righe.append(f"- {control['name']}{tipo}{auto_id}")
    else:
        righe.append("Nessun controllo con testo visibile.")
    if data.get("truncated"):
        righe.append("... elenco troncato (limite controlli raggiunto).")
    testo = "\n".join(righe)
    if len(testo) > INSPECT_MAX_CHARS:
        testo = testo[:INSPECT_MAX_CHARS] + "\n..."
    return testo


def _errore_pulito(azione: str, exc: BaseException) -> str:
    """Messaggio uniforme, senza traceback, per l'utente e per il LLM."""
    logger.warning("Computer Use '%s' fallita: %s: %s", azione, type(exc).__name__, exc)
    return f"ERRORE: {azione} non eseguita: {exc}"


# ── API pubblica (contratto del task) ─────────────────────────────────────────
def inspect_window(app_name: str) -> str:
    """Legge i controlli visibili dell'app consentita.

    Ritorna testo leggibile (titolo + controlli con nome/tipo/id) oppure una
    stringa che inizia con "ERRORE:" in caso di app non consentita, non
    avviata, pywinauto mancante o finestra non raggiungibile. Non solleva mai.
    """
    try:
        return _render_inspection(_inspect(app_name))
    except ComputerUseError as exc:
        return _errore_pulito("ispezione finestra", exc)
    except Exception as exc:  # pragma: no cover - difesa finale
        return _errore_pulito("ispezione finestra", exc)


def click_element(app_name: str, element_name: str) -> bool:
    """Clicca l'elemento con quel nome. False (senza crash) se non possibile."""
    try:
        _click(app_name, element_name)
        return True
    except ComputerUseError as exc:
        _errore_pulito("click elemento", exc)
        return False
    except Exception as exc:  # pragma: no cover - difesa finale
        _errore_pulito("click elemento", exc)
        return False


def type_text(app_name: str, text: str) -> bool:
    """Digita il testo nell'app. False (senza crash) se non possibile."""
    try:
        _type(app_name, text)
        return True
    except ComputerUseError as exc:
        _errore_pulito("digitazione testo", exc)
        return False
    except Exception as exc:  # pragma: no cover - difesa finale
        _errore_pulito("digitazione testo", exc)
        return False


def describe_computer_agent() -> str:
    """Riepilogo testuale di whitelist e limiti (diagnostica / voce)."""
    return (
        "Computer Use cross-app (UI Automation):\n"
        f"- app consentite: {', '.join(sorted(allowed_apps())) or '(nessuna)'}\n"
        f"- testo massimo: {MAX_TEXT_LENGTH} caratteri, senza Invio/Tab\n"
        f"- controlli per ispezione: {MAX_CONTROLS}\n"
        "- richiede sempre conferma (policy: SENSITIVE/IRREVERSIBLE)"
    )
