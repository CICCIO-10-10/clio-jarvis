# -*- coding: utf-8 -*-
"""test_coding_agent.py — R3 "Coding Assistito" in sandbox.

Verifica il contratto di `coding_agent.run_coding_task`:
  - l'agente (mockato) modifica DAVVERO la sandbox e pytest passa -> success True;
  - modifiche che rompono i test -> success False (nessun crash);
  - agente che esplode / va in timeout -> success False, timed_out True;
  - qualunque workspace fuori dalla sandbox -> rifiutata, agente mai eseguito;
  - `esegui_coding_task` passa da policy.guarded_call e chiede conferma SEMPRE
    (anche con origin="flask_ui", che prima era auto-approvato).

Nessun LLM vero, nessuna rete: l'agente è un callable iniettato. La sandbox è
un repo git temporaneo (monkeypatch di CLIO_SANDBOX), quindi il vero
`_clio_sandbox` non viene mai modificato dai test.
"""
import os
import sys
import time
import subprocess

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import coding_agent          # noqa: E402
import policy                # noqa: E402

MAIN_V1 = "def somma(a, b):\n    return a + b\n"
TEST_V1 = "from main import somma\n\n\ndef test_somma():\n    assert somma(1, 2) == 3\n"


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=CLIO Test", "-c", "user.email=test@localhost", *args],
        cwd=str(root), check=True, capture_output=True, text=True,
    )


def _make_sandbox(tmp_path):
    """Mini repo git con main.py + test_main.py, come _clio_sandbox."""
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
def sandbox(tmp_path, monkeypatch):
    root = _make_sandbox(tmp_path)
    monkeypatch.setenv("CLIO_SANDBOX", str(root))
    return root


def test_default_agent_non_seleziona_opencode_solo_perche_installato(monkeypatch):
    monkeypatch.delenv("CLIO_CODING_AGENT", raising=False)
    monkeypatch.setenv("CLIO_CODING_MODEL", "openai/modello-remoto")
    monkeypatch.setattr(coding_agent.shutil, "which", lambda _: "opencode.exe")
    locale = {"ok": True, "provider": "ollama", "output": "locale"}
    monkeypatch.setattr(coding_agent, "_local_agent", lambda *a, **k: locale)
    monkeypatch.setattr(
        coding_agent, "_opencode_agent",
        lambda *a, **k: pytest.fail("OpenCode non deve partire in automatico"),
    )

    assert coding_agent._default_agent("task") == locale


def test_opencode_richiede_un_modello_locale_esplicito(monkeypatch):
    monkeypatch.setenv("CLIO_CODING_AGENT", "opencode")
    monkeypatch.setenv("CLIO_CODING_MODEL", "openai/modello-remoto")
    monkeypatch.setattr(
        coding_agent, "_opencode_agent",
        lambda *a, **k: pytest.fail("il modello remoto deve essere rifiutato"),
    )

    esito = coding_agent._default_agent("task")

    assert esito["ok"] is False
    assert "modello locale esplicito" in esito["output"]


def test_opencode_si_avvia_solo_con_modello_locale_esplicito(monkeypatch):
    monkeypatch.setenv("CLIO_CODING_AGENT", "opencode")
    monkeypatch.setenv("CLIO_CODING_MODEL", "ollama/qwen2.5-coder:3b")
    risultato = {"ok": True, "provider": "opencode", "output": "locale"}
    chiamate = []

    def avvia(task, workspace=None, timeout=None):
        chiamate.append((task, workspace, timeout))
        return risultato

    monkeypatch.setattr(coding_agent, "_opencode_agent", avvia)

    assert coding_agent._default_agent("task") == risultato
    assert chiamate == [("task", None, coding_agent.TASK_TIMEOUT)]


def _agent_moltiplicazione(task, workspace, timeout):
    """Agente simulato: aggiunge moltiplicazione + test nella sandbox."""
    with open(os.path.join(workspace, "main.py"), "a", encoding="utf-8") as f:
        f.write("\n\ndef moltiplicazione(a, b):\n    return a * b\n")
    with open(os.path.join(workspace, "test_main.py"), "a", encoding="utf-8") as f:
        f.write("\n\nfrom main import moltiplicazione\n\n\n"
                "def test_moltiplicazione():\n    assert moltiplicazione(3, 4) == 12\n")
    return {"ok": True, "output": "main.py + test_main.py aggiornati"}


# ── 1) La sandbox reale esiste ed è un repo git ────────────────────────────
def test_sandbox_reale_esiste():
    root = coding_agent.get_sandbox_root()
    assert os.path.isdir(root), f"sandbox mancante: {root}"
    assert os.path.isfile(os.path.join(root, "main.py"))
    assert os.path.isfile(os.path.join(root, "test_main.py"))
    assert os.path.isdir(os.path.join(root, ".git"))


