"""Test R1 — Memoria come servizio: CLIO legge The Scribe (sola lettura).

Nessun database reale e nessun vault reale vengono toccati: ogni test costruisce
un DB temporaneo con lo schema di The Scribe e punta CLIO_SCRIBE_CONFIG a una
config temporanea.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

import policy
import scribe_control
from scribe_control import (
    detect_scribe_intent,
    get_scribe_resume,
    get_scribe_status,
    recupera_resume_progetto,
    resolve_project_id,
)

RESUME_TEXT = "# Resume - demo\n**Sessione del:** 2026-10-26\n\n- Fatto: pipeline testata\n"


def _write_config(tmp_path, *, projects=("demo",), vault=None, state=None):
    vault = vault or (tmp_path / "vault")
    state = state or (tmp_path / "state")
    vault.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    righe = [
        "schema_version: 1",
        "app:",
        f'  state_dir: "{state.as_posix()}"',
        "  inactivity_minutes: 20",
        "obsidian:",
        f'  vault: "{vault.as_posix()}"',
        '  session_root: "10_Progetti/Sessioni"',
        "projects:",
    ]
    for pid in projects:
        righe += [
            f'  - id: "{pid}"',
            f'    root: "{tmp_path.as_posix()}"',
            f'    note: "Projects/{pid}.md"',
            "    enabled: true",
        ]
    cfg = tmp_path / "daemon.yaml"
    cfg.write_text("\n".join(righe) + "\n", encoding="utf-8")
    return cfg, vault, state


def _insert_session(state, project_id="demo", *, status="CLOSED", with_resume=True,
                    resume_text=RESUME_TEXT, started_at=None):
    """Crea DB + sessione + (opzionale) write_job COMPLETED puntato al markdown."""
    db_path = state / "events.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, nome TEXT NOT NULL, root TEXT NOT NULL,
                note_path TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, last_activity_at TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                project_id TEXT NOT NULL, source TEXT NOT NULL, type TEXT NOT NULL,
                payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS write_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                target_path TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL);
            """
        )
        started = started_at or (datetime.now() - timedelta(hours=1)).isoformat()
        ended = None if status != "CLOSED" else datetime.now().isoformat()
        sid = f"sess_{project_id}_test"
        conn.execute(
            "INSERT INTO sessions (id, project_id, started_at, ended_at, last_activity_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sid, project_id, started, ended, ended or started, status),
        )
        if with_resume:
            target = state.parent / "vault" / f"{project_id}_resume.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(resume_text, encoding="utf-8")
            conn.execute(
                "INSERT INTO write_jobs (session_id, target_path, content_hash, status)"
                " VALUES (?, ?, 'hash', 'COMPLETED')",
                (sid, str(target)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def scribe_env(tmp_path, monkeypatch):
    """Ambiente The Scribe finto, isolato."""
    cfg, vault, state = _write_config(tmp_path)
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(cfg))
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    return {"config": cfg, "vault": vault, "state": state, "tmp": tmp_path}


def test_resume_returns_markdown(scribe_env):
    _insert_session(scribe_env["state"])
    assert get_scribe_resume("demo") == RESUME_TEXT


def test_resume_missing_returns_clear_message(scribe_env):
    _insert_session(scribe_env["state"], with_resume=False)
    out = get_scribe_resume("demo")
    assert out == "Nessun resume trovato per il progetto demo."


def test_unknown_project_lists_available(scribe_env):
    _insert_session(scribe_env["state"], with_resume=False)
    out = get_scribe_resume("progetto-inesistente")
    assert "non configurato" in out
    assert "demo" in out


def test_offline_when_database_missing(scribe_env):
    # config valida ma DB mai creato -> offline, nessuna eccezione
    out = get_scribe_resume("demo")
    assert "offline" in out.lower()


def test_offline_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(tmp_path / "assente.yaml"))
    assert "offline" in get_scribe_resume("demo").lower()
    assert "offline" in get_scribe_status().lower()


def test_status_active_session(scribe_env):
    _insert_session(scribe_env["state"], status="ACTIVE")
    out = get_scribe_status()
    assert "1 sessione attiva" in out
    assert "demo" in out
    assert "sessione attiva" in out


def test_status_closed_session(scribe_env):
    _insert_session(scribe_env["state"], status="CLOSED")
    out = get_scribe_status()
    assert "0 sessioni attive" in out
    assert "ultima sessione chiusa" in out


