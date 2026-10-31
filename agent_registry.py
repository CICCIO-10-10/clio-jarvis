"""
agent_registry.py — R5: Agent Registry di CLIO.

Registro delle CAPACITÀ degli agenti che CLIO può orchestrare. Non esegue nulla
fuori dal control-plane: `AgentRegistry.run()` è un dispatcher, e chi lo chiama
(`workflow_engine`) lo avvolge in `policy.guarded_call`.

Agenti registrati:
  - clio          ispezione/routing locale (read-only)
  - coding_agent  patch + test DENTRO la sandbox (irreversibile, verifica pytest)
  - scribe        memoria di The Scribe via ScribeBridge (read-only)

Ogni agente dichiara:
  id, capabilities, input_schema, output_schema, risk_level, permissions,
  timeout_seconds, verification_required

`risk_level` riusa la tassonomia di policy.py ("read-only" / "reversible" /
"irreversible"): il Workflow Engine decide con essa se un piano richiede una
conferma esplicita dell'utente.

Contratto del registro:
    registry = build_default_registry()
    esito = registry.run("coding_agent", "edit_code", {"task": "aggiungi somma"})
    # -> {"ok": bool, "agent": str, "action": str, "output": dict,
    #     "error": str|None, "duration_ms": int, "timed_out": bool}
    #    `ok` = l'agente ha risposto senza eccezioni e senza timeout.
    #    Il GIUDIZIO sul risultato (test verdi, diff sensato) è della fase VERIFY.

Nessun import pesante a livello di modulo: coding_agent e scribe_control vengono
importati dentro gli executor, così i test possono iniettare un registry finto
senza pagare l'import di requests/model_router.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ClioAgents")

# Tassonomia allineata a policy.classify()
RISK_LEVELS = ("read-only", "reversible", "irreversible")

Executor = Callable[[str, Dict[str, Any]], Dict[str, Any]]


@dataclass(frozen=True)
class AgentCapability:
    """Descrizione dichiarativa di un agente orchestrabile."""

    id: str
    capabilities: List[str]
    input_schema: Dict[str, str]
    output_schema: Dict[str, str]
    risk_level: str = "read-only"
    permissions: List[str] = field(default_factory=list)
    timeout_seconds: int = 120
    verification_required: bool = False
    description: str = ""

    def __post_init__(self):
        if not str(self.id or "").strip():
            raise ValueError("AgentCapability.id non può essere vuoto")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(
                f"risk_level '{self.risk_level}' non valido per agente '{self.id}': "
                f"usa uno di {RISK_LEVELS}"
            )
        if int(self.timeout_seconds) <= 0:
            raise ValueError(f"timeout_seconds deve essere > 0 (agente '{self.id}')")
        if not isinstance(self.capabilities, (list, tuple)) or not self.capabilities:
            raise ValueError(f"capabilities deve essere una lista non vuota (agente '{self.id}')")

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def supports(self, capability: str) -> bool:
        return (capability or "").strip().lower() in {
            str(c).strip().lower() for c in self.capabilities
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Executor degli agenti reali
# ─────────────────────────────────────────────────────────────────────────────
def _clio_executor(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """CLIO che ispeziona se stessa: ambiente, sandbox, stato di The Scribe."""
    import coding_agent
    import scribe_control

    if action not in ("inspect", "stato", "status"):
        raise ValueError(f"azione sconosciuta per l'agente clio: {action!r}")

    sandbox = coding_agent.get_sandbox_root()
    return {
        "sandbox": sandbox,
        "sandbox_exists": os.path.isdir(sandbox),
        "scribe_available": bool(scribe_control.SCRIBE_AVAILABLE),
        "python": sys.version.split()[0],
        "cwd": os.getcwd(),
        "requested": payload.get("request") or "",
    }


def _coding_executor(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coding Agent: patch + test, SEMPRE e solo dentro la sandbox."""
    import coding_agent

    if action not in ("edit_code", "coding", "fix"):
        raise ValueError(f"azione sconosciuta per il coding agent: {action!r}")

    task = payload.get("task") or payload.get("goal") or ""
    workspace = payload.get("workspace")
    # Difesa in profondità: run_coding_task rifiuta già i percorsi esterni,
    # ma qui blocchiamo prima ancora di chiamare l'agente.
    _, err = coding_agent._resolve_sandbox_workspace(workspace)
    if err:
        raise PermissionError(err)

    esito = coding_agent.run_coding_task(
        task,
        workspace=workspace,
        timeout=int(payload.get("timeout") or coding_agent.TASK_TIMEOUT),
    )
    # Ritorniamo il dict completo: VERIFY legge success/tests_touched/test_output.
    return dict(esito)


