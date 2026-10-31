"""research_agent.py — FASE 2 · C4: Research Agent (riferimenti web + YouTube).

Chiude il "Partner Creativo" lato ispirazione: mentre l'utente lavora a un'idea,
CLIO cerca riferimenti simili sul web e su YouTube, estrae i metadati che
servono (titolo, URL, durata per i video), li riassume e li salva nella
IdeaSession come materiali con `fonte: "research"`.

Contratto pubblico (registrato in AZIONI_MAP come `cerca_riferimenti`):
    search_web(query, max_results=3)        -> list[{title, url, snippet}]
    search_youtube(query, max_results=2)    -> list[{title, url, duration}]
    summarize_results(results, query="")    -> str       (2-3 frasi, via Gemini)
    research_for_idea(idea_id, query)       -> dict      (cerca + riassume + salva)
    cerca_riferimenti(query="", idea_id="") -> str       (azione CLIO, voce/LLM)
    format_research_reply(esito)            -> str       (frase unica, testabile)

Regole del modulo (le stesse di vision_agent):
  * Playwright si importa DENTRO le funzioni: CLIO parte anche senza browser e i
    test girano con un finto `_open_page` (nessuna GUI, nessuna rete);
  * nessuna eccezione raggiunge CLIO: rete assente, motore che blocca l'IP,
    timeout o browser non installato -> lista vuota + log, la sessione continua;
  * DuckDuckGo HTML è il motore richiesto; se risponde con la pagina anti-bot
    (succede con IP "da datacenter") si prova `lite`, poi Bing, poi ci si ferma;
  * il riassunto passa dal modello Gemini configurato; senza chiave/rete si usa
    un riassunto deterministico, quindi la ricerca resta utile anche offline;
  * `CLIO_RESEARCH_OFFLINE=1` disattiva del tutto la rete (test, demo offline).
"""
from __future__ import annotations

import base64
import contextlib
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import idea_session

logger = logging.getLogger("ClioResearch")

ACTION = "cerca_riferimenti"          # nome registrato in AZIONI_MAP / READ_ONLY

DEFAULT_MAX_WEB = 3
DEFAULT_MAX_YOUTUBE = 2
MAX_QUERY_CHARS = 200
MAX_TITOLO_CHARS = 200
MAX_SNIPPET_CHARS = 400
MAX_SUMMARY_CHARS = 1200
MAX_VOCI_PROMPT = 8

DEFAULT_TIMEOUT_MS = 10_000
DEFAULT_WAIT_MS = 1_200

ENV_OFFLINE = "CLIO_RESEARCH_OFFLINE"
ENV_TIMEOUT = "CLIO_RESEARCH_TIMEOUT_MS"
ENV_MODEL = "CLIO_RESEARCH_MODEL"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LOCALE = "it-IT"

DDG_HTML_URL = "https://html.duckduckgo.com/html/?q={}"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/?q={}"
BING_URL = "https://www.bing.com/search?q={}&setlang=it"
YOUTUBE_URL = "https://www.youtube.com/results?search_query={}"
YOUTUBE_BASE = "https://www.youtube.com"

PROMPT_RIASSUNTO = (
    "Sei CLIO, il partner creativo di Francesco. Ho raccolto questi riferimenti "
    "per l'idea «{query}»:\n{voci}\n\n"
    "Scrivi in italiano un riassunto di 2-3 frasi (massimo 90 parole) che dica "
    "cosa fanno questi riferimenti e cosa possono suggerire per l'idea. "
    "Cita SEMPRE il dominio della fonte accanto a ciò che riporti "
    "(es. «secondo python.org…», «su wikipedia.org…»): il dominio è la parte "
    "dell'URL tra «https://» e la prima «/». "
    "Usa SOLO i dati qui sopra: non inventare titoli, autori o contenuti."
)


class ResearchError(RuntimeError):
    """Errore prevedibile del Research Agent (messaggio già pronto per l'utente)."""


