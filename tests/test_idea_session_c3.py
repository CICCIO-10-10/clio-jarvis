# -*- coding: utf-8 -*-
"""test_idea_session_c3.py — C3: sessione persistente e "Riprendi da qui".

Nessun vault toccato (il bridge di The Scribe è disattivato: le osservazioni
finiscono nell'outbox in tmp), nessun LLM, nessun TTS vero (hook iniettabile).
Copre:
  * i campi C3 della IdeaSession (default, normalizzazione, store vecchi);
  * add_materiale / add_decisione / set_prossimo_passo / set_blocco_corrente;
  * get_resume_idea (idea popolata e idea inesistente);
  * il testo del resume e la domanda sui materiali;
  * il pre-router deterministico di jarvis_brain (con e senza idea aperta);
  * l'invio a The Scribe con la categoria giusta (decisione / completamento).

Esecuzione:  python -m pytest -q tests/test_idea_session_c3.py
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idea_session as idea  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402
import scribe_control  # noqa: E402

CHIAVI_RESUME = {"obiettivo", "materiali", "decisioni", "prossimo_passo",
                 "blocco", "ultima_verifica"}


# ── Isolamento: store in tmp, osservazioni nell'outbox in tmp, niente vault ──
@pytest.fixture(autouse=True)
def _isolamento(tmp_path, monkeypatch):
    monkeypatch.setattr(idea, "SESSIONS_FILE", tmp_path / "idea_sessions.json")
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "scribe_outbox"))
    # Il bridge scriverebbe la nota VERA nel vault: nei test resta spento e
    # l'osservazione va nell'outbox locale (che è comunque il percorso reale).
    monkeypatch.setattr(scribe_control, "_BRIDGE_HAS_OBSERVATION", False)
    monkeypatch.setattr(idea, "_ULTIMO_SCRIBE", {}, raising=False)
    yield


@pytest.fixture
def sessione():
    return idea.create_idea_session("disegnare il poster del concerto", "clio")


@pytest.fixture
def sessione_popolata(sessione):
    """Idea con tutto quello che serve per riprendere il lavoro."""
    idea.add_decisione(sessione.idea_id, "Uso il blu come colore principale")
    idea.add_decisione(sessione.idea_id, "Il titolo va in altro a sinistra")
    idea.set_prossimo_passo(sessione.idea_id, "abbozzare la tipografia")
    idea.add_materiale(sessione.idea_id, r"os.path.expanduser("~")\Desktop\bozza.png")
    idea.add_materiale(sessione.idea_id, r"os.path.expanduser("~")\Desktop\palette.md")
    idea.set_blocco_corrente(sessione.idea_id, "manca il logo del gruppo")
    return idea.load_idea_session(sessione.idea_id)


# ── Campi C3 della dataclass ────────────────────────────────────────────────
def test_campi_c3_hanno_default_vuoti(sessione):
    assert sessione.materiali == []
    assert sessione.decisioni == []
    assert sessione.prossimo_passo == ""
    assert sessione.blocco_corrente is None


def test_store_vecchio_senza_campi_c3_resta_leggibile():
    vecchia = idea.IdeaSession.from_dict(
        {"idea_id": "abc-1", "project_id": "clio", "user_goal": "vecchia idea",
         "last_observation": "un cubo", "file_3d": "x.obj", "status_3d": "fallback"})

    assert vecchia.materiali == [] and vecchia.decisioni == []
    assert vecchia.prossimo_passo == "" and vecchia.blocco_corrente is None


def test_materiali_normalizzati_e_senza_duplicati():
    sessione = idea.IdeaSession(
        idea_id="abc-2", project_id="clio", user_goal="idea",
        materiali=["  C:\\a\\uno.png  ", "", "c:\\A\\UNO.png", 42, "C:\\b\\due.md"])

    assert sessione.materiali == ["C:\\a\\uno.png", "C:\\b\\due.md"]


def test_decisioni_accettano_stringhe_e_dizionari():
    sessione = idea.IdeaSession(
        idea_id="abc-3", project_id="clio", user_goal="idea",
        decisioni=["scelta A", {"testo": "scelta B", "data": "2026-10-28"},
                   {"testo": "   "}, {"boh": "x"}, 7])

    assert [d["testo"] for d in sessione.decisioni] == ["scelta A", "scelta B"]
    assert sessione.decisioni[1]["data"] == "2026-10-28"


def test_decisioni_limitano_il_numero(monkeypatch):
    monkeypatch.setattr(idea, "MAX_DECISIONI", 3)
    sessione = idea.IdeaSession(idea_id="abc-4", project_id="clio", user_goal="idea",
                                decisioni=[f"d{i}" for i in range(10)])

    assert len(sessione.decisioni) == 3


def test_blocco_vuoto_diventa_none():
    sessione = idea.IdeaSession(idea_id="abc-5", project_id="clio", user_goal="idea",
                                blocco_corrente="   ")

    assert sessione.blocco_corrente is None


def test_todict_e_json_serializzabile(sessione_popolata):
    dati = sessione_popolata.to_dict()

    assert json.loads(json.dumps(dati, ensure_ascii=False))["prossimo_passo"] == \
        "abbozzare la tipografia"
    assert len(dati["materiali"]) == 2 and len(dati["decisioni"]) == 2


def test_describe_mostra_lo_stato_c3(sessione_popolata):
    testo = sessione_popolata.describe()

    assert "prossimo passo: abbozzare la tipografia" in testo
    assert "blocco: manca il logo del gruppo" in testo
    assert "decisioni: 2 | materiali: 2" in testo


# ── add_materiale ───────────────────────────────────────────────────────────
def test_add_materiale_salva_su_disco(sessione):
    assert idea.add_materiale(sessione.idea_id, r"C:\x\schizzo.png") is True

    riletta = idea.load_idea_session(sessione.idea_id)
    assert riletta.materiali == [r"C:\x\schizzo.png"]
    assert riletta.last_verified_at                     # touch()
    assert "schizzo.png" in idea.SESSIONS_FILE.read_text(encoding="utf-8")


def test_add_materiale_duplicato_non_si_aggiunge(sessione):
    idea.add_materiale(sessione.idea_id, r"C:\x\schizzo.png")

    assert idea.add_materiale(sessione.idea_id, "c:\\X\\SCHIZZO.png") is False
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 1


@pytest.mark.parametrize("path", ["", "   ", None, 12])
def test_add_materiale_percorso_non_valido(sessione, path):
    assert idea.add_materiale(sessione.idea_id, path) is False
    assert idea.load_idea_session(sessione.idea_id).materiali == []


def test_add_materiale_idea_inesistente():
    assert idea.add_materiale("idea-fantasma", r"C:\x\a.png") is False


def test_add_materiale_rispetta_il_limite(sessione, monkeypatch):
    monkeypatch.setattr(idea, "MAX_MATERIALI", 2)
    assert idea.add_materiale(sessione.idea_id, r"C:\x\1.png") is True
    assert idea.add_materiale(sessione.idea_id, r"C:\x\2.png") is True

    assert idea.add_materiale(sessione.idea_id, r"C:\x\3.png") is False
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 2


# ── add_decisione + The Scribe ──────────────────────────────────────────────
def test_add_decisione_salva_con_data(sessione):
    assert idea.add_decisione(sessione.idea_id, "Il logo va in alto") is True

    decisioni = idea.load_idea_session(sessione.idea_id).decisioni
    assert decisioni[0]["testo"] == "Il logo va in alto"
    assert decisioni[0]["data"].endswith("Z") and len(decisioni[0]["data"]) >= 19


def test_add_decisione_vuota_non_salva(sessione):
    assert idea.add_decisione(sessione.idea_id, "   ") is False
    assert idea.load_idea_session(sessione.idea_id).decisioni == []


def test_add_decisione_ripetuta_non_si_conta_due_volte(sessione, monkeypatch):
    inviate = []
    monkeypatch.setattr(
        scribe_control, "record_observation",
        lambda project_id, observation, workflow_id=None, category="nota":
            inviate.append(observation) or {"ok": True, "recorded": "scribe",
                                            "path": "X.md", "error": None})

    assert idea.add_decisione(sessione.idea_id, "Uso il blu") is True
    assert idea.add_decisione(sessione.idea_id, "  uso IL blu ") is False

    assert len(idea.get_resume_idea(sessione.idea_id)["decisioni"]) == 1
    assert inviate == ["Uso il blu"]                      # a Scribe una volta sola
    assert "già registrata" in idea.aggiungi_decisione_idea("Uso il blu", sessione.idea_id)


def test_add_decisione_manda_l_osservazione_a_scribe(sessione, monkeypatch):
    inviate = []

    def finto_record_observation(project_id, observation, workflow_id=None, category="nota"):
        inviate.append({"project": project_id, "testo": observation,
                        "workflow": workflow_id, "categoria": category})
        return {"ok": True, "recorded": "scribe", "project": project_id,
                "path": "Progetti/CLIO/Decisioni.md", "error": None, "bridge": True}

    monkeypatch.setattr(scribe_control, "record_observation", finto_record_observation)

    idea.add_decisione(sessione.idea_id, "Uso il blu")

    assert inviate[0]["categoria"] == "decisione"
    assert inviate[0]["project"] == "clio"
    assert inviate[0]["testo"] == "Uso il blu"
    assert inviate[0]["workflow"].startswith("idea-")
    assert idea.last_scribe_observation()["ok"] is True


def test_decisione_salvata_anche_se_scribe_fallisce(sessione, monkeypatch):
    def esplode(*argomenti, **kwargs):
        raise RuntimeError("vault offline")

    monkeypatch.setattr(scribe_control, "record_observation", esplode)

    assert idea.add_decisione(sessione.idea_id, "Uso il rosso") is True
    assert idea.load_idea_session(sessione.idea_id).decisioni[0]["testo"] == "Uso il rosso"
    assert idea.last_scribe_observation()["ok"] is False
    assert "vault offline" in idea.last_scribe_observation()["error"]


def test_decisione_finisce_nell_outbox_se_il_bridge_manca(sessione):
    """Senza bridge CLIO scrive l'outbox: nessun accesso diretto al vault."""
    idea.add_decisione(sessione.idea_id, "Il titolo va in altro a sinistra")

    esito = idea.last_scribe_observation()
    outbox = Path(esito["path"])
    assert esito["recorded"] == "outbox"
    assert outbox.is_file()
    testo = outbox.read_text(encoding="utf-8")
    assert "categoria: decisione" in testo
    assert "Il titolo va in altro a sinistra" in testo


