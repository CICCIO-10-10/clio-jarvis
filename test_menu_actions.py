"""test_menu_actions.py — voci [V] e [P] del menu principale di CLIO.

Nessun test lancia menu.py (interattivo) né mostra notifiche: si testano le
funzioni richiamate dalle voci, compreso il caso "vault non configurato".
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import knowledge_curator as kc     # noqa: E402
import menu_actions as ma          # noqa: E402
import policy                      # noqa: E402
import proactive_assistant as pa   # noqa: E402


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Isola audit/log e rende NON configurato il vault (env + The Scribe)."""
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    monkeypatch.delenv("CLIO_VAULT_PATH", raising=False)
    monkeypatch.setenv("CLIO_SCRIBE_CONFIG", str(tmp_path / "scribe-assente.yaml"))
    pa.reset_rate_limit()
    pa.clear_index_cache()
    yield tmp_path
    pa.ferma_proattivita(timeout=5)
    pa.reset_rate_limit()
    pa.clear_index_cache()


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    (root / "A.md").write_text("# A\n\nNota di prova.\n", encoding="utf-8")
    (root / "B.md").write_text("# B\n\n[[NonEsiste]]\n", encoding="utf-8")
    return root


# ── check ───────────────────────────────────────────────────────────────────
def test_check_conferma_le_azioni_del_menu(ambiente, capsys):
    assert ma.check() == 0
    uscita = capsys.readouterr().out

    assert "scansiona_vault" in uscita
    assert "proponi_pulizia_vault" in uscita
    assert "attiva_proattivita" in uscita
    assert "[OK]" in uscita and "[KO]" not in uscita
    assert "Menu pronto." in uscita


# ── [V] Scansiona Vault ─────────────────────────────────────────────────────
def test_scan_mostra_il_riepilogo(vault, ambiente, capsys):
    assert ma.scan(str(vault)) == 0
    uscita = capsys.readouterr().out

    assert "Note scansionate: 2" in uscita
    assert "1 link rotti" in uscita
    assert "Traceback" not in uscita


def test_scan_non_modifica_il_vault(vault, ambiente):
    prima = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in vault.iterdir()}
    ma.scan(str(vault))
    dopo = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in vault.iterdir()}
    assert dopo == prima


def test_scan_con_vault_non_configurato_non_crasha(ambiente, capsys):
    assert ma.scan("") == 2
    uscita = capsys.readouterr().out

    assert "Vault non configurato" in uscita
    assert "CLIO_VAULT_PATH" in uscita
    assert "--vault" in uscita
    assert "Traceback" not in uscita


def test_scan_con_percorso_inesistente_non_crasha(ambiente, tmp_path, capsys):
    assert ma.scan(str(tmp_path / "non-esiste")) == 2
    uscita = capsys.readouterr().out
    assert "Vault non configurato" in uscita and "Traceback" not in uscita


def test_scan_usa_la_variabile_d_ambiente(vault, ambiente, monkeypatch, capsys):
    monkeypatch.setenv("CLIO_VAULT_PATH", str(vault))
    assert ma.scan() == 0
    assert "Note scansionate: 2" in capsys.readouterr().out


def test_scan_rispetta_i_giorni(vault, ambiente, capsys):
    assert ma.scan(str(vault), days=30) == 0
    assert "Note scansionate: 2" in capsys.readouterr().out


# ── [P] Attiva Proattività ──────────────────────────────────────────────────
def test_proattivita_avvia_e_ferma_senza_restare_appesa(vault, ambiente, capsys):
    assert ma.proattivita(str(vault), interval_seconds=3600, keep_alive=False) == 0
    uscita = capsys.readouterr().out

    assert "Proattività attiva" in uscita
    assert "30 minuti" in uscita
    assert pa.proattivita_attiva() is False     # nessun thread lasciato appeso


def test_proattivita_con_vault_non_configurato_non_crasha(ambiente, capsys):
    assert ma.proattivita("", interval_seconds=3600, keep_alive=False) == 2
    uscita = capsys.readouterr().out

    assert "Vault non configurato" in uscita
    assert "Traceback" not in uscita
    assert pa.proattivita_attiva() is False


def test_proattivita_intervallo_non_valido(vault, ambiente, capsys):
    assert ma.proattivita(str(vault), interval_seconds=0, keep_alive=False) == 2
    assert "interval_seconds" in capsys.readouterr().out


# ── Riconoscimento errori ───────────────────────────────────────────────────
@pytest.mark.parametrize("testo,atteso", [
    ("ERRORE: vault non trovato", True),
    ("❌ Errore durante scansiona_vault: boom", True),
    ("Vault: C:/x\nNote scansionate: 3", False),
])
def test_riconoscimento_errori(testo, atteso):
    assert ma._e_errore(testo) is atteso


# ── CLI ─────────────────────────────────────────────────────────────────────
def test_cli_senza_comando_mostra_help(ambiente, capsys):
    assert ma.main([]) == 2
    uscita = capsys.readouterr().out
    assert "scan" in uscita and "proattivita" in uscita and "check" in uscita


def test_cli_scan(vault, ambiente):
    assert ma.main(["scan", "--vault", str(vault), "--days", "30"]) == 0


def test_cli_check(ambiente):
    assert ma.main(["check"]) == 0


def test_cli_comando_sconosciuto_non_crasha(ambiente):
    with pytest.raises(SystemExit) as errore:
        ma.main(["comando-inesistente"])
    assert errore.value.code == 2
