# -*- coding: utf-8 -*-
"""test_jarvis_acceptance.py — collaudo di accettazione E2E di "Jarvis" (CLIO R9).

Simula il comando vocale:

    "CLIO, riprendi il lavoro su clio, aggiungi una funzione divisione
     nella sandbox e documentala"

e verifica che il flusso completo funzioni davvero, un passo per volta:

    STEP 1  RIPRESA    ScribeBridge.get_resume("clio")            -> diario reale
    STEP 2  CODING     coding_agent.run_coding_task(...)          -> patch + pytest
    STEP 3  DOCUMENTA  ScribeBridge.record_observation(...)       -> nota del vault
    STEP 5  PULIZIA    git reset --hard nella sandbox

Cosa è SIMULATO (mock: nessun audio, nessuna rete, nessun LLM reale):
  * STT  — ``FakeSTT``: il microfono restituisce la frase;
  * LLM  — ``FakeLLM``: il router d'intenti restituisce il piano dei 3 passi;
  * TTS  — ``FakeTTS``: la voce annota le frasi invece di pronunciarle;
  * LLM del Coding Agent — ``coding_agent._call_coder`` restituisce il JSON che
    il profilo CODER locale avrebbe prodotto. ``_default_agent`` è forzato sul
    CODER locale, così OpenCode non viene mai invocato.

Cosa è REALE:
  * The Scribe (SQLite + ``SessionManager`` + ``writer``) via ``ScribeBridge``;
  * ``coding_agent.run_coding_task`` (diff git, scritture, pytest);
  * ``jarvis_brain.detect_coding_command`` (riconoscimento della frase);
  * la sandbox vera ``_clio_sandbox``, che deve contenere ``def divisione``.

Isolamento: vault, DB e config di The Scribe e il repo del progetto stanno in
``tmp_path`` (``CLIO_SCRIBE_CONFIG``); il vault vero e il DB del daemon non
vengono letti né scritti. L'unica cosa reale che il test modifica è la sandbox:
parte da un repo git pulito e torna a HEAD con ``git reset --hard`` (STEP 5).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import coding_agent       # noqa: E402
import jarvis_brain       # noqa: E402
import policy             # noqa: E402
import scribe_control     # noqa: E402

if not scribe_control.SCRIBE_AVAILABLE:      # pragma: no cover - dipende dall'ambiente
    pytest.skip(
        "The Scribe non è importabile "
        f"({scribe_control._IMPORT_ERROR}): il collaudo richiede il pacchetto installato.",
        allow_module_level=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Dati del collaudo
# ─────────────────────────────────────────────────────────────────────────────
FRASE = ("CLIO, riprendi il lavoro su clio, aggiungi una funzione divisione "
         "nella sandbox e documentala")

# La descrizione che il router (simulato) estrae dalla frase per il Coding Agent.
TASK_CODING = "aggiungi funzione divisione in main.py"

# Nota progetto "umana": deve restare intatta, il blocco SCRIBE va in fondo.
NOTE_UMANA = """---
verificato_il: 2026-10-19
stato: attivo-laboratorio
tags: [attivo, clio]
---

# C.L.I.O. - nota umana

Testo scritto a mano dal Capo: questo contenuto non deve mai essere sovrascritto.

## Aggiornamento verificato

- Motore di memoria collegato a The Scribe.
- Control plane stabile.

## TODO