# ── set_prossimo_passo + The Scribe ─────────────────────────────────────────
def test_set_prossimo_passo_salva(sessione):
    assert idea.set_prossimo_passo(sessione.idea_id, "abbozzare la tipografia") is True

    assert idea.load_idea_session(sessione.idea_id).prossimo_passo == "abbozzare la tipografia"


def test_set_prossimo_passo_vuoto_non_salva(sessione):
    assert idea.set_prossimo_passo(sessione.idea_id, "") is False
    assert idea.load_idea_session(sessione.idea_id).prossimo_passo == ""


def test_set_prossimo_passo_manda_osservazione_di_completamento(sessione, monkeypatch):
    inviate = []
    monkeypatch.setattr(
        scribe_control, "record_observation",
        lambda project_id, observation, workflow_id=None, category="nota":
            inviate.append(category) or {"ok": True, "recorded": "scribe",
                                         "path": "X.md", "error": None})

    idea.set_prossimo_passo(sessione.idea_id, "stampare una prova")

    assert inviate == ["completamento"]


def test_set_prossimo_passo_idea_inesistente():
    assert idea.set_prossimo_passo("idea-fantasma", "fare qualcosa") is False


# ── set_blocco_corrente ─────────────────────────────────────────────────────
def test_set_blocco_e_poi_azzeralo(sessione):
    assert idea.set_blocco_corrente(sessione.idea_id, "manca il logo") is True
    assert idea.load_idea_session(sessione.idea_id).blocco_corrente == "manca il logo"

    assert idea.set_blocco_corrente(sessione.idea_id, "") is True
    assert idea.load_idea_session(sessione.idea_id).blocco_corrente is None


