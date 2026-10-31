"""startup_summary.py — metriche REALI per il saluto d'avvio di Clio.

Regola del progetto: nessun numero hardcoded, nessuna stima inventata.
Ogni valore e' letto a runtime dalla fonte vera; le fonti sono iniettabili
cosi' i test possono verificarle senza toccare il PC o il vault.

Fonti:
  - tool: ``registry.AZIONI_MAP`` (import pigro: non carica il runtime)
  - note del vault: conteggio dei ``*.md`` sotto ``VAULT_DIR``
  - automazioni: ``.pipeline_status.json`` scritto dalla pipeline notturna
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_VAULT_DIR = r"os.path.expanduser("~")\OneDrive\OneSyncFiles"
PIPELINE_STATUS_NAME = ".pipeline_status.json"


def count_tools(registry_map=None) -> int:
    """Numero di tool realmente registrati (default: registry.AZIONI_MAP)."""
    if registry_map is None:
        import registry  # import leggero, nessun effetto collaterale
        registry_map = registry.AZIONI_MAP
    return len(registry_map or {})


def count_vault_notes(vault_dir=None) -> int:
    """Numero di note Markdown nel vault. 0 se il vault non e' raggiungibile."""
    root = Path(vault_dir or DEFAULT_VAULT_DIR)
    if not root.is_dir():
        return 0
    try:
        return sum(1 for _ in root.rglob("*.md"))
    except OSError:
        return 0


def read_pipeline_status(pipeline_file=None) -> dict:
    """Stato dell'ultima pipeline notturna. Dizionario vuoto se assente/illeggibile."""
    path = Path(pipeline_file) if pipeline_file else Path(DEFAULT_VAULT_DIR) / PIPELINE_STATUS_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_startup_summary(vault_dir=None, registry_map=None, pipeline_file=None) -> dict:
    """Riepilogo misurato all'avvio.

    Ritorna un dizionario con soli valori letti, mai costanti inventate.
    """
    pipeline = read_pipeline_status(pipeline_file)
    return {
        "tools": count_tools(registry_map),
        "vault_notes": count_vault_notes(vault_dir),
        "pipeline_total": _to_int(pipeline.get("totali")),
        "pipeline_failed": _to_int(pipeline.get("falliti")),
        "pipeline_updated": str(pipeline.get("aggiornato") or ""),
    }


def build_greeting(summary: dict) -> str:
    """Frase di saluto costruita SOLO dai numeri presenti nel riepilogo."""
    tools = _to_int(summary.get("tools"))
    note = _to_int(summary.get("vault_notes"))
    totali = _to_int(summary.get("pipeline_total"))
    falliti = _to_int(summary.get("pipeline_failed"))

    parti = [f"{tools} strumenti pronti", f"{note} note nel vault"]
    if totali <= 0:
        parti.append("pipeline notturna senza dati")
    elif falliti > 0:
        parti.append(f"pipeline notturna con {falliti} passaggi falliti su {totali}")
    else:
        parti.append(f"pipeline notturna {totali} su {totali} ok")

    return "Ciao, sono Clio. " + ", ".join(parti) + "."