# ── Estrazione lato pagina (JavaScript eseguito in Playwright) ───────────────
_JS_DDG = """
() => Array.from(document.querySelectorAll('div.result')).map(d => {
  const a = d.querySelector('a.result__a');
  const s = d.querySelector('.result__snippet');
  return { titolo: a ? a.textContent : '',
           href: a ? a.getAttribute('href') : '',
           snippet: s ? s.textContent : '' };
})
"""

_JS_DDG_LITE = """
() => {
  const out = [];
  document.querySelectorAll('a.result-link').forEach(a => {
    let snip = '';
    const riga = a.closest('tr');
    const dopo = riga ? riga.nextElementSibling : null;
    if (dopo) { const s = dopo.querySelector('.result-snippet, td'); if (s) snip = s.textContent; }
    out.push({ titolo: a.textContent, href: a.getAttribute('href'), snippet: snip });
  });
  return out;
}
"""

_JS_BING = """
() => Array.from(document.querySelectorAll('li.b_algo')).map(li => {
  const a = li.querySelector('h2 a');
  const p = li.querySelector('p');
  return { titolo: a ? a.textContent : '',
           href: a ? a.href : '',
           snippet: p ? p.textContent : '' };
})
"""

_JS_YOUTUBE = """
() => Array.from(document.querySelectorAll('ytd-video-renderer')).map(v => {
  const a = v.querySelector('a#video-title') || v.querySelector('a#video-title-link');
  const d = v.querySelector('ytd-thumbnail-overlay-time-status-renderer #text') ||
            v.querySelector('#text.ytd-thumbnail-overlay-time-status-renderer') ||
            v.querySelector('ytd-thumbnail-overlay-time-status-renderer span');
  const c = v.querySelector('#channel-name a') || v.querySelector('ytd-channel-name a');
  return { titolo: a ? a.textContent : '',
           href: a ? a.getAttribute('href') : '',
           durata: d ? d.textContent : '',
           canale: c ? c.textContent : '' };
})
"""

# Selettori del consenso cookie di YouTube (UE): senza, la pagina resta vuota.
_SELETTORI_CONSENSO_YT = (
    "button[aria-label*='Accetta']",
    "button[aria-label*='Accept']",
    "button[aria-label*='Rifiuta']",
    "#introAgreeButton",
)


# ── Configurazione ───────────────────────────────────────────────────────────
def _timeout_ms() -> int:
    """Timeout per singola navigazione (env `CLIO_RESEARCH_TIMEOUT_MS`)."""
    grezzo = (os.environ.get(ENV_TIMEOUT) or "").strip()
    try:
        valore = int(grezzo)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS
    return valore if 1000 <= valore <= 120_000 else DEFAULT_TIMEOUT_MS


def _offline() -> bool:
    """True se la rete è disattivata a mano (`CLIO_RESEARCH_OFFLINE=1`)."""
    return (os.environ.get(ENV_OFFLINE) or "").strip().lower() in ("1", "true", "yes", "si")


def _clean_text(valore: Any, massimo: int) -> str:
    """Testo su una riga (spazi collassati), troncato a `massimo` caratteri."""
    if not isinstance(valore, str):
        return ""
    testo = " ".join(valore.split())
    return testo[:massimo]


def _clean_query(query: Any) -> str:
    return _clean_text(query, MAX_QUERY_CHARS)


# ── URL: redirect, protocolli, YouTube ──────────────────────────────────────
def _bing_real_url(href: str) -> str:
    """Decodifica i redirect di Bing (`/ck/a?...&u=a1<base64url>`)."""
    if "bing.com/ck/a" not in href:
        return href
    valori = parse_qs(urlparse(href).query).get("u") or []
    if not valori:
        return href
    codificato = valori[0]
    if codificato[:2] in ("a1", "a2"):
        codificato = codificato[2:]
    riempimento = "=" * (-len(codificato) % 4)
    try:
        reale = base64.urlsafe_b64decode(codificato + riempimento).decode("utf-8", "replace")
    except Exception:
        return href
    return reale if reale.startswith(("http://", "https://")) else href