# ── get_resume_idea ─────────────────────────────────────────────────────────
def test_get_resume_idea_su_idea_popolata(sessione_popolata):
    resume = idea.get_resume_idea(sessione_popolata.idea_id)

    assert CHIAVI_RESUME <= set(resume)
    assert resume["obiettivo"] == "disegnare il poster del concerto"
    assert resume["prossimo_passo"] == "abbozzare la tipografia"
    assert resume["blocco"] == "manca il logo del gruppo"
    assert [d["testo"] for d in resume["decisioni"]] == [
        "Uso il blu come colore principale", "Il titolo va in altro a sinistra"]
    assert resume["materiali"] == [r"os.path.expanduser("~")\Desktop\bozza.png",
                                   r"os.path.expanduser("~")\Desktop\palette.md"]
    assert resume["ultima_verifica"].endswith("Z")
    assert resume["trovata"] is True


def test_get_resume_idea_modifica_del_dict_non_tocca_lo_store(sessione_popolata):
    resume = idea.get_resume_idea(sessione_popolata.idea_id)
    resume["materiali"].append("aggiunto-a-mano")
    resume["decisioni"][0]["testo"] = "cambiato"

    riletto = idea.get_resume_idea(sessione_popolata.idea_id)
    assert len(riletto["materiali"]) == 2
    assert riletto["decisioni"][0]["testo"] == "Uso il blu come colore principale"