- [ ] Forzare il flush della sessione
- [ ] Controllare il lock in multi-processo
- [x] TODO già completato
"""

# Patch che il "modello" restituisce: nuova funzione + test (contratto R4).
MAIN_PATCH = '''"""Sandbox di coding per CLIO — codice di partenza volutamente minimale."""


def somma(a, b):
    """Ritorna la somma di due numeri."""
    return a + b


def divisione(a, b):
    """Ritorna a / b; solleva ZeroDivisionError se b è 0."""
    if b == 0:
        raise ZeroDivisionError("divisione per zero")
    return a / b


if __name__ == "__main__":
    print("1 + 2 =", somma(1, 2))
    print("6 / 3 =", divisione(6, 3))
'''

TEST_PATCH = '''import pytest

from main import divisione, somma


def test_somma_interi():
    assert somma(1, 2) == 3


def test_somma_negativi():
    assert somma(-2, 5) == 3


def test_somma_zero():
    assert somma(0, 0) == 0


def test_divisione():
    assert divisione(6, 3) == 2


def test_divisione_negativi():
    assert divisione(-6, 3) == -2


def test_divisione_per_zero():
    with pytest.raises(ZeroDivisionError):
        divisione(1, 0)
'''


# ─────────────────────────────────────────────────────────────────────────────
#  Mock: voce, LLM, TTS (nessun hardware, nessuna rete)
# ─────────────────────────────────────────────────────────────────────────────
class FakeSTT:
    """Speech-to-text simulato: restituisce la frase, nessun microfono."""

    def __init__(self, frase):
        self.frase = frase
        self.chiamate = 0

    def ascolta(self, timeout=10):
        self.chiamate += 1
        return self.frase


class FakeTTS:
    """Text-to-speech simulato: annota le frasi invece di pronunciarle."""

    def __init__(self):
        self.pronunciate = []

    def di(self, testo):
        self.pronunciate.append(str(testo))
        return True


class FakeLLM:
    """Router LLM simulato: risponde con il piano atteso, nessuna chiamata."""

    def __init__(self, piano):
        self.piano = piano
        self.richieste = []

    def pianifica(self, trascrizione):
        self.richieste.append(trascrizione)
        return [dict(passo) for passo in self.piano]


# ─────────────────────────────────────────────────────────────────────────────
#  Utilità
# ─────────────────────────────────────────────────────────────────────────────
def _git(root, *args, check=True):
    """Esegue git nella cartella indicata e restituisce lo stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} in {root} è fallito: {proc.stderr.strip()}"
        )
    return proc.stdout


# ─────────────────────────────────────────────────────────────────────────────
#  Fixture
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def sandbox_reale(monkeypatch):
    """La `_clio_sandbox` VERA (repo git), garantita pulita e ripristinata.

    Il test la modifica davvero — è un collaudo di accettazione, non una
    simulazione — quindi pretende un repo pulito in partenza e fa sempre
    `git reset --hard` alla fine. Se la sandbox è sporca il test viene SALTATO,
    per non distruggere lavoro non committato.
    """
    monkeypatch.delenv(coding_agent.SANDBOX_ENV, raising=False)
    root = Path(coding_agent.get_sandbox_root()).resolve()

    if not (root / ".git").is_dir():
        pytest.skip(f"sandbox non trovata o non è un repo git: {root}")

    sporco = _git(root, "status", "--porcelain", check=False).strip()
    if sporco:
        pytest.skip(
            f"sandbox sporca in {root}: fai commit o `git reset --hard` prima del "
            f"collaudo (prime modifiche: {sporco.splitlines()[:3]})"
        )

    try:
        yield root
    finally:
        # Rete di sicurezza: qualunque sia l'esito, la sandbox torna a HEAD.
        _git(root, "reset", "--hard", check=False)
        _git(root, "clean", "-fdq", check=False)


