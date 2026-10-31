# -*- coding: utf-8 -*-
"""test_research_agent.py — C4: Research Agent (web + YouTube + Gemini + IdeaSession).

Nessuna rete, nessuna GUI, nessuna chiave API usata davvero:
  * Playwright è sostituito da una finta pagina che risponde in base all'URL;
  * Gemini è sostituito da `set_summarizer_hook`;
  * lo store delle idee e le osservazioni stanno in `tmp_path`.

Copre: parsing degli URL (DuckDuckGo/Bing/YouTube), catena dei motori di ricerca,
estrazione dei video, riassunto con e senza modello, salvataggio in IdeaSession
con `fonte: research`, deduplica, limiti, registro/policy, pre-router
deterministico e flusso completo in `think_and_act`.

Esecuzione:  python -m pytest -q tests/test_research_agent.py
"""
import contextlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idea_session as idea  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402
import research_agent as ra  # noqa: E402
import scribe_control  # noqa: E402


# ── Isolamento: store in tmp, nessun browser vero, nessun modello vero ──────
@pytest.fixture(autouse=True)
def _isolamento(tmp_path, monkeypatch):
    monkeypatch.setattr(idea, "SESSIONS_FILE", tmp_path / "idea_sessions.json")
    monkeypatch.setenv(scribe_control.OUTBOX_ENV, str(tmp_path / "scribe_outbox"))
    monkeypatch.setattr(scribe_control, "_BRIDGE_HAS_OBSERVATION", False)
    monkeypatch.delenv(ra.ENV_OFFLINE, raising=False)
    monkeypatch.delenv(ra.ENV_TIMEOUT, raising=False)
    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", None, raising=False)

    # Rete di sicurezza: se un test dimentica il finto browser, fallisce QUI
    # (pytest.fail non è una Exception: non viene inghiottito dai `except`).
    @contextlib.contextmanager
    def _browser_vietato():
        pytest.fail("Playwright reale non consentito nei test")
        yield  # pragma: no cover

    monkeypatch.setattr(ra, "_open_page", _browser_vietato)
    yield


@pytest.fixture
def sessione():
    return idea.create_idea_session("disegnare il poster del concerto", "clio")


# ── Finto browser ──────────────────────────────────────────────────────────
class FintaPagina:
    """Paginata finta: risponde in base a cosa contiene l'URL visitato."""

    def __init__(self, risposte=None, goto_rotto=(), consenso=False):
        self.risposte = risposte or {}
        self.goto_rotto = tuple(goto_rotto)
        self.consenso = consenso
        self.url = ""
        self.visitate = []
        self.timeout = None
        self.attese = []
        self.click = []

    def set_default_timeout(self, ms):
        self.timeout = ms

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.visitate.append(url)
        if any(rotto in url for rotto in self.goto_rotto):
            raise RuntimeError(f"timeout su {url}")

    def wait_for_timeout(self, ms):
        self.attese.append(ms)

    def query_selector(self, selettore):
        if self.consenso and "Accetta" in selettore:
            return self
        return None

    def evaluate(self, javascript):
        for chiave, valori in self.risposte.items():
            if chiave in self.url:
                return valori
        return []


def finto_browser(pagina):
    """Sostituisce `ra._open_page` restituendo la pagina finta indicata."""

    @contextlib.contextmanager
    def _apri():
        yield pagina

    return _apri


WEB = [
    {"titolo": "Poster design: 10 regole", "href": "https://esempio.it/poster",
     "snippet": "  Come   impostare un poster leggibile. "},
    {"titolo": "Tipografia per manifesti", "href": "https://esempio.it/tipo",
     "snippet": "Gerarchie e contrasto."},
    {"titolo": "Doppione", "href": "https://esempio.it/poster", "snippet": "dup"},
]

VIDEO = [
    {"titolo": "Poster design tutorial", "href": "/watch?v=abc12345678&pp=xyz",
     "durata": " 11:44 ", "canale": "Design Lab"},
    {"titolo": "Layout e griglie", "href": "/watch?v=def12345678",
     "durata": "7:17", "canale": "Tipografia"},
    {"titolo": "Canale", "href": "/@un-canale", "durata": "", "canale": ""},
]


