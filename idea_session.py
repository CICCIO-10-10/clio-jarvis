"""idea_session.py — FASE 2 · IdeaSession: stato minimo, esplicito e verificabile.

Un'"idea" per CLIO non e' una chat: e' una sessione di lavoro con un obiettivo,
un progetto, lo strumento aperto e un'ultima verifica tracciata. Questo modulo
e' la struttura dati + la persistenza, niente orchestrazione: chi la usa e'
`desktop_agent.apri_app_e_inizia_idea` (C1) e, in seguito, il ciclo "Partner
Creativo" (osserva -> propone -> verifica).

Contratto pubblico:
    create_idea_session("dipingi un tramonto", "clio", active_app="mspaint.exe")
        -> IdeaSession
    load_idea_session(idea_id)          -> IdeaSession | None
    list_idea_sessions(project_id=None) -> list[IdeaSession]
    close_idea_session(idea_id, "done") -> IdeaSession
    verify_idea_session(idea_id)        -> IdeaSession   (aggiorna last_verified_at)

C3 · sessione persistente ("riprendi da qui"):
    add_materiale(idea_id, path)        -> bool      (file/note collegati)
    add_decisione(idea_id, testo)       -> bool      (nota anche a The Scribe)
    set_prossimo_passo(idea_id, testo)  -> bool      (nota anche a The Scribe)
    set_blocco_corrente(idea_id, testo) -> bool      ("" azzera il blocco)
    get_resume_idea(idea_id)            -> dict      (fotografia dell'idea)
    riprendi_da_qui(progetto="", idea_id="", parla=None) -> str
    find_idea_for_resume(progetto)      -> IdeaSession | None

C4 · Research Agent (i riferimenti sono materiali con `fonte`):
    add_riferimento(idea_id, {"url", "titolo", "fonte": "research", ...}) -> bool
    materiale_url(m) / materiale_label(m) / materiale_fonte(m) -> str
    campo `ricerca`: ultima query, riassunto e conteggi

Persistenza: un solo file JSON (`data/idea_sessions.json`), scrittura atomica
(tmp + os.replace) sotto lock, UTF-8. Uno store corrotto o non leggibile viene
segnalato senza essere trattato come vuoto: nessun salvataggio lo sovrascrive.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ClioIdeaSession")

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_FILE = BASE_DIR / "data" / "idea_sessions.json"

# Stati ammessi: una sessione non e' mai "a meta'", o e' viva o e' chiusa.
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DONE = "done"
STATUS_ABANDONED = "abandoned"
VALID_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_DONE, STATUS_ABANDONED)

MAX_GOAL_CHARS = 500
MAX_PROJECT_ID_CHARS = 64
MAX_APP_CHARS = 120
MAX_OBSERVATION_CHARS = 2000   # descrizione C2 (Vision): troncata, non rifiutata
MAX_PATH_CHARS = 500           # percorso del modello 3D (C5)
VALID_3D_STATUS = ("blender", "fallback")   # "" = nessun modello ancora generato
# ── C3: sessione persistente ("riprendi da qui") ──
MAX_MATERIALI = 50                       # path collegati all'idea
MAX_DECISIONI = 100                      # decisioni registrate (le più vecchie restano)
MAX_TESTO_DECISIONE = 500
MAX_PROSSIMO_PASSO = 300
MAX_BLOCCO = 300
MAX_PATH_MATERIALE = 500
CATEGORIA_DECISIONE = "decisione"        # categoria dell'osservazione per The Scribe
CATEGORIA_COMPLETAMENTO = "completamento"
# ── C4: Research Agent (riferimenti web/YouTube salvati come materiali) ──
MAX_TITOLO_RIFERIMENTO = 200
MAX_SNIPPET_MATERIALE = 400
MAX_RIASSUNTO = 1200
FONTE_LOCALE = "locale"                  # file/nota del PC (materiale "normale")
DEFAULT_PROJECT_ID = "clio"

_LOCK = threading.RLock()


class IdeaSessionError(ValueError):
    """Errore prevedibile su una IdeaSession (messaggio già pronto per l'utente)."""


def _now() -> str:
    """Timestamp UTC ISO-8601 (secondi), lo stesso formato di logs/security.log."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_project_id(project_id: str) -> str:
    """Slug del progetto: minuscolo, spazi -> '-', solo [a-z0-9_-], max 64 char."""
    raw = (project_id or "").strip().lower()
    if not raw:
        raise IdeaSessionError("project_id mancante: indica il progetto dell'idea")
    slug = re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)[:MAX_PROJECT_ID_CHARS].strip("-.")
    if not slug:
        raise IdeaSessionError(f"project_id non valido: {project_id!r}")
    return slug


def _clean_goal(goal: str) -> str:
    testo = (goal or "").strip()
    if not testo:
        raise IdeaSessionError("obiettivo mancante: un'idea senza obiettivo non e' verificabile")
    if len(testo) > MAX_GOAL_CHARS:
        raise IdeaSessionError(
            f"obiettivo troppo lungo ({len(testo)} caratteri, massimo {MAX_GOAL_CHARS})")
    return testo


def _clean_app(active_app: str) -> str:
    return (active_app or "").strip()[:MAX_APP_CHARS]


# ── Normalizzatori C3/C4 (dati che possono arrivare da un JSON esterno) ──────
def _testo_semplice(valore: Any, massimo: int) -> str:
    """Stringa ripulita (spazi collassati, virgolette esterne, troncata)."""
    if isinstance(valore, bytes):
        try:
            valore = valore.decode("utf-8", "replace")
        except Exception:
            return ""
    if not isinstance(valore, str):
        return ""
    return " ".join(valore.strip().strip('"').strip("'").split())[:massimo]


def _clean_riferimento(voce: Any) -> Optional[Dict[str, str]]:
    """Materiale strutturato `{url, titolo, fonte, tipo, ...}` oppure None.

    C4: un riferimento web/YouTube è un materiale dell'idea come un file, ma
    porta con sé la fonte (`"research"`) e il tipo (`web`/`youtube`). Una stringa
    semplice resta una stringa (vedi `_clean_materiali`), così gli store e i
    chiamanti scritti per C3 continuano a funzionare.
    """
    if isinstance(voce, str):
        url = _testo_semplice(voce, MAX_PATH_MATERIALE)
        if not url:
            return None
        return {"url": url, "titolo": url, "fonte": FONTE_LOCALE,
                "tipo": "web" if url.startswith(("http://", "https://")) else "file"}
    if not isinstance(voce, dict):
        return None

    url = _testo_semplice(voce.get("url") or voce.get("href") or voce.get("path"),
                          MAX_PATH_MATERIALE)
    if not url:
        return None
    titolo = _testo_semplice(voce.get("titolo") or voce.get("title"), MAX_TITOLO_RIFERIMENTO)
    fonte = _testo_semplice(voce.get("fonte") or voce.get("source"), 40) or FONTE_LOCALE
    tipo = _testo_semplice(voce.get("tipo"), 20) or (
        "web" if url.startswith(("http://", "https://")) else "file")
    pulito: Dict[str, str] = {"url": url, "titolo": titolo or url,
                              "fonte": fonte, "tipo": tipo}
    durata = _testo_semplice(voce.get("durata") or voce.get("duration"), 20)
    if durata:
        pulito["durata"] = durata
    snippet = _testo_semplice(voce.get("snippet") or voce.get("descrizione"),
                              MAX_SNIPPET_MATERIALE)
    if snippet:
        pulito["snippet"] = snippet
    return pulito


def materiale_url(materiale: Any) -> str:
    """URL (o path) di un materiale, sia esso stringa o riferimento strutturato."""
    if isinstance(materiale, dict):
        return str(materiale.get("url") or materiale.get("href") or "")
    return str(materiale or "")


def materiale_label(materiale: Any) -> str:
    """Etichetta breve per la voce: titolo del riferimento o nome del file."""
    if isinstance(materiale, dict):
        return str(materiale.get("titolo") or materiale.get("title")
                   or materiale_url(materiale))
    percorso = materiale_url(materiale)
    return Path(percorso).name or percorso


def materiale_fonte(materiale: Any) -> str:
    """Fonte dichiarata ('research', 'locale', ...)."""
    if isinstance(materiale, dict):
        return str(materiale.get("fonte") or FONTE_LOCALE)
    return FONTE_LOCALE


def materiale_e_ricerca(materiale: Any) -> bool:
    """True se il materiale è un riferimento trovato dal Research Agent (C4)."""
    return materiale_fonte(materiale).strip().lower() == "research"


def _clean_materiali(valore: Any) -> List[Any]:
    """Lista di materiali pulita: path (str) o riferimenti web (dict), no duplicati.

    L'ordine è quello di inserimento; la deduplica confronta URL/path in
    minuscolo (su Windows due percorsi con case diverso sono lo stesso file).
    """
    if isinstance(valore, (str, bytes, dict)):
        valore = [valore]
    if not isinstance(valore, (list, tuple, set)):
        return []
    out: List[Any] = []
    visti = set()
    for voce in valore:
        if isinstance(voce, dict):
            pulito: Any = _clean_riferimento(voce)
        else:
            pulito = _testo_semplice(voce, MAX_PATH_MATERIALE) or None
        if pulito is None:
            continue
        chiave = materiale_url(pulito).lower()
        if not chiave or chiave in visti:
            continue
        visti.add(chiave)
        out.append(pulito)
        if len(out) >= MAX_MATERIALI:
            break
    return out


def _clean_ricerca(valore: Any) -> Dict[str, Any]:
    """Ultima ricerca (C4): `{query, riassunto, data, web, youtube, salvati}`."""
    if not isinstance(valore, dict):
        return {}
    out: Dict[str, Any] = {}
    query = _testo_semplice(valore.get("query"), 200)
    riassunto = _testo_semplice(valore.get("riassunto") or valore.get("summary"),
                                MAX_RIASSUNTO)
    data = _testo_semplice(valore.get("data"), 40)
    if query:
        out["query"] = query
    if riassunto:
        out["riassunto"] = riassunto
    if data:
        out["data"] = data
    for chiave in ("web", "youtube", "salvati"):
        try:
            out[chiave] = max(0, int(valore.get(chiave) or 0))
        except (TypeError, ValueError):
            out[chiave] = 0
    return out



def _clean_decisioni(valore: Any) -> List[Dict[str, str]]:
    """Lista di decisioni: `{"testo": ..., "data": ...}`.

    Accetta anche stringhe semplici (comodità/retro-compatibilità): diventano
    `{"testo": <stringa>, "data": ""}`. Le voci inutilizzabili vengono scartate.
    """
    if isinstance(valore, (str, bytes, dict)):
        valore = [valore]
    if not isinstance(valore, (list, tuple)):
        return []
    out: List[Dict[str, str]] = []
    for voce in valore:
        if isinstance(voce, str):
            testo, data = voce, ""
        elif isinstance(voce, dict):
            testo = voce.get("testo") or voce.get("text") or ""
            data = voce.get("data") or voce.get("date") or ""
        else:
            continue
        testo = str(testo or "").strip()[:MAX_TESTO_DECISIONE]
        if not testo:
            continue
        out.append({"testo": testo, "data": str(data or "").strip()[:40]})
        if len(out) >= MAX_DECISIONI:
            break
    return out


@dataclass
class IdeaSession:
    """Sessione di lavoro su un'idea: chi, su cosa, con quale strumento, in che stato."""

    idea_id: str
    project_id: str
    user_goal: str
    active_app: str = ""
    status: str = STATUS_ACTIVE
    last_verified_at: str = ""
    # C2 · Vision: ultima descrizione osservata sulla finestra dell'idea.
    # Campo opzionale in coda => gli store scritti da C1 restano leggibili.
    last_observation: str = ""
    # C5 · 2D->3D: ultimo modello generato e da quale motore ("blender"/"fallback").
    # In Python un campo non può iniziare con una cifra: `file_3d`/`status_3d`
    # corrispondono a `3d_file`/`3d_status`.
    file_3d: str = ""
    status_3d: str = ""
    # C3 · Sessione persistente ("riprendi da qui"): cosa accompagna l'idea.
    # Campi opzionali in coda => gli store scritti da C1/C2/C5 restano leggibili.
    materiali: List[str] = field(default_factory=list)        # path di file/note
    decisioni: List[Dict[str, str]] = field(default_factory=list)   # {"testo","data"}
    prossimo_passo: str = ""          # azione concreta, una sola
    blocco_corrente: Optional[str] = None   # cosa impedisce il progresso
    # C4 · Research Agent: ultima ricerca fatta per questa idea (query, riassunto,
    # conteggi). I link trovati finiscono in `materiali` con fonte="research".
    ricerca: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.idea_id = str(self.idea_id or "").strip()
        if not self.idea_id:
            raise IdeaSessionError("idea_id mancante")
        self.project_id = normalize_project_id(self.project_id)
        self.user_goal = _clean_goal(self.user_goal)
        self.active_app = _clean_app(self.active_app)
        self.status = str(self.status or STATUS_ACTIVE).strip().lower()
        if self.status not in VALID_STATUSES:
            raise IdeaSessionError(
                f"stato '{self.status}' non valido: usa uno di {VALID_STATUSES}")
        self.last_verified_at = str(self.last_verified_at or "").strip()
        self.last_observation = str(self.last_observation or "").strip()[:MAX_OBSERVATION_CHARS]
        self.file_3d = str(self.file_3d or "").strip()[:MAX_PATH_CHARS]
        self.status_3d = str(self.status_3d or "").strip().lower()
        if self.status_3d and self.status_3d not in VALID_3D_STATUS:
            # Uno status sconosciuto viene azzerato, non fa esplodere la lettura:
            # lo store può arrivare da una versione futura/diversa di CLIO.
            logger.warning(f"[IDEA] status_3d sconosciuto {self.status_3d!r}: azzerato")
            self.status_3d = ""
        # C3: liste sempre normalizzate (mai None, mai duplicati, limiti di lunghezza).
        self.materiali = _clean_materiali(self.materiali)
        self.decisioni = _clean_decisioni(self.decisioni)
        self.prossimo_passo = str(self.prossimo_passo or "").strip()[:MAX_PROSSIMO_PASSO]
        blocco = str(self.blocco_corrente or "").strip()[:MAX_BLOCCO]
        self.blocco_corrente = blocco or None
        self.ricerca = _clean_ricerca(self.ricerca)

    # ── serializzazione ──────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IdeaSession":
        if not isinstance(data, dict):
            raise IdeaSessionError("una IdeaSession deve essere un oggetto JSON")
        return cls(
            idea_id=data.get("idea_id", ""),
            project_id=data.get("project_id", ""),
            user_goal=data.get("user_goal", ""),
            active_app=data.get("active_app", ""),
            status=data.get("status", STATUS_ACTIVE),
            last_verified_at=data.get("last_verified_at", ""),
            last_observation=data.get("last_observation", ""),
            file_3d=data.get("file_3d", ""),
            status_3d=data.get("status_3d", ""),
            materiali=data.get("materiali", []),
            decisioni=data.get("decisioni", []),
            prossimo_passo=data.get("prossimo_passo", ""),
            blocco_corrente=data.get("blocco_corrente"),
            ricerca=data.get("ricerca") or {},
        )

    # ── stato ────────────────────────────────────────────────────────────────
    def touch(self) -> "IdeaSession":
        """Segna una verifica appena avvenuta (l'idea è ancora valida)."""
        self.last_verified_at = _now()
        return self

    @property
    def is_open(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_PAUSED)

    def describe(self) -> str:
        righe = [f"Idea {self.idea_id[:8]} [{self.status}] progetto={self.project_id} "
                 f"app={self.active_app or '-'} verificata={self.last_verified_at or 'mai'}",
                 f"  obiettivo: {self.user_goal}"]
        if self.last_observation:
            righe.append(f"  ultima osservazione: {self.last_observation}")
        if self.file_3d:
            righe.append(f"  modello 3D [{self.status_3d or 'sconosciuto'}]: {self.file_3d}")
        if self.prossimo_passo:
            righe.append(f"  prossimo passo: {self.prossimo_passo}")
        if self.blocco_corrente:
            righe.append(f"  blocco: {self.blocco_corrente}")
        if self.materiali or self.decisioni:
            righe.append(f"  decisioni: {len(self.decisioni)} | materiali: {len(self.materiali)}")
        if self.ricerca:
            righe.append(f"  ricerca: {self.ricerca.get('query', '')} "
                         f"({self.ricerca.get('web', 0)} web, "
                         f"{self.ricerca.get('youtube', 0)} youtube)")
        return "\n".join(righe)


# ── Persistenza ───────────────────────────────────────────────────────────────
def _store_path(path: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else Path(SESSIONS_FILE)


def _read_raw(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Legge tutte le sessioni; solo un file assente equivale a store vuoto.

    Un file illeggibile o con schema invalido genera IdeaSessionError. Trattare
    questi casi come lista vuota permetterebbe alla successiva scrittura di
    sovrascrivere silenziosamente lo storico e perdere sessioni recuperabili.
    """
    p = _store_path(path)
    try:
        testo = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        logger.error("[IDEA] impossibile leggere lo store %s: %s", p, exc)
        raise IdeaSessionError(
            f"store IdeaSession non leggibile in {p}; nessun dato è stato modificato"
        ) from exc
    try:
        dati = json.loads(testo)
    except ValueError as exc:
        logger.error("[IDEA] store JSON corrotto, scritture bloccate: %s", p)
        raise IdeaSessionError(
            f"store IdeaSession corrotto in {p}; nessun dato è stato modificato"
        ) from exc
    if isinstance(dati, dict):
        if "sessions" not in dati:
            raise IdeaSessionError(
                f"schema dello store IdeaSession non valido in {p}; nessun dato è stato modificato"
            )
        dati = dati["sessions"]
    if not isinstance(dati, list):
        raise IdeaSessionError(
            f"schema dello store IdeaSession non valido in {p}; nessun dato è stato modificato"
        )
    if any(not isinstance(d, dict) for d in dati):
        raise IdeaSessionError(
            f"lo store IdeaSession contiene righe non valide in {p}; nessun dato è stato modificato"
        )
    return dati


def _write_raw(sessioni: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    """Scrittura atomica: il file non resta mai scritto a metà."""
    p = _store_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps({"sessions": sessioni}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _load_all(path: Optional[Path] = None) -> List[IdeaSession]:
    sessioni: List[IdeaSession] = []
    for dato in _read_raw(path):
        try:
            sessioni.append(IdeaSession.from_dict(dato))
        except IdeaSessionError as e:
            logger.warning(f"[IDEA] sessione scartata: {e}")
    return sessioni


# ── API pubblica ──────────────────────────────────────────────────────────────
def create_idea_session(goal: str, project_id: str = DEFAULT_PROJECT_ID,
                        active_app: str = "", path: Optional[Path] = None) -> IdeaSession:
    """Crea e salva una nuova IdeaSession (UUID v4) in stato 'active'."""
    sessione = IdeaSession(
        idea_id=str(uuid.uuid4()),
        project_id=project_id,
        user_goal=goal,
        active_app=active_app,
        status=STATUS_ACTIVE,
        last_verified_at=_now(),
    )
    with _LOCK:
        tutte = _read_raw(path)
        tutte.append(sessione.to_dict())
        _write_raw(tutte, path)
    logger.info(f"[IDEA] creata {sessione.idea_id} (progetto {sessione.project_id})")
    return sessione


def load_idea_session(idea_id: str, path: Optional[Path] = None) -> Optional[IdeaSession]:
    """Ritrova una sessione per id. None se non esiste (nessuna eccezione)."""
    cercato = (idea_id or "").strip()
    if not cercato:
        return None
    for sessione in _load_all(path):
        if sessione.idea_id == cercato:
            return sessione
    return None


def list_idea_sessions(project_id: Optional[str] = None, status: Optional[str] = None,
                       path: Optional[Path] = None) -> List[IdeaSession]:
    """Elenca le sessioni, filtrando per progetto e/o stato."""
    progetto = normalize_project_id(project_id) if project_id else None
    stato = (status or "").strip().lower() or None
    if stato and stato not in VALID_STATUSES:
        raise IdeaSessionError(
            f"stato '{stato}' non valido: usa uno di {VALID_STATUSES}")
    return [s for s in _load_all(path)
            if (progetto is None or s.project_id == progetto)
            and (stato is None or s.status == stato)]


def _save(sessione: IdeaSession, path: Optional[Path] = None) -> IdeaSession:
    """Sostituisce la sessione salvata con lo stesso id. Errore se non esiste."""
    with _LOCK:
        tutte = _read_raw(path)
        for indice, dato in enumerate(tutte):
            if dato.get("idea_id") == sessione.idea_id:
                tutte[indice] = sessione.to_dict()
                _write_raw(tutte, path)
                return sessione
    raise IdeaSessionError(f"idea {sessione.idea_id} non trovata nello store")


def update_idea_session(sessione: IdeaSession, path: Optional[Path] = None) -> IdeaSession:
    """Salva lo stato aggiornato di una sessione esistente."""
    return _save(sessione, path)


def verify_idea_session(idea_id: str, path: Optional[Path] = None) -> IdeaSession:
    """Registra che l'idea è stata verificata ADESSO (aggiorna last_verified_at)."""
    sessione = load_idea_session(idea_id, path)
    if sessione is None:
        raise IdeaSessionError(f"idea {idea_id} non trovata")
    return _save(sessione.touch(), path)


def close_idea_session(idea_id: str, status: str = STATUS_DONE,
                       path: Optional[Path] = None) -> IdeaSession:
    """Chiude una sessione ('done' o 'abandoned')."""
    stato = (status or STATUS_DONE).strip().lower()
    if stato not in (STATUS_DONE, STATUS_ABANDONED):
        raise IdeaSessionError("una sessione si chiude con 'done' o 'abandoned'")
    sessione = load_idea_session(idea_id, path)
    if sessione is None:
        raise IdeaSessionError(f"idea {idea_id} non trovata")
    sessione.status = stato
    sessione.touch()
    return _save(sessione, path)


def get_active_idea_session(project_id: Optional[str] = None,
                            path: Optional[Path] = None) -> Optional[IdeaSession]:
    """Ultima sessione ancora aperta (opzionalmente di un progetto)."""
    aperte = [s for s in list_idea_sessions(project_id=project_id, path=path) if s.is_open]
    return aperte[-1] if aperte else None


def describe_idea_sessions(project_id: Optional[str] = None,
                           path: Optional[Path] = None) -> str:
    """Tabella leggibile delle sessioni (usata nei report e a voce)."""
    sessioni = list_idea_sessions(project_id=project_id, path=path)
    if not sessioni:
        return "Nessuna IdeaSession registrata."
    return "\n".join(s.describe() for s in sessioni)


# ══════════════════════════════════════════════════════════════════════════════
# C3 · Sessione persistente: materiali, decisioni, prossimo passo, blocco
# ══════════════════════════════════════════════════════════════════════════════
# Il patto: queste funzioni non sollevano MAI (tranne errori di programmazione):
# l'idea giusta viene aggiornata e si ritorna True/False. Un fallimento di The
# Scribe non annulla la decisione salvata in CLIO.

# Ultima osservazione inviata a The Scribe: serve ai report e ai test.
_ULTIMO_SCRIBE: Dict[str, Any] = {}


def last_scribe_observation() -> Dict[str, Any]:
    """Copia dell'ultimo esito di `_osserva_su_scribe` (diagnostica/report)."""
    return dict(_ULTIMO_SCRIBE)


def _osserva_su_scribe(project_id: str, categoria: str, testo: str,
                       idea_id: str) -> Dict[str, Any]:
    """Manda l'osservazione a The Scribe (categoria: decisione/completamento).

    Usa l'API pubblica `scribe_control.record_observation`: il bridge scrive la
    nota nel vault, altrimenti l'osservazione finisce nell'outbox di CLIO (che
    The Scribe assorbe). CLIO non tocca mai il vault direttamente.
    Non solleva: un errore diventa `{"ok": False, "error": ...}`.
    """
    global _ULTIMO_SCRIBE
    esito: Dict[str, Any] = {"ok": False, "categoria": categoria,
                             "project_id": project_id, "testo": testo,
                             "recorded": None, "path": None, "error": None}
    try:
        from scribe_control import record_observation
    except Exception as e:            # pragma: no cover - dipende dall'ambiente
        esito["error"] = f"scribe_control non disponibile: {type(e).__name__}: {e}"
        _ULTIMO_SCRIBE = esito
        logger.warning(f"[IDEA] Scribe non raggiungibile: {esito['error']}")
        return esito
    try:
        risposta = record_observation(
            project_id, testo,
            workflow_id=f"idea-{idea_id[:8]}",
            category=categoria,
        )
        esito.update({k: risposta.get(k) for k in ("ok", "recorded", "path", "error")})
        esito["ok"] = bool(risposta.get("ok"))
    except Exception as e:
        esito["error"] = f"{type(e).__name__}: {e}"
        logger.warning(f"[IDEA] osservazione per The Scribe fallita: {esito['error']}")
    _ULTIMO_SCRIBE = esito
    if esito["ok"]:
        logger.info(f"[IDEA] osservazione '{categoria}' per {project_id} -> "
                    f"{esito['recorded']}: {esito['path']}")
    return esito


def _sessione_per_id(idea_id: str = "", path: Optional[Path] = None):
    """Sessione per id, oppure l'ultima ancora aperta. (None, motivo) se non c'è."""
    if (idea_id or "").strip():
        sessione = load_idea_session(idea_id, path)
        if sessione is None:
            return None, f"idea «{idea_id}» non trovata"
        return sessione, ""
    sessione = get_active_idea_session(path=path)
    if sessione is None:
        return None, "nessuna IdeaSession aperta"
    return sessione, ""


def add_materiale(idea_id: str, path: str) -> bool:
    """Collega un file/nota all'idea. False se idea manca, path vuoto o duplicato."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        logger.warning(f"[IDEA] add_materiale: {motivo}")
        return False
    nuovi = _clean_materiali([path])
    if not nuovi:
        logger.warning("[IDEA] add_materiale: percorso vuoto o non valido")
        return False
    candidato = nuovi[0]
    if materiale_url(candidato).lower() in {materiale_url(m).lower()
                                            for m in sessione.materiali}:
        logger.info(f"[IDEA] add_materiale: già collegato ({candidato})")
        return False
    if len(sessione.materiali) >= MAX_MATERIALI:
        logger.warning(f"[IDEA] add_materiale: limite di {MAX_MATERIALI} materiali raggiunto")
        return False
    sessione.materiali.append(candidato)
    sessione.touch()
    try:
        update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[IDEA] add_materiale non salvato: {type(e).__name__}: {e}")
        return False
    logger.info(f"[IDEA] materiale collegato all'idea {sessione.idea_id[:8]}: {candidato}")
    return True


def add_riferimento(idea_id: str, riferimento: Any) -> bool:
    """C4 · collega un riferimento (web/YouTube) all'idea con la sua fonte.

    Accetta un dizionario (`{"url", "titolo", "fonte": "research", "tipo": ...}`)
    oppure una semplice URL/testo. False se l'idea manca, il riferimento è
    inutilizzabile, già presente (stesso URL) o il limite è raggiunto.
    """
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        logger.warning(f"[IDEA] add_riferimento: {motivo}")
        return False
    pulito = _clean_riferimento(riferimento)
    if pulito is None:
        logger.warning("[IDEA] add_riferimento: riferimento senza URL")
        return False
    chiave = materiale_url(pulito).lower()
    if chiave in {materiale_url(m).lower() for m in sessione.materiali}:
        logger.info(f"[IDEA] add_riferimento: già collegato ({pulito['url'][:80]})")
        return False
    if len(sessione.materiali) >= MAX_MATERIALI:
        logger.warning(f"[IDEA] add_riferimento: limite di {MAX_MATERIALI} materiali raggiunto")
        return False
    sessione.materiali.append(pulito)
    sessione.touch()
    try:
        update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[IDEA] add_riferimento non salvato: {type(e).__name__}: {e}")
        return False
    logger.info(f"[IDEA] riferimento [{pulito['fonte']}] collegato all'idea "
                f"{sessione.idea_id[:8]}: {pulito['url'][:100]}")
    return True


def add_decisione(idea_id: str, testo: str) -> bool:
    """Registra una decisione (chiude una scelta) e la manda a The Scribe."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        logger.warning(f"[IDEA] add_decisione: {motivo}")
        return False
    pulito = (testo or "").strip()[:MAX_TESTO_DECISIONE]
    if not pulito:
        logger.warning("[IDEA] add_decisione: testo vuoto")
        return False
    # Stessa decisione ripetuta = stesso fatto: The Scribe la deduplica per hash,
    # CLIO non la conta due volte.
    if any(d.get("testo", "").strip().lower() == pulito.lower() for d in sessione.decisioni):
        logger.info(f"[IDEA] add_decisione: già registrata ({pulito[:60]})")
        return False
    if len(sessione.decisioni) >= MAX_DECISIONI:
        logger.warning(f"[IDEA] add_decisione: limite di {MAX_DECISIONI} decisioni raggiunto")
        return False
    sessione.decisioni.append({"testo": pulito, "data": _now()})
    sessione.touch()
    try:
        update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[IDEA] add_decisione non salvata: {type(e).__name__}: {e}")
        return False
    logger.info(f"[IDEA] decisione salvata nell'idea {sessione.idea_id[:8]}: {pulito[:80]}")
    _osserva_su_scribe(sessione.project_id, CATEGORIA_DECISIONE, pulito, sessione.idea_id)
    return True


def set_prossimo_passo(idea_id: str, testo: str) -> bool:
    """Imposta il prossimo passo concreto e lo manda a The Scribe."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        logger.warning(f"[IDEA] set_prossimo_passo: {motivo}")
        return False
    pulito = (testo or "").strip()[:MAX_PROSSIMO_PASSO]
    if not pulito:
        logger.warning("[IDEA] set_prossimo_passo: testo vuoto")
        return False
    sessione.prossimo_passo = pulito
    sessione.touch()
    try:
        update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[IDEA] set_prossimo_passo non salvato: {type(e).__name__}: {e}")
        return False
    logger.info(f"[IDEA] prossimo passo dell'idea {sessione.idea_id[:8]}: {pulito[:80]}")
    _osserva_su_scribe(sessione.project_id, CATEGORIA_COMPLETAMENTO,
                       f"Prossimo passo: {pulito}", sessione.idea_id)
    return True


def set_blocco_corrente(idea_id: str, testo: str = "") -> bool:
    """Imposta (o azzera, con testo vuoto) ciò che blocca l'idea."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        logger.warning(f"[IDEA] set_blocco_corrente: {motivo}")
        return False
    pulito = (testo or "").strip()[:MAX_BLOCCO]
    sessione.blocco_corrente = pulito or None
    sessione.touch()
    try:
        update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[IDEA] set_blocco_corrente non salvato: {type(e).__name__}: {e}")
        return False
    logger.info(f"[IDEA] blocco dell'idea {sessione.idea_id[:8]}: "
                f"{sessione.blocco_corrente or '(nessuno)'}")
    return True


def get_resume_idea(idea_id: str) -> Dict[str, Any]:
    """Fotografia dell'idea per riprendere il lavoro: mai un'eccezione.

    Chiavi: obiettivo, materiali, decisioni, prossimo_passo, blocco,
    ultima_verifica (+ idea_id/progetto/stato/trovata per il chiamante).
    """
    sessione = load_idea_session(idea_id)
    if sessione is None:
        logger.info(f"[IDEA] get_resume_idea: idea «{idea_id}» non trovata")
        return {
            "obiettivo": "", "materiali": [], "decisioni": [], "prossimo_passo": "",
            "blocco": None, "ultima_verifica": "",
            "idea_id": (idea_id or "").strip(), "progetto": "", "stato": "",
            "trovata": False,
        }
    return {
        "obiettivo": sessione.user_goal,
        "materiali": [dict(m) if isinstance(m, dict) else m for m in sessione.materiali],
        "decisioni": [dict(d) for d in sessione.decisioni],
        "prossimo_passo": sessione.prossimo_passo,
        "blocco": sessione.blocco_corrente,
        "ultima_verifica": sessione.last_verified_at,
        "ricerca": dict(sessione.ricerca),
        "idea_id": sessione.idea_id,
        "progetto": sessione.project_id,
        "stato": sessione.status,
        "trovata": True,
    }


def find_idea_for_resume(project_id: str = "") -> Optional[IdeaSession]:
    """Ultima idea aperta del progetto; se il progetto non è dato, l'ultima aperta."""
    progetto = (project_id or "").strip()
    if progetto:
        try:
            slug = normalize_project_id(progetto)
        except IdeaSessionError:
            slug = progetto.lower()
        sessione = get_active_idea_session(project_id=slug)
        if sessione is not None:
            return sessione
        # Tolleranza: "Clio" vs "clio-lab" -> confronto per prefisso sul nome.
        for candidata in reversed(list_idea_sessions()):
            if candidata.is_open and (slug in candidata.project_id or
                                      candidata.project_id in slug):
                return candidata
        return None
    return get_active_idea_session()


def _nomi_materiali(materiali: List[Any], massimo: int = 3) -> str:
    """Nomi leggibili dei materiali (file o titoli dei riferimenti) per la voce."""
    nomi = [materiale_label(m) for m in materiali[:massimo]]
    extra = len(materiali) - len(nomi)
    testo = ", ".join(nomi)
    if extra > 0:
        testo += f" e altri {extra}"
    return testo


def _plurale(quantita: int, singolare: str, plurale: str) -> str:
    """'1 decisione' / '3 decisioni' — la frase viene letta ad alta voce."""
    return f"{quantita} {singolare if quantita == 1 else plurale}"


def format_resume(sessione: IdeaSession) -> str:
    """Frase da leggere ad alta voce per 'Riprendi da qui' (pura, testabile)."""
    resume = get_resume_idea(sessione.idea_id)
    decisioni = len(resume["decisioni"])
    materiali = len(resume["materiali"])

    parti = [f"Stavamo lavorando a {resume['obiettivo']}."]
    if resume["prossimo_passo"]:
        parti.append(f"Il prossimo passo è {resume['prossimo_passo']}.")
    else:
        parti.append("Il prossimo passo non è ancora definito.")
    parti.append(f"Ci sono {_plurale(decisioni, 'decisione', 'decisioni')} e "
                 f"{_plurale(materiali, 'materiale', 'materiali')} collegati.")
    if resume["blocco"]:
        parti.append(f"C'è un blocco: {resume['blocco']}.")
    if materiali:
        parti.append(f"Vuoi che apra uno dei materiali? Ho {_nomi_materiali(resume['materiali'])}.")
    return " ".join(parti)


def riprendi_da_qui(progetto: str = "", idea_id: str = "", parla=None) -> str:
    """C3 · 'Riprendi da qui': trova l'idea, riassume lo stato e chiede dei materiali.

    `progetto` vuoto = ultima idea aperta. `parla` è un TTS iniettabile
    (callable(testo)): da voce si passa il motore di CLIO, nei test un doppio.
    """
    sessione = load_idea_session(idea_id) if (idea_id or "").strip() else None
    if sessione is None:
        sessione = find_idea_for_resume(progetto)
    if sessione is None:
        dove = f" per il progetto «{progetto}»" if (progetto or "").strip() else ""
        return (f"Non ho nessuna idea aperta{dove}: non c'è niente da riprendere. "
                "Avvia un'idea con apri_app_e_inizia_idea e riprova.")
    testo = format_resume(sessione)
    if callable(parla):
        try:
            parla(testo)
        except Exception as e:
            logger.warning(f"[IDEA] TTS non disponibile: {type(e).__name__}: {e}")
    logger.info(f"[IDEA] riprendi da qui -> idea {sessione.idea_id[:8]} "
                f"({len(sessione.decisioni)} decisioni, {len(sessione.materiali)} materiali)")
    return testo


# ── Azioni CLIO (nome italiano, usate dal LLM e dalla voce) ───────────────────
def aggiungi_materiale_idea(path: str = "", idea_id: str = "") -> str:
    """Azione: collega un file/nota all'idea in corso."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        return f"❌ Non aggiungo il materiale: {motivo}."
    ok = add_materiale(sessione.idea_id, path)
    if not ok:
        return f"❌ Materiale non aggiunto ({path or 'percorso vuoto'}): vuoto, duplicato o limite raggiunto."
    return f"📎 Materiale collegato all'idea {sessione.idea_id[:8]}: {path}"


def aggiungi_decisione_idea(testo: str = "", idea_id: str = "") -> str:
    """Azione: registra una decisione e la manda a The Scribe."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        return f"❌ Non registro la decisione: {motivo}."
    pulito = (testo or "").strip()[:MAX_TESTO_DECISIONE]
    if pulito and any(d.get("testo", "").strip().lower() == pulito.lower()
                      for d in sessione.decisioni):
        return (f"ℹ️ Decisione già registrata nell'idea {sessione.idea_id[:8]}: "
                f"{pulito[:80]}")
    ok = add_decisione(sessione.idea_id, testo)
    if not ok:
        return "❌ Decisione non registrata: testo vuoto o limite raggiunto."
    esito = last_scribe_observation()
    dove = (f"e salvata in {esito.get('path')}" if esito.get("ok")
            else f"ma The Scribe non l'ha scritta ({esito.get('error') or 'offline'})")
    return (f"🧭 Decisione registrata nell'idea {sessione.idea_id[:8]} "
            f"({len(sessione.decisioni)} in totale) {dove}.")


def imposta_prossimo_passo(testo: str = "", idea_id: str = "") -> str:
    """Azione: imposta il prossimo passo concreto dell'idea."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        return f"❌ Non imposto il prossimo passo: {motivo}."
    ok = set_prossimo_passo(sessione.idea_id, testo)
    if not ok:
        return "❌ Prossimo passo non impostato: testo vuoto."
    esito = last_scribe_observation()
    dove = (f"e salvata in {esito.get('path')}" if esito.get("ok")
            else f"ma The Scribe non l'ha scritta ({esito.get('error') or 'offline'})")
    return (f"➡️ Prossimo passo dell'idea {sessione.idea_id[:8]}: {testo} ({dove}).")


def imposta_blocco_idea(testo: str = "", idea_id: str = "") -> str:
    """Azione: segnala (o azzera) ciò che blocca l'idea."""
    sessione, motivo = _sessione_per_id(idea_id)
    if sessione is None:
        return f"❌ Non aggiorno il blocco: {motivo}."
    if not set_blocco_corrente(sessione.idea_id, testo):
        return "❌ Blocco non aggiornato."
    if (testo or "").strip():
        return f"🚧 Blocco registrato sull'idea {sessione.idea_id[:8]}: {testo.strip()[:MAX_BLOCCO]}"
    return f"✅ Nessun blocco sull'idea {sessione.idea_id[:8]}: via libera."