# ── 2) Happy path: agente modifica la sandbox, pytest verde -> success ────
def test_run_coding_task_modifica_sandbox_e_test_verdi(sandbox):
    esito = coding_agent.run_coding_task(
        "Aggiungi una funzione moltiplicazione a main.py",
        agent=_agent_moltiplicazione,
    )

    assert esito["success"] is True, esito
    assert set(("success", "diff", "test_output")) <= set(esito)
    assert "moltiplicazione" in esito["diff"]
    assert "passed" in esito["test_output"]
    assert esito["timed_out"] is False
    assert esito["error"] is None
    assert esito["workspace"] == str(sandbox)

    # il file è stato davvero modificato su disco
    assert "moltiplicazione" in (sandbox / "main.py").read_text(encoding="utf-8")


# ── 3) Agente che fallisce: nessun crash, success False ────────────────────
def test_agente_che_esplode_non_crasha(sandbox):
    def agent_rotto(task, workspace, timeout):
        raise RuntimeError("modello offline")

    esito = coding_agent.run_coding_task("qualsiasi cosa", agent=agent_rotto)

    assert esito["success"] is False
    assert "modello offline" in esito["agent_output"]
    assert esito["error"]
    # i test preesistenti restano verdi: la sandbox non è stata toccata
    assert "passed" in esito["test_output"]


def test_agente_dichiara_fallimento(sandbox):
    def agent_ko(task, workspace, timeout):
        return {"ok": False, "output": "non ho capito il task"}

    esito = coding_agent.run_coding_task("task incomprensibile", agent=agent_ko)

    assert esito["success"] is False
    assert "non ho capito" in esito["agent_output"]


# ── 4) Modifica che rompe i test -> success False ──────────────────────────
def test_modifica_che_rompe_i_test_non_e_success(sandbox):
    def agent_sbagliato(task, workspace, timeout):
        with open(os.path.join(workspace, "main.py"), "w", encoding="utf-8") as f:
            f.write("def somma(a, b):\n    return a - b\n")
        return True

    esito = coding_agent.run_coding_task("rompi la somma", agent=agent_sbagliato)

    assert esito["success"] is False
    assert "test" in (esito["error"] or "").lower()
    assert "failed" in esito["test_output"]
    assert "somma" in esito["diff"]


# ── 5) Timeout: l'agente non deve bloccare CLIO ────────────────────────────
def test_timeout_agente_non_blocca(sandbox):
    def agent_lento(task, workspace, timeout):
        time.sleep(timeout + 10)
        return {"ok": True}

    t0 = time.time()
    esito = coding_agent.run_coding_task("task lentissimo", agent=agent_lento, timeout=1)
    durata = time.time() - t0

    assert esito["timed_out"] is True
    assert esito["success"] is False
    assert "timeout" in (esito["error"] or "").lower()
    assert durata < 30, f"run_coding_task ha bloccato per {durata:.1f}s"


# ── 6) Sicurezza: workspace fuori dalla sandbox -> rifiutata ───────────────
def test_workspace_fuori_sandbox_rifiutata(sandbox):
    chiamate = []

    def agent_spia(task, workspace, timeout):
        chiamate.append(workspace)
        return {"ok": True}

    fuori = os.path.dirname(BASE)          # C:\...\PROGETTI
    esito = coding_agent.run_coding_task("task", workspace=fuori, agent=agent_spia)

    assert esito["success"] is False
    assert "fuori dalla sandbox" in esito["error"]
    assert esito["diff"] == ""
    assert chiamate == [], "l'agente è stato eseguito su un percorso reale!"


def test_sottocartella_sandbox_ammessa(sandbox):
    sub = sandbox / "sotto"
    sub.mkdir()
    visti = []

    def agent_spia(task, workspace, timeout):
        visti.append(workspace)
        return {"ok": True}

    esito = coding_agent.run_coding_task("task", workspace=str(sub), agent=agent_spia)

    assert visti == [str(sub)]
    assert esito["workspace"] == str(sub)


# ── 6b) R4: ogni task deve chiedere anche il test ─────────────────────────
def test_task_senza_test_viene_arricchito(sandbox):
    visto = {}

    def agent_spia(task, workspace, timeout):
        visto["task"] = task
        return {"ok": True}

    esito = coding_agent.run_coding_task("aggiungi moltiplicazione", agent=agent_spia)

    assert visto["task"].startswith("aggiungi moltiplicazione")
    assert "test_main.py" in visto["task"]
    assert "test" in visto["task"].lower()
    assert esito["task_effective"] == visto["task"]


def test_task_con_test_non_viene_duplicato(sandbox):
    visto = {}

    def agent_spia(task, workspace, timeout):
        visto["task"] = task
        return {"ok": True}

    originale = "aggiungi moltiplicazione con il suo test"
    esito = coding_agent.run_coding_task(originale, agent=agent_spia)

    assert visto["task"] == originale
    assert esito["task_effective"] == originale


def test_with_test_requirement_non_duplica_pytest(sandbox):
    originale = "fixa il bug, lancia pytest e dimmi l'esito"
    assert coding_agent.with_test_requirement(originale) == originale
    assert coding_agent.with_test_requirement("") == ""


