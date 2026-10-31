"""Test del saluto d'avvio: i numeri devono venire dalle fonti, mai hardcoded.

Esecuzione:  python -m pytest -q tests/test_startup_greeting.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import startup_summary as ss  # noqa: E402


# ── fonti finte, isolate su tmp_path ──────────────────────────────────────────

def _vault_finto(tmp_path: Path, n_note: int) -> Path:
    vault = tmp_path / "vault"
    (vault / "Progetti").mkdir(parents=True)
    for i in range(n_note - 1):
        (vault / f"nota_{i}.md").write_text("x", encoding="utf-8")
    (vault / "Progetti" / "sotto_cartella.md").write_text("x", encoding="utf-8")
    # rumore che NON deve essere contato
    (vault / "immagine.png").write_text("x", encoding="utf-8")
    return vault


def _pipeline(tmp_path: Path, totali: int, falliti: int, aggiornato: str = "2026-10-28 00:05:40") -> Path:
    p = tmp_path / ".pipeline_status.json"
    p.write_text(
        json.dumps({"aggiornato": aggiornato, "totali": totali, "falliti": falliti, "step": []}),
        encoding="utf-8",
    )
    return p


# ── test ──────────────────────────────────────────────────────────────────────

def test_summary_legge_le_fonti_vere(tmp_path):
    vault = _vault_finto(tmp_path, n_note=7)
    pipeline = _pipeline(tmp_path, totali=20, falliti=2)

    s = ss.get_startup_summary(
        vault_dir=vault,
        registry_map={"a": 1, "b": 2, "c": 3},
        pipeline_file=pipeline,
    )

    assert s["tools"] == 3
    assert s["vault_notes"] == 7          # 7 .md, la .png non conta
    assert s["pipeline_total"] == 20
    assert s["pipeline_failed"] == 2
    assert s["pipeline_updated"] == "2026-10-28 00:05:40"


def test_saluto_contiene_i_numeri_misurati():
    s = {"tools": 94, "vault_notes": 1094, "pipeline_total": 20, "pipeline_failed": 0}

    testo = ss.build_greeting(s)

    assert "94" in testo
    assert "1094" in testo
    assert "20 su 20" in testo
    assert "strumenti" in testo and "note" in testo


def test_saluto_non_ha_numeri_hardcoded():
    """Cambiando le fonti deve cambiare la frase: nessuna costante nascosta."""
    a = ss.build_greeting({"tools": 3, "vault_notes": 5, "pipeline_total": 10, "pipeline_failed": 0})
    b = ss.build_greeting({"tools": 94, "vault_notes": 1094, "pipeline_total": 10, "pipeline_failed": 0})

    assert a != b
    assert "3 " in a and "94" in b
    assert "94" not in a and "1094" not in a


def test_saluto_gestisce_pipeline_assente(tmp_path):
    """Senza .pipeline_status.json non si inventa nulla: lo si dichiara."""
    vault = _vault_finto(tmp_path, n_note=2)
    s = ss.get_startup_summary(vault_dir=vault, registry_map={"x": 1}, pipeline_file=tmp_path / "manca.json")

    assert s["pipeline_total"] == 0
    assert s["pipeline_failed"] == 0
    assert "senza dati" in ss.build_greeting(s)


def test_saluto_riporta_i_fallimenti():
    testo = ss.build_greeting({"tools": 1, "vault_notes": 1, "pipeline_total": 20, "pipeline_failed": 3})
    assert "3 passaggi falliti su 20" in testo


def test_vault_inesistente_non_esplode(tmp_path):
    s = ss.get_startup_summary(vault_dir=tmp_path / "non_esiste", registry_map={}, pipeline_file=tmp_path / "x.json")
    assert s["vault_notes"] == 0
    assert s["tools"] == 0


def test_conteggio_reale_dal_registry():
    """Integrazione: il conteggio di default deve combaciare col registry vero."""
    import registry

    assert ss.count_tools() == len(registry.AZIONI_MAP)
    assert ss.count_tools() > 0
