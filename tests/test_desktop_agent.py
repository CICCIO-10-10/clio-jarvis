# -*- coding: utf-8 -*-
"""test_desktop_agent.py — FASE 2 · C1: Desktop Agent con MOCK.

Nessuna app viene aperta e nessuna finestra reale viene toccata: `_launch_process`,
`_enumerate_windows` e `_activate_window` sono sostituiti da doppi. Copre:
  * normalizzazione nomi/alias e whitelist (default + override da ambiente);
  * open_app: successo, app non consentita, launcher che esplode, conferma assente;
  * focus_window: finestra trovata, finestra assente, attivazione fallita;
  * azione composta: crea la IdeaSession e NON la crea se l'app non parte;
  * policy (apertura SENSITIVE, focus REVERSIBLE), registro e metadati del LLM.

Esecuzione:  python -m pytest -q tests/test_desktop_agent.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_agent as da  # noqa: E402
import idea_session as idea  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402


# ── Isolamento: niente whitelist ereditata dall'ambiente, niente conferme reali ──

@pytest.fixture(autouse=True)
def _ambiente_pulito(monkeypatch):
    """Nessuna variabile di whitelist e nessun canale di conferma reale."""
    monkeypatch.delenv(da.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(policy, "_telegram_confirm", lambda name, args: "timeout")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Store delle idee isolato in tmp_path (non tocca data/idea_sessions.json)."""
    percorso = tmp_path / "idea_sessions.json"
    monkeypatch.setattr(idea, "SESSIONS_FILE", percorso)
    return percorso


@pytest.fixture
def lanciate(monkeypatch):
    """Registra gli eseguibili che sarebbero stati avviati (nessun processo reale)."""
    chiamate = []

    def finto_launcher(exe):
        chiamate.append(exe)

    monkeypatch.setattr(da, "_launch_process", finto_launcher)
    return chiamate


# ── Normalizzazione e whitelist ───────────────────────────────────────────────

@pytest.mark.parametrize("ingresso,atteso", [
    ("Paint", "mspaint.exe"),
    ("mspaint", "mspaint.exe"),
    ("C:\\Windows\\System32\\mspaint.exe", "mspaint.exe"),
    ("blocco note", "notepad.exe"),
    ("Notepad", "notepad.exe"),
    ("Calcolatrice", "calc.exe"),
    ("calculator", "calc.exe"),
    ("vscode", "code.exe"),
    ("Esplora file", "explorer.exe"),
])
def test_normalizza_alias(ingresso, atteso):
    assert da.normalize_app_name(ingresso) == atteso


@pytest.mark.parametrize("ingresso", ["", "   ", None, 42])
def test_normalizza_rifiuta_input_vuoto(ingresso):
    with pytest.raises(da.DesktopAgentError):
        da.normalize_app_name(ingresso)


def test_whitelist_default():
    consentite = da.allowed_apps()

    assert "mspaint.exe" in consentite
    assert "notepad.exe" in consentite
    assert da.is_allowed_app("Paint") is True
    assert da.is_allowed_app("cmd.exe") is False


def test_whitelist_override_da_ambiente(monkeypatch):
    monkeypatch.setenv(da.ENV_ALLOWED_APPS, "notepad.exe, paint")

    consentite = da.allowed_apps()

    assert consentite == frozenset({"notepad.exe", "mspaint.exe"})
    assert da.is_allowed_app("calc") is False


# ── open_app ──────────────────────────────────────────────────────────────────

def test_open_app_avvia_l_eseguibile(lanciate):
    assert da.open_app("paint", confirmed=True) is True
    assert lanciate == ["mspaint.exe"]


def test_open_app_non_consentita_non_avvia(lanciate):
    assert da.open_app("cmd.exe", confirmed=True) is False
    assert lanciate == []


def test_open_app_errore_pulito_se_l_app_non_esiste(monkeypatch):
    def esplode(exe):
        raise FileNotFoundError(f"{exe} non trovato")

    monkeypatch.setattr(da, "_launch_process", esplode)

    assert da.open_app("notepad", confirmed=True) is False   # nessuna eccezione
    assert da.is_allowed_app("notepad") is True


def test_open_app_senza_conferma_non_avvia(lanciate):
    """Chiamata diretta senza control-plane: conferma assente -> nessun avvio."""
    assert da.open_app("notepad") is False
    assert lanciate == []


def test_open_app_conferma_negata(lanciate):
    assert da.open_app("notepad", confirm=lambda name, args: "no") is False
    assert lanciate == []


def test_open_app_conferma_accettata_via_guarded_call(lanciate):
    """La conferma passa da policy.guarded_call, che poi inietta confirmed=True."""
    assert da.open_app("notepad", confirm=lambda name, args: "yes") is True
    assert lanciate == ["notepad.exe"]


def test_open_app_registra_l_audit(tmp_path):
    """Ogni tentativo finisce in audit: il control-plane non è aggirabile."""
    audit = tmp_path / "security.log"
    originale = policy.AUDIT_PATH
    policy.AUDIT_PATH = str(audit)
    try:
        da.open_app("notepad", confirm=lambda name, args: "yes")
    finally:
        policy.AUDIT_PATH = originale

    righe = audit.read_text(encoding="utf-8").strip().splitlines()
    assert any(da.LAUNCH_ACTION in riga for riga in righe)


