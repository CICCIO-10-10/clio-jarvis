"""
CLIO — test_model_router.py
Test unitario del router deterministico (Fase 2).

Verifica:
1. route_task() per i 4 profili (FAST, GENERAL, CODER, VISION).
2. Normalizzazione input e default sicuro per task sconosciuti.
3. Determinismo (stessa richiesta -> stesso profilo).
4. Lettura dei profili da config (provider + modello presenti e sostituibili).
5. Catena provider: fallback cloud assente per CODER; gate USE_LOCAL_LLM sugli altri profili.
6. classify_task(): i segnali forti mappano al profilo giusto.

Eseguibile con:  python test_model_router.py
Oppure con:      pytest test_model_router.py
"""

import model_router


# ─────────────────────────────────────────────────────────────────────────
#  1) Routing dei 4 profili
# ─────────────────────────────────────────────────────────────────────────
def test_route_fast():
    for t in ("chat", "routing", "read_only", "readonly"):
        assert model_router.route_task(t) == "FAST", t


def test_route_general():
    for t in ("general", "planning", "research", "summary"):
        assert model_router.route_task(t) == "GENERAL", t


def test_route_coder():
    for t in ("code", "coding", "debug", "refactor"):
        assert model_router.route_task(t) == "CODER", t


def test_route_vision():
    for t in ("vision", "screenshot", "image", "screen"):
        assert model_router.route_task(t) == "VISION", t


# ─────────────────────────────────────────────────────────────────────────
#  2) Default sicuro e normalizzazione
# ─────────────────────────────────────────────────────────────────────────
def test_unknown_task_defaults_to_general():
    assert model_router.route_task("qualcosa_di_nuovo") == "GENERAL"
    assert model_router.route_task("") == "GENERAL"
    assert model_router.route_task(None) == "GENERAL"


def test_input_is_normalized():
    assert model_router.route_task("  CHAT  ") == "FAST"
    assert model_router.route_task("Read-Only") == "FAST"
    assert model_router.route_task("Code Review") == "CODER"


# ─────────────────────────────────────────────────────────────────────────
#  3) Determinismo
# ─────────────────────────────────────────────────────────────────────────
def test_routing_is_deterministic():
    for t in ("chat", "planning", "debug", "vision", "sconosciuto"):
        assert model_router.route_task(t) == model_router.route_task(t)


# ─────────────────────────────────────────────────────────────────────────
#  4) Profili letti da config (modelli sostituibili)
# ─────────────────────────────────────────────────────────────────────────
def test_profiles_from_config():
    profili = model_router.get_profiles()
    for nome in ("FAST", "GENERAL", "CODER", "VISION"):
        assert nome in profili, nome
        p = profili[nome]
        assert p.get("provider"), f"{nome} senza provider"
        assert p.get("model"), f"{nome} senza model"


def test_get_profile_fields():
    p = model_router.get_profile("CODER")
    # CODER è locale e non usa fallback cloud.
    assert p["provider"] == "ollama"
    assert "model" in p and p["model"]
    # get_profile su nome ignoto non solleva e torna il default
    assert model_router.get_profile("NON_ESISTE")["provider"]


def test_vision_profile_is_gemini():
    p = model_router.get_profile("VISION")
    assert p["provider"] == "gemini"
    assert p["model"].startswith("models/")


# ─────────────────────────────────────────────────────────────────────────
#  5) Catena provider e gate USE_LOCAL_LLM
# ─────────────────────────────────────────────────────────────────────────
def test_chain_primary_first_with_local_enabled():
    import config
    orig = config.USE_LOCAL_LLM
    try:
        config.USE_LOCAL_LLM = True
        chain = model_router.get_provider_chain("GENERAL")
        # Fase 6: chat veloci su Groq (primario), Gemini come fallback.
        assert chain[0] == "groq"
        assert chain == ["groq", "gemini"]
        assert model_router.get_provider_chain("VISION") == ["gemini"]
    finally:
        config.USE_LOCAL_LLM = orig


def test_chain_skips_ollama_when_local_disabled():
    import config
    orig = config.USE_LOCAL_LLM
    try:
        config.USE_LOCAL_LLM = False
        chain = model_router.get_provider_chain("GENERAL")
        assert "ollama" not in chain
        assert chain == ["groq", "gemini"]
    finally:
        config.USE_LOCAL_LLM = orig


def test_chain_has_no_duplicates():
    import config
    orig = config.USE_LOCAL_LLM
    try:
        config.USE_LOCAL_LLM = True
        chain = model_router.get_provider_chain("CODER")
        assert len(chain) == len(set(chain))
        assert chain == ["ollama"]
    finally:
        config.USE_LOCAL_LLM = orig


def test_coder_ignora_il_gate_generale_e_non_ha_fallback_cloud():
    import config
    orig = config.USE_LOCAL_LLM
    try:
        for enabled in (False, True):
            config.USE_LOCAL_LLM = enabled
            assert model_router.get_provider_chain("CODER") == ["ollama"]
    finally:
        config.USE_LOCAL_LLM = orig


# ─────────────────────────────────────────────────────────────────────────
#  6) classify_task() deterministico
# ─────────────────────────────────────────────────────────────────────────
def test_classify_task():
    assert model_router.classify_task("ciao Clio") == "chat"
    assert model_router.classify_task("debugga questo traceback python") == "code"
    assert model_router.classify_task("analizza lo schermo") == "vision"
    assert model_router.classify_task("metti la sveglia alle 7") == "general"
    assert model_router.classify_task("") == "general"


def test_describe_route():
    d = model_router.describe_route("chat")
    assert d["profile"] == "FAST"
    assert d["model"]
    assert isinstance(d["provider_chain"], list) and d["provider_chain"]


# ─────────────────────────────────────────────────────────────────────────
#  Runner autonomo (oltre a pytest)
# ─────────────────────────────────────────────────────────────────────────
def _run_all():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    ok = 0
    for t in tests:
        try:
            t()
            print("PASS -", t.__name__)
            ok += 1
        except AssertionError as e:
            print("FAIL -", t.__name__, "->", e)
        except Exception as e:  # pragma: no cover
            print("ERROR -", t.__name__, "->", type(e).__name__, e)
    print(f"\n==== RISULTATO: {'TUTTI PASS' if ok == len(tests) else 'CI SONO FAIL'} ({ok}/{len(tests)}) ====")
    return ok == len(tests)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _run_all() else 1)