def _absolute_url(href: str, base: str = "") -> str:
    """URL http(s) assoluto; stringa vuota se non è navigabile.

    Gestisce: protocollo relativo (`//host`), redirect DuckDuckGo (`uddg=`),
    redirect Bing (`u=a1...`), percorsi relativi con `base`.
    """
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "duckduckgo.com/l/" in href:
        valori = parse_qs(urlparse(href).query).get("uddg") or []
        if valori:
            href = unquote(valori[0])
    href = _bing_real_url(href)
    if href.startswith("/") and base:
        href = base + href
    if not href.startswith(("http://", "https://")):
        return ""
    return href[:500]


def _youtube_url(href: str) -> str:
    """URL 'pulito' del video (`watch?v=ID`); stringa vuota se non è un video."""
    assoluto = _absolute_url(href, base=YOUTUBE_BASE)
    if not assoluto:
        return ""
    video_id = (parse_qs(urlparse(assoluto).query).get("v") or [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or ""):
        return f"{YOUTUBE_BASE}/watch?v={video_id}"
    return assoluto


# ── Playwright: un solo punto di accesso (mockabile nei test) ───────────────
@contextlib.contextmanager
def _open_page():
    """Pagina Chromium headless pronta all'uso. ResearchError se non parte.

    È l'UNICO punto che tocca Playwright: i test sostituiscono questa funzione
    con un finto context manager, quindi nessuna GUI e nessuna rete.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                       # pragma: no cover - dipende dall'ambiente
        raise ResearchError(f"Playwright non installato: {type(e).__name__}: {e}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                contesto = browser.new_context(
                    user_agent=USER_AGENT,
                    locale=LOCALE,
                    viewport={"width": 1280, "height": 900},
                )
                pagina = contesto.new_page()
                pagina.set_default_timeout(_timeout_ms())
                yield pagina
            finally:
                browser.close()
    except ResearchError:
        raise
    except Exception as e:                       # pragma: no cover - dipende dall'ambiente
        raise ResearchError(f"browser headless non avviato: {type(e).__name__}: {e}")


def _estrai(pagina, javascript: str) -> List[Dict[str, Any]]:
    """Esegue l'estrazione JS e garantisce una lista di dizionari."""
    try:
        grezzi = pagina.evaluate(javascript)
    except Exception as e:
        logger.warning(f"[RESEARCH] estrazione dalla pagina fallita: {type(e).__name__}: {e}")
        return []
    if not isinstance(grezzi, list):
        return []
    return [v for v in grezzi if isinstance(v, dict)]


def _apri(pagina, url: str, attesa_ms: int = DEFAULT_WAIT_MS) -> bool:
    """Naviga e lascia respirare la pagina. False se la navigazione fallisce."""
    try:
        pagina.goto(url, wait_until="domcontentloaded", timeout=_timeout_ms())
    except Exception as e:
        logger.warning(f"[RESEARCH] navigazione fallita ({url[:80]}): {type(e).__name__}: {e}")
        return False
    try:
        pagina.wait_for_timeout(attesa_ms)
    except Exception:                            # pragma: no cover - pagina chiusa
        pass
    return True


# ── Ricerca web ─────────────────────────────────────────────────────────────
def _normalizza_web(grezzi: List[Dict[str, Any]], limite: int) -> List[Dict[str, str]]:
    """[{title, url, snippet}] pulito: niente vuoti, niente URL duplicati."""
    out: List[Dict[str, str]] = []
    visti = set()
    for voce in grezzi:
        titolo = _clean_text(voce.get("titolo") or voce.get("title"), MAX_TITOLO_CHARS)
        url = _absolute_url(voce.get("href") or voce.get("url") or "")
        if not titolo or not url:
            continue
        chiave = url.lower()
        if chiave in visti:
            continue
        visti.add(chiave)
        out.append({
            "title": titolo,
            "url": url,
            "snippet": _clean_text(voce.get("snippet") or voce.get("descrizione"), MAX_SNIPPET_CHARS),
        })
        if len(out) >= limite:
            break
    return out


def search_web(query: str, max_results: int = DEFAULT_MAX_WEB) -> List[Dict[str, str]]:
    """Cerca sul web con Playwright (DuckDuckGo HTML) e torna [{title,url,snippet}].

    Ordine dei motori: DuckDuckGo HTML (quello richiesto), DuckDuckGo lite,
    Bing. Il primo che restituisce risultati vince. Mai un'eccezione: in caso di
    blocco/rete assente/timeout si ritorna la lista vuota e si logga il motivo.
    """
    testo = _clean_query(query)
    if not testo or max_results <= 0:
        return []
    if _offline():
        logger.info("[RESEARCH] ricerca web saltata: CLIO_RESEARCH_OFFLINE attivo")
        return []

    motori = (
        ("duckduckgo-html", DDG_HTML_URL.format(quote_plus(testo)), _JS_DDG),
        ("duckduckgo-lite", DDG_LITE_URL.format(quote_plus(testo)), _JS_DDG_LITE),
        ("bing", BING_URL.format(quote_plus(testo)), _JS_BING),
    )
    try:
        with _open_page() as pagina:
            for nome, url, javascript in motori:
                if not _apri(pagina, url):
                    continue
                risultati = _normalizza_web(_estrai(pagina, javascript), max_results)
                if risultati:
                    logger.info(f"[RESEARCH] web «{testo}»: {len(risultati)} risultati da {nome}")
                    return risultati
                logger.info(f"[RESEARCH] {nome}: nessun risultato (blocco anti-bot o pagina vuota)")
    except ResearchError as e:
        logger.warning(f"[RESEARCH] ricerca web non disponibile: {e}")
    except Exception as e:                       # pragma: no cover - difesa
        logger.warning(f"[RESEARCH] ricerca web fallita: {type(e).__name__}: {e}")
    logger.info(f"[RESEARCH] web «{testo}»: nessun risultato")
    return []


# ── Ricerca YouTube ─────────────────────────────────────────────────────────
def _normalizza_youtube(grezzi: List[Dict[str, Any]], limite: int) -> List[Dict[str, str]]:
    """[{title, url, duration}] pulito, con URL `watch?v=ID` e durata leggibile."""
    out: List[Dict[str, str]] = []
    visti = set()
    for voce in grezzi:
        titolo = _clean_text(voce.get("titolo") or voce.get("title"), MAX_TITOLO_CHARS)
        url = _youtube_url(voce.get("href") or voce.get("url") or "")
        # Solo veri video: canali, playlist e pagine di ricerca non sono riferimenti.
        if not titolo or "watch?v=" not in url:
            continue
        chiave = url.lower()
        if chiave in visti:
            continue
        visti.add(chiave)
        elemento = {
            "title": titolo,
            "url": url,
            "duration": _clean_text(voce.get("durata") or voce.get("duration"), 20),
        }
        canale = _clean_text(voce.get("canale") or voce.get("channel"), 80)
        if canale:
            elemento["channel"] = canale
        out.append(elemento)
        if len(out) >= limite:
            break
    return out


def _accetta_consenso(pagina) -> None:
    """Prova ad accettare il banner cookie di YouTube (ignora ogni errore)."""
    for selettore in _SELETTORI_CONSENSO_YT:
        try:
            pulsante = pagina.query_selector(selettore)
            if pulsante:
                pulsante.click(timeout=2000)
                pagina.wait_for_timeout(1200)
                return
        except Exception:
            continue


def search_youtube(query: str, max_results: int = DEFAULT_MAX_YOUTUBE) -> List[Dict[str, str]]:
    """Cerca video su YouTube (solo metadati: nessun download). Mai un'eccezione."""
    testo = _clean_query(query)
    if not testo or max_results <= 0:
        return []
    if _offline():
        logger.info("[RESEARCH] ricerca YouTube saltata: CLIO_RESEARCH_OFFLINE attivo")
        return []

    url = YOUTUBE_URL.format(quote_plus(testo))
    try:
        with _open_page() as pagina:
            if not _apri(pagina, url, attesa_ms=2500):
                return []
            grezzi = _estrai(pagina, _JS_YOUTUBE)
            if not grezzi:
                _accetta_consenso(pagina)
                grezzi = _estrai(pagina, _JS_YOUTUBE)
            risultati = _normalizza_youtube(grezzi, max_results)
            if risultati:
                logger.info(f"[RESEARCH] YouTube «{testo}»: {len(risultati)} video")
            else:
                logger.info(f"[RESEARCH] YouTube «{testo}»: nessun video")
            return risultati
    except ResearchError as e:
        logger.warning(f"[RESEARCH] ricerca YouTube non disponibile: {e}")
    except Exception as e:                       # pragma: no cover - difesa
        logger.warning(f"[RESEARCH] ricerca YouTube fallita: {type(e).__name__}: {e}")
    return []


# ── Riassunto (Gemini, con ripiego deterministico) ──────────────────────────
_SUMMARIZER_HOOK = None


def set_summarizer_hook(fn):
    """Installa (o rimuove, con None) il riassuntore usato al posto di Gemini."""
    global _SUMMARIZER_HOOK
    _SUMMARIZER_HOOK = fn if callable(fn) else None
    return _SUMMARIZER_HOOK


def _research_model() -> str:
    """Modello per il riassunto: env CLIO_RESEARCH_MODEL, poi config.MODEL_PRIMARY."""
    da_env = (os.environ.get(ENV_MODEL) or "").strip()
    if da_env:
        return da_env
    try:
        from config import MODEL_PRIMARY
        return MODEL_PRIMARY or "models/gemini-3.8-flash"
    except Exception:                            # pragma: no cover - config assente
        return "models/gemini-3.8-flash"


def _gemini_summarize(prompt: str) -> str:
    """Chiamata a Gemini (google-genai) senza avviare il runtime di CLIO."""
    from config import GEMINI_API_KEY
    if not (GEMINI_API_KEY or "").strip():
        raise ResearchError("GEMINI_API_KEY non configurata in .env")
    from google import genai
    client = genai.Client(api_key=GEMINI_API_KEY)
    risposta = client.models.generate_content(model=_research_model(), contents=prompt)
    return _clean_text(getattr(risposta, "text", ""), MAX_SUMMARY_CHARS * 2)


def _voci_per_il_prompt(results: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Normalizza i risultati eterogenei (web + YouTube) per il prompt."""
    voci: List[Dict[str, str]] = []
    for voce in results or []:
        if not isinstance(voce, dict):
            continue
        titolo = _clean_text(voce.get("title") or voce.get("titolo"), MAX_TITOLO_CHARS)
        if not titolo:
            continue
        durata = _clean_text(voce.get("duration") or voce.get("durata"), 20)
        voci.append({
            "tipo": "youtube" if durata or "youtube.com" in str(voce.get("url") or "") else "web",
            "titolo": titolo,
            "url": _clean_text(voce.get("url"), 500),
            "extra": durata or _clean_text(voce.get("snippet") or voce.get("descrizione"),
                                           MAX_SNIPPET_CHARS),
        })
        if len(voci) >= MAX_VOCI_PROMPT:
            break
    return voci


def _riassunto_senza_modello(voci: List[Dict[str, str]], query: str = "") -> str:
    """Riassunto deterministico quando Gemini non è disponibile (mai un errore)."""
    web = [v for v in voci if v["tipo"] == "web"]
    video = [v for v in voci if v["tipo"] == "youtube"]
    parti = []
    contesto = f" per «{query}»" if query else ""
    parti.append(f"Ho trovato {len(voci)} riferimenti{contesto}: "
                 f"{len(web)} pagine web e {len(video)} video.")
    titoli = ", ".join(v["titolo"] for v in voci[:3])
    if titoli:
        parti.append(f"I titoli più vicini sono: {titoli}.")
    parti.append("Nessun modello disponibile: il riassunto è un elenco, non un'analisi.")
    return " ".join(parti)


def summarize_results(results: List[Dict[str, Any]], query: str = "") -> str:
    """Riassume i riferimenti in 2-3 frasi con Gemini. Mai un'eccezione.

    Se il modello non è raggiungibile (chiave assente, rete, timeout) si torna un
    riassunto deterministico: la ricerca resta utile e la sessione non si blocca.
    """
    voci = _voci_per_il_prompt(results)
    if not voci:
        return "Non ho trovato riferimenti da riassumere."

    righe = []
    for indice, voce in enumerate(voci, 1):
        riga = f"{indice}. [{voce['tipo']}] {voce['titolo']} — {voce['url']}"
        if voce["extra"]:
            riga += f" — {voce['extra']}"
        righe.append(riga)
    prompt = PROMPT_RIASSUNTO.format(query=_clean_query(query) or "in lavorazione",
                                     voci="\n".join(righe))

    hook = _SUMMARIZER_HOOK
    if callable(hook):
        try:
            testo = _clean_text(hook(prompt), MAX_SUMMARY_CHARS * 2)
        except Exception as e:
            logger.warning(f"[RESEARCH] riassuntore iniettato fallito: {type(e).__name__}: {e}")
            testo = ""
    else:
        try:
            testo = _gemini_summarize(prompt)
        except Exception as e:
            logger.warning(f"[RESEARCH] Gemini non disponibile: {type(e).__name__}: {e}")
            testo = ""

    if not testo:
        testo = _riassunto_senza_modello(voci, query)
    if len(testo) > MAX_SUMMARY_CHARS:
        testo = testo[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return testo


# ── Ciclo completo: cerca -> riassume -> IdeaSession ────────────────────────
def _resolve_session(idea_id: str) -> Tuple[Optional[Any], str]:
    """Sessione per id, oppure l'ultima aperta. (None, motivo) se non c'è."""
    if (idea_id or "").strip():
        sessione = idea_session.load_idea_session(idea_id)
        if sessione is None:
            return None, f"idea «{idea_id}» non trovata"
        return sessione, ""
    sessione = idea_session.get_active_idea_session()
    if sessione is None:
        return None, "nessuna IdeaSession aperta"
    return sessione, ""


def _riferimento_da_materiale(voce: Dict[str, str]) -> Dict[str, str]:
    """Materiale IdeaSession per un risultato web o un video YouTube.

    Accetta sia le chiavi normalizzate di `search_web`/`search_youtube`
    (`title`, `url`, `duration`) sia quelle grezze di una pagina
    (`titolo`, `href`, `durata`): l'agente non deve rompersi per un nome.
    """
    durata = voce.get("duration") or voce.get("durata") or ""
    elemento = {
        "url": voce.get("url") or voce.get("href") or "",
        "titolo": voce.get("title") or voce.get("titolo") or "",
        "fonte": "research",
        "tipo": "youtube" if durata else "web",
    }
    if durata:
        elemento["durata"] = durata
    if voce.get("snippet"):
        elemento["snippet"] = voce["snippet"]
    return elemento


def research_for_idea(idea_id: str = "", query: str = "",
                      max_web: int = DEFAULT_MAX_WEB,
                      max_youtube: int = DEFAULT_MAX_YOUTUBE) -> Dict[str, Any]:
    """Cerca web + YouTube, riassume e salva i link nell'IdeaSession.

    Ritorna `{ok, idea_id, query, web, youtube, riassunto, salvati, error}`.
    Non solleva mai: un guasto di rete diventa `web=[]` + `error`, e i
    riferimenti già trovati vengono comunque salvati.
    """
    esito: Dict[str, Any] = {
        "ok": False, "idea_id": "", "query": "",
        "web": [], "youtube": [], "riassunto": "", "salvati": 0, "error": None,
    }

    sessione, motivo = _resolve_session(idea_id)
    if sessione is None:
        logger.warning(f"[RESEARCH] research_for_idea: {motivo}")
        esito["error"] = motivo
        return esito
    esito["idea_id"] = sessione.idea_id

    testo_query = _clean_query(query) or _clean_query(sessione.user_goal)
    if not testo_query:
        esito["error"] = "query vuota e obiettivo dell'idea vuoto"
        logger.warning(f"[RESEARCH] research_for_idea: {esito['error']}")
        return esito
    esito["query"] = testo_query

    errori: List[str] = []
    try:
        esito["web"] = search_web(testo_query, max_web)
    except Exception as e:                       # pragma: no cover - difesa
        errori.append(f"web: {type(e).__name__}: {e}")
        logger.warning(f"[RESEARCH] web fallito: {errori[-1]}")
    try:
        esito["youtube"] = search_youtube(testo_query, max_youtube)
    except Exception as e:                       # pragma: no cover - difesa
        errori.append(f"youtube: {type(e).__name__}: {e}")
        logger.warning(f"[RESEARCH] youtube fallito: {errori[-1]}")

    tutti = list(esito["web"]) + list(esito["youtube"])
    if not tutti:
        esito["error"] = "; ".join(errori) or (
            "nessun riferimento trovato (motore bloccato, rete assente o query troppo stretta)")
        logger.info(f"[RESEARCH] «{testo_query}»: nessun riferimento ({esito['error']})")
        return esito

    try:
        esito["riassunto"] = summarize_results(tutti, testo_query)
    except Exception as e:                       # pragma: no cover - difesa
        errori.append(f"riassunto: {type(e).__name__}: {e}")
        logger.warning(f"[RESEARCH] riassunto fallito: {errori[-1]}")
        esito["riassunto"] = _riassunto_senza_modello(_voci_per_il_prompt(tutti), testo_query)

    for voce in tutti:
        try:
            if idea_session.add_riferimento(sessione.idea_id, _riferimento_da_materiale(voce)):
                esito["salvati"] += 1
        except Exception as e:                   # pragma: no cover - difesa
            logger.warning(f"[RESEARCH] riferimento non salvato: {type(e).__name__}: {e}")

    # "aggiorna lo stato": l'idea registra l'ultima ricerca fatta e quando.
    # NB: `add_riferimento` ha riscritto la sessione sullo store, quindi qui si
    # rilegge (una sessione "stantia" cancellerebbe i materiali appena salvati).
    aggiornata = idea_session.load_idea_session(sessione.idea_id) or sessione
    aggiornata.ricerca = {
        "query": testo_query,
        "riassunto": esito["riassunto"],
        "data": idea_session._now(),
        "web": len(esito["web"]),
        "youtube": len(esito["youtube"]),
        "salvati": esito["salvati"],
    }
    aggiornata.touch()
    try:
        idea_session.update_idea_session(aggiornata)
    except Exception as e:
        errori.append(f"salvataggio idea: {type(e).__name__}: {e}")
        logger.warning(f"[RESEARCH] stato idea non salvato: {errori[-1]}")

    esito["ok"] = True
    esito["error"] = "; ".join(errori) or None
    logger.info(f"[RESEARCH] «{testo_query}»: {len(esito['web'])} web, "
                f"{len(esito['youtube'])} video, {esito['salvati']} salvati nell'idea "
                f"{sessione.idea_id[:8]}")
    return esito


def format_research_reply(esito: Dict[str, Any]) -> str:
    """Frase unica (voce/LLM) a partire dal dizionario di `research_for_idea`."""
    if not isinstance(esito, dict):
        return "❌ Ricerca non eseguita: risposta non valida."
    if not esito.get("idea_id"):
        return f"❌ Ricerca non eseguita: {esito.get('error') or 'nessuna idea aperta'}."
    if not esito.get("ok"):
        return (f"🔎 Non ho trovato riferimenti per «{esito.get('query') or 'la tua idea'}»: "
                f"{esito.get('error') or 'motore non raggiungibile'}. "
                "L'idea resta com'è: riprovo quando vuoi.")

    web = len(esito.get("web") or [])
    video = len(esito.get("youtube") or [])
    salvati = esito.get("salvati") or 0
    parti = [f"🔎 Ho cercato «{esito.get('query')}»: {web} riferimenti web e {video} video."]
    if esito.get("riassunto"):
        parti.append(str(esito["riassunto"]))
    if salvati:
        parti.append(f"Ho collegato {salvati} link all'idea {str(esito.get('idea_id'))[:8]} "
                     "come materiali di ricerca.")
    else:
        parti.append(f"Erano già tutti collegati all'idea {str(esito.get('idea_id'))[:8]}: "
                     "nessun doppione.")
    return " ".join(parti)


def cerca_riferimenti(query: str = "", idea_id: str = "") -> str:
    """Azione CLIO: cerca riferimenti web/YouTube per l'idea e li salva."""
    return format_research_reply(research_for_idea(idea_id=idea_id, query=query))