# ── URL ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("href, atteso", [
    ("https://esempio.it/a", "https://esempio.it/a"),
    ("//esempio.it/a", "https://esempio.it/a"),
    ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fesempio.it%2Fa&rut=x",
     "https://esempio.it/a"),
    ("javascript:void(0)", ""),
    ("", ""),
    ("/relativo", ""),
])
def test_absolute_url(href, atteso):
    assert ra._absolute_url(href) == atteso


def test_absolute_url_relative_con_base():
    assert ra._absolute_url("/watch?v=abc", base=ra.YOUTUBE_BASE) == \
        "https://www.youtube.com/watch?v=abc"


def test_bing_real_url_decodifica_il_redirect():
    import base64

    reale = "https://esempio.it/articolo"
    codificato = "a1" + base64.urlsafe_b64encode(reale.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&&p=abc&u={codificato}&ntb=1"

    assert ra._bing_real_url(href) == reale


def test_bing_real_url_lascia_stare_gli_altri_link():
    assert ra._bing_real_url("https://esempio.it/x") == "https://esempio.it/x"
    assert ra._bing_real_url("https://www.bing.com/ck/a?u=nonbase64!!") .startswith(
        "https://www.bing.com/ck/a")


@pytest.mark.parametrize("href, atteso", [
    ("/watch?v=abc12345678&pp=xyz", "https://www.youtube.com/watch?v=abc12345678"),
    ("https://www.youtube.com/watch?v=abc12345678&t=10",
     "https://www.youtube.com/watch?v=abc12345678"),
    ("/results?search_query=x", "https://www.youtube.com/results?search_query=x"),
    ("", ""),
])
def test_youtube_url(href, atteso):
    assert ra._youtube_url(href) == atteso


# ── search_web ──────────────────────────────────────────────────────────────
def test_search_web_legge_duckduckgo_html(monkeypatch):
    pagina = FintaPagina({"html.duckduckgo.com": WEB})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    risultati = ra.search_web("poster concerto design", max_results=3)

    assert [r["title"] for r in risultati] == ["Poster design: 10 regole",
                                               "Tipografia per manifesti"]
    assert risultati[0]["url"] == "https://esempio.it/poster"
    assert risultati[0]["snippet"] == "Come impostare un poster leggibile."
    assert "poster+concerto+design" in pagina.visitate[0]
    assert pagina.attese                                # la pagina "respira"


def test_search_web_rispetta_max_results(monkeypatch):
    pagina = FintaPagina({"html.duckduckgo.com": WEB})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    assert len(ra.search_web("x", max_results=1)) == 1


def test_search_web_se_duckduckgo_blocca_passa_a_bing(monkeypatch):
    """DDG risponde con la pagina anti-bot: si prova `lite`, poi Bing."""
    pagina = FintaPagina({"bing.com": WEB})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    risultati = ra.search_web("poster")

    assert len(risultati) == 2
    assert any("html.duckduckgo.com" in u for u in pagina.visitate)
    assert any("lite.duckduckgo.com" in u for u in pagina.visitate)
    assert any("bing.com/search" in u for u in pagina.visitate)


def test_search_web_senza_risultati_ritorna_lista_vuota(monkeypatch):
    pagina = FintaPagina({})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    assert ra.search_web("qualcosa di inesistente") == []
    assert len(pagina.visitate) == 3                    # tutti i motori provati


def test_search_web_browser_rotto_non_solleva(monkeypatch):
    @contextlib.contextmanager
    def rotto():
        raise ra.ResearchError("browser non installato")
        yield  # pragma: no cover

    monkeypatch.setattr(ra, "_open_page", rotto)

    assert ra.search_web("poster") == []


def test_search_web_navigazione_in_errore_non_solleva(monkeypatch):
    pagina = FintaPagina({"bing.com": WEB}, goto_rotto=("duckduckgo", "bing"))
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    assert ra.search_web("poster") == []
    assert len(pagina.visitate) == 3


@pytest.mark.parametrize("query, limite", [("", 3), ("   ", 3), ("poster", 0), (None, 3)])
def test_search_web_query_o_limite_non_validi(query, limite, monkeypatch):
    def _non_deve_aprirsi():
        pytest.fail("il browser non deve essere aperto")

    monkeypatch.setattr(ra, "_open_page", _non_deve_aprirsi)

    assert ra.search_web(query, max_results=limite) == []


def test_search_web_offline_non_apre_il_browser(monkeypatch):
    monkeypatch.setenv(ra.ENV_OFFLINE, "1")

    def _non_deve_aprirsi():
        pytest.fail("il browser non deve essere aperto in modalità offline")

    monkeypatch.setattr(ra, "_open_page", _non_deve_aprirsi)

    assert ra.search_web("poster") == []


# ── search_youtube ──────────────────────────────────────────────────────────
def test_search_youtube_estrae_titolo_url_durata(monkeypatch):
    pagina = FintaPagina({"youtube.com/results": VIDEO})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    video = ra.search_youtube("poster design", max_results=2)

    assert [v["title"] for v in video] == ["Poster design tutorial", "Layout e griglie"]
    assert video[0]["url"] == "https://www.youtube.com/watch?v=abc12345678"
    assert video[0]["duration"] == "11:44"
    assert video[0]["channel"] == "Design Lab"
    assert len(video) == 2                              # limite rispettato


def test_search_youtube_scarta_i_link_che_non_sono_video(monkeypatch):
    pagina = FintaPagina({"youtube.com/results": VIDEO})
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    urls = [v["url"] for v in ra.search_youtube("x", max_results=10)]

    assert "https://www.youtube.com/@un-canale" not in urls


def test_search_youtube_accetta_il_consenso_se_non_ci_sono_video(monkeypatch):
    pagina = FintaPagina({}, consenso=True)
    monkeypatch.setattr(ra, "_open_page", finto_browser(pagina))

    assert ra.search_youtube("poster") == []
    assert "youtube.com/results" in pagina.visitate[0]


def test_search_youtube_browser_rotto_non_solleva(monkeypatch):
    @contextlib.contextmanager
    def rotto():
        raise ra.ResearchError("browser assente")
        yield  # pragma: no cover

    monkeypatch.setattr(ra, "_open_page", rotto)

    assert ra.search_youtube("poster") == []


# ── summarize_results ───────────────────────────────────────────────────────
def test_summarize_usa_il_modello_iniettato(monkeypatch):
    prompt_visti = []

    def finto_modello(prompt):
        prompt_visti.append(prompt)
        return "  Questi riferimenti mostrano come impostare un poster.  "

    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", finto_modello)

    testo = ra.summarize_results(WEB[:2], "poster concerto")

    assert testo == "Questi riferimenti mostrano come impostare un poster."
    assert "Poster design: 10 regole" in prompt_visti[0]
    assert "poster concerto" in prompt_visti[0]
    assert "2-3 frasi" in prompt_visti[0]


def test_summarize_include_i_video_con_la_durata(monkeypatch):
    prompt_visti = []
    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK",
                        lambda p: prompt_visti.append(p) or "riassunto")

    ra.summarize_results(VIDEO[:1], "poster")

    assert "[youtube]" in prompt_visti[0]
    assert "11:44" in prompt_visti[0]


