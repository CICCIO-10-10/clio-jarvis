"""
CLIO — model_router.py
Router deterministico dei profili modello (Fase 2 del Piano Unico).

Scopo
-----
Rendere i modelli *intercambiabili*: chi chiama un modello (jarvis_brain,
clio_core, ...) non nomina più provider né modelli. Chiede solo:

    profile_name = route_task("chat")      # -> "FAST"
    profile      = get_profile(profile_name)
    chain        = get_provider_chain(profile_name)   # es. ["ollama","gemini","groq"]

Il router:
1. Legge i profili da config.MODEL_PROFILES (fallback a DEFAULT_PROFILES).
2. Mappa in modo DETERMINISTICO task_type -> profilo (nessun LLM nel router).
3. Restituisce la catena di provider: primario del profilo + fallback esistente
   Ollama -> Gemini -> Groq (rispettando il gate globale config.USE_LOCAL_LLM).

Note di design (dal Piano Unico):
- "Nessun router LLM prima del router deterministico": qui non c'è IA, solo mappe.
- "Non passare al CODER richieste ordinarie": il default è GENERAL, e CODER
  scatta solo su task_type/chiave espliciti.
- La scelta del modello NON è una decisione di sicurezza: policy.py e il control
  plane restano l'unico punto che autorizza le azioni.
"""

import logging

logger = logging.getLogger("ModelRouter")

# ── Fallback se config.py non definisce ancora MODEL_PROFILES ──────────────
DEFAULT_PROFILES = {
    "FAST": {
        "provider": "groq",
        "model": "qwen/qwen3.8-27b",
        "fallback": ["gemini"],
        "temperature": 0.7,
        "max_tokens": 400,
        "description": "Chat veloce (Groq) per saluti, domande semplici e routing.",
    },
    "GENERAL": {
        "provider": "groq",
        "model": "qwen/qwen3.8-27b",
        "fallback": ["gemini"],
        "temperature": 0.7,
        "max_tokens": 700,
        "description": "Chat/task normali su Groq (veloce); fallback Gemini.",
    },
    "CODER": {
        "provider": "ollama",
        "model": "qwen2.5-coder:3b",
        "fallback": [],
        "temperature": 0.2,
        "max_tokens": 4096,
        "description": "Coding locale con Ollama; nessun fallback cloud.",
    },
    "VISION": {
        "provider": "gemini",
        "model": "models/gemini-3.8-flash",
        "fallback": [],
        "temperature": 0.4,
        "max_tokens": 1024,
        "description": "Analisi di immagini e screenshot dello schermo.",
    },
}

DEFAULT_PROFILE = "GENERAL"
DEFAULT_TASK_TYPE = "general"

# Fallback globale (l'ordine storico di Clio). Il router mette il provider
# primario del profilo in testa e usa il resto come catena di riserva.
FALLBACK_PROVIDER_CHAIN = ("ollama", "gemini", "groq")

# ── Mappa deterministica task_type -> profilo ──────────────────────────────
# Chiavi normalizzate (minuscolo, spazi/trattini -> underscore).
TASK_ROUTING = {
    # FAST — chat, routing, tool read-only semplici
    "fast": "FAST",              # nome esplicito del profilo (llama3.1:8b, Ollama)
    "chat": "FAST",
    "conversation": "FAST",
    "conversazione": "FAST",
    "routing": "FAST",
    "routing_model": "FAST",
    "classify": "FAST",
    "classificazione": "FAST",
    "readonly": "FAST",
    "read_only": "FAST",
    "tool_readonly": "FAST",
    "quick": "FAST",
    # GENERAL — task normali
    "general": "GENERAL",
    "default": "GENERAL",
    "planning": "GENERAL",
    "plan": "GENERAL",
    "pianificazione": "GENERAL",
    "research": "GENERAL",
    "ricerca": "GENERAL",
    "web": "GENERAL",
    "summary": "GENERAL",
    "riassunto": "GENERAL",
    # CODER — coding e debugging
    "coder": "CODER",            # nome esplicito del profilo
    "code": "CODER",
    "coding": "CODER",
    "debug": "CODER",
    "debugging": "CODER",
    "refactor": "CODER",
    "code_review": "CODER",
    "coding_task": "CODER",
    "tool_write": "CODER",
    # VISION — immagini e schermo
    "vision": "VISION",
    "visione": "VISION",
    "screenshot": "VISION",
    "image": "VISION",
    "immagine": "VISION",
    "screen": "VISION",
    "schermo": "VISION",
    "ocr": "VISION",
}

# ── Classificazione euristica DETERMINISTICA di una richiesta testuale ─────
# Conservativa: scatta su segnali forti, altrimenti resta GENERAL/FAST.
_CODER_KEYWORDS = (
    "codice", "code", "script", "python", "javascript", "typescript",
    "debug", "debugging", "refactor", "traceback", "stack trace",
    "unit test", "test unitario", "compila", "compilazione", "bug nel",
    "funzione python", "classe python",
)
_VISION_KEYWORDS = (
    "screenshot", "schermo", "immagine", "visione", "monitor",
    "cosa vedi", "analizza lo schermo", "foto dello schermo",
)
_FAST_KEYWORDS = (
    "ciao", "ehi", "grazie", "come stai", "buongiorno", "buonasera",
    "buonanotte", "a dopo", "a domani", "ok", "va bene", "perfetto",
)


