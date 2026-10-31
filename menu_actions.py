"""menu_actions.py — azioni richiamabili dal menu principale di CLIO.

Obiettivo: **zero comandi Python** per l'utente finale. Il menu (menu.bat →
menu.py di AUTOMAZIONI) richiama questo script che espone due funzioni:

    python menu_actions.py scan          # R7: scansione del vault (sola lettura)
    python menu_actions.py proattivita   # R8: monitor proattivo (finestra dedicata)
    python menu_actions.py check         # diagnostica rapida, nessun side effect

Le azioni passano dal control-plane (`policy.guarded_call`) e sono READ_ONLY:
nessuna conferma, nessuna modifica al vault, nessun log di cosa guardi.

Se il vault non è configurato il menu mostra un messaggio chiaro con le due
strade possibili (`CLIO_VAULT_PATH` o `--vault`) e NON crasha.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import policy                       # noqa: E402
import registry                     # noqa: E402

VAULT_HINT = (
    "Vault non configurato. Due strade:\n"
    "  1) imposta la variabile CLIO_VAULT_PATH (o il vault in The Scribe), oppure\n"
    "  2) passa il percorso esplicito: --vault \"C:\\percorso\\del\\vault\""
)

_ERRORE_INTERNO = "❌"


def _azione(nome: str, args: dict) -> str:
    """Invoca un'azione del registro via control-plane. Mai eccezioni."""
    funzione = registry.AZIONI_MAP.get(nome)
    if funzione is None:
        return f"ERRORE: azione '{nome}' non registrata in AZIONI_MAP"
    esito = policy.guarded_call(nome, funzione, args, confirm=lambda n, a: "no")
    return esito if isinstance(esito, str) else str(esito)


def _e_errore(esito: str) -> bool:
    return esito.startswith("ERRORE") or esito.startswith(_ERRORE_INTERNO)


def _stampa_errore(esito: str, dettaglio: str = "") -> None:
    print(esito)
    if dettaglio:
        print(dettaglio)


def scan(vault_path: str = "", days: int = 90) -> int:
    """Scansiona il vault e stampa il riepilogo. 0 = ok, 2 = vault assente."""
    esito = _azione("scansiona_vault", {"vault_path": vault_path, "days": days})
    if _e_errore(esito):
        _stampa_errore(esito, "\n" + VAULT_HINT)
        return 2
    print(esito)
    return 0


def proattivita(vault_path: str = "", interval_seconds: int = 60,
                keep_alive: bool = True) -> int:
    """Avvia il monitor proattivo. 0 = ok, 2 = vault assente.

    Con `keep_alive=False` (usato dai test e dalla diagnostica) il monitor viene
    avviato, verificato e subito fermato: nessun thread resta appeso.
    """
    esito = _azione("attiva_proattivita",
                    {"vault_path": vault_path, "interval_seconds": interval_seconds})
    if _e_errore(esito):
        _stampa_errore(esito, "\n" + VAULT_HINT)
        return 2
    print(esito)

    if not keep_alive:
        import proactive_assistant as pa
        pa.ferma_proattivita()
        return 0

    print("\nQuesta finestra è il monitor: lasciala aperta mentre lavori.")
    print("Chiudi la finestra (o premi Ctrl+C) per fermare la proattività.")
    import proactive_assistant as pa
    try:
        while pa.proattivita_attiva():
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStop richiesto...")
    finally:
        print(pa.ferma_proattivita())
    return 0


def check() -> int:
    """Diagnostica senza side effect: azioni registrate e classificate."""
    richieste = {
        "scansiona_vault": "read-only",
        "proponi_pulizia_vault": "read-only",
        "attiva_proattivita": "read-only",
    }
    mancanti: List[str] = []
    for nome, classe_attesa in richieste.items():
        registrata = nome in registry.AZIONI_MAP
        classe = policy.classify(nome)
        stato = "OK" if registrata and classe == classe_attesa else "KO"
        if stato == "KO":
            mancanti.append(nome)
        print(f"[{stato}] {nome:24s} registrata={registrata} classe={classe}")
    print("\nMenu pronto." if not mancanti else f"\nAzioni non pronte: {mancanti}")
    return 0 if not mancanti else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="menu_actions.py",
        description="Azioni del menu di CLIO (vault e proattività).")
    sub = parser.add_subparsers(dest="comando")

    p_scan = sub.add_parser("scan", help="scansiona il vault (sola lettura)")
    p_scan.add_argument("--vault", default="", help="percorso del vault Obsidian")
    p_scan.add_argument("--days", type=int, default=90,
                        help="giorni oltre i quali una nota è obsoleta (default 90)")

    p_pro = sub.add_parser("proattivita", help="avvia il monitor proattivo")
    p_pro.add_argument("--vault", default="", help="percorso del vault Obsidian")
    p_pro.add_argument("--interval", type=int, default=60,
                       help="secondi tra un controllo e l'altro (default 60)")

    sub.add_parser("check", help="verifica che le azioni siano pronte (no side effect)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.comando:
        parser.print_help()
        return 2
    if args.comando == "scan":
        return scan(args.vault, args.days)
    if args.comando == "proattivita":
        return proattivita(args.vault, args.interval)
    return check()


if __name__ == "__main__":
    sys.exit(main())