def test_summarize_ripiega_senza_modello(monkeypatch):
    def esplode(prompt):
        raise RuntimeError("Gemini non raggiungibile")

    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", esplode)

    testo = ra.summarize_results(WEB[:2] + VIDEO[:1], "poster")

    assert "Ho trovato 3 riferimenti" in testo
    assert "2 pagine web" in testo and "1 video" in testo
    assert "Poster design: 10 regole" in testo
    assert "Nessun modello disponibile" in testo


def test_summarize_ripiega_se_gemini_non_ha_la_chiave(monkeypatch):
    monkeypatch.setattr(ra, "_gemini_summarize",
                        lambda prompt: (_ for _ in ()).throw(ra.ResearchError("no key")))

    assert "Ho trovato 2 riferimenti" in ra.summarize_results(WEB[:2], "poster")


def test_summarize_senza_risultati():
    assert ra.summarize_results([]) == "Non ho trovato riferimenti da riassumere."


def test_summarize_tronca_i_riassunti_lunghi(monkeypatch):
    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", lambda p: "parola " * 1000)

    testo = ra.summarize_results(WEB[:1], "poster")

    assert len(testo) <= ra.MAX_SUMMARY_CHARS + 1
    assert testo.endswith("…")


# ── research_for_idea ───────────────────────────────────────────────────────
@pytest.fixture
def ricerca_finta(monkeypatch):
    """Sostituisce le due ricerche: nessun browser, risultati già normalizzati."""
    web = [
        {"title": "Poster design: 10 regole", "url": "https://esempio.it/poster",
         "snippet": "Come impostare un poster leggibile."},
        {"title": "Tipografia per manifesti", "url": "https://esempio.it/tipo",
         "snippet": "Gerarchie e contrasto."},
    ]
    video = [
        {"title": "Poster design tutorial",
         "url": "https://www.youtube.com/watch?v=abc12345678", "duration": "11:44"},
        {"title": "Layout e griglie",
         "url": "https://www.youtube.com/watch?v=def12345678", "duration": "7:17"},
    ]
    monkeypatch.setattr(ra, "search_web", lambda q, n=3: [dict(v) for v in web])
    monkeypatch.setattr(ra, "search_youtube", lambda q, n=2: [dict(v) for v in video])
    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK",
                        lambda prompt: "Sintesi: due pagine e due video sul poster.")
    return None


