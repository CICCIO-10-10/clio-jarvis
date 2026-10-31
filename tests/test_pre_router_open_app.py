# -*- coding: utf-8 -*-
"""test_pre_router_open_app.py — "apri [app]" deterministico, TTS e limite Groq.

Copre i tre fix del primo test vocale:
  1. pre-router "apri [app]" in `jarvis_brain.py` (il LLM rispondeva "Apro Paint"
     senza chiamare il tool);
  2. correzione di pronuncia edge-tts "Paint" -> "Peint" (`tts_pronuncia.py`,
     applicata in `clio_core.py` alle due chiamate di sintesi);
  3. limite di contesto per Groq (413: ITPM 7000 token).

Nessuna app viene aperta davvero: `desktop_agent.open_app` e
`desktop_agent.apri_app_e_inizia_idea` sono sostituiti da doppi. Nessuna
chiamata di rete: il client Groq è un finto modulo.

Esecuzione:  python -m pytest -q tests/test_pre_router_open_app.py
"""
import json
import logging
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import desktop_agent  # noqa: E402
import jarvis_brain as jb  # noqa: E402
from tts_pronuncia import PRONUNCIA_TTS, pronuncia_tts  # noqa: E402


# ── Isolamento: nessuna sessione reale su disco ─────────────────────────────
@pytest.fixture(autouse=True)
def _sessione_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(jb.JarvisBrain, "_session_file",
                        lambda self: str(tmp_path / "jarvis_session.json"))


# ── Doppi del Desktop Agent: nessuna app vera aperta ────────────────────────
@pytest.fixture
def app_finte(monkeypatch):
    chiamate = {"open": [], "idea": []}

    def finto_open_app(app_name, confirmed=False, confirm=None, origin=None):
        chiamate["open"].append(app_name)
        return True

    def finta_idea(app_name, obiettivo, progetto=desktop_agent.DEFAULT_PROJECT_ID,
                   confirmed=False, confirm=None, origin=None):
        chiamate["idea"].append({"app_name": app_name, "obiettivo": obiettivo,
                                 "progetto": progetto})
        return f"✅ {app_name} aperto. Idea avviata: {obiettivo}"

    monkeypatch.setattr(desktop_agent, "open_app", finto_open_app)
    monkeypatch.setattr(desktop_agent, "apri_app_e_inizia_idea", finta_idea)
    return chiamate


# ── 1. Parsing della frase ──────────────────────────────────────────────────
@pytest.mark.parametrize("frase, atteso", [
    ("apri Paint", ("Paint", "")),
    ("apri paint", ("paint", "")),
    ("apri il blocco note", ("blocco note", "")),
    ("apri il Paint", ("Paint", "")),
    ("Clio, apri Paint", ("Paint", "")),
    ("Ehi Clio, avvia Paint", ("Paint", "")),
    ("avvia il blocco note", ("blocco note", "")),
    ("lancia la calcolatrice", ("calcolatrice", "")),
    ("apri l'app Paint", ("Paint", "")),
    ("apri Paint e iniziamo un'idea", ("Paint", "iniziamo un'idea")),
    ("apri Paint e iniziamo un'idea.", ("Paint", "iniziamo un'idea")),
    ("apri Paint, e iniziamo un'idea", ("Paint", "iniziamo un'idea")),
    ("apri Paint per disegnare un poster", ("Paint", "disegnare un poster")),
    ("apri Paint per favore", ("Paint", "")),
    ("apri Paint, grazie", ("Paint", "")),
    ("apri Paint e basta", ("Paint", "")),
    ("apri Paint!", ("Paint", "")),
])
def test_parse_apri_app(frase, atteso):
    assert jb._parse_apri_app(frase) == atteso


@pytest.mark.parametrize("frase", [
    "", "   ", None, 42,
    "come apro Paint?",           # domanda, non comando
    "mi apri Paint",              # non inizia col verbo
    "apri",                       # niente app
    "riprendi da qui",            # altri pre-router
    "cerca riferimenti",
])
def test_parse_apri_app_ignora_le_altre_frasi(frase):
    assert jb._parse_apri_app(frase) is None


# ── 2. Pre-router: il tool viene davvero chiamato ──────────────────────────
def test_apri_paint_chiama_open_app(app_finte):
    risposta = jb._apri_app_pre_router("apri Paint")

    assert app_finte["open"] == ["mspaint.exe"]
    assert app_finte["idea"] == []
    assert risposta == "✅ Paint aperto, Capo."


def test_apri_il_blocco_note_chiama_open_app(app_finte):
    risposta = jb._apri_app_pre_router("apri il blocco note")

    assert app_finte["open"] == ["notepad.exe"]
    assert risposta == "✅ blocco note aperto, Capo."


