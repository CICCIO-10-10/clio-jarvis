"""R1 — Memoria come servizio: CLIO legge le sessioni di The Scribe.

Interfaccia unica di CLIO verso ScribeBridge (the_scribe.bridge.clio_adapter).
Lettura: apre il database di The Scribe, interroga il bridge, chiude la
connessione. Non avvia The Scribe e non finalizza sessioni. L'unica scrittura
ammessa passa dall'API del bridge (`record_observation`), che scrive la nota di
conoscenza nel vault; se l'API non è disponibile l'osservazione resta
nell'outbox locale di CLIO.

Limite noto (documentato da The Scribe): il bridge condivide il database, non la
memoria del processo. Alla costruzione del manager i metadati dei progetti della
config vengono riallineati nel DB (upsert idempotente): nessuna sessione, evento
o nota viene modificata.

Configurazione:
  CLIO_SCRIBE_CONFIG  percorso del daemon.yaml (default: <repo the_scribe>/config/daemon.yaml)
  CLIO_SCRIBE_ROOT    radice del repo di The Scribe (default: accanto a CLIO)
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("ClioScribe")

# ── Default: repo di The Scribe accanto a questo progetto ──────────────────────
_DEFAULT_SCRIBE_ROOT = Path(__file__).resolve().parent.parent / "the_scribe"
_DEFAULT_CONFIG = _DEFAULT_SCRIBE_ROOT / "config" / "daemon.yaml"

_INSTALL_HINT = (
    'Installa con: python -m pip install -e "C:/Users/User/Desktop/PROGETTI/the_scribe"'
)

# Import protetto: CLIO deve partire anche se The Scribe non è installato.
try:  # pragma: no cover - dipende dall'ambiente
    from the_scribe.bridge.clio_adapter import ScribeBridge
    from the_scribe.bridge.session_api import BridgeSessionManager
    from the_scribe.config import ConfigError, load_config
    from the_scribe.store import EventStore

    SCRIBE_AVAILABLE = True
    _IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # pragma: no cover - dipende dall'ambiente
    ScribeBridge = None  # type: ignore[assignment]
    BridgeSessionManager = None  # type: ignore[assignment]
    EventStore = None  # type: ignore[assignment]

    class ConfigError(Exception):  # type: ignore[no-redef]
        """Fallback quando The Scribe non è importabile."""

    def load_config(path):  # type: ignore[misc]
        raise RuntimeError("The Scribe non è installato")

    SCRIBE_AVAILABLE = False
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"
    logger.warning("The Scribe non importabile: %s", _IMPORT_ERROR)


class ScribeOffline(RuntimeError):
    """The Scribe non è raggiungibile (pacchetto, config o database mancanti)."""


# ── Percorsi e connessione ─────────────────────────────────────────────────────
def scribe_config_path(config_path: Optional[Path | str] = None) -> Path:
    """Percorso effettivo del daemon.yaml (parametro > env > default)."""
    if config_path:
        return Path(config_path).expanduser()
    env_cfg = os.getenv("CLIO_SCRIBE_CONFIG")
    if env_cfg:
        return Path(env_cfg).expanduser()
    return _DEFAULT_CONFIG


@contextlib.contextmanager
def _open_bridge(
    config_path: Optional[Path | str] = None,
) -> Iterator[Tuple[Any, Dict[str, Any]]]:
    """Apre config + DB + bridge e chiude SEMPRE la connessione.

    Non usa ``with EventStore(...)`` di proposito: quel costruttore esegue il DDL
    di inizializzazione. Qui il DB deve esistere già (lo crea il daemon) e viene
    aperto senza toccare lo schema.
    """
    if not SCRIBE_AVAILABLE:
        raise ScribeOffline(
            f"The Scribe non è installato nell'ambiente di CLIO ({_IMPORT_ERROR}). {_INSTALL_HINT}"
        )

    cfg_file = scribe_config_path(config_path)
    if not cfg_file.is_file():
        raise ScribeOffline(f"The Scribe è offline: configurazione non trovata in {cfg_file}.")

    try:
        config = load_config(cfg_file)
    except ConfigError as exc:
        raise ScribeOffline(f"The Scribe è offline: configurazione non valida ({exc}).") from exc

    db_path = Path(config["app"]["state_dir"]) / "events.db"
    if not db_path.is_file():
        raise ScribeOffline(f"The Scribe è offline: database sessioni non trovato in {db_path}.")

    store = EventStore(db_path)
    try:
        yield ScribeBridge(BridgeSessionManager(store, config)), config
    finally:
        store.close()


def _available_projects(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Progetti abilitati nella configurazione di The Scribe."""
    out = []
    for project in config.get("projects", []) or []:
        if isinstance(project, dict) and project.get("id") and project.get("enabled", True):
            out.append(project)
    return out