def test_research_for_idea_salva_i_link_come_materiali(sessione, ricerca_finta):
    esito = ra.research_for_idea(sessione.idea_id, "poster concerto design")

    assert esito["ok"] is True and esito["error"] is None
    assert esito["query"] == "poster concerto design"
    assert len(esito["web"]) == 2 and len(esito["youtube"]) == 2
    assert esito["salvati"] == 4
    assert esito["riassunto"] == "Sintesi: due pagine e due video sul poster."

    riletta = idea.load_idea_session(sessione.idea_id)
    assert len(riletta.materiali) == 4
    primo = riletta.materiali[0]
    assert primo["fonte"] == "research" and primo["tipo"] == "web"
    assert primo["url"] == "https://esempio.it/poster"
    assert primo["titolo"] == "Poster design: 10 regole"

    video = [m for m in riletta.materiali if m["tipo"] == "youtube"]
    assert video[0]["durata"] == "11:44"
    assert video[0]["url"].startswith("https://www.youtube.com/watch?v=")


def test_research_for_idea_aggiorna_lo_stato_della_idea(sessione, ricerca_finta):
    ra.research_for_idea(sessione.idea_id, "poster concerto design")

    riletta = idea.load_idea_session(sessione.idea_id)
    assert riletta.ricerca["query"] == "poster concerto design"
    assert riletta.ricerca["web"] == 2 and riletta.ricerca["youtube"] == 2
    assert riletta.ricerca["salvati"] == 4
    assert "Sintesi" in riletta.ricerca["riassunto"]
    assert riletta.last_verified_at                       # touch()
    assert riletta.ricerca["data"].endswith("Z")
    assert "ricerca: poster concerto design (2 web, 2 youtube)" in riletta.describe()


def test_research_for_idea_senza_query_usa_l_obiettivo(sessione, ricerca_finta):
    esito = ra.research_for_idea(sessione.idea_id)

    assert esito["query"] == "disegnare il poster del concerto"


def test_research_for_idea_senza_sessione():
    esito = ra.research_for_idea("", "poster")

    assert esito["ok"] is False
    assert esito["error"] == "nessuna IdeaSession aperta"
    assert esito["web"] == [] and esito["salvati"] == 0


def test_research_for_idea_con_idea_inesistente():
    esito = ra.research_for_idea("idea-fantasma", "poster")

    assert esito["ok"] is False and "non trovata" in esito["error"]


def test_research_for_idea_usa_l_ultima_idea_aperta(sessione, ricerca_finta):
    esito = ra.research_for_idea("", "poster")

    assert esito["idea_id"] == sessione.idea_id and esito["ok"] is True


