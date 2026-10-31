# -*- coding: utf-8 -*-
"""test_r5_workflow_e2e.py — verifica MANUALE della milestone R5.

Tre prove, tutte su sandbox/The Scribe/DB TEMPORANEI (il repo, la
`_clio_sandbox` e il DB del daemon non vengono toccati):

  [1] workflow reale: Coding Agent locale -> pytest -> The Scribe documenta
  [2] gate di conferma: piano rifiutato = nessun agente eseguito, sandbox intatta
  [3] crash + recovery: processo figlio ucciso a metà EXECUTE, ripresa dal checkpoint

Uso:
    python test_r5_workflow_e2e.py               # agenti veri (serve Ollama/llama.cpp locale)
    python test_r5_workflow_e2e.py --fake        # deterministico, senza LLM/rete
    python test_r5_workflow_e2e.py --recovery    # solo crash/recovery

Nota: NON è un file di pytest "puro" (nessun `test_*` qui dentro), è uno script
manuale come `test_r4_voice_e2e.py`.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from test_workflow_engine import (  # noqa: E402
    _agent_moltiplicazione,
    _make_sandbox,
    _write_scribe_env,
)

import agent_registry      # noqa: E402
import coding_agent        # noqa: E402
import policy              # noqa: E402
import scribe_control      # noqa: E402
import workflow_engine as we   # noqa: E402

GOAL = ("aggiungi una funzione moltiplicazione a main.py nella sandbox "
        "e documentala con The Scribe")
GOAL_RECOVERY = "fixa il bug dell'addizione nella sandbox e documentalo con The Scribe"

AGENTS_MD = (
    "# Sandbox di coding di CLIO\n\n"
    "1. Ogni nuova funzione DEVE avere un test in test_main.py.\n"
    "2. Dopo ogni modifica esegui `python -m pytest -q`.\n"
    "3. Non uscire da questa cartella.\n"
    "Un task senza test aggiornati è considerato NON completato.\n"
)

CRASH_SCRIPT = r'''
import os, sys
sys.path.insert(0, {clio!r})
import agent_registry, workflow_engine

def boom(action, payload):
    print("    [figlio] passo di coding avviato -> muoio (os._exit 17)")
    sys.stdout.flush()
    os._exit(17)

reg = agent_registry.AgentRegistry()
reg.register(agent_registry.CODING_CAPABILITY, boom)
eng = workflow_engine.WorkflowEngine(db_path={db!r}, registry=reg,
        artifact_root={art!r}, timeout_seconds=60, authorizer=lambda g, p, c: "yes")
eng.execute_workflow({goal!r}, {{"project": "clio"}})
'''

CODING_FAKE = {
    "success": True,
    "diff": "diff --git a/main.py b/main.py\n+def moltiplicazione(a, b): return a * b\n"
            "diff --git a/test_main.py b/test_main.py\n+def test_moltiplicazione(): assert True\n",
    "test_output": "$ pytest -q\n4 passed in 0.02s",
    "tests_touched": True, "timed_out": False, "error": None,
}


def _prepara(tmp: Path):
    sandbox = _make_sandbox(tmp)
    sandbox.joinpath("AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
    cfg = _write_scribe_env(tmp, project="clio")

    os.environ["CLIO_SANDBOX"] = str(sandbox)
    os.environ["CLIO_SCRIBE_CONFIG"] = str(cfg)
    os.environ[scribe_control.OUTBOX_ENV] = str(tmp / "outbox")
    os.environ[we.WORKFLOW_DB_ENV] = str(tmp / "workflow_state.db")
    os.environ[we.WORKFLOW_ARTIFACTS_ENV] = str(tmp / "artefatti")
    policy.AUDIT_PATH = str(tmp / "security.log")
    return sandbox, str(tmp / "workflow_state.db"), str(tmp / "artefatti")


def _autorizza(decisione="yes"):
    def _auth(goal, plan, context):
        print("    [AUTHORIZE] il control-plane chiede conferma per il piano:")
        for step in plan:
            print(f"        - {step['step_id']}: {step['agent']}:{step['action']}"
                  f" (policy: {step['policy_action']})")
        print(f"    [AUTHORIZE] risposta utente: {decisione.upper()} "
              "(in produzione: Telegram o voce)")
        return {"decision": decisione, "reason": f"demo: {decisione}",
                "via": "authorizer-demo"}
    return _auth


def prova_workflow(tmp, fake):
    sandbox, db, art = _prepara(tmp)
    reg = None
    if fake:
        # Agente deterministico ma REALE nel resto della catena: patch sui file,
        # git diff e pytest veri (nessun LLM, nessuna rete).
        reg = agent_registry.AgentRegistry()
        reg.register(agent_registry.CLIO_CAPABILITY, agent_registry._clio_executor)
        reg.register(agent_registry.CODING_CAPABILITY, lambda a, p: coding_agent.run_coding_task(
            p.get("task") or GOAL, agent=_agent_moltiplicazione, timeout=120))
        reg.register(agent_registry.SCRIBE_CAPABILITY, agent_registry._scribe_executor)
    agente = "deterministico (--fake)" if fake else "CODER locale (Ollama/llama.cpp)"

    print("[1] workflow reale: Coding Agent -> pytest -> The Scribe")
    print(f"    sandbox : {sandbox}")
    print(f"    agente  : {agente}")
    print(f"    goal    : {GOAL}")

    engine = we.WorkflowEngine(db_path=db, registry=reg, artifact_root=art,
                               timeout_seconds=300)
    run = engine.execute_workflow(GOAL, {"project": "clio"}, authorizer=_autorizza("yes"))
    print()
    print(run.report)
    ok1 = run.status == "completed" and "moltiplicazione" in (
        sandbox / "main.py").read_text(encoding="utf-8")

    print("[2] gate di conferma: l'utente dice NO")
    engine_no = we.WorkflowEngine(db_path=db, registry=reg, artifact_root=art)
    run_no = engine_no.execute_workflow(
        "fixa un altro bug nella sandbox", {"project": "clio"},
        authorizer=_autorizza("no"), resume=False)
    ok2 = run_no.status == "cancelled" and not any(
        s["status"] == "done" for s in engine_no.status(run_no.workflow_id)["steps"])
    print(f"    esito: {run_no.status} · passi eseguiti: "
          f"{[s['status'] for s in engine_no.status(run_no.workflow_id)['steps']]}")
    print(f"    {'OK' if ok2 else 'FAIL'}: nessun agente eseguito senza conferma")
    print()
    return ok1 and ok2, db, art


def prova_recovery(tmp, db, art):
    print("[3] crash + recovery: processo ucciso a metà EXECUTE")
    script = tmp / "crash.py"
    script.write_text(CRASH_SCRIPT.format(clio=str(BASE), db=db, art=art,
                                          goal=GOAL_RECOVERY),
                      encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    print(proc.stdout.rstrip())
    print(f"    exit code del figlio: {proc.returncode}")

    engine = we.WorkflowEngine(db_path=db, artifact_root=art)
    wf_id = engine.list_workflows()[0]["id"]
    prima = engine.status(wf_id)
    print(f"    dopo il crash: stato {prima['state']}/{prima['status']}, "
          f"passi {[(s['step_id'], s['status']) for s in prima['steps']]}")

    reg = agent_registry.AgentRegistry()
    reg.register(agent_registry.CODING_CAPABILITY, lambda a, p: dict(CODING_FAKE))
    reg.register(agent_registry.SCRIBE_CAPABILITY,
                 lambda a, p: {"ok": True, "recorded": "outbox", "path": None})
    engine2 = we.WorkflowEngine(db_path=db, registry=reg, artifact_root=art,
                                timeout_seconds=60, authorizer=lambda g, p, c: "yes")
    run = engine2.execute_workflow(GOAL_RECOVERY, {"project": "clio"}, workflow_id=wf_id)

    entrate = [c["state"] for c in engine2.history(wf_id) if c["kind"] == "state_enter"]
    ok = (run.resumed and run.status == "completed" and run.workflow_id == wf_id
          and entrate.count("UNDERSTAND") == 1 and entrate.count("EXECUTE") == 2)
    print(f"    ripreso: {run.resumed} · stesso id: {run.workflow_id == wf_id} · "
          f"esito: {run.state}/{run.status}")
    print(f"    UNDERSTAND eseguito {entrate.count('UNDERSTAND')}x, "
          f"EXECUTE {entrate.count('EXECUTE')}x")
    print(f"    {'OK' if ok else 'FAIL'}: ripresa dal checkpoint, non da capo")
    print()
    return ok


def main(argv):
    # Il report contiene emoji: i terminali Windows legacy usano cp1252 e
    # possono sollevare UnicodeEncodeError anche quando il workflow è riuscito.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass

    fake = "--fake" in argv
    solo_recovery = "--recovery" in argv
    tmp = Path(tempfile.mkdtemp(prefix="clio_r5_e2e_"))
    print("=" * 78)
    print("R5 — ORCHESTRAZIONE MULTI-AGENTE (Agent Registry + Workflow Engine)")
    print("=" * 78)
    print(f"work dir : {tmp}\n")

    if solo_recovery:
        sandbox, db, art = _prepara(tmp)
        esiti = [prova_recovery(tmp, db, art)]
    else:
        ok, db, art = prova_workflow(tmp, fake)
        esiti = [ok, prova_recovery(tmp, db, art)]

    print("=" * 78)
    print("=== RISULTATO:", "R5 DIMOSTRATA" if all(esiti) else "R5 FALLITA", "===")
    print("=" * 78)
    return 0 if all(esiti) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