@pytest.fixture
def scribe_env(tmp_path, monkeypatch):
    """The Scribe reale (SQLite + writer) su vault, DB, config e repo TEMPORANEI.

    Il progetto `clio` è un repo git temporaneo e pulito: così il diario è
    deterministico e non dipende dalle modifiche pendenti del repo reale.
    """
    vault = tmp_path / "vault"
    state = tmp_path / "state"
    repo = tmp_path / "clio_repo"
    for cartella in (vault, state, repo):
        cartella.mkdir(parents=True, exist_ok=True)

    nota = vault / "10_Progetti" / "Attivi" / "CLIO.md"
    nota.parent.mkdir(parents=True, exist_ok=True)
    nota.write_text(NOTE_UMANA, encoding="utf-8")

    (repo / "jarvis_brain.py").write_text("# modulo di prova\n", encoding="utf-8")
    (repo / "coding_agent.py").write_text("# modulo di prova\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=CLIO Test", "-c", "user.email=test@localhost",
         "commit", "-q", "-m", "baseline")

    # DB con lo schema corrente di The Scribe (include start_commit).
    store = scribe_control.EventStore(state / "events.db")
    store.initialize()
    store.close()

    cfg = tmp_path / "daemon.yaml"
    cfg.write_text(
        "schema_version: 1\n"
        "app:\n"
        f'  state_dir: "{state.as_posix()}"\n'
        "  inactivity_minutes: 20\n"
        "obsidian:\n"
        f'  vault: "{vault.as_posix()}"\n'
        '  session_root: "10_Progetti/Sessioni"\n'
        '  session_subdir: "Scribe"\n'
        "  update_project_note: true\n"
        "projects:\n"
        '  - id: "clio"\n'
        '    nome: "CLIO"\n'
        f'    root: "{repo.as_posix()}"\n'
        '    note: "10_Progetti/Attivi/CLIO.md"\n'
        "    enabled: true\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(cfg))
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "outbox"))
    # L'audit del control-plane resta fuori dal repo: nessun file sporco.
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))

    return {"cfg": cfg, "vault": vault, "state": state, "repo": repo, "nota": nota}


@pytest.fixture
def diario_precedente(scribe_env):
    """Sessione precedente già finalizzata: è ciò che "riprendi" deve leggere."""
    with scribe_control._open_bridge(scribe_env["cfg"]) as (bridge, _config):
        sessione = bridge.start_session("clio", "sessione precedente su CLIO")
        assert sessione["status"] == "ACTIVE"
        assert sessione["project_id"] == "clio"
        diario = bridge.finalize_session("clio")
    assert diario, "finalize_session non ha scritto il diario"
    assert Path(diario).is_file()
    return Path(diario)