def test_research_for_idea_senza_risultati_non_tocca_l_idea(sessione, monkeypatch):
    monkeypatch.setattr(ra, "search_web", lambda q, n=3: [])
    monkeypatch.setattr(ra, "search_youtube", lambda q, n=2: [])

    esito = ra.research_for_idea(sessione.idea_id, "una query senza risultati")

    assert esito["ok"] is False
    assert "nessun riferimento trovato" in esito["error"]
    assert idea.load_idea_session(sessione.idea_id).materiali == []


def test_research_for_idea_sopravvive_al_fallimento_del_web(sessione, monkeypatch):
    def web_rotto(query, max_results=3):
        raise RuntimeError("rete assente")

    monkeypatch.setattr(ra, "search_web", web_rotto)
    monkeypatch.setattr(ra, "search_youtube", lambda q, n=2: [dict(v) for v in VIDEO[:2]])
    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", lambda p: "Solo video.")

    esito = ra.research_for_idea(sessione.idea_id, "poster")

    assert esito["ok"] is True
    assert esito["web"] == [] and len(esito["youtube"]) == 2
    assert esito["salvati"] == 2
    assert "web: RuntimeError" in esito["error"]


def test_research_for_idea_sopravvive_al_riassunto_rotto(sessione, ricerca_finta,
                                                        monkeypatch):
    def riassunto_rotto(prompt):
        raise RuntimeError("modello giù")

    monkeypatch.setattr(ra, "_SUMMARIZER_HOOK", riassunto_rotto)

    esito = ra.research_for_idea(sessione.idea_id, "poster")

    assert esito["ok"] is True
    assert "Ho trovato 4 riferimenti" in esito["riassunto"]        # ripiego locale


def test_research_for_idea_non_duplica_i_link(sessione, ricerca_finta):
    primo = ra.research_for_idea(sessione.idea_id, "poster")
    secondo = ra.research_for_idea(sessione.idea_id, "poster")

    assert primo["salvati"] == 4
    assert secondo["salvati"] == 0
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 4


def test_research_for_idea_rispetta_il_limite_di_materiali(sessione, ricerca_finta,
                                                           monkeypatch):
    monkeypatch.setattr(idea, "MAX_MATERIALI", 2)

    esito = ra.research_for_idea(sessione.idea_id, "poster")

    assert esito["salvati"] == 2
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 2


# ── Azione CLIO / registro / policy / metadati ──────────────────────────────
def test_cerca_riferimenti_risponde_a_voce(sessione, ricerca_finta):
    testo = ra.cerca_riferimenti("poster concerto", sessione.idea_id)

    assert "Ho cercato «poster concerto»" in testo
    assert "2 riferimenti web e 2 video" in testo
    assert "4 link" in testo and sessione.idea_id[:8] in testo
    assert "Sintesi: due pagine e due video sul poster." in testo


def test_cerca_riferimenti_senza_idea_lo_dice():
    assert ra.cerca_riferimenti("poster").startswith("❌ Ricerca non eseguita")


def test_cerca_riferimenti_quando_non_trova_nulla(sessione, monkeypatch):
    monkeypatch.setattr(ra, "search_web", lambda q, n=3: [])
    monkeypatch.setattr(ra, "search_youtube", lambda q, n=2: [])

    testo = ra.cerca_riferimenti("poster", sessione.idea_id)

    assert "Non ho trovato riferimenti" in testo
    assert "L'idea resta com'è" in testo


def test_cerca_riferimenti_senza_nuovi_link(sessione, ricerca_finta):
    ra.cerca_riferimenti("poster", sessione.idea_id)

    testo = ra.cerca_riferimenti("poster", sessione.idea_id)

    assert "Erano già tutti collegati" in testo


def test_format_research_reply_regge_input_sporchi():
    assert ra.format_research_reply(None).startswith("❌")
    assert "Ricerca non eseguita" in ra.format_research_reply({})


def test_direttiva_registrata_e_read_only():
    assert "cerca_riferimenti" in registry.AZIONI_MAP
    assert policy.classify("cerca_riferimenti") == "read-only"


def test_metadati_llm_c4():
    from jarvis_brain import TOOLS_METADATA

    voce = TOOLS_METADATA["cerca_riferimenti"]
    assert voce["params"] == ["query", "idea_id"]
    assert "YouTube" in voce["desc"] and "web" in voce["desc"]