# ─────────────────────────────────────────────────────────────────────────
#  Profili
# ─────────────────────────────────────────────────────────────────────────
def _load_profiles():
    """Legge i profili da config (preferito) con fallback ai default interni."""
    try:
        import config
        profili = getattr(config, "MODEL_PROFILES", None)
        if isinstance(profili, dict) and profili:
            return profili
    except Exception as e:  # pragma: no cover - dipende dall'ambiente
        logger.warning(f"Impossibile leggere MODEL_PROFILES da config: {e}")
    return DEFAULT_PROFILES


def get_profiles():
    """Restituisce una copia dei profili disponibili."""
    return {k: dict(v) for k, v in _load_profiles().items()}


def get_profile(profile_name):
    """Restituisce il dict del profilo (provider, model, fallback, ...).

    Se il nome non esiste restituisce il profilo di default (GENERAL),
    così un errore di routing non blocca mai la risposta.
    """
    profili = _load_profiles()
    nome = (profile_name or DEFAULT_PROFILE).upper()
    if nome not in profili:
        logger.warning(f"Profilo '{profile_name}' sconosciuto, uso {DEFAULT_PROFILE}.")
        return dict(profili.get(DEFAULT_PROFILE, DEFAULT_PROFILES[DEFAULT_PROFILE]))
    return dict(profili[nome])


# ─────────────────────────────────────────────────────────────────────────
#  Routing
# ─────────────────────────────────────────────────────────────────────────
def _normalizza(task_type):
    return str(task_type or "").strip().lower().replace(" ", "_").replace("-", "_")


def route_task(task_type):
    """Mappa un task_type al nome del profilo. Funzione deterministica.

    Esempi:
        route_task("chat")      -> "FAST"
        route_task("planning")  -> "GENERAL"
        route_task("debug")     -> "CODER"
        route_task("vision")    -> "VISION"

    Un task_type sconosciuto o vuoto produce GENERAL (default sicuro).
    """
    chiave = _normalizza(task_type)
    if not chiave:
        return DEFAULT_PROFILE
    profilo = TASK_ROUTING.get(chiave)
    if profilo is None:
        # I 4 profili (fast/general/coder/vision) possono arrivare anche col
        # loro nome esplicito: in tal caso mappali direttamente senza passare
        # per la tabella, così non cadono mai su GENERAL per errore.
        candidato = chiave.upper()
        if candidato in _load_profiles():
            return candidato
        logger.info(f"Task type '{task_type}' non mappato, uso {DEFAULT_PROFILE}.")
        return DEFAULT_PROFILE
    return profilo


def _ollama_enabled():
    """Rispetta il gate globale config.USE_LOCAL_LLM per Ollama."""
    try:
        import config
        return bool(getattr(config, "USE_LOCAL_LLM", False))
    except Exception:
        return False


def get_provider_chain(profile_name):
    """Catena di provider da tentare per un profilo, primario in testa.

    Il primario è quello dichiarato dal profilo; il resto segue il fallback
    storico Ollama -> Gemini -> Groq (o il campo 'fallback' del profilo).

    Se config.USE_LOCAL_LLM è False, Ollama viene escluso dalla catena:
    Clio resta sul percorso cloud stabile finché i modelli locali non sono
    verificati, senza cambiare il resto dell'architettura.
    """
    profile = get_profile(profile_name)
    primary = profile.get("provider", "gemini")

    explicit = profile.get("fallback")
    if explicit is None:
        explicit = [p for p in FALLBACK_PROVIDER_CHAIN if p != primary]

    chain = [primary] + list(explicit)

    # Dedup preservando l'ordine
    visti, ordinati = set(), []
    for p in chain:
        if p and p not in visti:
            visti.add(p)
            ordinati.append(p)

    # CODER è locale per scelta esplicita: non dipende dal gate generale,
    # che continua a controllare Ollama per gli altri profili.
    if not _ollama_enabled() and primary != "llamacpp" and str(profile_name).upper() != "CODER":
        ordinati = [p for p in ordinati if p != "ollama"]
        # Guardia: se il gate svuota la catena, mantieni i cloud disponibili
        if not ordinati:
            ordinati = [p for p in FALLBACK_PROVIDER_CHAIN if p != "ollama"]

    return ordinati


def describe_route(task_type):
    """Spiega la decisione del router (per log e /api/status)."""
    profile_name = route_task(task_type)
    profile = get_profile(profile_name)
    chain = get_provider_chain(profile_name)
    return {
        "task_type": _normalizza(task_type) or DEFAULT_TASK_TYPE,
        "profile": profile_name,
        "primary_provider": profile.get("provider"),
        "model": profile.get("model"),
        "provider_chain": chain,
        "description": profile.get("description", ""),
        "ollama_enabled": _ollama_enabled(),
    }


def classify_task(testo):
    """Classifica (in modo deterministico) una richiesta testuale in un task_type.

    Euristica, non un modello: guarda parole chiave forti. In dubbio -> GENERAL.
    Pensata per chi vuole far decidere il profilo a partire dal testo utente,
    senza introdurre un router LLM.
    """
    t = str(testo or "").lower()
    if not t.strip():
        return DEFAULT_TASK_TYPE
    if any(k in t for k in _VISION_KEYWORDS):
        return "vision"
    if any(k in t for k in _CODER_KEYWORDS):
        return "code"
    if len(t) <= 40 and any(k in t for k in _FAST_KEYWORDS):
        return "chat"
    return DEFAULT_TASK_TYPE


__all__ = [
    "route_task",
    "get_profile",
    "get_profiles",
    "get_provider_chain",
    "describe_route",
    "classify_task",
    "DEFAULT_PROFILE",
    "DEFAULT_TASK_TYPE",
    "TASK_ROUTING",
    "FALLBACK_PROVIDER_CHAIN",
]