def test_apri_paint_con_obiettivo_chiama_apri_app_e_inizia_idea(app_finte):
    risposta = jb._apri_app_pre_router("apri Paint e iniziamo un'idea")

    assert app_finte["open"] == []
    assert app_finte["idea"] == [{"app_name": "mspaint.exe",
                                  "obiettivo": "iniziamo un'idea",
                                  "progetto": desktop_agent.DEFAULT_PROJECT_ID}]
    assert "Idea avviata: iniziamo un'idea" in risposta


def test_log_esplicito_del_pre_router(app_finte, caplog):
    with caplog.at_level(logging.INFO, logger="JarvisBrain"):
        jb._apri_app_pre_router("apri Paint")
        jb._apri_app_pre_router("apri Paint e iniziamo un'idea")

    assert "[PRE-ROUTER] apertura app: paint, idea: False" in caplog.text
    assert "[PRE-ROUTER] apertura app: paint, idea: True" in caplog.text


def test_app_non_in_whitelist_lascia_la_richiesta_al_llm(app_finte):
    assert jb._apri_app_pre_router("apri Photoshop") is None
    assert jb._apri_app_pre_router("apri la cartella Download") is None
    assert jb._apri_app_pre_router("apri il sito example.com") is None

    assert app_finte["open"] == [] and app_finte["idea"] == []


def test_sito_noto_apre_il_browser_senza_llm(app_finte, monkeypatch):
    """FASE 3: youtube/spotify/google/gmail/netflix non sono app desktop."""
    aperti = []
    monkeypatch.setattr(jb, "apri_sito", lambda url: aperti.append(url) or f"✅ {url}")

    assert jb._apri_app_pre_router("apri youtube") == "✅ https://www.youtube.com"
    assert jb._apri_app_pre_router("apri Spotify") == "✅ https://open.spotify.com"
    assert aperti == ["https://www.youtube.com", "https://open.spotify.com"]

    # Nessuna app avviata: il ramo siti non tocca il Desktop Agent.
    assert app_finte["open"] == [] and app_finte["idea"] == []


def test_app_non_in_whitelist_non_chiama_nulla(monkeypatch):
    def _non_deve_essere_chiamato(*a, **k):
        pytest.fail("il Desktop Agent non deve essere chiamato")

    monkeypatch.setattr(desktop_agent, "open_app", _non_deve_essere_chiamato)
    monkeypatch.setattr(desktop_agent, "apri_app_e_inizia_idea",
                        _non_deve_essere_chiamato)

    assert jb._apri_app_pre_router("apri Photoshop") is None


def test_apertura_rifiutata_lo_dice_senza_mentire(monkeypatch):
    monkeypatch.setattr(desktop_agent, "open_app",
                        lambda app_name, confirmed=False, confirm=None, origin=None: False)

    risposta = jb._apri_app_pre_router("apri Paint")

    # Paint È in whitelist: il fallimento può solo essere la conferma mancata.
    assert risposta.startswith("⌛")
    assert "Paint" in risposta and "Conferma scaduta" in risposta
    assert "NON eseguita" in risposta
    assert "non consentita" not in risposta


def test_on_text_riceve_la_risposta(app_finte):
    visti = []
    jb._apri_app_pre_router("apri Paint", on_text=visti.append)

    # L'apertura è SENSITIVE: la conferma la legge il control-plane, quindi qui
    # si annuncia solo l'esito (niente "Apro Paint" prima della conferma).
    assert visti == ["✅ Paint aperto, Capo."]


def test_on_text_rotto_non_blocca_l_apertura(app_finte):
    def rotto(testo):
        raise RuntimeError("audio occupato")

    assert jb._apri_app_pre_router("apri Paint", on_text=rotto) == \
        "✅ Paint aperto, Capo."


# ── 3. Il flusso vero: think_and_act non chiama il LLM ─────────────────────
def test_think_and_act_apre_l_app_senza_llm(app_finte, monkeypatch):
    def _non_deve_chiamare(*a, **k):
        pytest.fail("il pre-router non deve passare dal LLM")

    monkeypatch.setattr(jb.JarvisBrain, "_stream_llm", _non_deve_chiamare)
    monkeypatch.setattr(jb.JarvisBrain, "_call_llm", _non_deve_chiamare)

    cervello = jb.JarvisBrain()
    risposta = cervello.think_and_act("apri Paint", origin="voice")

    assert risposta == "✅ Paint aperto, Capo."
    assert app_finte["open"] == ["mspaint.exe"]
    assert cervello.session_history[-1] == {"user": "apri Paint",
                                            "assistant": "✅ Paint aperto, Capo."}


