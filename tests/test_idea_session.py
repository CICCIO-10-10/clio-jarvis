# -*- coding: utf-8 -*-
"""test_idea_session.py — FASE 2: struttura IdeaSession, senza toccare data/.

Lo store è sempre un file in tmp_path (`monkeypatch` su `SESSIONS_FILE`): i test
non leggono e non scrivono il `data/idea_sessions.json` reale. Copre:
  * creazione (UUID v4, stato, last_verified_at, slug del progetto, trim del goal);
  * persistenza atomica e round-trip (load / list / filtri);
  * chiusura, verifica, e casi d'errore con messaggi puliti;
  * store assente: nessuna sessione; store corrotto: scritture bloccate e dati preservati.

Esecuzione:  python -m pytest -q tests/test_idea_session.py
"""
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idea_session as idea  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    percorso = tmp_path / "idea_sessions.json"
    monkeypatch.setattr(idea, "SESSIONS_FILE", percorso)
    return percorso


# ── Creazione ─────────────────────────────────────────────────────────────────

def test_create_restituisce_la_sessione_completa(store):
    sessione = idea.create_idea_session("dipingere un tramonto", "clio")

    assert isinstance(sessione, idea.IdeaSession)
    uuid.UUID(sessione.idea_id)                     # UUID valido
    assert str(uuid.UUID(sessione.idea_id).version) == "4"
    assert sessione.project_id == "clio"
    assert sessione.user_goal == "dipingere un tramonto"
    assert sessione.active_app == ""
    assert sessione.status == idea.STATUS_ACTIVE
    assert sessione.last_verified_at.endswith("Z")


def test_create_salva_su_disco(store):
    sessione = idea.create_idea_session("scrivere la scaletta", "tyrion-clips-studio",
                                        active_app="notepad.exe")

    dati = json.loads(store.read_text(encoding="utf-8"))
    assert len(dati["sessions"]) == 1
    assert dati["sessions"][0] == sessione.to_dict()
    assert not store.with_name(store.name + ".tmp").exists()   # nessun file temporaneo


def test_create_ripulisce_il_goal(store):
    sessione = idea.create_idea_session("   dipingere un tramonto   ", "clio")

    assert sessione.user_goal == "dipingere un tramonto"


def test_create_normalizza_il_project_id(store):
    sessione = idea.create_idea_session("idea", "Clio Studio!")

    assert sessione.project_id == "clio-studio"


def test_create_senza_goal_e_un_errore_pulito(store):
    with pytest.raises(idea.IdeaSessionError, match="obiettivo mancante"):
        idea.create_idea_session("   ", "clio")
    assert not store.exists()


def test_create_senza_progetto_e_un_errore_pulito(store):
    with pytest.raises(idea.IdeaSessionError, match="project_id mancante"):
        idea.create_idea_session("idea", "")


def test_goal_troppo_lungo_rifiutato(store):
    with pytest.raises(idea.IdeaSessionError, match="troppo lungo"):
        idea.create_idea_session("x" * (idea.MAX_GOAL_CHARS + 1), "clio")


def test_due_sessioni_hanno_id_diversi(store):
    prima = idea.create_idea_session("prima", "clio")
    seconda = idea.create_idea_session("seconda", "clio")

    assert prima.idea_id != seconda.idea_id
    assert len(idea.list_idea_sessions()) == 2


# ── Lettura ───────────────────────────────────────────────────────────────────

def test_load_ritrova_la_sessione(store):
    creata = idea.create_idea_session("dipingere", "clio", active_app="mspaint.exe")

    letta = idea.load_idea_session(creata.idea_id)

    assert letta is not None
    assert letta.to_dict() == creata.to_dict()


@pytest.mark.parametrize("chiave", ["", "   ", "non-esiste", None])
def test_load_sconosciuta_ritorna_none(store, chiave):
    idea.create_idea_session("dipingere", "clio")

    assert idea.load_idea_session(chiave) is None


def test_list_filtra_per_progetto_e_stato(store):
    idea.create_idea_session("a", "clio")
    b = idea.create_idea_session("b", "clio")
    idea.create_idea_session("c", "tyrion-clips-studio")
    idea.close_idea_session(b.idea_id, idea.STATUS_DONE)

    assert len(idea.list_idea_sessions()) == 3
    assert len(idea.list_idea_sessions(project_id="clio")) == 2
    assert len(idea.list_idea_sessions(project_id="tyrion-clips-studio")) == 1
    assert [s.user_goal for s in idea.list_idea_sessions(status=idea.STATUS_ACTIVE)] == ["a", "c"]


def test_list_rifiuta_uno_stato_inventato(store):
    with pytest.raises(idea.IdeaSessionError, match="non valido"):
        idea.list_idea_sessions(status="quasi-fatto")


def test_get_active_idea_session(store):
    prima = idea.create_idea_session("prima", "clio")
    seconda = idea.create_idea_session("seconda", "clio")
    idea.close_idea_session(prima.idea_id, idea.STATUS_DONE)

    attiva = idea.get_active_idea_session("clio")

    assert attiva is not None and attiva.idea_id == seconda.idea_id