def _normalize(text: str) -> str:
    """Normalizza per confronto: solo lettere/cifre, minuscole."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def resolve_project_id(project_id: str, config: Dict[str, Any]) -> str:
    """Trova l'id reale del progetto tollerando spazi, trattini e maiuscole.

    Esempi: "Roast Room" -> "roast-room", "The Scribe" -> "the_scribe",
    "tyrion clips studio" -> "tyrion-clips-studio".
    """
    raw = (project_id or "").strip()
    if not raw:
        raise ValueError(
            "Specifica il progetto. Disponibili: "
            + ", ".join(p["id"] for p in _available_projects(config))
        )

    projects = _available_projects(config)
    by_id = {p["id"]: p["id"] for p in projects}
    if raw in by_id:
        return raw

    wanted = _normalize(raw)
    for project in projects:
        pid = project["id"]
        candidates = {_normalize(pid), _normalize(str(project.get("nome") or ""))}
        if wanted in candidates:
            return pid

    raise ValueError(
        f"Progetto '{raw}' non configurato in The Scribe. Disponibili: "
        + ", ".join(p["id"] for p in projects)
    )


# ── Formattazione ──────────────────────────────────────────────────────────────
def _fmt_ts(value: Optional[str]) -> str:
    """ISO -> 'oggi alle HH:MM' oppure 'il DD/MM alle HH:MM'; fallback grezzo."""
    if not value:
        return "data sconosciuta"
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return str(value)
    if parsed.date() == datetime.now().date():
        return f"oggi alle {parsed.strftime('%H:%M')}"
    return f"il {parsed.strftime('%d/%m')} alle {parsed.strftime('%H:%M')}"


def _format_status(rows: List[Dict[str, Any]]) -> str:
    """Righe di get_status() -> frase leggibile per la risposta di CLIO."""
    if not rows:
        return "The Scribe non ha ancora registrato sessioni."

    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        pid = str(row.get("project_id") or "?")
        key = (str(row.get("started_at") or ""), str(row.get("id") or ""))
        best = latest.get(pid)
        best_key = (str(best.get("started_at") or ""), str(best.get("id") or "")) if best else None
        if best is None or key >= best_key:
            latest[pid] = row

    # Le sessioni attive vengono prima: sono quelle che servono al Capo.
    ordered = sorted(
        latest.items(),
        key=lambda item: (str(item[1].get("status") or "").upper() == "CLOSED", str(item[0])),
    )

    righe: List[str] = []
    attive = 0
    for pid, row in ordered:
        status = str(row.get("status") or "").upper()
        if status == "CLOSED":
            righe.append(f"- {pid}: ultima sessione chiusa {_fmt_ts(row.get('ended_at') or row.get('last_activity_at'))}.")
        else:
            attive += 1
            righe.append(
                f"- {pid}: sessione attiva, iniziata {_fmt_ts(row.get('started_at'))}"
                f" (ultima attività {_fmt_ts(row.get('last_activity_at'))})."
            )

    intestazione = (
        f"The Scribe: 1 sessione attiva su {len(latest)} progetti."
        if attive == 1
        else f"The Scribe: {attive} sessioni attive su {len(latest)} progetti."
    )
    return intestazione + "\n" + "\n".join(righe)


# ── API pubblica (usata da skills.py / registry.py / jarvis_brain.py) ──────────
def get_scribe_resume(project_id: str) -> str:
    """Restituisce l'ultimo Resume Markdown di un progetto, o un messaggio chiaro."""
    try:
        with _open_bridge() as (bridge, config):
            try:
                pid = resolve_project_id(project_id, config)
            except ValueError as exc:
                return f"Capo, {exc}."
            resume = bridge.get_resume(pid)
    except ScribeOffline as exc:
        logger.warning("Resume non disponibile: %s", exc)
        return f"Capo, {exc}"
    except Exception as exc:  # sqlite, I/O, marker danneggiati: non fingere successo
        logger.error("Errore lettura resume per %r: %s", project_id, exc)
        return f"Capo, errore leggendo il resume di '{project_id}': {type(exc).__name__}: {exc}"

    if resume is None:
        return f"Nessun resume trovato per il progetto {pid}."
    return resume


