# -*- coding: utf-8 -*-
"""test_workflow_engine.py — R5 "Orchestrazione multi-agente".

Copre:
  * Agent Registry: dataclass `AgentCapability`, validazione, capacita' dei tre
    agenti (CLIO, Coding sandbox, The Scribe via ScribeBridge), timeout reale;
  * Workflow Engine: macchina a stati completa
    UNDERSTAND -> GROUND -> PLAN -> AUTHORIZE -> EXECUTE -> VERIFY -> PUBLISH -> REPORT
    con checkpoint SQLite;
  * workflow reale "coding agent fixa la sandbox -> The Scribe documenta":
    la patch viene applicata nella sandbox temporanea, pytest gira davvero,
    l'osservazione finisce nell'outbox di CLIO;
  * AUTHORIZE instradato su policy.guarded_call (audit compreso) e rifiuto che
    NON esegue nulla;
  * timeout globale ripartibile;
  * CRASH/RECOVERY: un processo figlio muore a metà EXECUTE; il padre riprende
    dal checkpoint senza rieseguire UNDERSTAND/GROUND/PLAN.

Nessun LLM, nessuna rete, nessun microfono. La sandbox è un repo git temporaneo
(CLIO_SANDBOX), il DB dei workflow e l'outbox di The Scribe stanno in tmp_path:
il repository reale e la sandbox reale non vengono toccati.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import agent_registry           # noqa: E402
import coding_agent             # noqa: E402
import policy                   # noqa: E402
import registry                 # noqa: E402
import scribe_control           # noqa: E402
import workflow_engine as we    # noqa: E402

MAIN_V1 = "def somma(a, b):\n    return a + b\n"
TEST_V1 = "from main import somma\n\n\ndef test_somma():\n    assert somma(1, 2) == 3\n"

GOAL_CODING_DOC = "fixa il bug: manca la moltiplicazione e documentalo con The Scribe"
GOAL_CODING = "fixa il bug dell'addizione nella sandbox"


# ─────────────────────────────────────────────────────────────────────────────
#  Fixture
# ─────────────────────────────────────────────────────────────────────────────
def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=CLIO Test", "-c", "user.email=test@localhost", *args],
        cwd=str(root), check=True, capture_output=True, text=True,
    )


def _make_sandbox(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "main.py").write_text(MAIN_V1, encoding="utf-8")
    (root / "test_main.py").write_text(TEST_V1, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Sandbox git temporanea + DB workflow + outbox isolati."""
    sandbox = _make_sandbox(tmp_path)
    monkeypatch.setenv("CLIO_SANDBOX", str(sandbox))
    monkeypatch.setenv(we.WORKFLOW_DB_ENV, str(tmp_path / "workflow_state.db"))
    monkeypatch.setenv(we.WORKFLOW_ARTIFACTS_ENV, str(tmp_path / "artefatti"))
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "outbox"))
    # The Scribe volutamente offline: GROUND non deve aprire il DB reale del daemon.
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(tmp_path / "scribe-assente.yaml"))
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    return {
        "root": tmp_path,
        "sandbox": sandbox,
        "db": str(tmp_path / "workflow_state.db"),
        "artefatti": tmp_path / "artefatti",
        "outbox": tmp_path / "outbox",
    }


def _agent_moltiplicazione(task, workspace, timeout):
    """Finto agente di coding: applica DAVVERO patch + test nella sandbox."""
    main = Path(workspace) / "main.py"
    main.write_text(
        main.read_text(encoding="utf-8")
        + "\n\ndef moltiplicazione(a, b):\n    return a * b\n",
        encoding="utf-8",
    )
    test = Path(workspace) / "test_main.py"
    test.write_text(
        test.read_text(encoding="utf-8")
        + "\nfrom main import moltiplicazione\n\n\n"
          "def test_moltiplicazione():\n    assert moltiplicazione(2, 3) == 6\n",
        encoding="utf-8",
    )
    return {"ok": True, "output": "patch applicata"}


