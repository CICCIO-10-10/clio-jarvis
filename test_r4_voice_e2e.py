# -*- coding: utf-8 -*-
"""test_r4_voice_e2e.py — R4: dal comando vocale alla patch verificata.

Catena REALE esercitata dallo script (nessun pezzo finto tranne l'agente,
se usi --fake):

    voce (testo simulato)
      -> jarvis_brain.detect_coding_command()        routing vocale deterministico
      -> policy.guarded_call("esegui_coding_task")   gate di conferma + audit
      -> coding_agent.run_coding_task()              sandbox _clio_sandbox
      -> pytest nella sandbox                        verifica reale del patch

Uso:
    python test_r4_voice_e2e.py            agente REALE (CODER locale)
    python test_r4_voice_e2e.py --fake     agente deterministico, zero token
    python test_r4_voice_e2e.py --keep     non ripristina la sandbox alla fine

Sotto pytest il modulo viene solo importato: nessun effetto collaterale
(gli import pesanti stanno dentro main()).
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

FRASE_VOCALE = ("CLIO, aggiungi una funzione moltiplicazione a main.py "
                "nella sandbox")

AGENTE_FAKE_MAIN = (
    "\n\n"
    "def moltiplicazione(a, b):\n"
    "    return a * b\n"
)
AGENTE_FAKE_TEST = (
    "\n\n"
    "from main import moltiplicazione\n"
    "\n\n"
    "def test_moltiplicazione():\n"
    "    assert moltiplicazione(3, 4) == 12\n"
)


def agente_deterministico(task, workspace, timeout):
    """Agente simulato (--fake): modifica davvero la sandbox, zero token."""
    with open(os.path.join(workspace, "main.py"), "a", encoding="utf-8") as f:
        f.write(AGENTE_FAKE_MAIN)
    with open(os.path.join(workspace, "test_main.py"), "a", encoding="utf-8") as f:
        f.write(AGENTE_FAKE_TEST)
    return {"ok": True, "output": "agente simulato: main.py + test_main.py"}


def _git(root, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=str(root),
                          capture_output=True, text=True)


def _stato_sandbox(root):
    """(pulita: bool, main.py contiene moltiplicazione: bool)."""
    res = _git(root, "status", "--porcelain")
    pulita = res.returncode == 0 and not res.stdout.strip()
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        ha_molt = "moltiplicazione" in f.read()
    return pulita, ha_molt


def _ripristina(root):
    _git(root, "reset", "-q", "--hard", "HEAD")
    _git(root, "clean", "-qfd")


def _esegui_dal_control_plane(descrizione, risposta_detta, agent):
    """Voce -> policy (conferma VOCALE) -> coding_agent.

    Non passiamo `confirm`: lasciamo che sia `guarded_call` a scegliere il canale
    in base all'origine ("voce" -> `policy._voice_confirm`). TTS e STT sono
    sostituiti da mock: la domanda viene stampata, la risposta è `risposta_detta`.
    """
    import policy
    import coding_agent

    detto = []
    policy._telegram_disponibile = lambda: False          # nessun Telegram
    policy._voice_speak = lambda testo: (detto.append(testo), True)[1]
    policy._voice_listen = lambda timeout: risposta_detta

    def azione(task_descrizione: str, workspace: str = None):
        # Il workspace arriva dal routing vocale ma è sempre la sandbox:
        # run_coding_task rifiuta qualunque percorso esterno.
        return coding_agent.run_coding_task(task_descrizione, workspace=workspace,
                                            agent=agent)

    esito = policy.guarded_call("esegui_coding_task", azione,
                                {"task_descrizione": descrizione}, origin="voce")
    print(f"    [TTS Clio] {detto[0] if detto else '(nessuna domanda)'}")
    print(f"    [STT Capo] {risposta_detta!r} -> {policy._normalizza_risposta_voce(risposta_detta)}")
    return esito


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    usa_fake = "--fake" in argv
    keep = "--keep" in argv

    from jarvis_brain import detect_coding_command

    import coding_agent
    import policy

    root = coding_agent.get_sandbox_root()
    agent = agente_deterministico if usa_fake else None   # None = _default_agent
    nome_agente = "deterministico (--fake)" if usa_fake else "CODER locale (Ollama/llama.cpp)"

    print("=" * 72)
    print("R4 — VOICE-TO-CODE E2E  (voce -> policy -> sandbox -> pytest)")
    print("=" * 72)
    print(f"sandbox     : {root}")
    print(f"agente      : {nome_agente}")
    print(f"frase vocale: {FRASE_VOCALE!r}")
    print()

    # ── [1] routing vocale ────────────────────────────────────────────────
    print("[1] routing vocale (jarvis_brain.detect_coding_command)")
    rilevato = detect_coding_command(FRASE_VOCALE)
    if not rilevato:
        print("    FAIL: la frase non è stata riconosciuta come comando di coding")
        return 1
    descrizione, ws_hint = rilevato
    print(f"    OK  task      = {descrizione!r}")
    print(f"        workspace = {ws_hint or '(sandbox di default)'}")
    print()

    _ripristina(root)
    print(f"[i] sandbox ripristinata: {_stato_sandbox(root)}")
    print()

    # ── [2] la conferma è un gate VERO: con "no" non si esegue nulla ──────
    print("[2] gate di conferma — il Capo dice NO")
    esito_no = _esegui_dal_control_plane(descrizione, "no, annulla", agent)
    pulita, ha_molt = _stato_sandbox(root)
    if "annullata" not in str(esito_no).lower():
        print(f"    FAIL: atteso annullamento, ricevuto: {esito_no!r}")
        return 1
    if not pulita or ha_molt:
        print(f"    FAIL: la sandbox è stata modificata senza conferma ({pulita=}, {ha_molt=})")
        return 1
    print(f"    OK  azione annullata, sandbox intatta (pulita={pulita})")
    print()

    # ── [3] il Capo conferma a voce -> esecuzione reale nella sandbox ─────
    print("[3] gate di conferma — il Capo dice SÌ -> esecuzione")
    t0 = time.time()
    esito = _esegui_dal_control_plane(descrizione, "sì", agent)
    durata = time.time() - t0
    print()

    if not isinstance(esito, dict):
        print(f"    FAIL: esito inatteso dal control-plane: {esito!r}")
        return 1

    # ── [4] verifica del risultato ────────────────────────────────────────
    print("[4] verifica")
    print(f"    success   : {esito['success']}")
    print(f"    timed_out : {esito['timed_out']}")
    print(f"    error     : {esito['error']}")
    print(f"    durata    : {durata:.1f}s")
    print(f"    task inviato all'agente: {esito.get('task_effective')!r}")
    pulita, ha_molt = _stato_sandbox(root)
    print(f"    main.py contiene 'moltiplicazione': {ha_molt}")
    print(f"    file di test aggiornati: {esito.get('tests_touched')}")
    print("    --- test_output ---")
    for riga in (esito["test_output"] or "").splitlines():
        print(f"    {riga}")
    print("    --- diff ---")
    for riga in (esito["diff"] or "(nessuna modifica)").splitlines()[:40]:
        print(f"    {riga}")

    ok = (bool(esito["success"]) and "passed" in esito["test_output"]
          and ha_molt and bool(esito.get("tests_touched")))
    print()
    if ok:
        print("=== RISULTATO: R4 DIMOSTRATA (patch prodotta e test verdi) ===")
    else:
        print("=== RISULTATO: R4 NON dimostrata (vedi error/test_output) ===")

    # ── [5] ripristino ────────────────────────────────────────────────────
    if keep:
        print("\n[i] sandbox lasciata modificata (--keep)")
    else:
        _ripristina(root)
        pulita, ha_molt = _stato_sandbox(root)
        print(f"\n[i] sandbox ripristinata: pulita={pulita}, moltiplicazione={ha_molt}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