def test_status_empty_db(scribe_env):
    _insert_session(scribe_env["state"], with_resume=False)
    # il DB esiste ma senza righe di sessione: la config projects viene upsertata
    db = scribe_env["state"] / "events.db"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()
    assert "non ha ancora registrato sessioni" in get_scribe_status()


def test_action_is_read_only_and_skips_confirmation(scribe_env):
    _insert_session(scribe_env["state"])
    assert policy.classify("recupera_resume_progetto") == "read-only"
    assert policy.classify("stato_sessioni_scribe") == "read-only"

    def _non_deve_chiedere(*_a, **_k):
        raise AssertionError("La lettura non deve richiedere conferma")

    out = policy.guarded_call(
        "recupera_resume_progetto",
        recupera_resume_progetto,
        {"project_id": "demo"},
        confirm=_non_deve_chiedere,
        origin="test",
    )
    assert out == RESUME_TEXT

    audit = (scribe_env["tmp"] / "security.log").read_text(encoding="utf-8")
    assert "recupera_resume_progetto" in audit
    assert '"outcome": "executed"' in audit


def test_resolve_project_id_aliases(scribe_env):
    from the_scribe.config import load_config

    config = load_config(scribe_env["config"])
    assert resolve_project_id("demo", config) == "demo"
    assert resolve_project_id(" Demo ", config) == "demo"
    with pytest.raises(ValueError):
        resolve_project_id("", config)


def test_detect_scribe_intent():
    assert detect_scribe_intent("Dove ero rimasto su Clio") == {
        "action": "recupera_resume_progetto",
        "params": {"project_id": "Clio"},
    }
    assert detect_scribe_intent("cosa stavo facendo su roast room per favore") == {
        "action": "recupera_resume_progetto",
        "params": {"project_id": "roast room"},
    }
    stato = detect_scribe_intent("Stato sessioni di The Scribe")
    assert stato == {"action": "stato_sessioni_scribe", "params": {}}
    assert detect_scribe_intent("accendi la luce") is None


def test_think_and_act_routes_to_scribe_without_llm(scribe_env, monkeypatch):
    """'Dove ero rimasto su demo' risponde dal DB senza chiamare il LLM."""
    _insert_session(scribe_env["state"])

    import jarvis_brain

    monkeypatch.setattr(jarvis_brain, "log_clio_action", lambda *_a, **_k: None)
    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_save_session", lambda self: None)

    # Se il router deterministico non scatta, questa richiesta arriverebbe al LLM
    # (rete). Il test deve fallire esplicitamente in quel caso.
    def _no_llm(*_a, **_k):
        raise AssertionError("Il LLM non deve essere chiamato per questo intento")

    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_call_llm", _no_llm)
    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_stream_answer", _no_llm)

    brain = jarvis_brain.JarvisBrain()
    assert brain.think_and_act("Dove ero rimasto su demo") == RESUME_TEXT


def test_r1_dieci_query_resume_corrette_senza_modificare_il_vault(scribe_env, monkeypatch):
    _insert_session(scribe_env["state"])
    resume_file = scribe_env["vault"] / "demo_resume.md"
    contenuto_prima = resume_file.read_text(encoding="utf-8")

    import jarvis_brain

    monkeypatch.setattr(jarvis_brain, "log_clio_action", lambda *_a, **_k: None)
    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_save_session", lambda self: None)

    def _no_llm(*_a, **_k):
        raise AssertionError("Il gate R1 deve rispondere dal bridge senza LLM")

    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_call_llm", _no_llm)
    monkeypatch.setattr(jarvis_brain.JarvisBrain, "_stream_answer", _no_llm)

    richieste = (
        "Dove ero rimasto su demo",
        "Dove mi ero fermato su demo",
        "Dove avevo lasciato demo",
        "Cosa stavo facendo su demo",
        "Cosa stavamo facendo su demo",
        "Cosa ho fatto su demo",
        "DOVE ERO RIMASTO SU DEMO, PER FAVORE",
        "Cosa stavamo facendo per progetto demo grazie",
        "Dove mi ero fermato sul progetto demo adesso",
        "Cosa ho fatto su demo ieri",
    )
    brain = jarvis_brain.JarvisBrain()

    for richiesta in richieste:
        assert brain.think_and_act(richiesta) == RESUME_TEXT, richiesta

    assert resume_file.read_text(encoding="utf-8") == contenuto_prima
