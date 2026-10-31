
import sys


def main() -> int:
    # I simboli di stato del report non sono rappresentabili da cp1252.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass

    import os, json, tempfile
    import policy

    # reindirizza l'audit su file temporaneo
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".log"); tmp.close()
    policy.AUDIT_PATH = tmp.name

    executed = {"flag": False}
    def act_ok(**k):
        executed["flag"] = True
        return "RISULTATO_OK " + json.dumps(k)
    def act_boom(**k):
        executed["flag"] = True
        raise RuntimeError("kaboom")

    def confirm_yes(n,a):    return "yes"
    def confirm_no(n,a):     return "no"
    def confirm_timeout(n,a):return "timeout"
    def confirm_forbidden(n,a):
        raise AssertionError("CONFIRM NON deve essere chiamata per azioni non-irreversibili!")

    results = []
    def check(desc, cond):
        results.append((desc, cond)); print(("PASS" if cond else "FAIL"), "-", desc)

    # 1) read-only -> esegue senza conferma
    executed["flag"]=False
    r = policy.guarded_call("stato_batteria", act_ok, {}, confirm=confirm_forbidden)
    check("1 read-only: parte senza conferma", executed["flag"] and r.startswith("RISULTATO_OK"))
    check("1b classe read-only", policy.classify("stato_batteria")=="read-only")

    # 2) reversibile -> esegue, nessuna conferma
    executed["flag"]=False
    r = policy.guarded_call("muta_volume", act_ok, {}, confirm=confirm_forbidden)
    check("2 reversibile: esegue senza conferma", executed["flag"] and r.startswith("RISULTATO_OK"))
    check("2b classe reversibile", policy.classify("muta_volume")=="reversibile")

    # 3) irreversibile -> CHIEDE conferma
    asked = {"n":0}
    def confirm_probe(n,a):
        asked["n"]+=1; return "yes"
    executed["flag"]=False
    r = policy.guarded_call("elimina_file", act_ok, {"percorso":"x.txt"}, confirm=confirm_probe)
    check("3 irreversibile: chiede conferma", asked["n"]==1)
    check("3b classe irreversibile", policy.classify("elimina_file")=="irreversibile")

    # 4) rifiuto -> NON esegue
    executed["flag"]=False
    r = policy.guarded_call("elimina_file", act_ok, {"percorso":"x"}, confirm=confirm_no)
    check("4 rifiuto: NON esegue", (not executed["flag"]) and "annullata" in r)

    # 5) timeout -> NON esegue
    executed["flag"]=False
    r = policy.guarded_call("spegni_pc", act_ok, {}, confirm=confirm_timeout)
    check("5 timeout: NON esegue", (not executed["flag"]) and "scaduta" in r)

    # 6) conferma -> esegue e logga
    executed["flag"]=False
    r = policy.guarded_call("svuota_cestino", act_ok, {}, confirm=confirm_yes)
    check("6 conferma: esegue", executed["flag"] and r.startswith("RISULTATO_OK"))

    # 7) errore tool -> logga errore, ritorna stringa sicura
    executed["flag"]=False
    r = policy.guarded_call("uccidi_processo", act_boom, {"pid":1}, confirm=confirm_yes)
    check("7 errore tool: catturato+loggato", r.startswith("❌ Errore durante uccidi_processo"))

    # bonus: sconosciuto -> irreversibile (fail-safe)
    check("bonus default sconosciuto=irreversibile", policy.classify("tool_mai_visto")=="irreversibile")

    # ── verifica AUDIT LOG ──
    lines = [json.loads(l) for l in open(tmp.name, encoding="utf-8") if l.strip()]
    outs = [(l["action"], l["outcome"], l.get("confirmed")) for l in lines]
    print("\n=== AUDIT LOG (", len(lines), "righe ) ===")
    for l in lines: print(" ", json.dumps({k:l[k] for k in ("action","class","outcome","confirmed","error")}, ensure_ascii=False))

    check("audit: refused presente",  any(o==("elimina_file","refused",False) for o in outs))
    check("audit: timeout presente",  any(o==("spegni_pc","timeout",False) for o in outs))
    check("audit: executed+confirmed presente", any(o==("svuota_cestino","executed",True) for o in outs))
    check("audit: error presente",    any(a=="uccidi_processo" and o=="error" for a,o,c in outs))
    check("audit: read-only executed confirmed=None", any(o==("stato_batteria","executed",None) for o in outs))

    os.unlink(tmp.name)
    ok = all(c for _,c in results)
    print("\n==== RISULTATO:", "TUTTI PASS ✅" if ok else "CI SONO FAIL ❌", f"({sum(c for _,c in results)}/{len(results)}) ====")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