def get_scribe_status() -> str:
    """Stato leggibile delle sessioni registrate da The Scribe."""
    try:
        with _open_bridge() as (bridge, _config):
            rows = bridge.get_status()
    except ScribeOffline as exc:
        logger.warning("Stato non disponibile: %s", exc)
        return f"Capo, {exc}"
    except Exception as exc:
        logger.error("Errore lettura stato The Scribe: %s", exc)
        return f"Capo, errore leggendo lo stato di The Scribe: {type(exc).__name__}: {exc}"

    return _format_status(rows)


# ── R5: accesso strutturato per l'Agent Registry / Workflow Engine ─────────────
# Le funzioni qui sopra restituiscono FRASI pronte per la voce (compatibilità R1);
# il Workflow Engine ha invece bisogno dei dati grezzi e di sapere se The Scribe
# è andato offline, senza dover interpretare il testo.
def read_resume(project_id: str) -> Dict[str, Any]:
    """Resume strutturato: `{available, project_id, resume, offline, error}`.

    Non solleva mai: un problema diventa `available=False` + `error`.
    """
    out: Dict[str, Any] = {
        "available": False,
        "project_id": project_id,
        "resume": None,
        "offline": False,
        "error": None,
    }
    try:
        with _open_bridge() as (bridge, config):
            try:
                pid = resolve_project_id(project_id, config)
            except ValueError as exc:
                out["error"] = str(exc)
                return out
            resume = bridge.get_resume(pid)
    except ScribeOffline as exc:
        out["offline"] = True
        out["error"] = str(exc)
        return out
    except Exception as exc:  # sqlite, I/O, marker danneggiati
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["project_id"] = pid
    out["resume"] = resume
    out["available"] = resume is not None
    return out


# Il ScribeBridge installato (R1) è di sola lettura: `record_observation` potrebbe
# esistere in versioni future. Lo si rileva SULLA CLASSE, senza aprire il DB.
_BRIDGE_HAS_OBSERVATION = bool(
    SCRIBE_AVAILABLE and callable(getattr(ScribeBridge, "record_observation", None))
)

OUTBOX_ENV = "CLIO_SCRIBE_OUTBOX"