# ── focus_window ──────────────────────────────────────────────────────────────

def _finestre(*titoli):
    return [{"hwnd": 100 + i, "title": t, "pid": 1000 + i} for i, t in enumerate(titoli)]


def test_focus_porta_avanti_la_finestra(monkeypatch):
    attivate = []
    monkeypatch.setattr(da, "_enumerate_windows",
                        lambda: _finestre("Documento - Word", "Senza titolo - Blocco Note"))
    monkeypatch.setattr(da, "_activate_window", lambda hwnd: attivate.append(hwnd) or True)

    assert da.focus_window("notepad") is True
    assert attivate == [101]


def test_focus_senza_finestra_corrispondente(monkeypatch):
    attivate = []
    monkeypatch.setattr(da, "_enumerate_windows", lambda: _finestre("Documento - Word"))
    monkeypatch.setattr(da, "_activate_window", lambda hwnd: attivate.append(hwnd) or True)

    assert da.focus_window("paint") is False
    assert attivate == []


def test_focus_attivazione_fallita(monkeypatch):
    monkeypatch.setattr(da, "_enumerate_windows", lambda: _finestre("Paint"))
    monkeypatch.setattr(da, "_activate_window", lambda hwnd: False)

    assert da.focus_window("paint") is False


def test_focus_senza_nome_app(monkeypatch):
    monkeypatch.setattr(da, "_activate_window", lambda hwnd: pytest.fail("non deve attivare"))

    assert da.focus_window("") is False


# ── Azione composta: apri app + IdeaSession ───────────────────────────────────

def test_azione_composta_crea_la_sessione(store, lanciate):
    esito = da.apri_app_e_inizia_idea("paint", "dipingere un tramonto", "clio", confirmed=True)

    sessioni = idea.list_idea_sessions()
    assert len(sessioni) == 1
    sessione = sessioni[0]
    assert "✅" in esito and "mspaint.exe" in esito
    assert lanciate == ["mspaint.exe"]
    assert sessione.project_id == "clio"
    assert sessione.user_goal == "dipingere un tramonto"
    assert sessione.active_app == "mspaint.exe"
    assert sessione.status == idea.STATUS_ACTIVE
    assert sessione.last_verified_at
    assert store.is_file()


def test_azione_composta_senza_sessione_se_l_app_fallisce(store, monkeypatch):
    def esplode(exe):
        raise OSError("avvio impossibile")

    monkeypatch.setattr(da, "_launch_process", esplode)

    esito = da.apri_app_e_inizia_idea("paint", "dipingere un tramonto", confirmed=True)

    assert "❌" in esito
    assert idea.list_idea_sessions() == []


def test_azione_composta_senza_obiettivo_non_apre_nulla(store, lanciate):
    esito = da.apri_app_e_inizia_idea("paint", "   ", confirmed=True)

    assert "❌" in esito and "obiettivo" in esito.lower()
    assert lanciate == []
    assert idea.list_idea_sessions() == []


def test_azione_composta_app_non_consentita(store, lanciate):
    esito = da.apri_app_e_inizia_idea("cmd.exe", "aprire un terminale", confirmed=True)

    assert "❌" in esito and "non consentita" in esito
    assert lanciate == []
    assert idea.list_idea_sessions() == []


def test_azione_composta_senza_conferma_non_apre_nulla(store, lanciate):
    esito = da.apri_app_e_inizia_idea("paint", "dipingere un tramonto")

    assert "⌛" in esito
    assert lanciate == []
    assert idea.list_idea_sessions() == []


def test_azione_composta_accetta_la_whitelist_da_ambiente(store, lanciate, monkeypatch):
    monkeypatch.setenv(da.ENV_ALLOWED_APPS, "notepad.exe")

    assert "✅" in da.apri_app_e_inizia_idea("notepad", "scrivere la scaletta", confirmed=True)
    assert "❌" in da.apri_app_e_inizia_idea("paint", "dipingere", confirmed=True)
    assert lanciate == ["notepad.exe"]


# ── Integrazione con policy, registro e LLM ──────────────────────────────────

def test_policy_apertura_sensibile_e_focus_reversibile():
    assert policy.classify(da.LAUNCH_ACTION) == "sensitive"
    assert policy.classify(da.FOCUS_ACTION) == "reversibile"


def test_azioni_registrate_nel_registro_unico():
    assert registry.AZIONI_MAP[da.LAUNCH_ACTION] is da.apri_app_e_inizia_idea
    assert registry.AZIONI_MAP[da.FOCUS_ACTION] is da.focus_window


def test_metadati_llm_presenti():
    from jarvis_brain import TOOLS_METADATA

    assert TOOLS_METADATA[da.LAUNCH_ACTION]["params"] == ["app_name", "obiettivo", "progetto"]
    assert TOOLS_METADATA[da.FOCUS_ACTION]["params"] == ["app_name"]


def test_describe_desktop_agent():
    testo = da.describe_desktop_agent()

    assert "Desktop Agent" in testo and "mspaint.exe" in testo