def test_think_and_act_apre_l_idea_senza_llm(app_finte, monkeypatch):
    def _non_deve_chiamare(*a, **k):
        pytest.fail("il pre-router non deve passare dal LLM")

    monkeypatch.setattr(jb.JarvisBrain, "_stream_llm", _non_deve_chiamare)
    monkeypatch.setattr(jb.JarvisBrain, "_call_llm", _non_deve_chiamare)

    cervello = jb.JarvisBrain()
    risposta = cervello.think_and_act("apri Paint e iniziamo un'idea")

    assert "Idea avviata" in risposta
    assert app_finte["idea"][0]["obiettivo"] == "iniziamo un'idea"


# ── 4. Pronuncia TTS ───────────────────────────────────────────────────────
def test_mappa_di_pronuncia_richiesta():
    assert PRONUNCIA_TTS["Paint"] == "Peint"
    assert PRONUNCIA_TTS["mspaint"] == "emme esse paint"


@pytest.mark.parametrize("testo, atteso", [
    ("Apro Paint.", "Apro Peint."),
    ("Paint", "Peint"),
    ("stiamo usando paint", "stiamo usando Peint"),
    ("mspaint", "emme esse paint"),
    ("mspaint.exe", "emme esse paint.exe"),
    ("Paint 3D", "Peint 3D"),
    ("repaint", "repaint"),                 # dentro un'altra parola: non tocca
    ("mspaint2", "mspaint2"),
    ("Nessun nome qui", "Nessun nome qui"),
    ("", ""),
])
def test_pronuncia_tts(testo, atteso):
    assert pronuncia_tts(testo) == atteso


def test_pronuncia_tts_non_rifa_la_sostituzione():
    # "mspaint" -> "emme esse paint" e la mappa non rilegge il risultato.
    assert pronuncia_tts("mspaint") == "emme esse paint"


def test_pronuncia_tts_regge_none_e_non_stringhe():
    assert pronuncia_tts(None) is None
    assert pronuncia_tts(42) == 42


def test_clio_core_applica_la_pronuncia_alle_due_sintesi_edge_tts():
    """Le DUE chiamate edge-tts (file + streaming) devono passare dalla mappa."""
    sorgente = Path(jb.__file__).with_name("clio_core.py").read_text(encoding="utf-8")
    assert sorgente.count("edge_tts.Communicate(pronuncia_tts(") == 2
    assert "from tts_pronuncia import" in sorgente


# ── 5. Limite contesto Groq ────────────────────────────────────────────────
def _prompt_con_storia(turni, riempimento=80, vault=""):
    """Prompt con la stessa forma di quello vero (storia + vault + richiesta)."""
    righe = ["\n═══ STORIA CONVERSAZIONE (ricorda questi scambi) ═══"]
    for i in range(turni):
        righe.append(f"Capo: domanda-{i}")
        righe.append(f"Clio: risposta-{i} " + "x" * riempimento)
    righe.append("═══ FINE STORIA ═══")
    return ("PREAMBOLO DEL PROMPT\n" + "\n".join(righe) + "\n" + vault
            + "\nRichiesta attuale: ULTIMA-RICHIESTA\nStrumenti: " + "s" * 500)


def test_il_limite_e_dieci_turni():
    assert jb.GROQ_MAX_HISTORY_TURNS == 10


def test_tronca_turni_groq_tiene_gli_ultimi_dieci():
    prompt = _prompt_con_storia(14)

    ridotto = jb._tronca_turni_groq(prompt)

    assert ridotto.count("Capo:") == 10
    assert "domanda-4" in ridotto and "domanda-13" in ridotto
    assert "domanda-3" not in ridotto
    assert "Richiesta attuale: ULTIMA-RICHIESTA" in ridotto


def test_tronca_turni_groq_lascia_stare_i_prompt_corti():
    prompt = _prompt_con_storia(3)

    assert jb._tronca_turni_groq(prompt) == prompt


def test_tronca_turni_groq_regge_prompt_senza_storia():
    assert jb._tronca_turni_groq("solo testo") == "solo testo"


def test_limita_prompt_groq_rispetta_il_budget():
    vault = ("═══ CONTESTO DAL VAULT OBSIDIAN DEL CAPO ═══\n"
             + "v" * 40000 + "\n═══ FINE CONTESTO VAULT ═══\n")

    ridotto = jb._limita_prompt_groq(_prompt_con_storia(0, vault=vault))

    assert len(ridotto) <= jb.GROQ_MAX_PROMPT_CHARS
    assert "contesto vault omesso" in ridotto
    assert "Richiesta attuale: ULTIMA-RICHIESTA" in ridotto