def test_modifica_senza_test_non_e_success(sandbox):
    """Contratto R4: patch senza test aggiornati = task NON completato."""

    def agent_senza_test(task, workspace, timeout):
        with open(os.path.join(workspace, "main.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef moltiplicazione(a, b):\n    return a * b\n")
        return {"ok": True}

    esito = coding_agent.run_coding_task("aggiungi moltiplicazione", agent=agent_senza_test)

    assert esito["success"] is False
    assert esito["tests_touched"] is False
    assert "test" in (esito["error"] or "").lower()
    # la suite resta verde: manca il test, non è un errore di codice
    assert "passed" in esito["test_output"]
    assert "moltiplicazione" in esito["diff"]


def test_tests_touched_dal_diff():
    assert coding_agent._tests_touched(
        "diff --git a/test_main.py b/test_main.py\n--- a/test_main.py\n") is True
    assert coding_agent._tests_touched(
        "diff --git a/tests/foo.py b/tests/foo.py\n") is True
    assert coding_agent._tests_touched(
        "diff --git a/main.py b/main.py\n--- a/main.py\n") is False
    assert coding_agent._tests_touched("") is False


# ── 7) policy: conferma SEMPRE richiesta, anche da flask_ui ───────────────
def test_policy_classifica_irreversibile():
    assert policy.classify("esegui_coding_task") == "irreversibile"


def test_policy_conferma_no_non_esegue_anche_da_flask_ui(sandbox):
    chiamate = []

    def finto(task_descrizione, workspace=None):
        chiamate.append(task_descrizione)
        return "eseguito"

    esito = policy.guarded_call(
        "esegui_coding_task", finto, {"task_descrizione": "x"},
        confirm=lambda nome, args: "no", origin="flask_ui")

    assert "annullata" in esito.lower()
    assert chiamate == []


def test_policy_conferma_yes_esegue(sandbox):
    chiamate = []

    def finto(task_descrizione, workspace=None):
        chiamate.append(task_descrizione)
        return "eseguito"

    esito = policy.guarded_call(
        "esegui_coding_task", finto, {"task_descrizione": "x"},
        confirm=lambda nome, args: "yes", origin="telegram")

    assert esito == "eseguito"
    assert chiamate == ["x"]


# ── 8) L'azione registrata ritorna una stringa per Jarvis ─────────────────
def test_esegui_coding_task_ritorna_stringa(sandbox, monkeypatch):
    monkeypatch.setattr(coding_agent, "_default_agent", _agent_moltiplicazione)

    out = coding_agent.esegui_coding_task("Aggiungi una moltiplicazione")

    assert isinstance(out, str)
    assert "sandbox" in out.lower()
    assert "PASSATI" in out or "✅" in out
    assert "moltiplicazione" in (sandbox / "main.py").read_text(encoding="utf-8")


# ── 9) Rischio residuo chiuso: nessuna auto-approvazione, mai ─────────────
def test_nessuna_azione_e_auto_approvata():
    assert policy.AUTO_APPROVE_ORIGINS == set()


def test_run_opencode_task_senza_conferma_rifiuta():
    """La vecchia funzione OpenCode non deve partire senza conferma esplicita."""
    import skills

    esito = skills.run_opencode_task("aggiungi una funzione a main.py")

    assert isinstance(esito, str)
    assert "conferma" in esito.lower()
    assert "OpenCode ha completato" not in esito


def test_run_opencode_task_passa_dalla_policy():
    """Anche da flask_ui (ex auto-approve) la conferma è obbligatoria."""
    import skills

    esito = policy.guarded_call(
        "run_opencode_task", skills.run_opencode_task,
        {"prompt": "aggiungi una funzione a main.py"},
        confirm=lambda nome, args: "no", origin="flask_ui")

    assert "annullata" in esito.lower()


def test_alias_opencode_delega_al_coder_locale_senza_subprocess(monkeypatch):
    import skills

    risultato = {
        "success": True,
        "agent_output": "modifica locale verificata",
        "test_output": "2 passed",
    }
    chiamate = []

    def coder_locale(prompt, workspace=None, timeout=None):
        chiamate.append((prompt, workspace, timeout))
        return risultato

    monkeypatch.setattr(coding_agent, "run_coding_task", coder_locale)
    monkeypatch.setattr(
        skills.subprocess, "run",
        lambda *a, **k: pytest.fail("il tool legacy non deve avviare processi"),
    )

    esito = skills.run_opencode_task("correggi il bug", confirmed=True)

    assert "Coding Agent locale" in esito
    assert "2 passed" in esito
    assert chiamate == [("correggi il bug", None, 120)]


def test_alias_opencode_non_esce_dalla_sandbox(sandbox, tmp_path):
    import skills

    esito = skills.run_opencode_task(
        "correggi il bug", cwd=str(tmp_path / "altra_workspace"), confirmed=True
    )

    assert "non completato" in esito.lower()
    assert "sandbox" in esito.lower()