def test_get_resume_idea_inesistente_ha_tutte_le_chiavi():
    resume = idea.get_resume_idea("idea-fantasma")

    assert CHIAVI_RESUME <= set(resume)
    assert resume["obiettivo"] == "" and resume["materiali"] == []
    assert resume["decisioni"] == [] and resume["blocco"] is None
    assert resume["trovata"] is False


# ── Testo e voce del "Riprendi da qui" ──────────────────────────────────────
def test_format_resume_dice_tutto(sessione_popolata):
    testo = idea.format_resume(sessione_popolata)

    assert "Stavamo lavorando a disegnare il poster del concerto" in testo
    assert "Il prossimo passo è abbozzare la tipografia." in testo
    assert "2 decisioni" in testo and "2 materiali" in testo
    assert "manca il logo del gruppo" in testo
    assert "Vuoi che apra uno dei materiali?" in testo
    assert "bozza.png" in testo and "palette.md" in testo


def test_format_resume_conta_al_singolare(sessione):
    idea.add_decisione(sessione.idea_id, "una sola")
    idea.add_materiale(sessione.idea_id, r"C:\x\uno.png")

    testo = idea.format_resume(idea.load_idea_session(sessione.idea_id))

    assert "1 decisione e 1 materiale" in testo


def test_format_resume_senza_prossimo_passo(sessione):
    testo = idea.format_resume(sessione)

    assert "Il prossimo passo non è ancora definito." in testo
    assert "0 decisioni e 0 materiali" in testo
    assert "Vuoi che apra uno dei materiali?" not in testo


def test_riprendi_da_qui_legge_ad_alta_voce(sessione_popolata):
    detto = []

    testo = idea.riprendi_da_qui(parla=detto.append)

    assert detto == [testo] and "Stavamo lavorando a" in testo


def test_riprendi_da_qui_senza_idee():
    testo = idea.riprendi_da_qui()

    assert "Non ho nessuna idea aperta" in testo
    assert "apri_app_e_inizia_idea" in testo


def test_riprendi_da_qui_filtra_per_progetto():
    idea.create_idea_session("idea del sito", "clio-web")

    testo = idea.riprendi_da_qui(progetto="clio-web")

    assert "idea del sito" in testo


def test_riprendi_da_qui_usa_la_piu_recente_aperta():
    idea.create_idea_session("prima idea", "clio")
    seconda = idea.create_idea_session("seconda idea", "clio")
    idea.close_idea_session(seconda.idea_id, status=idea.STATUS_DONE)
    idea.create_idea_session("terza idea", "clio")

    testo = idea.riprendi_da_qui(progetto="clio")

    assert "terza idea" in testo
    assert "seconda idea" not in testo        # chiusa: non si riprende
    assert "prima idea" not in testo


def test_riprendi_da_qui_con_idea_id_esplicito(sessione):
    altra = idea.create_idea_session("un'altra idea", "clio")

    testo = idea.riprendi_da_qui(idea_id=sessione.idea_id)

    assert "disegnare il poster del concerto" in testo
    assert "un'altra idea" not in testo
    assert altra.idea_id != sessione.idea_id


def test_tts_rotto_non_impedisce_la_risposta(sessione):
    def tts_rotto(testo):
        raise RuntimeError("audio occupato")

    assert "Stavamo lavorando a" in idea.riprendi_da_qui(parla=tts_rotto)


# ── Pre-router deterministico in jarvis_brain ───────────────────────────────
@pytest.mark.parametrize("frase", [
    "riprendi da qui",
    "Clio, riprendi da qui",
    "riprendiamo da qui",
    "dove eravamo rimasti",
    "dove eravamo rimasti su clio",
    "dove ci eravamo fermati",
    "dove ero rimasto",
    "da dove ripartiamo",
    "riprendi l'idea",
])
def test_pre_router_riconosce_le_frasi(frase, sessione_popolata):
    import jarvis_brain as jb

    risposta = jb._riprendi_da_qui_pre_router(frase)

    assert risposta and "Stavamo lavorando a disegnare il poster del concerto" in risposta
    assert "abbozzare la tipografia" in risposta


@pytest.mark.parametrize("frase", ["che ore sono", "apri paint", "riassumi il vault", ""])
def test_pre_router_ignora_le_altre_frasi(frase, sessione_popolata):
    import jarvis_brain as jb

    assert jb._riprendi_da_qui_pre_router(frase) is None


def test_pre_router_senza_idee_lascia_lavorare_scribe():
    """Il comportamento precedente (router di The Scribe) resta intatto."""
    import jarvis_brain as jb

    assert jb._riprendi_da_qui_pre_router("dove ero rimasto su clio") is None
    assert scribe_control.detect_scribe_intent("dove ero rimasto su clio") is not None