CODING_OK_OUTPUT = {
    "success": True,
    "diff": "diff --git a/main.py b/main.py\n+def mul(a,b): return a*b\n"
            "diff --git a/test_main.py b/test_main.py\n+def test_mul(): assert True\n",
    "test_output": "$ pytest -q\n6 passed in 0.02s",
    "tests_touched": True,
    "timed_out": False,
    "error": None,
}


def _registry_finto(coding=None, scribe=None, clio=None):
    reg = agent_registry.AgentRegistry()
    if clio is not None:
        reg.register(agent_registry.CLIO_CAPABILITY, clio)
    if coding is not None:
        reg.register(agent_registry.CODING_CAPABILITY, coding)
    if scribe is not None:
        reg.register(agent_registry.SCRIBE_CAPABILITY, scribe)
    return reg


def _coding_ok(action, payload):
    return dict(CODING_OK_OUTPUT)


def _scribe_ok(action, payload):
    return {"ok": True, "recorded": "outbox", "project": payload.get("project") or "",
            "path": "C:/outbox/oss.md", "resume": None, "offline": False, "error": None}


# ─────────────────────────────────────────────────────────────────────────────
#  1) AgentCapability
# ─────────────────────────────────────────────────────────────────────────────
def test_agent_capability_ha_tutti_i_campi():
    cap = agent_registry.AgentCapability(
        id="demo",
        capabilities=["a.b"],
        input_schema={"x": "str"},
        output_schema={"y": "bool"},
        risk_level="reversible",
        permissions=["local"],
        timeout_seconds=42,
        verification_required=True,
    )
    dati = cap.as_dict()
    for campo in ("id", "capabilities", "input_schema", "output_schema", "risk_level",
                  "permissions", "timeout_seconds", "verification_required"):
        assert campo in dati, f"campo mancante: {campo}"
    assert cap.supports("A.B") is True
    assert cap.supports("altro") is False


@pytest.mark.parametrize("kwargs", [
    {"risk_level": "boh"},
    {"id": "   "},
    {"timeout_seconds": 0},
    {"capabilities": []},
])
def test_agent_capability_rifiuta_valori_invalidi(kwargs):
    base = dict(id="x", capabilities=["c"], input_schema={}, output_schema={})
    base.update(kwargs)
    with pytest.raises(ValueError):
        agent_registry.AgentCapability(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  2) Registry
# ─────────────────────────────────────────────────────────────────────────────
def test_registry_ha_i_tre_agenti_di_clio():
    reg = agent_registry.build_default_registry()
    assert reg.agent_ids() == ["clio", "coding_agent", "scribe"]
    assert reg.find("code.edit") == ["coding_agent"]
    assert reg.find("memory.resume") == ["scribe"]

    coding = reg.capability("coding_agent")
    assert coding.risk_level == "irreversible"
    assert coding.verification_required is True
    assert "sandbox_only" in coding.permissions

    assert reg.capability("scribe").risk_level == "read-only"
    assert reg.capability("clio").risk_level == "read-only"
    assert "read:scribe_db" in reg.capability("scribe").permissions


def test_registry_run_incapsula_eccezioni_e_timeout():
    def esplode(action, payload):
        raise RuntimeError("agente rotto")

    def lento(action, payload):
        time.sleep(5)

    reg = _registry_finto(coding=esplode, scribe=lento)

    ko = reg.run("coding_agent", "edit_code", {"task": "x"})
    assert ko["ok"] is False
    assert "agente rotto" in ko["error"]

    # timeout esplicito più corto del timeout dell'agente
    t0 = time.time()
    slow = reg.run("scribe", "resume", {"project": "demo"}, timeout=1)
    assert slow["ok"] is False
    assert slow["timed_out"] is True
    assert time.time() - t0 < 4


def test_agente_sconosciuto_e_errore_chiaro():
    reg = _registry_finto()
    with pytest.raises(KeyError) as exc:
        reg.run("fantasma", "x", {})
    assert "non registrato" in str(exc.value)


def test_coding_executor_rifiuta_workspace_fuori_sandbox(ambiente, monkeypatch):
    def non_deve_eseguire(*_a, **_k):
        raise AssertionError("run_coding_task non deve essere chiamato")

    monkeypatch.setattr(coding_agent, "run_coding_task", non_deve_eseguire)

    esito = agent_registry.build_default_registry().run(
        "coding_agent", "edit_code",
        {"task": "x", "workspace": str(ambiente["root"] / "fuori-sandbox")},
    )

    assert esito["ok"] is False
    assert "sandbox" in esito["error"].lower()


