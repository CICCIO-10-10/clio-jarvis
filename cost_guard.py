"""Costi stimati delle azioni CLIO e registro locale dei consumi."""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger("ClioCostGuard")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COSTS_LOG_PATH = os.path.join(BASE_DIR, "data", "costs_log.json")
_LOG_LOCK = threading.Lock()

# Stime illustrative in USD; None indica un costo variabile/non determinabile.
# Il costo coding si applica solo se il profilo CODER include un provider cloud.
# Pollinations usa Pollen: non e' corretto considerarlo sempre gratuito.
PAID_ACTIONS = {
    "genera_immagine_gemini": 0.03,
    "esegui_coding_task": 0.10,
    "gemini-pro": 0.05,
    "gemini": 0.03,
    "see_screen": 0.01,
    # analyze_sketch_and_update_idea: usa pygetwindow locale (read-only, gratuito).
    "genera_immagine_pollinations": None,
}
COST_UNITS = {"genera_immagine_pollinations": "Pollen"}

# Servizi locali e operazioni gratuite esplicite. La classificazione di policy
# completa la lista per i tool locali già registrati, senza confondere costo e rischio.
FREE_ACTIONS = {
    "ollama", "apri_applicazione", "open_app", "apri_app",
    "apri_app_e_inizia_idea",
    # PyAutoGUI + pygetwindow in locale: nessuna rete, API o costo per disegnare.
    "disegna_su_paint",
    "genera_modello_3d_blender",
    "click_coordinates", "clicca_coordinate", "type_text", "digita_testo",
    "press_key", "premi_tasto", "take_screenshot", "fai_screenshot",
    "esegui_script_python", "run_python_code", "esegui_python", "esegui_codice",
    "esegui_comando_shell", "elimina_file", "apri_sito", "apri_cartella",
    # Vision locale (pygetwindow): cattura e analisi schermo senza API cloud.
    "analyze_sketch_and_update_idea",
    # Alias legacy: ora delegano soltanto al Coding Agent locale sandboxato.
    "run_opencode_task", "opencode_task", "esegui_opencode", "chiedi_a_opencode",
}


def estimated_cost(action_name: str) -> Optional[float]:
    """Restituisce la stima USD; None significa sconosciuta o variabile."""
    name = (action_name or "").strip()
    if name == "esegui_coding_task" and _coder_route_is_local_only():
        return 0.0
    if name in PAID_ACTIONS:
        return PAID_ACTIONS[name]
    return None


def _coder_route_is_local_only() -> bool:
    """True solo se il CODER ha un provider primario e fallback tutti locali."""
    try:
        backend = (os.getenv("CLIO_CODING_AGENT") or "local").strip().lower()
        if backend not in {"", "local", "ollama", "llamacpp"}:
            return False
        import model_router
        profile = model_router.get_profile("CODER")
        provider = str(profile.get("provider") or "").strip().lower()
        chain = [str(item).strip().lower()
                 for item in model_router.get_provider_chain("CODER")]
        local = {"ollama", "llamacpp"}
        return provider in local and bool(chain) and all(item in local for item in chain)
    except Exception as exc:
        logger.warning("Impossibile verificare la route CODER locale: %s", exc)
        return False


def is_paid_action(action_name: str) -> bool:
    """True per azioni esplicitamente marcate a pagamento o a costo variabile."""
    name = (action_name or "").strip()
    if name == "esegui_coding_task" and _coder_route_is_local_only():
        return False
    return name in PAID_ACTIONS


def check_cost(action_name: str, origin: str = None) -> bool:
    """True per azioni gratuite/locali; False per azioni a pagamento o non note.

    Per mantenere il comportamento sicuro, un tool non registrato come locale
    viene trattato come costo non verificato e richiede consenso.
    """
    name = (action_name or "").strip()
    if is_paid_action(name):
        cost = estimated_cost(name)
        estimate = f"${cost:.2f}" if cost is not None else "variabile/non determinabile"
        logger.warning(
            "⚠️ ATTENZIONE: L'azione '%s' comporterà un costo stimato di %s. "
            "In attesa di conferma... (origine: %s)",
            name, estimate, origin or "sconosciuta",
        )
        return False
    if name == "esegui_coding_task" and _coder_route_is_local_only():
        return True
    if name in FREE_ACTIONS:
        return True

    # Se il tool appartiene alle classi conosciute del Control Plane, il suo
    # effetto e' locale/gratuito ai fini del costo. Il rischio resta gestito
    # separatamente dalla conferma di policy.
    try:
        import policy
        known_actions = policy.READ_ONLY | policy.REVERSIBLE | policy.SENSITIVE | policy.IRREVERSIBLE
        if name in known_actions:
            return True
    except Exception:
        pass

    logger.warning(
        "⚠️ ATTENZIONE: costo dell'azione '%s' non verificato. "
        "In attesa di conferma esplicita (origine: %s).",
        name, origin or "sconosciuta",
    )
    return False


def record_paid_action(action_name: str, origin: str = None) -> dict:
    """Registra localmente un'azione a pagamento autorizzata/avviata."""
    name = (action_name or "").strip()
    record = {
        "date": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "action": name,
        "estimated_cost_usd": PAID_ACTIONS.get(name),
        "cost_unit": COST_UNITS.get(
            name, "USD" if PAID_ACTIONS.get(name) is not None else "unknown"
        ),
        "origin": origin,
    }
    try:
        os.makedirs(os.path.dirname(COSTS_LOG_PATH), exist_ok=True)
        with _LOG_LOCK:
            try:
                with open(COSTS_LOG_PATH, "r", encoding="utf-8") as file:
                    records = json.load(file)
                if not isinstance(records, list):
                    records = []
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                records = []
            records.append(record)
            tmp_path = COSTS_LOG_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as file:
                json.dump(records, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, COSTS_LOG_PATH)
    except Exception as exc:
        logger.error("Impossibile aggiornare il registro costi: %s", exc)
    return record


def get_total_spent() -> float:
    """Somma le stime in USD registrate (esclude i costi variabili non noti)."""
    try:
        with open(COSTS_LOG_PATH, "r", encoding="utf-8") as file:
            records = json.load(file)
        if not isinstance(records, list):
            return 0.0
        return round(sum(
            float(item.get("estimated_cost_usd") or 0.0)
            for item in records
            if isinstance(item, dict)
        ), 4)
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0.0


__all__ = [
    "PAID_ACTIONS", "FREE_ACTIONS", "check_cost", "estimated_cost",
    "is_paid_action", "record_paid_action", "get_total_spent",
]