def test_limita_prompt_groq_scarta_prima_i_turni_vecchi():
    prompt = _prompt_con_storia(10, riempimento=1500)

    ridotto = jb._limita_prompt_groq(prompt)

    assert len(ridotto) <= jb.GROQ_MAX_PROMPT_CHARS
    assert "domanda-9" in ridotto            # il turno più recente resta
    assert "domanda-0" not in ridotto        # i più vecchi spariscono
    assert "Richiesta attuale: ULTIMA-RICHIESTA" in ridotto


def test_limita_prompt_groq_tiene_gli_ultimi_due_turni_prima_del_vault():
    vault = ("═══ CONTESTO DAL VAULT OBSIDIAN DEL CAPO ═══\n"
             + "v" * 30000 + "\n═══ FINE CONTESTO VAULT ═══\n")

    ridotto = jb._limita_prompt_groq(_prompt_con_storia(10, riempimento=800,
                                                        vault=vault))

    assert len(ridotto) <= jb.GROQ_MAX_PROMPT_CHARS
    assert "contesto vault omesso" in ridotto
    assert ridotto.count("Capo:") == 2       # i due scambi più recenti
    assert "domanda-8" in ridotto and "domanda-9" in ridotto
    assert "domanda-7" not in ridotto
    assert "Richiesta attuale: ULTIMA-RICHIESTA" in ridotto


def test_limita_prompt_groq_non_tocca_i_prompt_corti():
    prompt = "prompt corto: nessuna storia, nessun vault"

    assert jb._limita_prompt_groq(prompt) == prompt


# ── 6. La richiesta a Groq usa davvero il limite ───────────────────────────
class _FakeStream:
    def __iter__(self):
        for pezzo in ("Ciao", " Capo"):
            delta = types.SimpleNamespace(content=pezzo)
            yield types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])


class _FakeGroqClient:
    ultimo = {}

    def __init__(self, api_key=None):
        pass

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        _FakeGroqClient.ultimo = kwargs
        if kwargs.get("stream"):
            return _FakeStream()
        messaggio = types.SimpleNamespace(content='{"azione": "rispondi"}')
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=messaggio)])


def _installa_finto_groq(monkeypatch):
    monkeypatch.setitem(sys.modules, "groq", types.SimpleNamespace(Groq=_FakeGroqClient))


def test_call_groq_limita_il_prompt(monkeypatch):
    _installa_finto_groq(monkeypatch)
    cervello = jb.JarvisBrain()

    testo = cervello._call_groq(_prompt_con_storia(30, riempimento=3000),
                                {"provider": "groq", "model": "qwen/qwen3.8-27b"})

    assert testo == '{"azione": "rispondi"}'
    inviato = _FakeGroqClient.ultimo["messages"][0]["content"]
    assert len(inviato) <= jb.GROQ_MAX_PROMPT_CHARS + 200
    assert "Richiesta attuale: ULTIMA-RICHIESTA" in inviato
    assert "domanda-0" not in inviato


def test_stream_groq_limita_il_prompt(monkeypatch):
    _installa_finto_groq(monkeypatch)
    cervello = jb.JarvisBrain()

    pezzi = list(cervello._stream_groq(_prompt_con_storia(30, riempimento=3000),
                                       {"provider": "groq", "model": "qwen/qwen3.8-27b"}))

    assert pezzi == ["Ciao", " Capo"]
    inviato = _FakeGroqClient.ultimo["messages"][1]["content"]
    assert len(inviato) <= jb.GROQ_MAX_PROMPT_CHARS + 200
    assert "domanda-0" not in inviato


def test_load_session_taglia_la_storia_vecchia(tmp_path, monkeypatch):
    percorso = tmp_path / "jarvis_session.json"
    percorso.write_text(json.dumps({
        "history": [{"user": f"u{i}", "assistant": f"a{i}"} for i in range(25)],
        "saved_at": "2026-10-28 10:00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(jb.JarvisBrain, "_session_file", lambda self: str(percorso))

    cervello = jb.JarvisBrain()

    assert len(cervello.session_history) == jb.GROQ_MAX_HISTORY_TURNS
    assert cervello.session_history[-1] == {"user": "u24", "assistant": "a24"}
    assert cervello.session_history[0] == {"user": "u15", "assistant": "a15"}


def test_history_to_str_non_supera_i_dieci_turni():
    cervello = jb.JarvisBrain()
    cervello.session_history = [{"user": f"u{i}", "assistant": f"a{i}"}
                                for i in range(40)]

    testo = cervello._history_to_str()

    # In memoria restano 10 scambi; nel prompt (come prima) gli ultimi 6.
    assert len(cervello.session_history) == jb.GROQ_MAX_HISTORY_TURNS
    assert "u39" in testo and "u34" in testo and "u33" not in testo
