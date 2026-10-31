"""
workflow_engine.py — R5: orchestrazione multi-agente con checkpoint e recovery.

Il motore esegue un OBIETTIVO (goal) come una macchina a stati:

    UNDERSTAND -> GROUND -> PLAN -> AUTHORIZE -> EXECUTE -> VERIFY -> PUBLISH -> REPORT

Garanzie:
  * ogni transizione è un CHECKPOINT su SQLite (`workflow_state.db`): se il
    processo muore, al riavvio il workflow riprende dallo stato salvato e non
    riesegue gli stati già completati;
  * ogni passo marcato `running` al momento del crash viene riconosciuto come
    "interrotto" e rieseguito (gli altri passi `done` vengono saltati);
  * AUTHORIZE passa da `policy.guarded_call` con l'azione `esegui_workflow`
    (classificata irreversibile in policy.py): nessun piano che tocchi il
    coding agent parte senza conferma esplicita;
  * EXECUTE esegue OGNI passo tramite `policy.guarded_call`, con l'azione
    dichiarata nel piano (`esegui_coding_task`, `recupera_resume_progetto`,
    `registra_osservazione_scribe`, `ispeziona_agenti`). La conferma del piano
    già ottenuta viene propagata al passo tramite `confirm`, così l'utente non
    riceve due domande per la stessa decisione;
  * timeout GLOBALE: allo scadere il workflow va in `status="timeout"` senza
    perdere i checkpoint; un secondo `execute_workflow` riparte da lì con un
    budget nuovo;
  * nessun passo può uscire dai confini degli agenti: il coding agent lavora
    solo dentro `_clio_sandbox`, The Scribe è di sola lettura (R1).

Uso tipico (programmatico):

    engine = WorkflowEngine()
    run = engine.execute_workflow("fixa il bug dell'addizione e documentalo", {"project": "clio"})
    print(run.report)

Uso da CLIO (voce/Telegram), tramite il control-plane:

    policy.guarded_call("esegui_workflow", esegui_workflow,
                        {"goal": "...", "progetto": "clio"}, origin="voice")

Uso manuale da console:

    python -c "from workflow_engine import esegui_workflow; print(esegui_workflow('...','clio'))"

Nota su The Scribe: `ScribeBridge` (R1) espone `get_resume`/`get_status` ed è di
sola lettura. L'osservazione prodotta dal workflow viene registrata in
`data/scribe_outbox/` (outbox di CLIO) oppure con `bridge.record_observation`
se la versione installata lo espone. Il vault di The Scribe non viene mai
modificato da CLIO.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import policy
from agent_registry import (
    AgentCapability,
    AgentRegistry,
    build_default_registry,
)

logger = logging.getLogger("ClioWorkflow")

# ── Costanti ──────────────────────────────────────────────────────────────────
STATES = (
    "UNDERSTAND", "GROUND", "PLAN", "AUTHORIZE",
    "EXECUTE", "VERIFY", "PUBLISH", "REPORT",
)
TERMINAL_STATE = "DONE"
ACTIVE_STATUSES = ("running", "timeout", "failed")

WORKFLOW_DB_ENV = "CLIO_WORKFLOW_DB"
WORKFLOW_ARTIFACTS_ENV = "CLIO_WORKFLOW_ARTIFACTS"
DEFAULT_WORKFLOW_TIMEOUT = 900          # 15 minuti: budget globale del workflow

# Azioni del control-plane usate dai passi (devono esistere in policy.py)
POLICY_ACTION_CODING = "esegui_coding_task"
POLICY_ACTION_SCRIBE_RESUME = "recupera_resume_progetto"
POLICY_ACTION_SCRIBE_DOC = "registra_osservazione_scribe"
POLICY_ACTION_INSPECT = "ispeziona_agenti"
POLICY_ACTION_WORKFLOW = "esegui_workflow"

# Riconoscimento deterministico dell'intento (niente LLM: stessa scelta di R3/R4)
_CODING_RE = re.compile(
    r"(?i)\b(fix|fixa|correggi|correggere|ripara|bug|errore|aggiungi|aggiungere|"
    r"implementa|implementare|crea|creare|sviluppa|sandbox|codice|coding|"
    r"patch|refactor|funzione|funzioni|test)\b"
)
_DOC_RE = re.compile(
    r"(?i)\b(documenta|documentare|documentalo|documentala|documentazione|"
    r"scribe|resume|diario|nota|note|memoria|registra)\b"
)

INTENT_CODING = "coding"
INTENT_DOC = "documentation"
INTENT_CODING_DOC = "coding+documentation"
INTENT_INSPECT = "inspect"

_AUTH_SENTINEL = object()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_safe(value: Any, limit: int = 4000) -> Any:
    """Tronca le stringhe lunghe (diff, output pytest) prima di salvarle."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n…[{len(value) - limit} caratteri troncati]"
    if isinstance(value, dict):
        return {str(k): _json_safe(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, limit) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _dumps(value: Any, limit: int = 4000) -> str:
    return json.dumps(_json_safe(value, limit), ensure_ascii=False, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
#  Percorsi
# ─────────────────────────────────────────────────────────────────────────────
def default_workflow_db() -> str:
    """Percorso del DB dei workflow (env CLIO_WORKFLOW_DB, default data/)."""
    env = (os.getenv(WORKFLOW_DB_ENV) or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "workflow_state.db")


def default_artifact_root() -> str:
    env = (os.getenv(WORKFLOW_ARTIFACTS_ENV) or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "workflows")


# ─────────────────────────────────────────────────────────────────────────────
#  Pianificazione deterministica
# ─────────────────────────────────────────────────────────────────────────────
def classify_goal(goal: str) -> str:
    """Classifica l'obiettivo nei 4 intenti gestiti (deterministico)."""
    text = (goal or "").strip()
    code = bool(_CODING_RE.search(text))
    doc = bool(_DOC_RE.search(text))
    if code and doc:
        return INTENT_CODING_DOC
    if code:
        return INTENT_CODING
    if doc:
        return INTENT_DOC
    return INTENT_INSPECT


def _plan_cost_snapshot(plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Costi paid/unverified inclusi nel consenso e nell'impronta del piano."""
    try:
        import cost_guard
    except Exception:
        return [
            {"step_id": step.get("step_id"),
             "action": step.get("policy_action") or step.get("action"),
             "status": "unverified"}
            for step in plan
        ]

    known_actions = set(cost_guard.FREE_ACTIONS)
    try:
        known_actions.update(
            policy.READ_ONLY | policy.REVERSIBLE | policy.SENSITIVE | policy.IRREVERSIBLE
        )
    except Exception:
        pass

    snapshot = []
    for step in plan:
        action = step.get("policy_action") or step.get("action") or ""
        if cost_guard.is_paid_action(action):
            snapshot.append({
                "step_id": step.get("step_id"),
                "action": action,
                "status": "paid",
                "estimate": cost_guard.estimated_cost(action),
                "unit": cost_guard.COST_UNITS.get(action, "USD"),
            })
        elif action not in known_actions:
            snapshot.append({
                "step_id": step.get("step_id"), "action": action,
                "status": "unverified",
            })
    return snapshot


def _plan_cost_summary(plan: List[Dict[str, Any]]) -> str:
    """Frase da mostrare prima del sì/no al piano, senza stime inventate."""
    snapshot = _plan_cost_snapshot(plan)
    if not snapshot:
        return "Nessun costo API a pagamento rilevato nel piano."

    usd = [item for item in snapshot
           if item.get("status") == "paid" and item.get("unit") == "USD"
           and item.get("estimate") is not None]
    variabili = [item for item in snapshot
                 if item.get("status") == "paid" and
                 (item.get("estimate") is None or item.get("unit") != "USD")]
    non_verificati = [item for item in snapshot if item.get("status") == "unverified"]

    parti = []
    if usd:
        totale = sum(float(item["estimate"]) for item in usd)
        azioni = ", ".join(item["action"] for item in usd)
        parti.append(f"${totale:.2f} USD stimati ({azioni})")
    for item in variabili:
        prezzo = "variabile/non determinabile" if item.get("estimate") is None else item["estimate"]
        parti.append(f"{item['action']}: {prezzo} {item.get('unit', 'unknown')}")
    if non_verificati:
        nomi = ", ".join(item["action"] for item in non_verificati)
        parti.append(f"costo non verificato per {nomi}")
    return "Costo stimato del piano: " + "; ".join(parti) + "."


def plan_hash(plan: List[Dict[str, Any]]) -> str:
    """Impronta del piano: lega il consenso ai passi e alle stime correnti."""
    material = json.dumps({
        "steps": [
            {k: step.get(k) for k in ("step_id", "agent", "action", "payload", "policy_action")}
            for step in plan
        ],
        "costs": _plan_cost_snapshot(plan),
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_plan(goal: str, context: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Piano deterministico: quali agenti, in che ordine, con quali parametri."""
    context = context or {}
    intent = classify_goal(goal)
    project = (context.get("project") or "").strip() or None
    plan: List[Dict[str, Any]] = []

    if intent in (INTENT_CODING, INTENT_CODING_DOC):
        plan.append({
            "step_id": "code_1",
            "agent": "coding_agent",
            "action": "edit_code",
            "policy_action": POLICY_ACTION_CODING,
            "payload": {"task": goal},
            "description": "Coding Agent: patch + test nella sandbox",
        })

    if intent in (INTENT_DOC, INTENT_CODING_DOC):
        plan.append({
            "step_id": "scribe_1",
            "agent": "scribe",
            "action": "document",
            "policy_action": POLICY_ACTION_SCRIBE_DOC,
            "payload": {
                "project": project or "",
                # Il segnaposto viene risolto in EXECUTE, dopo il passo di coding.
                "observation": "{{coding_summary}}",
            },
            "description": "The Scribe: registra l'esito del workflow",
        })

    if not plan:
        plan.append({
            "step_id": "inspect_1",
            "agent": "clio",
            "action": "inspect",
            "policy_action": POLICY_ACTION_INSPECT,
            "payload": {"request": goal},
            "description": "CLIO: ispezione di ambiente, sandbox e memoria",
        })

    return plan


def requires_confirmation(plan: List[Dict[str, Any]],
                          registry: Optional[AgentRegistry] = None) -> bool:
    """True se il piano contiene almeno un passo sensibile o irreversibile."""
    registry = registry or build_default_registry()
    for step in plan:
        if policy.classify(step.get("policy_action") or "") in (
                "irreversibile", "sensitive"):
            return True
        try:
            cap = registry.capability(step.get("agent"))
        except KeyError:
            return True          # agente sconosciuto -> fail-safe
        if cap.risk_level == "irreversible":
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  Autorizzazione (control-plane)
# ─────────────────────────────────────────────────────────────────────────────
def policy_authorize(goal: str, plan: List[Dict[str, Any]],
                     context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Chiede il permesso di eseguire il piano a `policy.guarded_call`.

    `esegui_workflow` è classificata irreversibile in policy.py: la conferma
    arriva dall'utente (Telegram o voce, in base all'origine). Il piano è
    riassunto negli argomenti, così la domanda mostra cosa verrà eseguito.

    Ritorna `{decision: yes|no|timeout, reason, via}`; non solleva mai.
    """
    context = context or {}
    steps_desc = [f"{s.get('agent')}:{s.get('action')}" for s in plan]
    cost_summary = _plan_cost_summary(plan)
    try:
        esito = policy.guarded_call(
            POLICY_ACTION_WORKFLOW,
            lambda **_: _AUTH_SENTINEL,
            {"goal": (goal or "")[:160], "steps": ", ".join(steps_desc)[:200],
             "cost_summary": cost_summary},
            origin=context.get("origin") or "workflow",
        )
    except Exception as exc:                      # pragma: no cover - difensivo
        return {"decision": "timeout", "via": "policy.guarded_call",
                "reason": f"gate non disponibile: {type(exc).__name__}: {exc}"}

    if esito is _AUTH_SENTINEL:
        return {"decision": "yes", "via": "policy.guarded_call",
                "reason": "conferma utente ottenuta"}

    testo = str(esito)
    if "annullata" in testo:
        return {"decision": "no", "via": "policy.guarded_call", "reason": testo}
    return {"decision": "timeout", "via": "policy.guarded_call", "reason": testo}


# ─────────────────────────────────────────────────────────────────────────────
#  Esito del workflow
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class WorkflowRun:
    workflow_id: str
    goal: str
    state: str
    status: str
    report: str = ""
    results: Dict[str, Any] = field(default_factory=dict)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    plan: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    resumed: bool = False
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "goal": self.goal,
            "state": self.state,
            "status": self.status,
            "report": self.report,
            "results": self.results,
            "artifacts": self.artifacts,
            "plan": self.plan,
            "error": self.error,
            "resumed": self.resumed,
            "duration_ms": self.duration_ms,
        }

    @property
    def success(self) -> bool:
        return self.status == "completed"


# ─────────────────────────────────────────────────────────────────────────────
#  Motore
# ─────────────────────────────────────────────────────────────────────────────
class WorkflowEngine:
    """Macchina a stati con checkpoint SQLite, timeout globale e recovery."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        registry: Optional[AgentRegistry] = None,
        timeout_seconds: int = DEFAULT_WORKFLOW_TIMEOUT,
        authorizer: Optional[Callable[..., Any]] = None,
        artifact_root: Optional[str] = None,
        origin: str = "workflow",
    ) -> None:
        self.db_path = db_path or default_workflow_db()
        self.registry = registry or build_default_registry()
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.authorizer = authorizer
        self.artifact_root = Path(artifact_root or default_artifact_root())
        self.origin = origin
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── persistenza ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL DEFAULT '[]',
                    authorization_json TEXT NOT NULL DEFAULT '{}',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    results_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deadline_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS steps (
                    workflow_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    seq INTEGER NOT NULL DEFAULT 0,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    output_json TEXT,
                    error TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    PRIMARY KEY (workflow_id, step_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoints_wf
                    ON checkpoints (workflow_id, id);
                CREATE INDEX IF NOT EXISTS idx_workflows_goal
                    ON workflows (goal, created_at);
                """
            )
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:      # pragma: no cover - ambienti esotici
                pass

    def _checkpoint(self, workflow_id: str, state: str, kind: str,
                    payload: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM checkpoints WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO checkpoints (workflow_id, seq, state, kind, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, seq, state, kind, _dumps(payload or {}), _now_iso()),
            )

    def _save(self, wf: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE workflows SET context_json=?, state=?, status=?, plan_json=?,"
                " authorization_json=?, verification_json=?, results_json=?,"
                " artifacts_json=?, error=?, updated_at=?, deadline_at=? WHERE id=?",
                (
                    _dumps(wf["context"]), wf["state"], wf["status"], _dumps(wf["plan"]),
                    _dumps(wf["authorization"]), _dumps(wf["verification"]),
                    _dumps(wf["results"]), _dumps(wf["artifacts"]),
                    wf.get("error"), _now_iso(), float(wf["deadline_at"]), wf["id"],
                ),
            )

    def _row_to_wf(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "goal": row["goal"],
            "context": _loads(row["context_json"], {}),
            "state": row["state"],
            "status": row["status"],
            "plan": _loads(row["plan_json"], []),
            "authorization": _loads(row["authorization_json"], {}),
            "verification": _loads(row["verification_json"], {}),
            "results": _loads(row["results_json"], {}),
            "artifacts": _loads(row["artifacts_json"], {}),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deadline_at": float(row["deadline_at"] or 0),
            "_resumed": False,
            "_t0": time.time(),
        }

    def _load(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return self._row_to_wf(row) if row else None

    def _find_resumable(self, goal: str) -> Optional[Dict[str, Any]]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM workflows WHERE goal=? AND status IN ({marks})"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (goal, *ACTIVE_STATUSES),
            ).fetchone()
        return self._row_to_wf(row) if row else None

    def _create(self, goal: str, context: Dict[str, Any]) -> Dict[str, Any]:
        wf_id = f"wf_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO workflows (id, goal, context_json, state, status, created_at,"
                " updated_at, deadline_at) VALUES (?, ?, ?, 'UNDERSTAND', 'running', ?, ?, ?)",
                (wf_id, goal, _dumps(context), now, now, time.time() + self.timeout_seconds),
            )
        return {
            "id": wf_id, "goal": goal, "context": dict(context), "state": "UNDERSTAND",
            "status": "running", "plan": [], "authorization": {}, "verification": {},
            "results": {}, "artifacts": {}, "error": None,
            "created_at": now, "updated_at": now,
            "deadline_at": time.time() + self.timeout_seconds,
            "_resumed": False, "_t0": time.time(),
        }

    # ── passi ────────────────────────────────────────────────────────────────
    def _sync_plan_steps(self, wf: Dict[str, Any]) -> None:
        """Registra i passi del piano (idempotente)."""
        with self._connect() as conn:
            for seq, step in enumerate(wf["plan"], start=1):
                conn.execute(
                    "INSERT INTO steps (workflow_id, step_id, seq, agent, action,"
                    " payload_json, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')"
                    " ON CONFLICT(workflow_id, step_id) DO NOTHING",
                    (wf["id"], step["step_id"], seq, step["agent"], step["action"],
                     _dumps(step.get("payload") or {})),
                )

    def _step_status(self, workflow_id: str, step_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM steps WHERE workflow_id=? AND step_id=?",
                (workflow_id, step_id),
            ).fetchone()
        return row["status"] if row else None

    def _mark_step(self, wf: Dict[str, Any], step: Dict[str, Any], status: str,
                   output: Any = None, error: Optional[str] = None) -> None:
        now = _now_iso()
        started = now if status == "running" else None
        finished = now if status in ("done", "failed", "interrupted", "cancelled") else None
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO steps (workflow_id, step_id, seq, agent, action, payload_json,"
                " status, output_json, error, started_at, finished_at)"
                " VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(workflow_id, step_id) DO UPDATE SET"
                "  status=excluded.status,"
                "  output_json=COALESCE(excluded.output_json, steps.output_json),"
                "  error=excluded.error,"
                "  started_at=COALESCE(steps.started_at, excluded.started_at),"
                "  finished_at=COALESCE(excluded.finished_at, steps.finished_at)",
                (wf["id"], step["step_id"], step["agent"], step["action"],
                 _dumps(step.get("payload") or {}), status,
                 _dumps(output) if output is not None else None, error,
                 started, finished),
            )

    def _steps(self, workflow_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM steps WHERE workflow_id=? ORDER BY seq, step_id",
                (workflow_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history(self, workflow_id: str) -> List[Dict[str, Any]]:
        """Journal dei checkpoint (utile per debug e per il test di recovery)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE workflow_id=? ORDER BY id",
                (workflow_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self, workflow_id: str) -> Dict[str, Any]:
        """Snapshot completo di un workflow (o KeyError se non esiste)."""
        wf = self._load(workflow_id)
        if wf is None:
            raise KeyError(f"workflow '{workflow_id}' inesistente in {self.db_path}")
        wf["steps"] = self._steps(workflow_id)
        wf["checkpoints"] = len(self.history(workflow_id))
        wf.pop("_t0", None)
        return wf

    def list_workflows(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, goal, state, status, created_at, updated_at, error"
                " FROM workflows ORDER BY rowid DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── utilità ──────────────────────────────────────────────────────────────
    def _deadline_exceeded(self, wf: Dict[str, Any]) -> bool:
        return time.time() > float(wf["deadline_at"])

    def _remaining(self, wf: Dict[str, Any]) -> float:
        return float(wf["deadline_at"]) - time.time()

    def _step_grant(self, wf: Dict[str, Any]) -> Callable[[str, Dict[str, Any]], str]:
        """Conferma già ottenuta in AUTHORIZE, validata sull'impronta del piano."""
        auth = wf.get("authorization") or {}
        current = plan_hash(wf["plan"])

        def _grant(name: str, args: Dict[str, Any]) -> str:
            if auth.get("decision") == "yes" and auth.get("plan_hash") == current:
                return "yes"
            logger.warning("[WF] conferma non valida per il passo %r: non eseguo", name)
            return "timeout"

        return _grant

    def _coding_summary(self, wf: Dict[str, Any]) -> str:
        """Testo che il passo Scribe documenta (esito reale del coding)."""
        for step in wf["plan"]:
            if step.get("agent") != "coding_agent":
                continue
            result = wf["results"].get(step["step_id"]) or {}
            out = result.get("output") or {}
            if not out:
                return f"Obiettivo: {wf['goal']}\nIl passo di coding non ha prodotto risultati."
            test_out = (out.get("test_output") or "").strip().splitlines()
            ultima = test_out[-1] if test_out else "(pytest non eseguito)"
            diff = (out.get("diff") or "").strip()
            return (
                f"Obiettivo: {wf['goal']}\n"
                f"Workflow: {wf['id']}\n"
                f"Esito coding: {'OK' if out.get('success') else 'FALLITO'}\n"
                f"Test aggiornati: {'sì' if out.get('tests_touched') else 'no'}\n"
                f"pytest: {ultima}\n"
                f"Motivo: {out.get('error') or '-'}\n"
                + (f"\nDiff:\n{diff[:1200]}" if diff else "")
            )
        return f"Obiettivo: {wf['goal']}\nWorkflow {wf['id']} completato."

    def _resolve_payload(self, step: Dict[str, Any], wf: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        for key, value in (step.get("payload") or {}).items():
            if isinstance(value, str):
                value = value.replace("{{goal}}", wf["goal"])
                value = value.replace("{{coding_summary}}", self._coding_summary(wf))
            payload[key] = value
        return payload

    # ── stati ────────────────────────────────────────────────────────────────
    def _state_understand(self, wf: Dict[str, Any]) -> Optional[str]:
        understanding = {
            "goal": wf["goal"],
            "intent": classify_goal(wf["goal"]),
            "project": (wf["context"].get("project") or "").strip() or None,
            "at": _now_iso(),
        }
        wf["context"]["understanding"] = understanding
        logger.info("[WF] UNDERSTAND: %s", understanding)
        return "GROUND"

    def _state_ground(self, wf: Dict[str, Any]) -> Optional[str]:
        grounding: Dict[str, Any] = {"at": _now_iso()}
        if "clio" in self.registry:
            esito = self.registry.run("clio", "inspect", {"request": wf["goal"]})
            grounding["env"] = esito.get("output") or {}
            grounding["env_ok"] = bool(esito.get("ok"))
            grounding["env_error"] = esito.get("error")
            sandbox = (esito.get("output") or {}).get("sandbox")
            if sandbox:
                grounding["sandbox_dirty"] = _git_dirty(sandbox)
        project = (wf["context"].get("understanding") or {}).get("project")
        if project and "scribe" in self.registry:
            esito = self.registry.run("scribe", "resume", {"project": project})
            out = esito.get("output") or {}
            grounding["scribe"] = {
                "project": out.get("project") or project,
                "available": bool(out.get("available") or out.get("resume")),
                "offline": bool(out.get("offline")),
                "error": out.get("error") or esito.get("error"),
                "resume_preview": (out.get("resume") or "")[:400] or None,
            }
        wf["context"]["grounding"] = grounding
        logger.info("[WF] GROUND: %s", {k: v for k, v in grounding.items() if k != "env"})
        return "PLAN"

    def _state_plan(self, wf: Dict[str, Any]) -> Optional[str]:
        if not wf["plan"]:
            wf["plan"] = build_plan(wf["goal"], wf["context"])
        self._sync_plan_steps(wf)
        self._save(wf)
        logger.info("[WF] PLAN: %s", [f"{s['agent']}:{s['action']}" for s in wf["plan"]])
        return "AUTHORIZE"

    def _state_authorize(self, wf: Dict[str, Any]) -> Optional[str]:
        plan = wf["plan"]
        needs = requires_confirmation(plan, self.registry)
        authorization: Dict[str, Any] = {
            "plan_hash": plan_hash(plan),
            "requires_confirmation": needs,
            "at": _now_iso(),
        }
        if not needs:
            authorization.update({"decision": "yes", "via": "policy.read-only",
                                  "reason": "piano di sola lettura: nessuna conferma richiesta"})
        else:
            authorizer = self.authorizer or policy_authorize
            context = dict(wf["context"])
            context.setdefault("origin", self.origin)
            try:
                esito = authorizer(wf["goal"], plan, context)
            except Exception as exc:              # pragma: no cover - difensivo
                esito = {"decision": "timeout", "reason": f"{type(exc).__name__}: {exc}",
                         "via": "authorizer-error"}
            if isinstance(esito, str):
                esito = {"decision": esito, "reason": esito, "via": "authorizer"}
            authorization.update({
                "decision": str(esito.get("decision") or "timeout"),
                "reason": esito.get("reason"),
                "via": esito.get("via"),
            })

        wf["authorization"] = authorization
        self._checkpoint(wf["id"], "AUTHORIZE", "authorization", authorization)
        logger.info("[WF] AUTHORIZE: %s", authorization)

        if needs and authorization["decision"] != "yes":
            wf["status"] = "cancelled"
            wf["error"] = (f"piano non autorizzato ({authorization['decision']}): "
                           f"{authorization.get('reason')}")
            self._save(wf)
            return "REPORT"
        return "EXECUTE"

    def _state_execute(self, wf: Dict[str, Any]) -> Optional[str]:
        grant = self._step_grant(wf)
        for step in wf["plan"]:
            status = self._step_status(wf["id"], step["step_id"])
            if status == "done":
                logger.info("[WF] EXECUTE: passo %s già completato, salto", step["step_id"])
                continue
            if status in ("running", "failed", "interrupted"):
                # Ripresa dopo un crash: il passo era in corso, va rieseguito.
                self._checkpoint(wf["id"], "EXECUTE", "step_interrupted",
                                 {"step": step["step_id"], "previous": status})
                self._mark_step(wf, step, "interrupted")

            if self._deadline_exceeded(wf):
                wf["status"] = "timeout"
                wf["error"] = (f"timeout globale ({self.timeout_seconds}s) prima del passo "
                               f"{step['step_id']}")
                self._save(wf)
                return None

            payload = self._resolve_payload(step, wf)
            agent_budget = max(1, int(min(
                self.registry.capability(step["agent"]).timeout_seconds
                if step["agent"] in self.registry else 60,
                max(1, self._remaining(wf)),
            )))

            self._mark_step(wf, step, "running")
            self._checkpoint(wf["id"], "EXECUTE", "step_start",
                             {"step": step["step_id"], "agent": step["agent"],
                              "action": step["action"], "budget_s": agent_budget})

            def _runner(**kwargs):
                return self.registry.run(step["agent"], step["action"], kwargs,
                                         timeout=agent_budget)

            esito = policy.guarded_call(
                step.get("policy_action") or step["action"],
                _runner, payload, confirm=grant, origin=self.origin,
            )

            # Il gateway risponde con una frase quando rifiuta o scade.
            if isinstance(esito, str):
                self._mark_step(wf, step, "cancelled", error=esito)
                wf["status"] = "cancelled"
                wf["error"] = esito
                self._checkpoint(wf["id"], "EXECUTE", "step_cancelled",
                                 {"step": step["step_id"], "reason": esito})
                self._save(wf)
                return "REPORT"

            wf["results"][step["step_id"]] = esito
            status = "done" if esito.get("ok") else "failed"
            self._mark_step(wf, step, status, output=esito, error=esito.get("error"))
            self._checkpoint(wf["id"], "EXECUTE", "step_done",
                             {"step": step["step_id"], "ok": bool(esito.get("ok")),
                              "timed_out": bool(esito.get("timed_out")),
                              "error": esito.get("error")})
            self._save(wf)
            logger.info("[WF] EXECUTE %s -> ok=%s error=%s",
                        step["step_id"], esito.get("ok"), esito.get("error"))
        return "VERIFY"

    def _verifiers(self) -> Dict[str, Callable[[Dict[str, Any]], List[Dict[str, Any]]]]:
        def _coding(result: Dict[str, Any]) -> List[Dict[str, Any]]:
            out = result.get("output") or {}
            test_out = str(out.get("test_output") or "")
            return [
                {"check": "agente_ok", "ok": bool(result.get("ok")),
                 "detail": result.get("error") or "agente completato"},
                {"check": "non_timeout", "ok": not result.get("timed_out"),
                 "detail": "nessun timeout" if not result.get("timed_out") else "timeout"},
                {"check": "patch_prodotta", "ok": bool((out.get("diff") or "").strip()),
                 "detail": "diff presente" if out.get("diff") else "nessuna modifica"},
                {"check": "test_aggiornati", "ok": bool(out.get("tests_touched")),
                 "detail": "file test_*.py nel diff" if out.get("tests_touched")
                           else "nessun file di test nel diff"},
                {"check": "pytest_verde", "ok": bool(out.get("success")) and "passed" in test_out,
                 "detail": (test_out.strip().splitlines() or ["pytest non eseguito"])[-1]},
            ]

        def _scribe(result: Dict[str, Any]) -> List[Dict[str, Any]]:
            out = result.get("output") or {}
            recorded = out.get("recorded")
            return [{
                "check": "osservazione_registrata",
                "ok": bool(result.get("ok")) and bool(recorded or out.get("ok")),
                "detail": (f"registrata in {recorded}" + (f" ({out.get('path')})" if out.get("path") else "")
                           if recorded else (result.get("error") or "non registrata")),
            }]

        return {"coding_agent": _coding, "scribe": _scribe}

    def _state_verify(self, wf: Dict[str, Any]) -> Optional[str]:
        verifiers = self._verifiers()
        checks: List[Dict[str, Any]] = []
        hard_ok = True
        for step in wf["plan"]:
            result = wf["results"].get(step["step_id"]) or {}
            verifier = verifiers.get(step["agent"])
            if verifier is None:
                continue
            step_checks = verifier(result)
            capability: Optional[AgentCapability] = None
            if step["agent"] in self.registry:
                capability = self.registry.capability(step["agent"])
            required = bool(capability.verification_required) or step["agent"] == "coding_agent"
            step_ok = all(c["ok"] for c in step_checks)
            for check in step_checks:
                check.update({"step": step["step_id"], "agent": step["agent"],
                              "required": required})
                checks.append(check)
            if required and not step_ok:
                hard_ok = False

        wf["verification"] = {"ok": hard_ok, "checks": checks, "at": _now_iso()}
        self._checkpoint(wf["id"], "VERIFY", "verification", {"ok": hard_ok})
        if not hard_ok and wf["status"] == "running":
            fallite = [c["check"] for c in checks if c["required"] and not c["ok"]]
            wf["status"] = "failed"
            wf["error"] = "verifica fallita: " + ", ".join(fallite)
        self._save(wf)
        logger.info("[WF] VERIFY ok=%s", hard_ok)
        return "PUBLISH"

    def _state_publish(self, wf: Dict[str, Any]) -> Optional[str]:
        art_dir = self.artifact_root / wf["id"]
        artifacts: Dict[str, Any] = {"dir": str(art_dir)}
        try:
            art_dir.mkdir(parents=True, exist_ok=True)
            coding = None
            for step in wf["plan"]:
                if step.get("agent") == "coding_agent":
                    coding = wf["results"].get(step["step_id"]) or {}
            if coding:
                out = coding.get("output") or {}
                diff = (out.get("diff") or "").strip()
                if diff:
                    patch = art_dir / "code.patch"
                    patch.write_text(diff + "\n", encoding="utf-8")
                    artifacts["patch"] = str(patch)
                tests = (out.get("test_output") or "").strip()
                if tests:
                    test_log = art_dir / "pytest.log"
                    test_log.write_text(tests + "\n", encoding="utf-8")
                    artifacts["pytest_log"] = str(test_log)
            for step in wf["plan"]:
                if step.get("agent") != "scribe":
                    continue
                out = (wf["results"].get(step["step_id"]) or {}).get("output") or {}
                if out.get("path"):
                    artifacts["observation"] = out["path"]
                elif out.get("resume"):
                    resume_file = art_dir / "scribe_resume.md"
                    resume_file.write_text(str(out["resume"]), encoding="utf-8")
                    artifacts["scribe_resume"] = str(resume_file)

            snapshot = art_dir / "state.json"
            snapshot.write_text(json.dumps({
                "workflow_id": wf["id"],
                "goal": wf["goal"],
                "state": wf["state"],
                "status": wf["status"],
                "plan": wf["plan"],
                "authorization": wf["authorization"],
                "verification": wf["verification"],
                "results": _json_safe(wf["results"]),
                "artifacts": artifacts,
                "error": wf["error"],
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            artifacts["snapshot"] = str(snapshot)
        except Exception as exc:
            artifacts["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[WF] PUBLISH parziale: %s", artifacts["error"])

        wf["artifacts"] = artifacts
        self._checkpoint(wf["id"], "PUBLISH", "artifacts", artifacts)
        self._save(wf)
        return "REPORT"

    def _state_report(self, wf: Dict[str, Any]) -> Optional[str]:
        # Lo stato finale deve essere già scritto nel report (non "in corso").
        if wf["status"] == "running":
            wf["status"] = "completed"
        report = self._build_report(wf)
        art_dir = self.artifact_root / wf["id"]
        try:
            art_dir.mkdir(parents=True, exist_ok=True)
            path = art_dir / "report.md"
            path.write_text(report, encoding="utf-8")
            wf["artifacts"]["report"] = str(path)
        except Exception as exc:                  # pragma: no cover - difensivo
            logger.warning("[WF] report non scritto su disco: %s", exc)

        wf["state"] = TERMINAL_STATE
        self._checkpoint(wf["id"], "REPORT", "report", {"status": wf["status"],
                                                       "chars": len(report)})
        self._save(wf)
        return None

    # ── report ───────────────────────────────────────────────────────────────
    def _build_report(self, wf: Dict[str, Any]) -> str:
        esito = {
            "completed": "✅ COMPLETATO",
            "failed": "❌ FALLITO (verifica)",
            "cancelled": "🛑 ANNULLATO",
            "timeout": "⏱️ IN TIMEOUT (ripartibile)",
            "running": "… in corso",
        }.get(wf["status"], wf["status"])

        righe = [
            f"# Workflow {wf['id']}",
            "",
            f"- Obiettivo: {wf['goal']}",
            f"- Esito: {esito}",
            f"- Stato macchina: {wf['state']}",
            f"- Creato: {wf.get('created_at')} · aggiornato: {_now_iso()}",
            f"- Progetto: {(wf['context'].get('understanding') or {}).get('project') or '-'}",
        ]
        if wf.get("error"):
            righe.append(f"- Errore: {wf['error']}")

        auth = wf.get("authorization") or {}
        if auth:
            righe += [
                "",
                "## Autorizzazione",
                f"- conferma richiesta: {'sì' if auth.get('requires_confirmation') else 'no'}",
                f"- decisione: {auth.get('decision')} (via {auth.get('via')})",
                f"- motivo: {auth.get('reason')}",
                f"- impronta piano: {auth.get('plan_hash')}",
            ]

        righe += ["", "## Piano ed esecuzione"]
        for step in wf["plan"]:
            res = wf["results"].get(step["step_id"]) or {}
            out = res.get("output") or {}
            righe.append(
                f"- **{step['step_id']}** `{step['agent']}:{step['action']}` — "
                f"{'ok' if res.get('ok') else 'ko'}"
                + (f" ({res.get('duration_ms')} ms)" if res.get("duration_ms") is not None else "")
                + (f" · {res.get('error')}" if res.get("error") else "")
            )
            if step["agent"] == "coding_agent" and out:
                test_line = (str(out.get("test_output") or "").strip().splitlines() or ["-"])[-1]
                righe.append(f"  - patch: {'sì' if out.get('diff') else 'no'} · "
                             f"test aggiornati: {'sì' if out.get('tests_touched') else 'no'} · "
                             f"{test_line}")
            if step["agent"] == "scribe" and out:
                righe.append(f"  - registrata: {out.get('recorded') or out.get('ok')}"
                             + (f" → {out.get('path')}" if out.get("path") else ""))

        ver = wf.get("verification") or {}
        if ver.get("checks"):
            righe += ["", "## Verifica"]
            for check in ver["checks"]:
                segno = "✅" if check.get("ok") else "❌"
                righe.append(f"- {segno} {check.get('step')} · {check.get('check')}"
                             f" — {check.get('detail')}")

        ground = (wf["context"].get("grounding") or {})
        if ground:
            righe += ["", "## Contesto raccolto (GROUND)"]
            env = ground.get("env") or {}
            if env:
                righe.append(f"- sandbox: {env.get('sandbox')} "
                             f"(esiste: {env.get('sandbox_exists')}, "
                             f"modifiche pendenti: {ground.get('sandbox_dirty')})")
            scribe = ground.get("scribe") or {}
            if scribe:
                righe.append(f"- The Scribe: progetto {scribe.get('project')}, "
                             f"resume {'disponibile' if scribe.get('available') else 'assente'}"
                             + (" (offline)" if scribe.get("offline") else ""))

        if wf.get("artifacts"):
            righe += ["", "## Artefatti"]
            for name, path in sorted(wf["artifacts"].items()):
                if name == "dir":
                    continue
                righe.append(f"- {name}: {path}")

        righe += ["", "## Timeline dei checkpoint"]
        for cp in self.history(wf["id"]):
            righe.append(f"- {cp['seq']:>2}. {cp['state']:<10} {cp['kind']:<16} {cp['created_at']}")

        return "\n".join(righe) + "\n"

    # ── API principale ───────────────────────────────────────────────────────
    def execute_workflow(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        workflow_id: Optional[str] = None,
        authorizer: Optional[Callable[..., Any]] = None,
        resume: Optional[bool] = None,
    ) -> WorkflowRun:
        """Esegue (o riprende) un workflow multi-agente.

        Args:
            goal: obiettivo in linguaggio naturale.
            context: dati di partenza (`project`, `origin`, note libere).
            workflow_id: riprende un workflow specifico.
            authorizer: callable(goal, plan, context) -> decisione. Default:
                        `policy_authorize` (control-plane).
            resume: True = cerca un workflow interrotto con lo stesso goal;
                    False = ne crea sempre uno nuovo; None = automatico
                    (riprende se esiste, altrimenti crea).

        Non solleva eccezioni di esecuzione: i problemi diventano `status` +
        `error` nel `WorkflowRun`. `ValueError`/`KeyError` solo per input invalidi.
        """
        goal = (goal or "").strip()
        context = dict(context or {})
        if not goal and not workflow_id:
            raise ValueError("goal vuoto: impossibile eseguire un workflow")

        if authorizer is not None:
            self.authorizer = authorizer

        if workflow_id:
            wf = self._load(workflow_id)
            if wf is None:
                raise KeyError(f"workflow '{workflow_id}' inesistente in {self.db_path}")
            wf["_resumed"] = True
        elif resume is False:
            wf = self._create(goal, context)
        else:
            wf = self._find_resumable(goal)
            if wf is not None:
                wf["_resumed"] = True
                logger.info("[WF] ripresa dal checkpoint %s (%s)", wf["state"], wf["id"])
            else:
                wf = self._create(goal, context)

        # Budget nuovo a ogni avvio (recovery = nuova finestra di tempo).
        if wf["_resumed"] and wf["state"] != TERMINAL_STATE:
            wf["deadline_at"] = time.time() + self.timeout_seconds
            if wf["status"] in ("timeout",):
                wf["status"] = "running"
                wf["error"] = None
        elif not wf["_resumed"]:
            wf["context"].update(context)

        resumed = wf["_resumed"]
        t0 = wf["_t0"]
        self._checkpoint(wf["id"], wf["state"], "resume" if resumed else "start",
                         {"goal": goal, "context": _json_safe(context, 500)})
        self._save(wf)

        while wf["state"] != TERMINAL_STATE:
            if self._deadline_exceeded(wf):
                wf["status"] = "timeout"
                wf["error"] = (f"timeout globale ({self.timeout_seconds}s) prima di "
                               f"{wf['state']}")
                self._save(wf)
                self._checkpoint(wf["id"], wf["state"], "timeout", {})
                logger.warning("[WF] %s", wf["error"])
                break

            state = wf["state"]
            self._checkpoint(wf["id"], state, "state_enter")
            handler = getattr(self, f"_state_{state.lower()}", None)
            if handler is None:                   # pragma: no cover - difensivo
                wf["status"] = "failed"
                wf["error"] = f"stato sconosciuto: {state}"
                self._save(wf)
                break

            try:
                nxt = handler(wf)
            except Exception as exc:
                wf["status"] = "failed"
                wf["error"] = f"{state}: {type(exc).__name__}: {exc}"
                self._save(wf)
                self._checkpoint(wf["id"], state, "error", {"error": wf["error"]})
                logger.exception("[WF] errore in %s", state)
                break

            if nxt is None:
                break
            self._checkpoint(wf["id"], state, "state_done", {"next": nxt})
            wf["state"] = nxt
            self._save(wf)

        report = _read_text((wf.get("artifacts") or {}).get("report")) or self._build_report(wf)
        return WorkflowRun(
            workflow_id=wf["id"],
            goal=wf["goal"],
            state=wf["state"],
            status=wf["status"],
            report=report or "",
            results=wf["results"],
            artifacts=wf["artifacts"],
            plan=wf["plan"],
            error=wf["error"],
            resumed=resumed,
            duration_ms=int((time.time() - t0) * 1000),
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Helper
# ─────────────────────────────────────────────────────────────────────────────
def _read_text(path: Optional[str]) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return ""


def _git_dirty(path: str) -> Optional[bool]:
    """True/False se la workspace è un repo git con modifiche pendenti."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=path,
                              capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return None
        return bool((proc.stdout or "").strip())
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Azioni CLIO (registry.AZIONI_MAP / policy)
# ─────────────────────────────────────────────────────────────────────────────
def esegui_workflow(goal: str, progetto: str = "", confirmed: bool = False) -> str:
    """Azione CLIO: esegue un workflow multi-agente e restituisce il report.

    Registrata in registry.AZIONI_MAP e classificata IRREVERSIBILE in policy.py:
    passa da `policy.guarded_call`. Quando il gateway ha già ottenuto la conferma
    (`confirmed=True` iniettato da policy._invoke) il motore non richiede una
    seconda approvazione per lo stesso piano.
    """
    context = {"project": (progetto or "").strip() or None, "origin": "workflow"}
    authorizer = None
    if confirmed:
        authorizer = lambda _goal, _plan, _ctx: {           # noqa: E731
            "decision": "yes", "via": "policy.guarded_call",
            "reason": "esegui_workflow già autorizzato dal control-plane",
        }
    engine = WorkflowEngine()
    run = engine.execute_workflow(goal, context, authorizer=authorizer)
    return run.report


def stato_workflow(workflow_id: str = "") -> str:
    """Azione CLIO: stato di un workflow (o elenco degli ultimi)."""
    engine = WorkflowEngine()
    if not (workflow_id or "").strip():
        righe = ["Ultimi workflow:"]
        for row in engine.list_workflows(limit=5):
            righe.append(f"- {row['id']} · {row['status']} · stato {row['state']} · {row['goal'][:60]}")
        if len(righe) == 1:
            righe.append("- nessun workflow registrato")
        return "\n".join(righe)

    try:
        info = engine.status(workflow_id.strip())
    except KeyError as exc:
        return f"❌ {exc}"
    righe = [
        f"Workflow {info['id']}",
        f"- obiettivo: {info['goal']}",
        f"- stato: {info['state']} / {info['status']}",
        f"- creato: {info['created_at']} · aggiornato: {info['updated_at']}",
    ]
    if info.get("error"):
        righe.append(f"- errore: {info['error']}")
    righe.append("- passi:")
    for step in info.get("steps") or []:
        righe.append(f"  - {step['step_id']} ({step['agent']}:{step['action']}): {step['status']}")
    righe.append(f"- checkpoint: {info.get('checkpoints')}")
    if info.get("artifacts", {}).get("report"):
        righe.append(f"- report: {info['artifacts']['report']}")
    return "\n".join(righe)