def _scribe_executor(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The Scribe tramite ScribeBridge: lettura resume / registrazione osservazione.

    Integrazione di SOLA LETTURA verso il vault di The Scribe (decisione R1):
    CLIO non scrive note, non finalizza sessioni, non modifica il DB del daemon.
    L'osservazione prodotta dal workflow viene quindi registrata in un OUTBOX
    locale di CLIO (`data/scribe_outbox/`); se il ScribeBridge installato espone
    `record_observation` viene usato quello, altrimenti resta l'outbox.
    """
    import scribe_control

    project = (payload.get("project") or payload.get("project_id") or "").strip()

    if action in ("resume", "get_resume", "leggi_resume"):
        info = scribe_control.read_resume(project)
        return {
            "ok": info.get("available", False),
            "available": info.get("available", False),
            "project": info.get("project_id") or project,
            "resume": info.get("resume"),
            "offline": info.get("offline", False),
            "error": info.get("error"),
        }

    if action in ("document", "record_observation", "documenta"):
        testo = payload.get("observation") or payload.get("text") or ""
        if not testo.strip():
            raise ValueError("osservazione vuota: niente da documentare")

        esito = scribe_control.record_observation(project, testo)
        return dict(esito)

    raise ValueError(f"azione sconosciuta per l'agente scribe: {action!r}")


# ─────────────────────────────────────────────────────────────────────────────
#  Registry
# ─────────────────────────────────────────────────────────────────────────────
class AgentRegistry:
    """Mappa `agent_id -> (AgentCapability, executor)`."""

    def __init__(self) -> None:
        self._agents: Dict[str, tuple] = {}

    # ── registrazione ──
    def register(self, capability: AgentCapability, executor: Executor) -> AgentCapability:
        if not isinstance(capability, AgentCapability):
            raise TypeError("register() richiede un AgentCapability")
        if not callable(executor):
            raise TypeError(f"executor non chiamabile per l'agente '{capability.id}'")
        self._agents[capability.id] = (capability, executor)
        logger.info("Agente registrato: %s (risk=%s)", capability.id, capability.risk_level)
        return capability

    # ── lettura ──
    def agent_ids(self) -> List[str]:
        return sorted(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._agents

    def capability(self, agent_id: str) -> AgentCapability:
        try:
            return self._agents[agent_id][0]
        except KeyError:
            raise KeyError(
                f"agente '{agent_id}' non registrato (disponibili: {', '.join(self.agent_ids())})"
            ) from None

    def executor(self, agent_id: str) -> Executor:
        self.capability(agent_id)  # KeyError leggibile
        return self._agents[agent_id][1]

    def find(self, capability: str) -> List[str]:
        """Agenti che dichiarano `capability` (es. 'code.edit')."""
        return [aid for aid in self.agent_ids() if self._agents[aid][0].supports(capability)]

    def describe(self) -> Dict[str, Dict[str, Any]]:
        """Snapshot serializzabile del registry (per LLM, UI, report)."""
        return {aid: cap.as_dict() for aid, (cap, _fn) in sorted(self._agents.items())}

    # ── esecuzione ──
    def run(
        self,
        agent_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Esegue `action` sull'agente con un timeout reale. Non solleva MAI."""
        cap = self.capability(agent_id)
        fn = self.executor(agent_id)
        payload = dict(payload or {})

        budget = int(cap.timeout_seconds if not timeout else min(int(timeout), cap.timeout_seconds))
        budget = max(1, budget)

        esito: Dict[str, Any] = {
            "ok": False,
            "agent": agent_id,
            "action": action,
            "output": {},
            "error": None,
            "duration_ms": 0,
            "timed_out": False,
        }

        box: Dict[str, Any] = {}
        t0 = time.time()

        def _worker():
            try:
                box["value"] = fn(action, payload)
            except BaseException as exc:  # noqa: BLE001 - l'agente non deve abbattere CLIO
                box["error"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=_worker, name=f"clio-agent-{agent_id}", daemon=True)
        th.start()
        th.join(budget)
        esito["duration_ms"] = int((time.time() - t0) * 1000)

        if th.is_alive():
            esito["timed_out"] = True
            esito["error"] = f"timeout: l'agente '{agent_id}' non ha risposto entro {budget}s"
            logger.warning("⏱️  %s", esito["error"])
            return esito

        if "error" in box:
            esito["error"] = box["error"]
            logger.error("Agente '%s' ha fallito: %s", agent_id, esito["error"])
            return esito

        value = box.get("value")
        if isinstance(value, dict):
            esito["output"] = value
        elif value is None:
            esito["output"] = {}
        else:
            esito["output"] = {"result": value}

        esito["ok"] = True
        return esito


# ─────────────────────────────────────────────────────────────────────────────
#  Registry di default: CLIO + Coding Agent (sandbox) + The Scribe
# ─────────────────────────────────────────────────────────────────────────────
CLIO_CAPABILITY = AgentCapability(
    id="clio",
    capabilities=["self.inspect", "route", "state.read"],
    input_schema={"request": "str (opzionale): cosa vuole sapere il workflow"},
    output_schema={
        "sandbox": "str", "sandbox_exists": "bool",
        "scribe_available": "bool", "python": "str", "cwd": "str",
    },
    risk_level="read-only",
    permissions=["local"],
    timeout_seconds=30,
    verification_required=False,
    description="CLIO stessa: ispezione ambiente, sandbox e disponibilità di The Scribe.",
)

CODING_CAPABILITY = AgentCapability(
    id="coding_agent",
    capabilities=["code.edit", "code.test", "sandbox.write"],
    input_schema={
        "task": "str: cosa modificare (linguaggio naturale)",
        "workspace": "str (opzionale): solo sandbox o sottocartella",
        "timeout": "int (opzionale): secondi complessivi",
    },
    output_schema={
        "success": "bool", "diff": "str", "test_output": "str",
        "tests_touched": "bool", "timed_out": "bool", "error": "str|None",
    },
    risk_level="irreversible",
    permissions=["sandbox_only", "subprocess:opencode", "subprocess:pytest"],
    timeout_seconds=300,
    verification_required=True,
    description="Coding Agent locale: produce patch + test dentro _clio_sandbox; "
                "successo solo se pytest passa E i test sono stati aggiornati.",
)

SCRIBE_CAPABILITY = AgentCapability(
    id="scribe",
    capabilities=["memory.resume", "memory.observe"],
    input_schema={
        "project": "str: id del progetto configurato in The Scribe",
        "observation": "str: testo dell'osservazione (solo per 'document')",
    },
    output_schema={
        "resume": "str|None", "project": "str", "offline": "bool",
        "recorded": "str ('scribe'|'outbox')", "path": "str|None",
    },
    risk_level="read-only",
    permissions=["read:scribe_db", "write:clio_outbox"],
    timeout_seconds=60,
    verification_required=False,
    description="The Scribe via ScribeBridge: legge l'ultimo resume e registra "
                "l'osservazione del workflow (nessuna scrittura nel vault).",
)


def build_default_registry() -> AgentRegistry:
    """Registry pronto all'uso con i tre agenti di CLIO."""
    registry = AgentRegistry()
    registry.register(CLIO_CAPABILITY, _clio_executor)
    registry.register(CODING_CAPABILITY, _coding_executor)
    registry.register(SCRIBE_CAPABILITY, _scribe_executor)
    return registry


def describe_registry(registry: Optional[AgentRegistry] = None) -> str:
    """Tabella leggibile del registry (usata dal report e a voce)."""
    registry = registry or build_default_registry()
    righe = []
    for aid, cap in registry.describe().items():
        righe.append(
            f"- {aid}: risk={cap['risk_level']}, timeout={cap['timeout_seconds']}s, "
            f"verifica={'sì' if cap['verification_required'] else 'no'}\n"
            f"  capacità: {', '.join(cap['capabilities'])}\n"
            f"  permessi : {', '.join(cap['permissions']) or '-'}"
        )
    return "\n".join(righe)
