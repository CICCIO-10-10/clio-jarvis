# -*- coding: utf-8 -*-
"""test_vision_agent.py — FASE 2 · C2: Vision Agent con MOCK.

Nessuna app viene aperta, nessuna finestra reale viene catturata e nessuna
chiamata a Gemini viene fatta: `find_window`, `_window_rect`, `_grab_region` e
`_default_vision` sono sostituiti da doppi. Copre:
  * capture_window: solo la finestra indicata, errori puliti (app/finestra/rect/save);
  * describe_sketch: prompt di default e personalizzato, file mancante/vuoto,
    backend che esplode, risposta vuota, troncamento;
  * analyze_sketch_and_update_idea: aggiorna la IdeaSession, non la tocca se la
    cattura o la visione falliscono, usa l'ultima idea aperta, privacy dei PNG;
  * catena control-plane -> registry -> azione, policy READ_ONLY, metadati LLM.

Esecuzione:  python -m pytest -q tests/test_vision_agent.py
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_agent as da  # noqa: E402
import idea_session as idea  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402
import vision_agent as va  # noqa: E402

DESCRIZIONE = "Un tramonto arancione con due colline blu e un sole basso."


# ── Doppi: nessuno schermo vero, nessun modello vero ─────────────────────────

class FakeImage:
    """Sostituto di PIL.Image: `.save()` scrive un PNG finto."""

    def __init__(self, errore=None):
        self.errore = errore
        self.salvati = []

    def save(self, percorso):
        if self.errore:
            raise self.errore
        Path(percorso).write_bytes(b"\x89PNG finto")
        self.salvati.append(Path(percorso))


@pytest.fixture(autouse=True)
def _isolamento(tmp_path, monkeypatch):
    """Store delle idee in tmp_path e nessun PNG conservato per default."""
    monkeypatch.setattr(idea, "SESSIONS_FILE", tmp_path / "idea_sessions.json")
    monkeypatch.delenv(va.ENV_KEEP_SCREENSHOTS, raising=False)


@pytest.fixture
def finestra(tmp_path, monkeypatch):
    """Paint aperto: finestra nota, rettangolo noto, cattura finta in tmp_path."""
    monkeypatch.setattr(
        da, "find_window",
        lambda app: {"hwnd": 42, "title": "Senza titolo - Paint", "pid": 7})
    monkeypatch.setattr(va, "_window_rect", lambda hwnd: (10, 20, 810, 620))
    monkeypatch.setattr(va, "_screenshot_dir", lambda: tmp_path)
    immagine = FakeImage()
    monkeypatch.setattr(va, "_grab_region", lambda rect: immagine)
    return immagine


@pytest.fixture
def vision_finta(monkeypatch):
    """Modello visivo finto: registra le chiamate e risponde con DESCRIZIONE."""
    chiamate = []

    def finto(percorso, prompt):
        chiamate.append((percorso, prompt))
        return DESCRIZIONE

    monkeypatch.setattr(va, "_default_vision", finto)
    return chiamate


def _immagine_finta(tmp_path, nome="sketch.png", contenuto=b"\x89PNG finto"):
    percorso = tmp_path / nome
    percorso.write_bytes(contenuto)
    return percorso


# ── capture_window ───────────────────────────────────────────────────────────

def test_capture_salva_un_png_della_finestra(finestra, tmp_path):
    percorso = va.capture_window("paint", out_dir=tmp_path)

    assert percorso is not None
    assert Path(percorso).is_file()
    assert Path(percorso).name.startswith("clio_window_mspaint_")
    assert Path(percorso).suffix == ".png"
    assert finestra.salvati == [Path(percorso)]


def test_capture_usa_il_rettangolo_della_finestra(tmp_path, monkeypatch):
    rettangoli = []
    monkeypatch.setattr(da, "find_window",
                        lambda app: {"hwnd": 42, "title": "Paint", "pid": 7})
    monkeypatch.setattr(va, "_window_rect", lambda hwnd: (10, 20, 810, 620))
    monkeypatch.setattr(va, "_grab_region",
                        lambda rect: rettangoli.append(rect) or FakeImage())

    va.capture_window("paint", out_dir=tmp_path)

    assert rettangoli == [(10, 20, 810, 620)]      # solo la finestra, non lo schermo


def test_capture_senza_finestra_non_scatta_nulla(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "find_window", lambda app: None)
    monkeypatch.setattr(va, "_grab_region", lambda rect: pytest.fail("non deve catturare"))

    assert va.capture_window("paint", out_dir=tmp_path) is None
    assert list(tmp_path.glob("*.png")) == []


def test_capture_finestra_troppo_piccola(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "find_window",
                        lambda app: {"hwnd": 42, "title": "Paint", "pid": 7})
    monkeypatch.setattr(va, "_window_rect", lambda hwnd: None)
    monkeypatch.setattr(va, "_grab_region", lambda rect: pytest.fail("non deve catturare"))

    assert va.capture_window("paint", out_dir=tmp_path) is None


@pytest.mark.parametrize("app", ["", "   ", None, 42])
def test_capture_app_non_valida(tmp_path, app):
    assert va.capture_window(app, out_dir=tmp_path) is None


def test_capture_errore_di_salvataggio_pulito(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "find_window",
                        lambda app: {"hwnd": 42, "title": "Paint", "pid": 7})
    monkeypatch.setattr(va, "_window_rect", lambda hwnd: (0, 0, 400, 300))
    monkeypatch.setattr(va, "_grab_region", lambda rect: FakeImage(errore=OSError("disco pieno")))

    assert va.capture_window("paint", out_dir=tmp_path) is None   # nessuna eccezione


def test_prune_tiene_solo_gli_ultimi_png(tmp_path):
    for indice in range(25):
        percorso = tmp_path / f"clio_window_test_{indice:02d}.png"
        percorso.write_bytes(b"x")
        os.utime(percorso, (time.time() + indice, time.time() + indice))

    va._prune(tmp_path, keep=20)

    assert len(list(tmp_path.glob("clio_window_*.png"))) == 20


# ── describe_sketch ──────────────────────────────────────────────────────────

def test_describe_usa_il_prompt_di_default(tmp_path, vision_finta):
    percorso = _immagine_finta(tmp_path)

    assert va.describe_sketch(str(percorso)) == DESCRIZIONE
    assert vision_finta[0][0] == str(percorso)
    assert vision_finta[0][1] == va.DEFAULT_PROMPT


def test_describe_usa_il_prompt_personalizzato(tmp_path, vision_finta):
    percorso = _immagine_finta(tmp_path)

    va.describe_sketch(str(percorso), "Che colori vedi?")

    assert vision_finta[0][1] == "Che colori vedi?"


def test_describe_immagine_mancante(tmp_path, vision_finta):
    esito = va.describe_sketch(str(tmp_path / "non_esiste.png"))

    assert esito.startswith("❌") and "non trovata" in esito
    assert vision_finta == []


def test_describe_immagine_vuota(tmp_path, vision_finta):
    percorso = _immagine_finta(tmp_path, contenuto=b"")

    esito = va.describe_sketch(str(percorso))

    assert esito.startswith("❌") and "vuota" in esito
    assert vision_finta == []


@pytest.mark.parametrize("valore", ["", None, "   "])
def test_describe_percorso_mancante(valore, vision_finta):
    assert va.describe_sketch(valore).startswith("❌")
    assert vision_finta == []


def test_describe_backend_che_esplode(tmp_path, monkeypatch):
    percorso = _immagine_finta(tmp_path)

    def esplode(immagine, prompt):
        raise RuntimeError("quota Gemini esaurita")

    monkeypatch.setattr(va, "_default_vision", esplode)

    esito = va.describe_sketch(str(percorso))

    assert esito.startswith("Vision non disponibile") and "quota Gemini" in esito


def test_describe_risposta_vuota(tmp_path, monkeypatch):
    percorso = _immagine_finta(tmp_path)
    monkeypatch.setattr(va, "_default_vision", lambda immagine, prompt: "   ")

    assert va.describe_sketch(str(percorso)).startswith("Vision non disponibile")


def test_describe_tronca_le_descrizioni_lunghe(tmp_path, monkeypatch):
    percorso = _immagine_finta(tmp_path)
    monkeypatch.setattr(va, "_default_vision", lambda immagine, prompt: "x" * 5000)

    esito = va.describe_sketch(str(percorso))

    assert len(esito) <= va.MAX_DESCRIPTION_CHARS + 1
    assert esito.endswith("…")


# ── analyze_sketch_and_update_idea ───────────────────────────────────────────

def test_analyze_aggiorna_la_sessione(finestra, vision_finta):
    sessione = idea.create_idea_session("dipingere un tramonto", "clio",
                                        active_app="mspaint.exe")

    esito = va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    riletta = idea.load_idea_session(sessione.idea_id)
    assert "🎨" in esito and DESCRIZIONE in esito
    assert riletta.last_observation == DESCRIZIONE
    assert riletta.active_app == "mspaint.exe"
    assert riletta.last_verified_at


def test_analyze_cancella_il_png_dopo_l_analisi(finestra, vision_finta):
    sessione = idea.create_idea_session("dipingere", "clio")

    va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    assert list(Path(va._screenshot_dir()).glob("clio_window_*.png")) == []


def test_analyze_conserva_il_png_se_richiesto(finestra, vision_finta, monkeypatch):
    monkeypatch.setenv(va.ENV_KEEP_SCREENSHOTS, "1")
    sessione = idea.create_idea_session("dipingere", "clio")

    va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    assert len(list(Path(va._screenshot_dir()).glob("clio_window_*.png"))) == 1


def test_analyze_idea_inesistente(vision_finta, monkeypatch):
    monkeypatch.setattr(va, "capture_window", lambda app: pytest.fail("non deve catturare"))

    esito = va.analyze_sketch_and_update_idea("idea-fantasma", "paint")

    assert esito.startswith("❌") and "non trovata" in esito
    assert vision_finta == []


def test_analyze_senza_idee_aperte(vision_finta, monkeypatch):
    monkeypatch.setattr(va, "capture_window", lambda app: pytest.fail("non deve catturare"))

    esito = va.analyze_sketch_and_update_idea("", "paint")

    assert esito.startswith("❌") and "Nessuna IdeaSession" in esito
    assert vision_finta == []


def test_analyze_senza_idea_id_usa_l_ultima_aperta(finestra, vision_finta):
    prima = idea.create_idea_session("prima idea", "clio")
    seconda = idea.create_idea_session("seconda idea", "clio")

    esito = va.analyze_sketch_and_update_idea("", "paint")

    assert "🎨" in esito
    assert idea.load_idea_session(seconda.idea_id).last_observation == DESCRIZIONE
    assert idea.load_idea_session(prima.idea_id).last_observation == ""


def test_analyze_cattura_fallita_non_tocca_la_sessione(vision_finta, monkeypatch):
    sessione = idea.create_idea_session("dipingere", "clio")
    monkeypatch.setattr(va, "capture_window", lambda app: None)

    esito = va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    assert esito.startswith("❌") and "catturare" in esito
    assert idea.load_idea_session(sessione.idea_id).last_observation == ""
    assert vision_finta == []


def test_analyze_vision_non_disponibile_non_tocca_la_sessione(finestra, monkeypatch):
    sessione = idea.create_idea_session("dipingere", "clio")
    monkeypatch.setattr(va, "_default_vision",
                        lambda immagine, prompt: "Vision non disponibile: chiave non valida.")

    esito = va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    assert esito.startswith("❌") and "visione non è disponibile" in esito
    assert idea.load_idea_session(sessione.idea_id).last_observation == ""


def test_analyze_app_non_valida(finestra, vision_finta):
    sessione = idea.create_idea_session("dipingere", "clio")

    esito = va.analyze_sketch_and_update_idea(sessione.idea_id, "   ")

    assert esito.startswith("❌") and "App non valida" in esito
    assert idea.load_idea_session(sessione.idea_id).last_observation == ""
    assert vision_finta == []


def test_analyze_salvataggio_impossibile_e_pulito(finestra, vision_finta, monkeypatch):
    sessione = idea.create_idea_session("dipingere", "clio")

    def esplode(*argomenti, **kwargs):
        raise idea.IdeaSessionError("store in sola lettura")

    monkeypatch.setattr(va.idea_session, "update_idea_session", esplode)

    esito = va.analyze_sketch_and_update_idea(sessione.idea_id, "paint")

    assert esito.startswith("❌") and "non riesco a salvare" in esito


# ── Integrazione: control-plane, registro, metadati, policy ──────────────────

def test_catena_control_plane_aggiorna_la_sessione(finestra, vision_finta):
    """Il percorso reale del LLM: registry -> policy.guarded_call -> vision_agent."""
    sessione = idea.create_idea_session("dipingere un tramonto", "clio")

    esito = policy.guarded_call(
        va.ACTION_ANALYZE, registry.AZIONI_MAP[va.ACTION_ANALYZE],
        {"idea_id": sessione.idea_id, "app_name": "paint"})

    assert isinstance(esito, str) and "🎨" in esito
    assert idea.load_idea_session(sessione.idea_id).last_observation == DESCRIZIONE


def test_policy_vision_read_only():
    assert policy.classify(va.ACTION_ANALYZE) == "read-only"


def test_azione_registrata_nel_registro_unico():
    assert registry.AZIONI_MAP[va.ACTION_ANALYZE] is va.analyze_sketch_and_update_idea


def test_metadati_llm_presenti():
    from jarvis_brain import TOOLS_METADATA

    assert TOOLS_METADATA[va.ACTION_ANALYZE]["params"] == ["idea_id", "app_name", "prompt"]


def test_il_modulo_non_cattura_lo_schermo_intero():
    import inspect

    sorgente = inspect.getsource(va)
    for vietato in ("ImageGrab", "sct.monitors", "monitors[0]", "pyautogui"):
        assert vietato not in sorgente


def test_describe_vision_agent():
    testo = va.describe_vision_agent()

    assert "Vision Agent" in testo and va.ENV_KEEP_SCREENSHOTS in testo


def test_idea_session_vecchia_senza_osservazione(tmp_path):
    """Gli store scritti da C1 (senza last_observation) restano leggibili."""
    sessione = idea.IdeaSession(idea_id="abc-123", project_id="clio", user_goal="vecchia idea")

    assert sessione.last_observation == ""
    assert idea.IdeaSession.from_dict(
        {"idea_id": "abc-123", "project_id": "clio", "user_goal": "vecchia idea"}
    ).last_observation == ""


# ── Scelta della finestra: processo prima del titolo ─────────────────────────

def test_find_window_preferisce_il_processo_al_titolo(monkeypatch):
    """Una scheda del browser che parla di Paint non deve essere scambiata per Paint."""
    finestre = [
        {"hwnd": 1, "title": "Come disegnare con Paint - Chrome", "pid": 111},
        {"hwnd": 2, "title": "Senza titolo", "pid": 222},
    ]
    nomi = {111: "chrome.exe", 222: "mspaint.exe"}
    monkeypatch.setattr(da, "_process_name", lambda pid: nomi.get(pid, ""))

    assert da._find_window("mspaint.exe", windows=finestre)["hwnd"] == 2


def test_find_window_fallback_sul_titolo(monkeypatch):
    finestre = [{"hwnd": 3, "title": "Senza titolo - Blocco Note", "pid": 333}]
    monkeypatch.setattr(da, "_process_name", lambda pid: "")

    assert da._find_window("notepad.exe", windows=finestre)["hwnd"] == 3


def test_find_window_nessuna_corrispondenza(monkeypatch):
    finestre = [{"hwnd": 4, "title": "Documento", "pid": 444}]
    monkeypatch.setattr(da, "_process_name", lambda pid: "winword.exe")

    assert da._find_window("mspaint.exe", windows=finestre) is None


def test_capture_mai_ridotta_a_icona(tmp_path, monkeypatch):
    """Finestra minimizzata: nessuna cattura (e nessun ripristino della finestra)."""
    monkeypatch.setattr(da, "find_window",
                        lambda app: {"hwnd": 42, "title": "Paint", "pid": 7})
    monkeypatch.setattr(va, "_window_rect", lambda hwnd: None)
    monkeypatch.setattr(va, "_is_minimized", lambda hwnd: True)
    monkeypatch.setattr(va, "_grab_region", lambda rect: pytest.fail("non deve catturare"))

    assert va.capture_window("paint", out_dir=tmp_path) is None


# ── Chiamata al modello visivo (Gemini) senza rete ──────────────────────────

class _FakeModels:
    def __init__(self):
        self.chiamate = []

    def generate_content(self, model, contents):
        self.chiamate.append((model, contents))
        return type("Risposta", (), {"text": "descrizione dal modello"})()


class _FakeClient:
    def __init__(self):
        self.models = _FakeModels()


def test_default_vision_chiama_il_modello(tmp_path, monkeypatch):
    percorso = _immagine_finta(tmp_path)
    client = _FakeClient()
    monkeypatch.setattr(va, "_gemini_client", lambda: client)
    monkeypatch.setattr(va, "_vision_model", lambda: "models/prova-vision")
    monkeypatch.setattr(va, "_prepare_jpeg", lambda image_path: b"jpeg-finto")

    assert va._default_vision(str(percorso), "Che cosa vedi?") == "descrizione dal modello"

    modello, contenuti = client.models.chiamate[0]
    assert modello == "models/prova-vision"
    assert contenuti[0] == "Che cosa vedi?"
    assert len(contenuti) == 2                      # prompt + immagine JPEG


def test_default_vision_senza_chiave_e_un_errore_pulito(monkeypatch):
    monkeypatch.setattr(va, "_GEMINI_CLIENT", None)
    monkeypatch.setattr(va, "_vision_api_key", lambda: "")

    with pytest.raises(va.VisionAgentError, match="GEMINI_API_KEY"):
        va._gemini_client()


def test_describe_senza_chiave_ritorna_vision_non_disponibile(tmp_path, monkeypatch):
    percorso = _immagine_finta(tmp_path)
    monkeypatch.setattr(va, "_GEMINI_CLIENT", None)
    monkeypatch.setattr(va, "_vision_api_key", lambda: "")

    esito = va.describe_sketch(str(percorso))

    assert esito.startswith("Vision non disponibile") and "GEMINI_API_KEY" in esito


def test_il_modulo_non_importa_clio_core():
    """Importare clio_core avvia il runtime (thread proattivo, screenshot, audio)."""
    import inspect

    sorgente = inspect.getsource(va)

    assert "import clio_core" not in sorgente
    assert "from clio_core" not in sorgente
