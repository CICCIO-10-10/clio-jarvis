
# -*- coding: utf-8 -*-
"""
test_e2e_control_plane.py — Test End-to-End MINIMO del control-plane di Clio.

Verifica i 3 scenari richiesti, SENZA avviare Telegram e SENZA eseguire azioni
distruttive o chiamate di rete:

  1) Tool read-only/reversibile eseguito VIA LLM (loop di bot.chiedi_a_clio)
     -> produce una riga di audit in logs/security.log con origin=telegram.
  2) Tool IRREVERSIBILE (esegui_comando_shell) -> richiede conferma:
     senza "yes" NON viene eseguito; con "yes" viene eseguito.
  3) Slash command Telegram (/meteo) -> passa da policy e produce audit
     (origin=telegram) rispondendo all'utente.

Sicurezza dell'ambiente di test:
  - clio_core (thread, screenshot, Gemini Vision) e memory_manager (ChromaDB)
    sono STUBBATI prima di importare bot, cosi' non parte nessun loop reale.
  - I tool con side-effect (meteo -> rete) sono sostituiti con mock innocui.
  - L'unico tool reale eseguito e' ottieni_info_sistema (psutil, read-only).

Esegui con:  python test_e2e_control_plane.py
"""


def main() -> int:
    import sys, os, json, types, asyncio

    BASE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, BASE)

    # ── STUB dei moduli con effetti collaterali pesanti (prima di importare bot) ──
    cc = types.ModuleType("clio_core")
    cc.SYSTEM_STATE = {"telegram": "offline"}
    cc.carica_biografia = lambda: {"user_profile": {"name": "Test", "preferences": {}}}
    cc.salva_biografia = lambda bio: None
    sys.modules["clio_core"] = cc

    mm = types.ModuleType("memory_manager")
    class _MM:
        def query_relevant_context(self, *a, **k): return ""
        def store_interaction(self, *a, **k): return None
    mm.memory_manager = _MM()
    sys.modules["memory_manager"] = mm

    import policy            # noqa: E402
    import bot               # noqa: E402

    # Nessun canale di conferma: le irreversibili devono fallire in modo fail-safe.
    policy._BOT = None
    policy._LOOP = None

    RESULTS = []
    def check(name, cond):
        RESULTS.append((name, bool(cond)))
        print(("  PASS " if cond else "  FAIL ") + name)

    def audit_lines():
        if not os.path.exists(policy.AUDIT_PATH):
            return []
        with open(policy.AUDIT_PATH, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l.strip()]

    def new_audit_since(n):
        return [json.loads(l) for l in audit_lines()[n:]]


    # ============================================================================
    # 1) TOOL READ-ONLY VIA LLM  (loop reale di bot.chiedi_a_clio)
    # ============================================================================
    print("[1] Tool read-only eseguito via LLM -> audit")

    class _Fn:
        def __init__(self, name, args): self.name = name; self.arguments = json.dumps(args)
    class _ToolCall:
        def __init__(self, name, args, tid="call_1"): self.id = tid; self.function = _Fn(name, args)
    class _Msg:
        def __init__(self, content="", tool_calls=None, role="assistant"):
            self.content = content; self.tool_calls = tool_calls; self.role = role
    class _Choice:
        def __init__(self, m): self.message = m
    class _Resp:
        def __init__(self, m): self.choices = [_Choice(m)]

    # Fake client: 1a chiamata -> tool_call ottieni_info_sistema; 2a -> risposta finale
    _calls = {"n": 0}
    class _Completions:
        def create(self, **kw):
            _calls["n"] += 1
            if _calls["n"] == 1:
                return _Resp(_Msg(tool_calls=[_ToolCall("ottieni_info_sistema", {})]))
            return _Resp(_Msg(content="Ecco lo stato del sistema, Capo."))
    class _Chat:
        def __init__(self): self.completions = _Completions()
    class _Client:
        def __init__(self): self.chat = _Chat()

    bot.client = _Client()
    bot.get_or_create_chat = lambda uid: [{"role": "system", "content": "test"}]
    bot.salva_chat = lambda uid: None

    before = len(audit_lines())
    out = bot.chiedi_a_clio(999999, "dammi info sistema")
    recs = new_audit_since(before)
    sysrec = [r for r in recs if r["action"] == "ottieni_info_sistema"]
    check("LLM->tool read-only: eseguito", any(r["outcome"] == "executed" for r in sysrec))
    check("LLM->tool read-only: class=read-only", any(r.get("class") == "read-only" for r in sysrec))
    check("LLM->tool read-only: origin=telegram", any(r.get("origin") == "telegram" for r in sysrec))
    check("LLM->tool read-only: risposta finale generata", bool(out) and _calls["n"] >= 2)


    # ============================================================================
    # 2) TOOL IRREVERSIBILE -> RICHIEDE CONFERMA
    # ============================================================================
    print("[2] Tool irreversibile (esegui_comando_shell) -> richiede conferma")

    _eseguiti = []
    def fake_shell(**k): _eseguiti.append(k); return "MOCK eseguito"

    assert policy.classify("esegui_comando_shell") == "irreversibile"

    before = len(audit_lines())

    # 2a) nessun canale -> timeout -> NON esegue (fail-safe)
    r = policy.guarded_call("esegui_comando_shell", fake_shell,
                            {"comando": "echo ciao"}, origin="telegram")
    check("irrev senza canale: NON eseguito", _eseguiti == [] and "scaduta" in r.lower())

    # 2b) utente annulla -> NON esegue
    r = policy.guarded_call("esegui_comando_shell", fake_shell,
                            {"comando": "echo ciao"}, confirm=lambda *a: "no", origin="telegram")
    check("irrev + NO: annullato e NON eseguito", _eseguiti == [] and "annullata" in r.lower())

    # 2c) utente conferma -> esegue
    r = policy.guarded_call("esegui_comando_shell", fake_shell,
                            {"comando": "echo ciao"}, confirm=lambda *a: "yes", origin="telegram")
    check("irrev + YES: eseguito", _eseguiti == [{"comando": "echo ciao"}])

    recs = new_audit_since(before)
    outcomes = {r["outcome"] for r in recs if r["action"] == "esegui_comando_shell"}
    check("audit irreversibile: confirm_requested presente", "confirm_requested" in outcomes)
    check("audit irreversibile: refused presente", "refused" in outcomes)
    check("audit irreversibile: timeout presente", "timeout" in outcomes)
    check("audit irreversibile: executed presente", "executed" in outcomes)


    # ============================================================================
    # 3) SLASH COMMAND TELEGRAM /meteo -> policy + audit (origin=telegram)
    # ============================================================================
    print("[3] Slash command /meteo -> passa da policy + audit")

    # mock innocuo al posto della chiamata di rete al servizio meteo
    bot.AZIONI_MAP["ottieni_meteo"] = lambda citta="Roma": f"MOCK meteo per {citta}"

    class _MsgOut:
        def __init__(self): self.texts = []
        async def reply_text(self, text, **kw): self.texts.append(text)
    class _User:
        def __init__(self, uid): self.id = uid
    class _Update:
        def __init__(self, uid): self.effective_user = _User(uid); self.message = _MsgOut()
    class _Ctx:
        def __init__(self, args): self.args = args

    uid = bot.MY_TELEGRAM_USER_ID  # autorizzato (is_autorizzato True se 0)
    upd = _Update(uid)
    ctx = _Ctx(["Roma"])

    before = len(audit_lines())
    asyncio.run(bot.cmd_meteo(upd, ctx))
    recs = new_audit_since(before)
    meteo = [r for r in recs if r["action"] == "ottieni_meteo"]
    check("/meteo: risponde all'utente", any("MOCK meteo per Roma" in t for t in upd.message.texts))
    check("/meteo: audit executed", any(r["outcome"] == "executed" for r in meteo))
    check("/meteo: origin=telegram", any(r.get("origin") == "telegram" for r in meteo))
    check("/meteo: class=read-only", any(r.get("class") == "read-only" for r in meteo))


    # ============================================================================
    # RIEPILOGO
    # ============================================================================
    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== RISULTATO: {passed}/{total} PASS ===")
    print("Audit file:", policy.AUDIT_PATH)
    print("Ultimo record audit:")
    print("  " + audit_lines()[-1])
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