# ─────────────────────────────────────────────────────────────────────────────
#  3) Scribe: bridge (lettura) e outbox (osservazione)
# ─────────────────────────────────────────────────────────────────────────────
def test_scrive_osservazione_nell_outbox_di_clio(ambiente):
    esito = scribe_control.record_observation(
        "demo", "Patch applicata: moltiplicazione + test. pytest 6 passed.",
        workflow_id="wf_test",
    )
    assert esito["recorded"] == "outbox"
    path = Path(esito["path"])
    assert path.is_file()
    testo = path.read_text(encoding="utf-8")
    assert "demo" in testo
    assert "6 passed" in testo
    assert "wf_test" in testo


def test_record_observation_rifiuta_testo_vuoto(ambiente):
    with pytest.raises(ValueError):
        scribe_control.record_observation("demo", "   ")


@pytest.mark.skipif(not scribe_control._BRIDGE_HAS_OBSERVATION,
                    reason="il ScribeBridge installato non espone record_observation")
def test_record_observation_usa_il_bridge_e_non_l_outbox(tmp_path, monkeypatch):
    """Con l'API disponibile si scrive nel vault via bridge, outbox intatta."""
    cfg = _write_scribe_env(tmp_path)
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(cfg))
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "outbox"))

    esito = scribe_control.record_observation(
        "demo", "Errore di login riprodotto", workflow_id="wf_bridge")

    assert esito["ok"] is True
    assert esito["recorded"] == "scribe" and esito["bridge"] is True
    assert esito["project"] == "demo" and esito["error"] is None

    nota = Path(esito["path"])
    assert nota.is_file() and nota.name == "Note.md" and nota.parent.name == "demo"
    testo = nota.read_text(encoding="utf-8")
    assert "Errore di login riprodotto" in testo
    assert "<!-- SCRIBE:KNOWLEDGE:nota_generica:START -->" in testo
    assert not (tmp_path / "outbox").exists()