def test_pre_router_store_corrotto_risponde_senza_sovrascriverlo():
    import jarvis_brain as jb

    contenuto_corrotto = "{ sessione non valida"
    idea.SESSIONS_FILE.write_text(contenuto_corrotto, encoding="utf-8")

    risposta = jb._riprendi_da_qui_pre_router("riprendi da qui")

    assert "store delle idee è danneggiato o non leggibile" in risposta
    assert "Non ho modificato i dati" in risposta
    assert idea.SESSIONS_FILE.read_text(encoding="utf-8") == contenuto_corrotto


def test_pre_router_progetto_sbagliato_non_risponde(sessione_popolata):
    import jarvis_brain as jb

    assert jb._riprendi_da_qui_pre_router("riprendi da qui sul progetto sito-web") is None


def test_pre_router_chiama_il_tts(sessione_popolata):
    import jarvis_brain as jb

    detto = []
    jb.set_tts_hook(detto.append)
    try:
        risposta = jb._riprendi_da_qui_pre_router("riprendi da qui")
    finally:
        jb.set_tts_hook(None)

    assert detto == [risposta]
    assert jb._TTS_HOOK is None


def test_think_and_act_risponde_senza_llm(sessione_popolata, monkeypatch, tmp_path):
    """Il flusso vero: think_and_act corto-circuita prima del LLM e 'parla'."""
    import jarvis_brain as jb

    monkeypatch.setattr(jb.JarvisBrain, "_session_file",
                        lambda self: str(tmp_path / "jarvis_session.json"))
    monkeypatch.setattr(jb, "_riprendi_da_qui_pre_router", jb._riprendi_da_qui_pre_router)
    # Se il codice arrivasse al LLM questo esploderebbe: la prova che non ci arriva.
    monkeypatch.setattr(jb, "chat", lambda *a, **k: pytest.fail("non deve chiamare il LLM"),
                        raising=False)

    detto = []
    jb.set_tts_hook(detto.append)
    try:
        cervello = jb.JarvisBrain()
        risposta = cervello.think_and_act("Clio, riprendi da qui", origin="voice")
    finally:
        jb.set_tts_hook(None)

    assert "Stavamo lavorando a disegnare il poster del concerto" in risposta
    assert detto == [risposta]
    assert cervello.session_history[-1]["assistant"].startswith("Stavamo lavorando a")


# ── Registro, policy, metadati ──────────────────────────────────────────────
@pytest.mark.parametrize("azione", ["riprendi_da_qui", "aggiungi_decisione_idea",
                                    "imposta_prossimo_passo", "aggiungi_materiale_idea",
                                    "imposta_blocco_idea"])
def test_azioni_c3_registrate_e_read_only(azione):
    assert azione in registry.AZIONI_MAP
    assert policy.classify(azione) == "read-only"


def test_metadati_llm_c3():
    from jarvis_brain import TOOLS_METADATA

    assert TOOLS_METADATA["riprendi_da_qui"]["params"] == ["progetto", "idea_id"]
    assert TOOLS_METADATA["aggiungi_decisione_idea"]["params"] == ["testo", "idea_id"]
    assert TOOLS_METADATA["imposta_prossimo_passo"]["params"] == ["testo", "idea_id"]


def test_azioni_c3_chiamabili_dal_control_plane(sessione):
    """Stessa strada di voce/API: registry -> policy.guarded_call -> idea_session."""
    esito = policy.guarded_call("aggiungi_decisione_idea",
                                registry.AZIONI_MAP["aggiungi_decisione_idea"],
                                {"testo": "Decisione presa via control-plane"},
                                origin="flask")
    assert "Decisione registrata" in esito

    esito = policy.guarded_call("imposta_prossimo_passo",
                                registry.AZIONI_MAP["imposta_prossimo_passo"],
                                {"testo": "fare la prova di stampa"}, origin="voice")
    assert "Prossimo passo" in esito

    resume = idea.get_resume_idea(sessione.idea_id)
    assert resume["decisioni"][0]["testo"] == "Decisione presa via control-plane"
    assert resume["prossimo_passo"] == "fare la prova di stampa"


def test_azioni_c3_senza_idea_rispondono_con_errore_chiaro():
    assert "❌" in idea.aggiungi_decisione_idea("qualcosa")
    assert "❌" in idea.imposta_prossimo_passo("qualcosa")
    assert "❌" in idea.aggiungi_materiale_idea(r"C:\x\a.png")
