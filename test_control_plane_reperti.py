
# -*- coding: utf-8 -*-
"""
test_control_plane_reperti.py — Test dei reperti #2 e #3 del control-plane.

Reperto #2 — ottieni_telemetria_hardware:
  - e' nel registro AZIONI_MAP ed e' classificata read-only;
  - via guarded_call ritorna un dict e produce una riga di audit;
  - i tool esistenti continuano a funzionare.

Reperto #3 — tool_factory (creazione di nuovo codice in actions.py):
  - "crea_tool" e' classificata IRREVERSIBILE;
  - SENZA conferma la creazione NON avviene (nessun codice scritto in actions.py);
  - con conferma "no" -> NON avviene;
  - con conferma "yes" -> avviene (su file temporanei, il vero actions.py non e' toccato).

NB: la scrittura viene dirottata su file temporanei; il vero actions.py e' protetto
da un hash prima/dopo. Nessuna azione distruttiva, nessuna rete.
"""


def main() -> int:
    import sys, os, json, tempfile, hashlib

    BASE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, BASE)

    import policy                      # noqa: E402
    import registry                    # noqa: E402
    import tool_factory                # noqa: E402

    AZIONI_MAP = registry.AZIONI_MAP

    RESULTS = []
    def check(name, cond):
        RESULTS.append((name, bool(cond)))
        print(("  PASS " if cond else "  FAIL ") + name)

    def audit_lines():
        if not os.path.exists(policy.AUDIT_PATH): return []
        with open(policy.AUDIT_PATH, encoding="utf-8") as f:
            return [l for l in f.read().splitlines() if l.strip()]

    def new_audit_since(n):
        return [json.loads(l) for l in audit_lines()[n:]]

    def sha(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()


    # ============================================================================
    # REPERTO #2 — ottieni_telemetria_hardware nel registro + read-only + audit
    # ============================================================================
    print("[2] ottieni_telemetria_hardware -> registro + policy + audit")

    check("presente in AZIONI_MAP", "ottieni_telemetria_hardware" in AZIONI_MAP)
    check("classify = read-only", policy.classify("ottieni_telemetria_hardware") == "read-only")

    before = len(audit_lines())
    tel = policy.guarded_call("ottieni_telemetria_hardware", AZIONI_MAP["ottieni_telemetria_hardware"], {}, origin="test2")
    recs = new_audit_since(before)
    check("guarded_call ritorna un dict", isinstance(tel, dict))
    check("audit: executed", any(r["action"] == "ottieni_telemetria_hardware" and r["outcome"] == "executed" for r in recs))
    check("audit: class=read-only + origin", any(r.get("class") == "read-only" and r.get("origin") == "test2" for r in recs))

    # i tool esistenti continuano a funzionare
    info = policy.guarded_call("ottieni_info_sistema", AZIONI_MAP["ottieni_info_sistema"], {}, origin="test2")
    check("tool esistente ottieni_info_sistema OK", isinstance(info, str) and len(info) > 0)
    check("tool esistente chi_sei OK", isinstance(policy.guarded_call("chi_sei", AZIONI_MAP["chi_sei"], {}, origin="test2"), str))


    # ============================================================================
    # REPERTO #3 — creazione tool sotto control-plane
    # ============================================================================
    print("[3] tool_factory -> creazione tool gated da policy")

    check("classify crea_tool = irreversibile", policy.classify("crea_tool") == "irreversibile")

    REAL_ACTIONS = os.path.join(BASE, "actions.py")
    real_hash_before = sha(REAL_ACTIONS)

    # dirotta la scrittura su file temporanei
    TMP = tempfile.mkdtemp(prefix="clio_tf_")
    tmp_actions = os.path.join(TMP, "actions_test.py")
    with open(tmp_actions, "w", encoding="utf-8") as f:
        f.write("# temp actions for test\n")
    tool_factory.ACTIONS_FILE = tmp_actions
    tool_factory.CUSTOM_TOOLS_FILE = os.path.join(TMP, "custom_tools.json")
    tool_factory.carica_tool_in_runtime = lambda nome, amap: (amap.__setitem__(nome, (lambda *a, **k: "MOCK")), True)[1]

    RICH = (
        "Ok, creo il tool.\n"
        "[NUOVO_TOOL_RICHIESTO]\n"
        "nome: saluta_test\n"
        "descrizione: saluta l'utente di test\n"
        "parametri: []\n"
        "codice_python: |\n"
        "    def saluta_test():\n"
        "        return 'ciao'\n"
        "[/NUOVO_TOOL_RICHIESTO]\n"
    )

    def temp_actions_contains(token):
        return token in open(tmp_actions, encoding="utf-8").read()

    # 3a) nessun canale di conferma -> NON crea (fail-safe)
    policy._BOT = None
    policy._LOOP = None
    before = len(audit_lines())
    risp, creato = tool_factory.processa_risposta_clio(RICH, AZIONI_MAP, origin="telegram")
    recs = new_audit_since(before)
    check("senza conferma: NON creato", creato is False and not temp_actions_contains("def saluta_test("))
    check("senza conferma: audit confirm_requested+timeout",
          any(r["action"] == "crea_tool" and r["outcome"] == "confirm_requested" for r in recs) and
          any(r["action"] == "crea_tool" and r["outcome"] == "timeout" for r in recs))

    # 3b) conferma "no" -> NON crea
    policy._telegram_confirm = lambda name, args: "no"
    risp, creato = tool_factory.processa_risposta_clio(RICH, AZIONI_MAP, origin="telegram")
    check("conferma NO: NON creato", creato is False and not temp_actions_contains("def saluta_test("))

    # 3c) conferma "yes" -> crea (su file temporaneo)
    policy._telegram_confirm = lambda name, args: "yes"
    before = len(audit_lines())
    risp, creato = tool_factory.processa_risposta_clio(RICH, AZIONI_MAP, origin="telegram")
    recs = new_audit_since(before)
    check("conferma YES: creato", creato is True and temp_actions_contains("def saluta_test("))
    check("conferma YES: audit executed", any(r["action"] == "crea_tool" and r["outcome"] == "executed" for r in recs))
    check("conferma YES: tool registrato in AZIONI_MAP", "saluta_test" in AZIONI_MAP)

    # il vero file di produzione non e' mai stato toccato
    check("actions.py reale INTATTO (hash invariato)", sha(REAL_ACTIONS) == real_hash_before)


    # ============================================================================
    # RIEPILOGO
    # ============================================================================
    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== RISULTATO: {passed}/{total} PASS ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