def _write_scribe_env(tmp_path, project="demo"):
    """Config + DB temporanei di The Scribe (schema minimo, come test_scribe_control)."""
    state = tmp_path / "scribe_state"
    vault = tmp_path / "scribe_vault"
    state.mkdir(parents=True, exist_ok=True)
    vault.mkdir(parents=True, exist_ok=True)
    righe = [
        "schema_version: 1",
        "app:",
        f'  state_dir: "{state.as_posix()}"',
        "  inactivity_minutes: 20",
        "obsidian:",
        f'  vault: "{vault.as_posix()}"',
        '  session_root: "10_Progetti/Sessioni"',
        "projects:",
        f'  - id: "{project}"',
        f'    root: "{tmp_path.as_posix()}"',
        f'    note: "Projects/{project}.md"',
        "    enabled: true",
    ]
    cfg = tmp_path / "daemon.yaml"
    cfg.write_text("\n".join(righe) + "\n", encoding="utf-8")

    import sqlite3
    from datetime import datetime

    conn = sqlite3.connect(state / "events.db")
    try:
        conn.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, nome TEXT NOT NULL, root TEXT NOT NULL,
                note_path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE sessions (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, last_activity_at TEXT NOT NULL,
                status TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                project_id TEXT NOT NULL, source TEXT NOT NULL, type TEXT NOT NULL,
                payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL);
            CREATE TABLE write_jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, target_path TEXT NOT NULL,
                content_hash TEXT NOT NULL, status TEXT NOT NULL);
            """
        )
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at, ended_at, last_activity_at, status)"
            " VALUES ('sess_demo_test', ?, ?, ?, ?, 'CLOSED')",
            (project, now, now, now),
        )
        resume = vault / "demo_resume.md"
        resume.write_text("# Resume - demo\n\n- Fatto: pipeline testata\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO write_jobs (session_id, target_path, content_hash, status)"
            " VALUES ('sess_demo_test', ?, 'hash', 'COMPLETED')",
            (str(resume),),
        )
        conn.commit()
    finally:
        conn.close()
    return cfg


@pytest.mark.skipif(not scribe_control.SCRIBE_AVAILABLE,
                    reason="The Scribe non installato in questo ambiente")
def test_scribe_executor_legge_resume_dal_bridge(tmp_path, monkeypatch):
    cfg = _write_scribe_env(tmp_path)
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(cfg))

    esito = agent_registry.build_default_registry().run(
        "scribe", "resume", {"project": "demo"})
    out = esito["output"]
    assert esito["ok"] is True
    assert out["available"] is True
    assert "Resume - demo" in out["resume"]
    assert out["offline"] is False


@pytest.mark.skipif(not scribe_control.SCRIBE_AVAILABLE,
                    reason="The Scribe non installato in questo ambiente")
def test_scribe_executor_documenta_in_outbox_se_il_bridge_e_solo_lettura(tmp_path, monkeypatch):
    cfg = _write_scribe_env(tmp_path)
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(cfg))
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "outbox"))

    esito = agent_registry.build_default_registry().run(
        "scribe", "document",
        {"project": "demo", "observation": "Fatto: moltiplicazione aggiunta con test."})
    out = esito["output"]
    assert esito["ok"] is True
    # Il bridge installato è di sola lettura: si usa l'outbox di CLIO.
    assert out["recorded"] in ("outbox", "scribe")
    assert out["path"] and Path(out["path"]).is_file()


# ─────────────────────────────────────────────────────────────────────────────
#  4) Workflow completo: Coding Agent -> The Scribe
# ─────────────────────────────────────────────────────────────────────────────
def test_workflow_coding_then_scribe_completo(ambiente, monkeypatch):
    # L'agente di coding è finto, ma pytest, git e sandbox sono REALI.
    monkeypatch.setattr(coding_agent, "_default_agent", _agent_moltiplicazione)

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=agent_registry.build_default_registry(),
        timeout_seconds=180,
        authorizer=lambda goal, plan, ctx: {"decision": "yes", "reason": "test"},
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    assert run.status == "completed", run.report
    assert run.state == "DONE"
    assert [s["step_id"] for s in run.plan] == ["code_1", "scribe_1"]

    # 1) la patch è davvero nella sandbox e i test sono verdi
    assert "moltiplicazione" in (ambiente["sandbox"] / "main.py").read_text(encoding="utf-8")
    coding_out = run.results["code_1"]["output"]
    assert coding_out["success"] is True
    assert coding_out["tests_touched"] is True
    assert "passed" in coding_out["test_output"]

    # 2) verifica: tutti i check richiesti sono verdi
    stato = engine.status(run.workflow_id)
    verifica = stato["verification"]
    assert verifica["ok"] is True
    assert {c["check"]: c["ok"] for c in verifica["checks"]}["pytest_verde"] is True

    # 3) The Scribe ha ricevuto l'osservazione (outbox di CLIO)
    observation = Path(run.artifacts["observation"])
    assert observation.is_file()
    testo = observation.read_text(encoding="utf-8")
    assert "Esito coding: OK" in testo
    assert "moltiplicazione" in testo

    # 4) artefatti + report su disco
    assert Path(run.artifacts["patch"]).is_file()
    report = Path(run.artifacts["report"])
    assert report.is_file()
    contenuto = report.read_text(encoding="utf-8")
    assert "COMPLETATO" in contenuto
    assert "Timeline dei checkpoint" in contenuto

    # 5) tutti gli stati attraversati, in ordine
    timeline = [cp["state"] for cp in engine.history(run.workflow_id)
                if cp["kind"] == "state_enter"]
    assert timeline == list(we.STATES)

    # 6) passi persistiti come done
    passi = {s["step_id"]: s["status"] for s in engine.status(run.workflow_id)["steps"]}
    assert passi == {"code_1": "done", "scribe_1": "done"}


def test_workflow_fallito_scrive_comunque_il_report(ambiente):
    def coding_rotto(action, payload):
        return {"success": False, "diff": "", "test_output": "$ pytest -q\n1 failed",
                "tests_touched": False, "timed_out": False, "error": "test rossi"}

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=coding_rotto, scribe=_scribe_ok),
        authorizer=lambda goal, plan, ctx: "yes",
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    assert run.status == "failed"
    assert "verifica fallita" in (run.error or "")
    contenuto = Path(run.artifacts["report"]).read_text(encoding="utf-8")
    assert "FALLITO" in contenuto
    assert "pytest_verde" in contenuto


# ─────────────────────────────────────────────────────────────────────────────
#  5) AUTHORIZE: policy.guarded_call
# ─────────────────────────────────────────────────────────────────────────────
def test_authorize_passa_da_policy_guarded_call(ambiente, monkeypatch):
    monkeypatch.delenv("CLIO_CODING_AGENT", raising=False)
    chiamate = []

    def conferma_finta(nome, argomenti):
        chiamate.append((nome, argomenti))
        return "yes"

    monkeypatch.setattr(policy, "_telegram_confirm", conferma_finta)

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok, scribe=_scribe_ok),
        authorizer=None,       # default -> policy_authorize
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    assert run.status == "completed"
    nomi = [nome for nome, _ in chiamate]
    assert nomi == ["esegui_workflow"], "il gate deve chiedere UNA volta per il piano"
    assert "code_1" not in str(chiamate[0][1])   # argomenti sanificati/riassunti
    assert "coding_agent:edit_code" in str(chiamate[0][1])
    assert "Nessun costo API a pagamento" in chiamate[0][1]["cost_summary"]

    audit = Path(policy.AUDIT_PATH).read_text(encoding="utf-8")
    assert '"esegui_workflow"' in audit
    assert '"outcome": "confirm_requested"' in audit
    assert '"outcome": "executed"' in audit

    assert engine.status(run.workflow_id)["authorization"]["via"] == "policy.guarded_call"


def test_autorizzazione_web_indica_che_il_coder_locale_e_gratuito(ambiente, monkeypatch):
    monkeypatch.delenv("CLIO_CODING_AGENT", raising=False)
    catturato = {}

    def conferma_web(nome, argomenti, message):
        catturato.update(name=nome, args=argomenti, message=message)
        return "yes"

    monkeypatch.setattr(policy, "_web_cost_confirm", conferma_web)
    eseguiti = []
    esito = policy.guarded_call(
        we.POLICY_ACTION_WORKFLOW,
        lambda **_: eseguiti.append(True) or "workflow autorizzato",
        {"goal": "correggi un bug", "progetto": "clio"},
        origin="flask_ui",
    )

    assert esito == "workflow autorizzato"
    assert eseguiti == [True]
    assert catturato["name"] == we.POLICY_ACTION_WORKFLOW
    assert "correggi un bug" in catturato["message"]
    assert "Nessun costo API a pagamento" in catturato["message"]
    assert "coding_agent:edit_code" in catturato["message"]


def test_impronta_piano_cambia_se_cambia_la_stima_cost(ambiente, monkeypatch):
    import cost_guard
    import model_router

    monkeypatch.setattr(model_router, "get_provider_chain", lambda _: ["gemini"])

    piano = [{
        "step_id": "code_1", "agent": "coding_agent", "action": "edit_code",
        "policy_action": "esegui_coding_task", "payload": {"task": "correggi il bug"},
    }]
    impronta_originale = we.plan_hash(piano)
    monkeypatch.setitem(cost_guard.PAID_ACTIONS, "esegui_coding_task", 0.11)

    assert we.plan_hash(piano) != impronta_originale


def test_authorize_rifiutato_non_esegue_nessun_agente(ambiente):
    eseguiti = []

    def coding_spia(action, payload):
        eseguiti.append("coding")
        return dict(CODING_OK_OUTPUT)

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=coding_spia, scribe=_scribe_ok),
        authorizer=lambda goal, plan, ctx: {"decision": "no", "reason": "annullato"},
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    assert run.status == "cancelled"
    assert eseguiti == []
    assert "EXECUTE" not in [cp["state"] for cp in engine.history(run.workflow_id)]
    # la sandbox non è stata toccata
    assert "moltiplicazione" not in (ambiente["sandbox"] / "main.py").read_text(encoding="utf-8")


def test_authorize_timeout_non_esegue(ambiente, monkeypatch):
    monkeypatch.setattr(policy, "_telegram_confirm", lambda nome, args: "timeout")

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok),
        authorizer=None,
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING, {"project": "demo"})

    assert run.status == "cancelled"
    assert "scaduta" in (run.error or "").lower()
    assert engine.status(run.workflow_id)["steps"][0]["status"] == "pending"


def test_piano_read_only_non_chiede_conferma(ambiente, monkeypatch):
    def non_deve_chiedere(*_a, **_k):
        raise AssertionError("un piano di sola lettura non deve chiedere conferma")

    monkeypatch.setattr(policy, "_telegram_confirm", non_deve_chiedere)

    def clio_ok(action, payload):
        return {"sandbox": "x", "sandbox_exists": True, "scribe_available": True,
                "python": "3.12", "cwd": ".", "requested": payload.get("request")}

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(clio=clio_ok),
        authorizer=None,
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow("che ora è e come stanno gli agenti")

    assert run.status == "completed"
    auth = engine.status(run.workflow_id)["authorization"]
    assert auth["requires_confirmation"] is False
    assert auth["decision"] == "yes"
    assert "sola lettura" in auth["reason"]


def test_esegui_workflow_salta_la_seconda_conferma_quando_il_gate_ha_gia_approvato(
        ambiente, monkeypatch):
    catturato = {}

    class RunFinto:
        report = "REPORT-FINTO"

    def fake_execute(self, goal, context=None, workflow_id=None, authorizer=None, resume=None):
        catturato["authorizer"] = authorizer
        catturato["context"] = context
        return RunFinto()

    monkeypatch.setattr(we.WorkflowEngine, "execute_workflow", fake_execute)

    assert we.esegui_workflow("fixa il bug", "clio", confirmed=True) == "REPORT-FINTO"
    assert callable(catturato["authorizer"])
    assert catturato["authorizer"]("g", [], {})["decision"] == "yes"
    assert catturato["context"]["project"] == "clio"

    we.esegui_workflow("fixa il bug")
    assert catturato["authorizer"] is None, "senza conferma il motore deve chiedere"


# ─────────────────────────────────────────────────────────────────────────────
#  6) Timeout globale + ripresa
# ─────────────────────────────────────────────────────────────────────────────
def test_timeout_globale_e_ripresa_con_budget_nuovo(ambiente):
    def conferma_lenta(goal, plan, ctx):
        time.sleep(2)
        return "yes"

    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok),
        timeout_seconds=1,
        authorizer=conferma_lenta,
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING, {"project": "demo"})

    assert run.status == "timeout"
    assert run.state == "EXECUTE"
    assert "timeout globale" in (run.error or "")
    # il workflow resta ripartibile
    assert engine.status(run.workflow_id)["status"] == "timeout"

    engine2 = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok),
        timeout_seconds=60,
        authorizer=lambda goal, plan, ctx: "yes",
        artifact_root=str(ambiente["artefatti"]),
    )
    run2 = engine2.execute_workflow(GOAL_CODING, {"project": "demo"})

    assert run2.workflow_id == run.workflow_id, "la ripresa deve riusare lo stesso workflow"
    assert run2.resumed is True
    assert run2.status == "completed"


# ─────────────────────────────────────────────────────────────────────────────
#  7) Crash / recovery (processo ucciso a metà EXECUTE)
# ─────────────────────────────────────────────────────────────────────────────
CRASH_SCRIPT = r'''
import os, sys
sys.path.insert(0, {clio!r})
import agent_registry, workflow_engine

def boom(action, payload):
    # morte brutale: nessuna eccezione, nessun finally, nessun flush applicativo
    os._exit(17)

reg = agent_registry.AgentRegistry()
reg.register(agent_registry.CODING_CAPABILITY, boom)
engine = workflow_engine.WorkflowEngine(
    db_path={db!r}, registry=reg, artifact_root={art!r},
    timeout_seconds=60, authorizer=lambda g, p, c: "yes")
engine.execute_workflow({goal!r}, {{"project": "demo"}})
'''


def test_crash_e_ripresa_dal_checkpoint(ambiente, tmp_path):
    script = tmp_path / "crash_worker.py"
    script.write_text(
        CRASH_SCRIPT.format(clio=BASE, db=ambiente["db"],
                            art=str(ambiente["artefatti"]), goal=GOAL_CODING_DOC),
        encoding="utf-8",
    )
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, timeout=180)
    assert proc.returncode == 17, f"il figlio non è morto come previsto: {proc.stderr}"

    # Il DB contiene il checkpoint: stato EXECUTE, passo di coding "running".
    engine = we.WorkflowEngine(db_path=ambiente["db"], registry=_registry_finto(),
                               artifact_root=str(ambiente["artefatti"]))
    wf_id = engine.list_workflows()[0]["id"]
    prima = engine.status(wf_id)
    assert prima["state"] == "EXECUTE"
    assert prima["status"] == "running"
    assert {s["step_id"]: s["status"] for s in prima["steps"]}["code_1"] == "running"

    # Riavvio con agenti funzionanti: deve riprendere, non ricominciare.
    engine2 = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok, scribe=_scribe_ok),
        timeout_seconds=60,
        authorizer=lambda goal, plan, ctx: "yes",
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine2.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    assert run.resumed is True
    assert run.workflow_id == wf_id
    assert run.status == "completed"

    storia = engine2.history(wf_id)
    entrate = [cp["state"] for cp in storia if cp["kind"] == "state_enter"]
    # UNDERSTAND/GROUND/PLAN/AUTHORIZE eseguiti UNA sola volta (prima del crash)
    for stato in ("UNDERSTAND", "GROUND", "PLAN", "AUTHORIZE"):
        assert entrate.count(stato) == 1, f"{stato} rieseguito dopo il crash"
    # EXECUTE ripreso dal checkpoint
    assert entrate.count("EXECUTE") == 2
    assert any(cp["kind"] == "step_interrupted" for cp in storia)
    assert any(cp["kind"] == "resume" for cp in storia)

    passi = {s["step_id"]: s["status"] for s in engine2.status(wf_id)["steps"]}
    assert passi == {"code_1": "done", "scribe_1": "done"}


# ─────────────────────────────────────────────────────────────────────────────
#  8) Integrazione CLIO: azioni registrate e classificazioni
# ─────────────────────────────────────────────────────────────────────────────
def test_azioni_workflow_registrate_e_classificate():
    for nome in ("esegui_workflow", "stato_workflow", "ispeziona_agenti"):
        assert nome in registry.AZIONI_MAP, f"{nome} non registrata in AZIONI_MAP"

    assert policy.classify("esegui_workflow") == "irreversibile"
    assert policy.classify("stato_workflow") == "read-only"
    assert policy.classify("ispeziona_agenti") == "read-only"
    assert policy.classify("registra_osservazione_scribe") == "reversibile"


def test_stato_workflow_elenca_i_passi(ambiente):
    engine = we.WorkflowEngine(
        db_path=ambiente["db"],
        registry=_registry_finto(coding=_coding_ok, scribe=_scribe_ok),
        authorizer=lambda goal, plan, ctx: "yes",
        artifact_root=str(ambiente["artefatti"]),
    )
    run = engine.execute_workflow(GOAL_CODING_DOC, {"project": "demo"})

    dettaglio = we.stato_workflow(run.workflow_id)
    assert "code_1" in dettaglio
    assert "scribe_1 (scribe:document): done" in dettaglio
    assert "DONE" in dettaglio
    assert "checkpoint" in dettaglio

    elenco = we.stato_workflow("")
    assert "Ultimi workflow" in elenco
    assert run.workflow_id in elenco


def test_input_invalidi(tmp_path, monkeypatch):
    monkeypatch.setenv(we.WORKFLOW_DB_ENV, str(tmp_path / "wf.db"))
    engine = we.WorkflowEngine(db_path=str(tmp_path / "wf.db"))

    with pytest.raises(ValueError):
        engine.execute_workflow("   ")
    with pytest.raises(KeyError):
        engine.execute_workflow("goal", workflow_id="wf_inesistente")