def scribe_outbox_dir() -> Path:
    """Cartella (di CLIO) dove finiscono le osservazioni non scritte nel vault."""
    env = (os.getenv(OUTBOX_ENV) or "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent / "data" / "scribe_outbox"


def _slug(text: str, limit: int = 40) -> str:
    clean = _normalize(text)[:limit]
    return clean or "osservazione"


def record_observation(
    project_id: str,
    observation: str,
    workflow_id: Optional[str] = None,
    category: str = "nota",
) -> Dict[str, Any]:
    """Registra un'osservazione di workflow destinata a The Scribe.

    Ordine:
      1. se il ScribeBridge installato espone `record_observation`, si usa quello
         (API pubblica del bridge: nessun accesso diretto al DB o al vault);
      2. altrimenti l'osservazione viene scritta nell'OUTBOX locale di CLIO
         (`data/scribe_outbox/*.md`). CLIO non tocca mai il vault direttamente:
         la nota viene scritta da The Scribe (bridge) o assorbito nella sua
         sessione (outbox, decisione R1).

    `category` (C3) etichetta l'osservazione nella nota: "decisione",
    "completamento", "nota" (default). È retro-compatibile: i chiamanti
    precedenti non la passano e restano su "nota".

    Ritorna `{ok, recorded, project, path, error, bridge}`. Non solleva mai,
    tranne ValueError se l'osservazione è vuota. `path` è la nota scritta dal
    bridge o, in fallback, il file dell'outbox.
    """
    testo = (observation or "").strip()
    if not testo:
        raise ValueError("osservazione vuota: niente da documentare")

    project = (project_id or "").strip()
    etichetta = (category or "nota").strip().lower() or "nota"

    # 1) API del bridge, se disponibile.
    if _BRIDGE_HAS_OBSERVATION:
        try:
            with _open_bridge() as (bridge, config):
                pid = resolve_project_id(project, config) if project else ""
                if pid:
                    try:
                        note_path = bridge.record_observation(pid, testo, category=etichetta)  # type: ignore[attr-defined]
                    except TypeError:
                        # Versioni del bridge senza `category`: si resta compatibili.
                        note_path = bridge.record_observation(pid, testo)  # type: ignore[attr-defined]
                    return {"ok": True, "recorded": "scribe", "project": pid,
                            "path": str(note_path) if note_path else None,
                            "error": None, "bridge": True, "category": etichetta}
        except ScribeOffline as exc:
            logger.warning("Bridge non disponibile per l'osservazione: %s", exc)
        except Exception as exc:  # pragma: no cover - dipende dalla versione del bridge
            logger.warning("record_observation del bridge fallita: %s", exc)

    # 2) Outbox locale di CLIO.
    try:
        outbox = scribe_outbox_dir()
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{_slug(project or 'workflow')}-{_slug(workflow_id or 'wf', 12)}.md"
        target = outbox / name
        target.write_text(
            f"# Osservazione per The Scribe\n\n"
            f"- progetto: {project or '(non specificato)'}\n"
            f"- workflow: {workflow_id or '(non specificato)'}\n"
            f"- categoria: {etichetta}\n"
            f"- registrata: {datetime.now().isoformat(timespec='seconds')}\n"
            f"- origine: CLIO Workflow Engine (R5)\n\n"
            f"{testo}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return {"ok": False, "recorded": None, "project": project,
                "path": None, "error": f"{type(exc).__name__}: {exc}", "bridge": False,
                "category": etichetta}

    return {"ok": True, "recorded": "outbox", "project": project,
            "path": str(target), "error": None, "bridge": False, "category": etichetta}


# ── Alias azioni CLIO (italiano) ───────────────────────────────────────────────
# I nomi inglesi restano disponibili come funzioni Python; questi sono i nomi
# esposti al LLM e registrati in policy.READ_ONLY.
def recupera_resume_progetto(project_id: str = "", progetto: str = "") -> str:
    """Azione CLIO: 'Dove ero rimasto su <progetto>'."""
    return get_scribe_resume(project_id or progetto)


def stato_sessioni_scribe() -> str:
    """Azione CLIO: stato delle sessioni di The Scribe."""
    return get_scribe_status()


# ── Routing deterministico (intento esplicito, prima del LLM) ──────────────────
import re as _re

_RESUME_TRIGGERS = (
    _re.compile(r"dove\s+(?:ero\s+rimasto|mi\s+ero\s+fermato|avevo\s+lasciato)", _re.I),
    _re.compile(r"cosa\s+(?:stavo\s+facendo|stavamo\s+facendo|ho\s+fatto)", _re.I),
)

_STATUS_PATTERNS = (
    _re.compile(r"(?:stato|sessione|sessioni)\s+(?:di|de)\s+the\s*scribe", _re.I),
    _re.compile(r"stato\s+(?:delle\s+)?sessioni\b", _re.I),
    _re.compile(r"the\s*scribe\s+(?:è|e'|e)\s+attivo", _re.I),
)

# Parole di raccordo da togliere in testa alla coda della frase.
_PROJECT_LEADING_STOPWORDS = {
    "su", "con", "in", "per", "del", "della", "dello", "dei", "degli",
    "nel", "nella", "al", "alla", "ai", "sul", "sulla", "di", "a", "da",
    "progetto", "progetti",
}

# Parole da togliere in coda ("... su Clio per favore" -> "Clio").
_PROJECT_TRAILING_STOPWORDS = {
    "ora", "adesso", "oggi", "ieri", "prima", "per", "favore", "grazie",
    "capo", "progetto", "progetti", "clio?",
}


def _clean_project_hint(raw: str) -> str:
    """Ripulisce la coda della frase ('su Clio per favore' -> 'Clio')."""
    words = [w for w in _re.split(r"\s+", (raw or "").strip(" ?!.,;:")) if w]
    while words and words[0].lower() in _PROJECT_LEADING_STOPWORDS:
        words.pop(0)
    while words and words[-1].lower() in _PROJECT_TRAILING_STOPWORDS:
        words.pop()
    return " ".join(words).strip()[:40]


def detect_scribe_intent(user_request: str) -> Optional[Dict[str, Any]]:
    """Riconosce 'Dove ero rimasto su X' e 'Stato sessioni' senza chiamare il LLM.

    Ritorna {'action': ..., 'params': {...}} oppure None. Il chiamante deve
    eseguire l'azione tramite policy.guarded_call (nessun bypass del control-plane).
    """
    text = (user_request or "").strip()
    if not text:
        return None

    for pattern in _STATUS_PATTERNS:
        if pattern.search(text):
            return {"action": "stato_sessioni_scribe", "params": {}}

    for trigger in _RESUME_TRIGGERS:
        match = trigger.search(text)
        if match:
            hint = _clean_project_hint(text[match.end():])
            return {"action": "recupera_resume_progetto", "params": {"project_id": hint}}

    return None