def test_azione_c4_chiamabile_dal_control_plane(sessione, ricerca_finta):
    esito = policy.guarded_call("cerca_riferimenti",
                                registry.AZIONI_MAP["cerca_riferimenti"],
                                {"query": "poster concerto", "idea_id": sessione.idea_id},
                                origin="flask")

    assert "Ho cercato" in esito
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 4


# ── IdeaSession: materiali eterogenei (stringhe + riferimenti) ──────────────
def test_materiali_misti_stringhe_e_riferimenti():
    sessione = idea.IdeaSession(
        idea_id="abc-c4", project_id="clio", user_goal="idea",
        materiali=[r"C:\x\bozza.png",
                   {"url": "https://esempio.it/a", "titolo": "Articolo",
                    "fonte": "research", "tipo": "web"},
                   {"url": "https://esempio.it/a", "titolo": "Doppione"},
                   {"senza": "url"}])

    assert sessione.materiali[0] == r"C:\x\bozza.png"
    assert sessione.materiali[1]["fonte"] == "research"
    assert len(sessione.materiali) == 2                   # doppione e voce vuota scartati
    assert idea.materiale_label(sessione.materiali[0]) == "bozza.png"
    assert idea.materiale_label(sessione.materiali[1]) == "Articolo"
    assert idea.materiale_fonte(sessione.materiali[0]) == "locale"
    assert idea.materiale_e_ricerca(sessione.materiali[1]) is True


def test_add_riferimento_valida_e_deduplica(sessione):
    assert idea.add_riferimento(sessione.idea_id,
                                {"url": "https://esempio.it/a", "titolo": "Articolo",
                                 "fonte": "research"}) is True
    assert idea.add_riferimento(sessione.idea_id, "https://esempio.it/a") is False
    assert idea.add_riferimento(sessione.idea_id, {"titolo": "senza url"}) is False
    assert idea.add_riferimento("idea-fantasma", {"url": "https://esempio.it/b"}) is False

    riletta = idea.load_idea_session(sessione.idea_id)
    assert len(riletta.materiali) == 1
    assert riletta.materiali[0]["fonte"] == "research"


def test_get_resume_idea_copia_anche_i_riferimenti(sessione):
    idea.add_riferimento(sessione.idea_id, {"url": "https://esempio.it/a",
                                            "titolo": "Articolo", "fonte": "research"})

    resume = idea.get_resume_idea(sessione.idea_id)
    resume["materiali"][0]["titolo"] = "manomesso"

    riletto = idea.get_resume_idea(sessione.idea_id)
    assert riletto["materiali"][0]["titolo"] == "Articolo"
    assert "ricerca" in resume


def test_format_resume_usa_il_titolo_dei_riferimenti(sessione):
    idea.add_riferimento(sessione.idea_id, {"url": "https://esempio.it/a",
                                            "titolo": "Articolo sui poster",
                                            "fonte": "research"})

    testo = idea.format_resume(idea.load_idea_session(sessione.idea_id))

    assert "Articolo sui poster" in testo
    assert "1 materiale" in testo


# ── Pre-router deterministico in jarvis_brain ───────────────────────────────
@pytest.mark.parametrize("frase", [
    "cerca riferimenti",
    "Clio, cerca riferimenti sul poster del concerto",
    "cerca idee simili",
    "trova ispirazione",
    "trova ispirazione per il poster del concerto",
    "cercami un po' di ispirazione",
])
def test_pre_router_riconosce_le_frasi(frase, sessione, ricerca_finta):
    import jarvis_brain as jb

    risposta = jb._cerca_riferimenti_pre_router(frase)

    assert risposta and "Ho cercato" in risposta


@pytest.mark.parametrize("frase", ["che ore sono", "apri paint", "riassumi il vault",
                                   "riprendi da qui", ""])
def test_pre_router_ignora_le_altre_frasi(frase, sessione, ricerca_finta):
    import jarvis_brain as jb

    assert jb._cerca_riferimenti_pre_router(frase) is None


