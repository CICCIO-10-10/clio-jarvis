"""vision_agent.py — FASE 2 · C2: Vision Agent (guarda la finestra e aggiorna l'idea).

Chiude il ciclo del "Partner Creativo": CLIO apre lo strumento (C1), poi GUARDA
cosa l'utente sta facendo su quella finestra e lo racconta nella IdeaSession.

Contratto pubblico (registrato in AZIONI_MAP e usato dal LLM):
    capture_window("mspaint")                  -> str | None
    describe_sketch("/tmp/....png")            -> str
    analyze_sketch_and_update_idea(idea_id, app_name) -> str

Sicurezza e limiti:
  * si cattura SOLO il rettangolo della finestra richiesta (mai tutto lo schermo
    per sbaglio): se la finestra non esiste non si scatta nulla e si ritorna None;
  * l'immagine viene passata al modello VISION configurato (config.GEMINI_VISION_MODEL
    / profilo VISION del model_router, Gemini di default). NON si importa
    `clio_core`: il suo import avvia il runtime (thread proattivo, screenshot
    silenziosi, audio) — qui servono solo client e immagine;
  * i PNG temporanei vengono cancellati dopo l'analisi (privacy) a meno di
    CLIO_KEEP_SCREENSHOTS=1;
  * nessuna eccezione raggiunge CLIO: app assente, finestra non visibile,
    immagine mancante, provider non configurato -> stringa pulita / None.

Nessun import pesante a livello di modulo: mss, PIL, google-genai e actions
vengono importati dentro le funzioni, cosi' i test girano con mock e CLIO parte
anche senza schermo o senza rete.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import desktop_agent
import idea_session

logger = logging.getLogger("ClioVision")

ACTION_ANALYZE = "analyze_sketch_and_update_idea"   # READ_ONLY in policy.py
ENV_KEEP_SCREENSHOTS = "CLIO_KEEP_SCREENSHOTS"

# Una finestra più piccola di così è un residuo/tooltip: non vale una chiamata al modello.
MIN_CAPTURE_PX = 64
MAX_DESCRIPTION_CHARS = 2000
MAX_SCREENSHOTS = 20            # PNG temporanei da tenere al massimo

DEFAULT_PROMPT = (
    "Descrivi in italiano, in modo breve (max 120 parole), cosa mostra questa "
    "finestra. Se è un disegno o uno schizzo, descrivi forme, colori, "
    "composizione e cosa sembra rappresentare. Non inventare dettagli che non vedi."
)


class VisionAgentError(RuntimeError):
    """Errore prevedibile del Vision Agent (messaggio già pronto per l'utente)."""


# ── Cattura della finestra ───────────────────────────────────────────────────
def _screenshot_dir() -> Path:
    """Cartella dei PNG temporanei (fuori dal repo: sono dati personali)."""
    return Path(tempfile.gettempdir()) / "clio_vision"


def _prune(directory: Path, keep: int = MAX_SCREENSHOTS) -> None:
    """Tiene solo gli ultimi `keep` PNG: la cartella temporanea non cresce."""
    try:
        png = sorted(directory.glob("clio_window_*.png"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for vecchio in png[keep:]:
        try:
            vecchio.unlink()
        except OSError:
            pass


def _window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Rettangolo (left, top, right, bottom) della finestra. None se non valido."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    except Exception as e:  # pragma: no cover - dipende dall'ambiente
        logger.warning(f"[VISION] GetWindowRect fallita: {type(e).__name__}: {e}")
        return None
    if right - left < MIN_CAPTURE_PX or bottom - top < MIN_CAPTURE_PX:
        logger.info(f"[VISION] finestra troppo piccola ({right - left}x{bottom - top}px)")
        return None
    return (left, top, right, bottom)


def _is_minimized(hwnd: int) -> bool:
    """True se la finestra è ridotta a icona (allora non c'è nulla da catturare)."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.IsIconic(hwnd))
    except Exception:  # pragma: no cover - dipende dall'ambiente
        return False


def _grab_region(rect: Tuple[int, int, int, int]):
    """Cattura il rettangolo indicato con mss e ritorna una PIL.Image RGB."""
    import mss
    from PIL import Image

    left, top, right, bottom = rect
    regione = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    with mss.mss() as sct:
        shot = sct.grab(regione)
        # mss restituisce BGRA: convertiamo in RGB (stessa conversione di actions.py)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def capture_window(app_name: str, out_dir: Optional[Path] = None) -> Optional[str]:
    """Salva in un PNG temporaneo SOLO la finestra dell'app. None se non è possibile."""
    try:
        exe = desktop_agent.normalize_app_name(app_name)
    except Exception as e:
        logger.warning(f"[VISION] app non valida: {type(e).__name__}: {e}")
        return None

    finestra = desktop_agent.find_window(exe)
    if not finestra:
        logger.info(f"[VISION] nessuna finestra per {exe}: niente cattura")
        return None

    rect = _window_rect(int(finestra["hwnd"]))
    if not rect:
        if _is_minimized(int(finestra["hwnd"])):
            logger.info(f"[VISION] {exe} è ridotta a icona: niente cattura")
        return None

    try:
        immagine = _grab_region(rect)
    except Exception as e:
        logger.warning(f"[VISION] cattura fallita per {exe}: {type(e).__name__}: {e}")
        return None

    directory = Path(out_dir) if out_dir is not None else _screenshot_dir()
    percorso = directory / f"clio_window_{Path(exe).stem}_{time.strftime('%Y%m%d-%H%M%S')}.png"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        immagine.save(percorso)
    except Exception as e:
        logger.warning(f"[VISION] salvataggio fallito ({percorso}): {type(e).__name__}: {e}")
        return None

    _prune(directory)
    larghezza, altezza = (rect[2] - rect[0], rect[3] - rect[1])
    logger.info(f"[VISION] finestra '{finestra.get('title')}' catturata: "
                f"{larghezza}x{altezza} -> {percorso.name}")
    return str(percorso)


def capture_window_image(app_name: str):
    """PIL.Image RGB della SOLA finestra dell'app, o None.

    Differenza rispetto a capture_window(): non scrive nessun PNG su disco.
    L'immagine resta in memoria e viene usata da chi la richiede (es. il
    rilevamento del foglio bianco in skills.disegna_su_paint): nessun pixel
    finisce in un file, in un log o in rete.

    Riusa la stessa pipeline già testata (_window_rect + _grab_region), quindi
    il rettangolo è quello della finestra e non dello schermo intero.
    """
    try:
        exe = desktop_agent.normalize_app_name(app_name)
    except Exception as e:
        logger.warning(f"[VISION] app non valida: {type(e).__name__}: {e}")
        return None

    finestra = desktop_agent.find_window(exe)
    if not finestra:
        logger.info(f"[VISION] nessuna finestra per {exe}: niente cattura")
        return None

    hwnd = int(finestra["hwnd"])
    if _is_minimized(hwnd):
        logger.info(f"[VISION] {exe} è ridotta a icona: niente cattura")
        return None

    rect = _window_rect(hwnd)
    if not rect:
        return None

    try:
        immagine = _grab_region(rect)
    except Exception as e:
        logger.warning(f"[VISION] cattura fallita per {exe}: {type(e).__name__}: {e}")
        return None

    logger.info(f"[VISION] finestra '{finestra.get('title')}' catturata in RAM: "
                f"{immagine.width}x{immagine.height}")
    return immagine


# ── Descrizione con il modello visivo ────────────────────────────────────────
def _vision_api_key() -> str:
    """Chiave Gemini da .env (stringa vuota se non configurata)."""
    try:
        from config import GEMINI_API_KEY
    except Exception:  # config non importabile: nessuna chiave
        return ""
    return (GEMINI_API_KEY or "").strip()


def _vision_model() -> str:
    """Modello del profilo VISION (configurabile), con fallback sicuro."""
    predefinito = "models/gemini-3.8-flash"
    try:
        import model_router

        profilo = model_router.get_profile(model_router.route_task("vision")) or {}
        return profilo.get("model") or predefinito
    except Exception:
        return predefinito


def _prepare_jpeg(image_path: str) -> bytes:
    """Ridimensiona (max 1280px) e comprime in JPEG: stessa pipeline di actions.py."""
    from actions import prepara_immagine_visione

    return prepara_immagine_visione(image_path)


_GEMINI_CLIENT = None


def _gemini_client():
    """Client google-genai condiviso. VisionAgentError se manca la chiave."""
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    chiave = _vision_api_key()
    if not chiave:
        raise VisionAgentError("GEMINI_API_KEY non configurata in .env")
    from google import genai

    _GEMINI_CLIENT = genai.Client(api_key=chiave)
    return _GEMINI_CLIENT


def _default_vision(image_path: str, prompt: str) -> str:
    """Chiama il modello VISION (Gemini) senza avviare il runtime di CLIO."""
    from google.genai import types

    client = _gemini_client()
    jpeg_bytes = _prepare_jpeg(image_path)
    risposta = client.models.generate_content(
        model=_vision_model(),
        contents=[prompt, types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")],
    )
    return getattr(risposta, "text", "") or ""


def _existing_image(image_path: str) -> Path:
    """Percorso di un'immagine leggibile. VisionAgentError se manca o è vuota."""
    if not isinstance(image_path, str) or not image_path.strip():
        raise VisionAgentError("percorso immagine mancante")
    percorso = Path(image_path.strip().strip('"').strip("'"))
    if not percorso.is_file():
        raise VisionAgentError(f"immagine non trovata: {percorso}")
    try:
        if percorso.stat().st_size == 0:
            raise VisionAgentError(f"immagine vuota: {percorso}")
    except OSError as e:
        raise VisionAgentError(f"immagine non leggibile ({percorso}): {e}")
    return percorso


def describe_sketch(image_path: str, prompt: Optional[str] = None) -> str:
    """Descrive l'immagine con il modello visivo configurato. Mai un'eccezione."""
    try:
        percorso = _existing_image(image_path)
    except VisionAgentError as e:
        logger.warning(f"[VISION] {e}")
        return f"❌ {e}"

    istruzione = (prompt or "").strip() or DEFAULT_PROMPT
    try:
        descrizione = _default_vision(str(percorso), istruzione)
    except Exception as e:
        logger.warning(f"[VISION] modello visivo non disponibile: {type(e).__name__}: {e}")
        return f"Vision non disponibile: {type(e).__name__}: {e}"

    testo = (descrizione or "").strip()
    if not testo:
        return "Vision non disponibile: risposta vuota dal modello."
    if len(testo) > MAX_DESCRIPTION_CHARS:
        testo = testo[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    logger.info(f"[VISION] descrizione pronta ({len(testo)} caratteri)")
    return testo


# ── Ciclo completo: cattura -> visione -> IdeaSession ────────────────────────
def _cleanup(percorso: str) -> None:
    """Cancella il PNG dopo l'analisi (privacy), salvo CLIO_KEEP_SCREENSHOTS=1."""
    if (os.environ.get(ENV_KEEP_SCREENSHOTS) or "").strip().lower() in ("1", "true", "yes", "si"):
        return
    try:
        Path(percorso).unlink()
    except OSError:
        pass


def _resolve_session(idea_id: str):
    """Sessione per id, oppure l'ultima ancora aperta. (None, motivo) se non c'è."""
    if (idea_id or "").strip():
        sessione = idea_session.load_idea_session(idea_id)
        if sessione is None:
            return None, f"❌ Idea «{idea_id}» non trovata: non aggiorno nulla."
        return sessione, ""
    sessione = idea_session.get_active_idea_session()
    if sessione is None:
        return None, ("❌ Nessuna IdeaSession aperta: avvia prima un'idea con "
                      "apri_app_e_inizia_idea.")
    return sessione, ""


def analyze_sketch_and_update_idea(idea_id: str = "", app_name: str = "mspaint",
                                   prompt: Optional[str] = None) -> str:
    """Cattura la finestra, la fa descrivere al modello visivo e aggiorna l'idea.

    `idea_id` vuoto = ultima IdeaSession ancora aperta. Se la cattura o la
    visione falliscono, la sessione NON viene toccata (niente osservazioni finte).
    READ_ONLY in policy.py: legge lo schermo e aggiorna solo lo stato di CLIO.
    """
    sessione, motivo = _resolve_session(idea_id)
    if sessione is None:
        return motivo

    try:
        exe = desktop_agent.normalize_app_name(app_name)
    except Exception as e:
        return f"❌ App non valida: {e}"

    percorso = capture_window(exe)
    if not percorso:
        return (f"❌ Non riesco a catturare la finestra di «{exe}». "
                "Verifica che l'app sia aperta e visibile: se è ridotta a icona, "
                "portala davanti con porta_finestra_in_primo_piano e riprova.")

    descrizione = describe_sketch(percorso, prompt)
    if descrizione.startswith("Vision non disponibile"):
        _cleanup(percorso)
        return f"❌ Screenshot acquisito ma la visione non è disponibile: {descrizione}"

    sessione.active_app = exe
    sessione.last_observation = descrizione
    sessione.touch()
    try:
        idea_session.update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[VISION] impossibile salvare l'osservazione: {type(e).__name__}: {e}")
        _cleanup(percorso)
        return f"❌ Visione riuscita ma non riesco a salvare l'idea {sessione.idea_id[:8]}: {e}"

    _cleanup(percorso)
    logger.info(f"[VISION] idea {sessione.idea_id} aggiornata con l'osservazione di {exe}")
    return (f"🎨 Idea {sessione.idea_id[:8]} aggiornata con l'osservazione di {exe}: "
            f"{descrizione}")


def describe_vision_agent() -> str:
    """Riepilogo leggibile dell'agente (usato a voce e nei report)."""
    return ("Vision Agent (C2): cattura solo la finestra indicata, la descrive con "
            "il profilo VISION (Gemini, o modello locale se configurato) e salva "
            "l'osservazione nella IdeaSession. I PNG temporanei vengono cancellati "
            f"dopo l'analisi (override: {ENV_KEEP_SCREENSHOTS}=1).")