# ── Chiusura e verifica ───────────────────────────────────────────────────────

def test_close_segna_la_sessione_come_chiusa(store):
    sessione = idea.create_idea_session("dipingere", "clio")

    chiusa = idea.close_idea_session(sessione.idea_id, idea.STATUS_DONE)

    assert chiusa.status == idea.STATUS_DONE
    assert chiusa.is_open is False
    assert idea.load_idea_session(sessione.idea_id).status == idea.STATUS_DONE


def test_close_con_stato_non_ammesso(store):
    sessione = idea.create_idea_session("dipingere", "clio")

    with pytest.raises(idea.IdeaSessionError, match="done.*abandoned"):
        idea.close_idea_session(sessione.idea_id, idea.STATUS_ACTIVE)


def test_close_di_una_idea_inesistente(store):
    with pytest.raises(idea.IdeaSessionError, match="non trovata"):
        idea.close_idea_session("idea-fantasma")


def test_verify_aggiorna_last_verified_at(store, monkeypatch):
    orari = iter(["2026-10-28T10:00:00Z", "2026-10-28T10:05:00Z"])
    monkeypatch.setattr(idea, "_now", lambda: next(orari))

    sessione = idea.create_idea_session("dipingere", "clio")
    assert sessione.last_verified_at == "2026-10-28T10:00:00Z"

    verificata = idea.verify_idea_session(sessione.idea_id)

    assert verificata.last_verified_at == "2026-10-28T10:05:00Z"
    assert idea.load_idea_session(sessione.idea_id).last_verified_at == "2026-10-28T10:05:00Z"


def test_verify_di_una_idea_inesistente(store):
    with pytest.raises(idea.IdeaSessionError, match="non trovata"):
        idea.verify_idea_session("idea-fantasma")


def test_update_salva_le_modifiche(store):
    sessione = idea.create_idea_session("dipingere", "clio")
    sessione.active_app = "mspaint.exe"
    sessione.status = idea.STATUS_PAUSED

    idea.update_idea_session(sessione)

    riletta = idea.load_idea_session(sessione.idea_id)
    assert riletta.active_app == "mspaint.exe"
    assert riletta.status == idea.STATUS_PAUSED


def test_update_di_una_idea_mai_creata(store):
    orfana = idea.IdeaSession(idea_id=str(uuid.uuid4()), project_id="clio", user_goal="x")

    with pytest.raises(idea.IdeaSessionError, match="non trovata"):
        idea.update_idea_session(orfana)


# ── Robustezza dello store ────────────────────────────────────────────────────

def test_store_assente_non_esplode(store):
    assert idea.list_idea_sessions() == []
    assert idea.load_idea_session("qualsiasi") is None


def test_store_corrotto_viene_preservato_e_blocca_le_scritture(store):
    contenuto_corrotto = "{ questo non è json"
    store.write_text(contenuto_corrotto, encoding="utf-8")

    with pytest.raises(idea.IdeaSessionError, match="corrotto"):
        idea.list_idea_sessions()
    with pytest.raises(idea.IdeaSessionError, match="corrotto"):
        idea.create_idea_session("ripartiamo", "clio")

    assert store.read_text(encoding="utf-8") == contenuto_corrotto


def test_righe_non_valide_vengono_scartate(store):
    orari = {"idea_id": "", "project_id": "clio", "user_goal": "rotta"}
    valida = idea.IdeaSession(idea_id=str(uuid.uuid4()), project_id="clio", user_goal="buona")
    store.write_text(json.dumps({"sessions": [orari, valida.to_dict()]}), encoding="utf-8")

    sessioni = idea.list_idea_sessions()

    assert [s.idea_id for s in sessioni] == [valida.idea_id]


# ── Validazione del dataclass ─────────────────────────────────────────────────

@pytest.mark.parametrize("campo,valore,messaggio", [
    ("idea_id", "", "idea_id mancante"),
    ("status", "in-corso", "non valido"),
])
def test_dataclass_rifiuta_valori_invalidi(campo, valore, messaggio):
    argomenti = {"idea_id": str(uuid.uuid4()), "project_id": "clio",
                 "user_goal": "dipingere", campo: valore}

    with pytest.raises(idea.IdeaSessionError, match=messaggio):
        idea.IdeaSession(**argomenti)


def test_from_dict_e_round_trip():
    sessione = idea.IdeaSession(idea_id=str(uuid.uuid4()), project_id="clio",
                                user_goal="dipingere", active_app="mspaint.exe")

    ricostruita = idea.IdeaSession.from_dict(sessione.to_dict())

    assert ricostruita == sessione


def test_from_dict_non_valido():
    with pytest.raises(idea.IdeaSessionError):
        idea.IdeaSession.from_dict({"project_id": "clio", "user_goal": "x"})


def test_describe_elenca_le_sessioni(store):
    idea.create_idea_session("dipingere un tramonto", "clio")

    testo = idea.describe_idea_sessions()

    assert "clio" in testo and "dipingere un tramonto" in testo
    assert idea.describe_idea_sessions(project_id="altro") == "Nessuna IdeaSession registrata."