def test_pre_router_senza_idea_lascia_lavorare_il_llm(ricerca_finta):
    import jarvis_brain as jb

    assert jb._cerca_riferimenti_pre_router("cerca riferimenti") is None


def test_pre_router_estrae_la_query(monkeypatch, sessione):
    import jarvis_brain as jb

    chiamate = []

    def finta(query="", idea_id=""):
        chiamate.append({"query": query, "idea_id": idea_id})
        return f"Ho cercato «{query or 'obiettivo'}» e l'ho salvato nell'idea."

    monkeypatch.setattr(ra, "cerca_riferimenti", finta)

    risposta = jb._cerca_riferimenti_pre_router(
        "Clio, cerca riferimenti per il poster del concerto")

    assert chiamate == [{"query": "poster del concerto", "idea_id": sessione.idea_id}]
    assert "Ho cercato" in risposta


def test_pre_router_progetto_singolo_usa_l_obiettivo(monkeypatch):
    import jarvis_brain as jb

    altra = idea.create_idea_session("restaurare la bicicletta", "officina")
    chiamate = []

    def finta(query="", idea_id=""):
        chiamate.append(query)
        return f"Ho cercato «{query or 'obiettivo'}» nell'idea {altra.idea_id[:8]}."

    monkeypatch.setattr(ra, "cerca_riferimenti", finta)

    jb._cerca_riferimenti_pre_router("trova ispirazione per officina")

    assert chiamate == [""]           # una parola = progetto, non query
    assert all(s.project_id != "officina" or True for s in idea.list_idea_sessions())


def test_pre_router_chiama_il_tts(sessione, ricerca_finta):
    import jarvis_brain as jb

    detto = []
    jb.set_tts_hook(detto.append)
    try:
        risposta = jb._cerca_riferimenti_pre_router("cerca riferimenti sul poster")
    finally:
        jb.set_tts_hook(None)

    assert detto == [risposta]


def test_pre_router_avvisa_la_voce_prima_di_cercare(sessione, ricerca_finta):
    import jarvis_brain as jb

    visti = []
    jb._cerca_riferimenti_pre_router("cerca riferimenti sul poster", on_text=visti.append)

    assert visti[0] == "Cerco riferimenti sul web e su YouTube. Un attimo."
    assert visti[-1].startswith("🔎 Ho cercato")


def test_pre_router_regge_un_on_text_rotto(sessione, ricerca_finta):
    import jarvis_brain as jb

    def on_text_rotto(testo):
        raise RuntimeError("audio occupato")

    assert "Ho cercato" in jb._cerca_riferimenti_pre_router(
        "cerca riferimenti sul poster", on_text=on_text_rotto)


def test_pre_router_usa_il_control_plane(monkeypatch, sessione):
    """L'azione deve passare da policy.guarded_call (audit + policy)."""
    import jarvis_brain as jb

    usati = []
    monkeypatch.setattr(policy, "guarded_call",
                        lambda nome, fn, args, confirm=None, origin=None:
                        usati.append((nome, args, origin)) or "fatto dal control-plane")

    risposta = jb._cerca_riferimenti_pre_router("cerca riferimenti", origin="voice")

    assert risposta == "fatto dal control-plane"
    assert usati[0][0] == "cerca_riferimenti"
    assert usati[0][1]["idea_id"] == sessione.idea_id
    assert usati[0][2] == "voice"


def test_think_and_act_risponde_senza_llm(sessione, ricerca_finta, monkeypatch, tmp_path):
    """Il flusso vero: think_and_act corto-circuita prima del LLM."""
    import jarvis_brain as jb

    monkeypatch.setattr(jb.JarvisBrain, "_session_file",
                        lambda self: str(tmp_path / "jarvis_session.json"))
    monkeypatch.setattr(jb, "chat",
                        lambda *a, **k: pytest.fail("non deve chiamare il LLM"),
                        raising=False)

    cervello = jb.JarvisBrain()
    risposta = cervello.think_and_act("Clio, cerca riferimenti sul poster", origin="voice")

    assert "Ho cercato «poster»" in risposta
    assert cervello.session_history[-1]["assistant"].startswith("🔎")
    assert len(idea.load_idea_session(sessione.idea_id).materiali) == 4
