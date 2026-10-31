# -*- coding: utf-8 -*-
"""
test_control_plane.py — Regression suite del control-plane (STEP 5).
Testa policy/classify/confirm/audit/origin con MOCK, senza azioni distruttive reali.
NON importa i moduli Windows (actions/clio_core/bot) e NON avvia Clio.

Esecuzione come script:  python test_control_plane.py
Sotto pytest il corpo non viene eseguito all'import (niente sys.exit in fase di
raccolta): la suite manuale resta invariata.
"""
import sys, os, json, types, time, threading, asyncio, tempfile

import policy

RESULTS = []


def check(name, cond):
    RESULTS.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name)


def main() -> int:
    # I report includono simboli Unicode non supportati dai terminali cp1252.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass

    # --- mock tool innocui (nessuna azione reale) ---
    # In questo ambiente il temp di sistema è isolato e non scrivibile dal test.
    # Usa una directory temporanea accanto al test, poi la rimuove al termine.
    TMP = tempfile.mkdtemp(prefix=".clio_test_", dir=os.path.dirname(os.path.abspath(__file__)))

    def mock_readonly(**k):
        return "MOCK sysinfo"

    def mock_reversibile(**k):
        return "MOCK app aperta"

    def mock_irreversibile(**k):
        # invece di eliminare: scrive un file temporaneo innocuo
        with open(os.path.join(TMP, "eseguito.txt"), "w") as f:
            f.write("mock")
        return "MOCK eliminazione simulata (file temp)"

    # ============ 1) CLASSIFICAZIONE ============
    print("[1] classify()")
    check("read-only  -> ottieni_info_sistema", policy.classify("ottieni_info_sistema") == "read-only")
    check("reversibile-> apri_applicazione", policy.classify("apri_applicazione") == "reversibile")
    check("irrev      -> elimina_file", policy.classify("elimina_file") == "irreversibile")
    check("ignoto     -> irreversibile (fail-safe)", policy.classify("azione_ignota_xyz") == "irreversibile")

    # conta righe audit prima
    def audit_lines():
        if not os.path.exists(policy.AUDIT_PATH):
            return []
        return [l for l in open(policy.AUDIT_PATH, encoding="utf-8").read().splitlines() if l.strip()]

    before = len(audit_lines())

    # ============ 2) BATTERIA 3 CLASSI da Telegram e Flask ============
    for origin in ("telegram", "flask"):
        print(f"[2] batteria classi — origin={origin}")
        r = policy.guarded_call("ottieni_info_sistema", mock_readonly, {}, origin=origin)
        check(f"[{origin}] read-only eseguito", r == "MOCK sysinfo")
        r = policy.guarded_call("apri_applicazione", mock_reversibile, {"nome_app": "chrome"}, origin=origin)
        check(f"[{origin}] reversibile eseguito", r == "MOCK app aperta")
        # irreversibile con conferma iniettata
        # run_python_code era nella vecchia whitelist UI nonostante sia
        # irreversibile: il caso Flask verifica che l'origine non bypassi il YES.
        azione_pericolosa = "run_python_code" if origin == "flask" else "elimina_file"
        conferme_richieste = []

        def conferma_si(*_args):
            conferme_richieste.append(True)
            return "yes"

        r = policy.guarded_call(azione_pericolosa, mock_irreversibile, {"percorso": "/tmp/x"},
                                confirm=conferma_si, origin=origin)
        check(f"[{origin}] irrev + YES -> eseguito con conferma esplicita",
              "MOCK eliminazione" in r and len(conferme_richieste) == 1)
        r = policy.guarded_call(azione_pericolosa, mock_irreversibile, {"percorso": "/tmp/x"},
                                confirm=lambda *a: "no", origin=origin)
        check(f"[{origin}] irrev + NO -> annullato", "annullata" in r.lower())
        r = policy.guarded_call(azione_pericolosa, mock_irreversibile, {"percorso": "/tmp/x"},
                                confirm=lambda *a: "timeout", origin=origin)
        check(f"[{origin}] irrev + TIMEOUT -> non eseguito", "scaduta" in r.lower())

    # ============ 3) MECCANISMO REALE DI CONFERMA (il fix cross-process) ============
    print("[3] meccanismo reale _telegram_confirm (bind + loop condiviso)")
    # stub del pacchetto 'telegram' (non installato nella VM)
    tg = types.ModuleType("telegram")

    class _KB:
        def __init__(self, *a, **k):
            pass

    tg.InlineKeyboardButton = _KB
    tg.InlineKeyboardMarkup = _KB
    sys.modules["telegram"] = tg

    class FakeBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.sent.append(text)
            return True

    # 3a) SENZA bind (riproduce il vecchio processo clio_core: nessun canale) -> timeout
    policy._BOT = None
    policy._LOOP = None
    r = policy.guarded_call("elimina_file", mock_irreversibile, {}, origin="flask")
    check("UNBOUND (vecchio bug cross-process) -> non eseguito", "scaduta" in r.lower())

    # 3b) CON bind (processo unico) -> conferma arriva ed esegue
    # L'event loop Proactor di Windows crea un socketpair durante l'avvio; in
    # ambienti isolati quel bind/accept può bloccarsi. Le coroutine qui sono
    # intenzionalmente sincrone (FakeBot e Q non sospendono), quindi un runner
    # immediato conserva il test del callback senza dipendere da socket reali.
    class _ImmediateFuture:
        def __init__(self, result):
            self._result = result

        def result(self, timeout=None):
            return self._result

    def run_coroutine_immediately(coro, _loop):
        try:
            coro.send(None)
        except StopIteration as done:
            return _ImmediateFuture(done.value)
        coro.close()
        raise RuntimeError("La coroutine di test ha sospeso l'esecuzione")

    loop = object()
    run_coroutine_threadsafe_originale = asyncio.run_coroutine_threadsafe
    asyncio.run_coroutine_threadsafe = run_coroutine_immediately
    fb = FakeBot()
    policy.bind(fb, loop)
    policy.CHAT_ID = 12345
    confirm_timeout_originale = policy.CONFIRM_TIMEOUT
    policy.CONFIRM_TIMEOUT = 3  # bounded: a regression can never strand the worker for 60s

    res = {}

    def worker():
        res["r"] = policy.guarded_call("elimina_file", mock_irreversibile, {"percorso": "/tmp/y"}, origin="telegram")

    wt = threading.Thread(target=worker)
    wt.start()

    # attende che compaia il token pending, poi simula il click "Conferma"
    token = None
    for _ in range(50):
        if policy._pending:
            token = list(policy._pending.keys())[0]
            break
        time.sleep(0.1)
    check("BOUND: messaggio conferma inviato al bot", len(fb.sent) >= 1)

    class Q:
        def __init__(self, data):
            self.data = data

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, *a, **k):
            pass

    class U:
        def __init__(self, data):
            self.callback_query = Q(data)

    if token:
        fut = asyncio.run_coroutine_threadsafe(policy.on_confirm_callback(U(f"clioconf:{token}:yes"), None), loop)
        fut.result(timeout=5)
    wt.join(timeout=5)
    check("BOUND: irrev confermato -> ESEGUITO (fix cross-process)", res.get("r", "").find("MOCK eliminazione") >= 0)
    asyncio.run_coroutine_threadsafe = run_coroutine_threadsafe_originale
    policy.CONFIRM_TIMEOUT = confirm_timeout_originale
    policy.bind(None, None)

    # ============ 4) REDAZIONE SECRET NELL'AUDIT ============
    print("[4] redazione secret negli args di audit")
    policy.guarded_call("ottieni_info_sistema", mock_readonly,
                        {"nota": "chiave AIzaFAKE000000000000000000000000000000000 fine"}, origin="telegram")
    lines = audit_lines()
    last = lines[-1] if lines else ""
    check("secret AIza... redatto nell'audit", ("AIzaFAKE" not in last) and ("REDACTED_GOOGLE_KEY" in last))

    # ============ 5) SECURITY.LOG POPOLATO + campo origin ============
    print("[5] security.log popolato + origin")
    after = len(audit_lines())
    check("security.log e' cresciuto", after > before)
    recs = [json.loads(l) for l in audit_lines()[-12:]]
    origins = {r.get("origin") for r in recs}
    check("audit contiene origin telegram+flask", {"telegram", "flask"} <= origins)
    check("ogni record ha campo 'origin'", all("origin" in r for r in recs))

    # ---- riepilogo ----
    passed = sum(1 for _, ok in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== RISULTATO: {passed}/{total} PASS ===")
    print("Origini viste nell'audit:", sorted(o for o in origins if o))
    print("Esempio record audit (ultimo, redatto):")
    print("  " + audit_lines()[-1])
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