# ─────────────────────────────────────────────────────────────────────────────
#  Il collaudo
# ─────────────────────────────────────────────────────────────────────────────
def test_comando_vocale_jarvis_end_to_end(
    scribe_env, diario_precedente, sandbox_reale, monkeypatch
):
    """Voce -> ripresa del lavoro -> coding in sandbox -> documentazione."""
    head_iniziale = _git(sandbox_reale, "rev-parse", "HEAD").strip()

    # ── STEP 0 — la voce (STT + LLM + TTS simulati) ───────────────────────
    stt = FakeSTT(FRASE)
    tts = FakeTTS()
    llm = FakeLLM([
        {"azione": "riprendi", "progetto": "clio"},
        {"azione": "coding", "task": TASK_CODING},
        {"azione": "documenta", "progetto": "clio",
         "testo": "Aggiunta funzione divisione", "categoria": "completamento"},
    ])

    trascrizione = stt.ascolta()
    assert trascrizione == FRASE
    assert stt.chiamate == 1

    piano = llm.pianifica(trascrizione)
    assert [passo["azione"] for passo in piano] == ["riprendi", "coding", "documenta"]
    assert llm.richieste == [FRASE]

    # Il riconoscimento VERO di Jarvis deve vedere la parte di coding.
    rilevato = jarvis_brain.detect_coding_command(trascrizione)
    assert rilevato is not None, "Jarvis non riconosce la frase come task di coding"
    descrizione, workspace_ignorata = rilevato
    assert "divisione" in descrizione
    assert "sandbox" in descrizione.lower()
    assert workspace_ignorata is None      # nessun percorso: si lavora nella sandbox

    # ── STEP 1 — "riprendi il lavoro su clio" ────────────────────────────
    with scribe_control._open_bridge(scribe_env["cfg"]) as (bridge, _config):
        assert Path(diario_precedente).is_file()

        resume = bridge.get_resume("clio")
        assert isinstance(resume, str) and resume.strip()
        assert "## Resume - clio" in resume
        assert "### File modificati in questa sessione" in resume
        assert "### Prossimo passo (deterministico)" in resume
        # BUG 3 del R7.1: i TODO aperti della nota, non un generico "Tutto committato".
        assert "TODO aperti:" in resume
        assert "Forzare il flush della sessione" in resume
        assert "Controllare il lock in multi-processo" in resume
        assert "TODO già completato" not in resume      # spuntato: escluso
        tts.di("Riprendo il lavoro su CLIO dal diario di sessione.")

        # BUG 2 del R7.1: il blocco macchina va in fondo, il testo umano resta.
        nota = scribe_env["nota"].read_text(encoding="utf-8")
        assert "# C.L.I.O. - nota umana" in nota
        assert "Testo scritto a mano dal Capo" in nota
        assert nota.count("<!-- SCRIBE:STATE:START -->") == 1
        assert nota.count("<!-- SCRIBE:STATE:END -->") == 1
        assert nota.rstrip().endswith("<!-- SCRIBE:STATE:END -->")
        assert nota.index("Testo scritto a mano") < nota.index("<!-- SCRIBE:STATE:START -->")

        # ── STEP 2 — "aggiungi una funzione divisione nella sandbox" ─────
        prompt_visti = []

        def finto_modello_coder(messages, deadline):
            """LLM simulato: il JSON che il profilo CODER locale avrebbe prodotto."""
            prompt_visti.append(messages[-1]["content"])
            return (
                json.dumps({"files": [
                    {"path": "main.py", "content": MAIN_PATCH},
                    {"path": "test_main.py", "content": TEST_PATCH},
                ]}),
                "mock-coder",
            )

        def finto_default_agent(task, workspace=None, timeout=None):
            """Niente OpenCode: si usa il CODER locale con il modello simulato."""
            return coding_agent._local_agent(
                task, workspace, timeout or coding_agent.TASK_TIMEOUT
            )

        monkeypatch.setattr(coding_agent, "_default_agent", finto_default_agent)
        monkeypatch.setattr(coding_agent, "_call_coder", finto_modello_coder)

        esito = coding_agent.run_coding_task(TASK_CODING)

        assert esito["success"] is True, esito.get("error") or esito
        assert esito["timed_out"] is False
        assert esito["tests_touched"] is True          # contratto: patch + test
        assert Path(esito["workspace"]).resolve() == sandbox_reale.resolve()
        assert "def divisione" in esito["diff"]
        assert "test_main.py" in esito["diff"]
        assert "passed" in esito["test_output"].lower()
        assert "failed" not in esito["test_output"].lower()
        # il prompt è stato costruito dal modulo reale, non dal test
        assert prompt_visti and "divisione" in prompt_visti[0]

        # La sandbox contiene DAVVERO la funzione, e somma non è stata persa.
        main_py = (sandbox_reale / "main.py").read_text(encoding="utf-8")
        assert "def divisione(a, b):" in main_py
        assert "def somma(a, b):" in main_py
        tts.di("Fatto, Capo. Task di coding completato, i test passano.")

        # ── STEP 3 — "e documentala" ─────────────────────────────────────
        percorso = bridge.record_observation(
            "clio", "Aggiunta funzione divisione", "completamento"
        )
        assert isinstance(percorso, str)
        nota_conoscenza = Path(percorso)
        assert nota_conoscenza.is_file()
        assert nota_conoscenza.resolve().is_relative_to(scribe_env["vault"].resolve())
        assert nota_conoscenza.name == "Completamenti.md"
        testo = nota_conoscenza.read_text(encoding="utf-8")
        assert "Aggiunta funzione divisione" in testo
        assert "SCRIBE:KNOWLEDGE:completamento:START" in testo
        tts.di("Documentato in Completamenti.")

    # Una risposta vocale per ogni passo eseguito.
    assert len(tts.pronunciate) == 3

    # ── STEP 5 — pulizia: la sandbox torna esattamente al commit iniziale ─
    _git(sandbox_reale, "reset", "--hard")
    assert _git(sandbox_reale, "status", "--porcelain").strip() == ""
    assert _git(sandbox_reale, "rev-parse", "HEAD").strip() == head_iniziale
    assert "def divisione" not in (sandbox_reale / "main.py").read_text(encoding="utf-8")
