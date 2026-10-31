"""test_knowledge_curator.py — R7: Vault Intelligence, SOLA LETTURA.

Tutti i test lavorano su un vault finto in `tmp_path`: nessun test tocca il vault
Obsidian reale e nessun file del vault viene mai modificato.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import knowledge_curator as kc     # noqa: E402
import policy                      # noqa: E402
import registry                    # noqa: E402

GIORNI_STALE = 120


# ── Fixture: vault finto con problemi noti ────────────────────────────────────
def _scrivi(path: Path, testo: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(testo, encoding="utf-8")
    return path


def _invecchia(path: Path, days: int) -> None:
    """Sposta mtime nel passato (nessuna modifica al contenuto).

    Toglie un'ora in più così `days` resta un limite inferiore anche dopo il
    troncamento dei giorni interi fatto da find_stale_notes.
    """
    vecchio = time.time() - days * 86400 - 3600
    os.utime(path, (vecchio, vecchio))


@pytest.fixture
def vault(tmp_path):
    """Vault con: 2 duplicati di titolo, 2 duplicati di contenuto, 1 nota
    vecchia, 1 nota fresca, 1 nota con link rotto e 1 nota che esiste."""
    root = tmp_path / "vault"
    root.mkdir()

    corpo_ab = ("Questa nota descrive il progetto con obiettivi chiari, "
                "milestone verificabili e documentazione sempre aggiornata")
    _scrivi(root / "A.md", f"---\ntitle: Progetto Uguale\n---\n\n{corpo_ab}\n")
    _scrivi(root / "B.md", f"---\ntitle: progetto uguale\n---\n\n{corpo_ab}\n")

    corpo = ("Questa nota parla di python con esempi pratici di codice e test "
             "automatici scritti insieme")
    # Titoli diversi (frontmatter) ma corpo identico: duplicato di contenuto.
    _scrivi(root / "C.md", f"---\ntitle: Alfa\n---\n\n{corpo}\n")
    _scrivi(root / "D.md", f"---\ntitle: Beta\n---\n\n{corpo}\n")

    vecchia = _scrivi(root / "Vecchia.md", "# Vecchia\n\nNota dimenticata.\n")
    _invecchia(vecchia, GIORNI_STALE)
    fresca = _scrivi(root / "Fresca.md", "# Fresca\n\nNota aggiornata.\n")
    _invecchia(fresca, 1)

    _scrivi(root / "Rotta.md",
            "# Rotta\n\n[[NonEsiste]] e [[Esistente|alias]] e [[Fresca#Sezione]] "
            "e [[#SoloAncora]] e [[https://example.com]] e [[allegato.pdf]]\n")
    _scrivi(root / "Esistente.md", "# Esistente\n\nEsiste davvero.\n")

    return root


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Isola audit e cartella report: niente scritture in clio/logs o clio/data."""
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    monkeypatch.setattr(kc, "DATA_DIR", tmp_path / "data")
    monkeypatch.delenv("CLIO_VAULT_PATH", raising=False)
    return tmp_path


# ── scan_vault ───────────────────────────────────────────────────────────────
def test_scan_vault_legge_frontmatter_titoli_link_e_mtime(vault):
    data = kc.scan_vault(str(vault))

    assert data["count"] == 8
    assert Path(data["vault"]).name == "vault"
    note = {nota["path"]: nota for nota in data["notes"]}

    assert set(note) == {"A.md", "B.md", "C.md", "D.md", "Vecchia.md",
                         "Fresca.md", "Rotta.md", "Esistente.md"}
    assert note["A.md"]["title"] == "Progetto Uguale"
    assert note["A.md"]["frontmatter"]["title"] == "Progetto Uguale"
    assert note["C.md"]["title"] == "Alfa"        # dal frontmatter
    assert note["Esistente.md"]["title"] == "Esistente"
    assert note["Rotta.md"]["links"] == [
        "NonEsiste", "Esistente|alias", "Fresca#Sezione", "#SoloAncora",
        "https://example.com", "allegato.pdf"]
    assert "\n---" not in note["A.md"]["content"]   # frontmatter rimosso dal corpo
    assert note["A.md"]["modified"]
    assert note["A.md"]["size"] > 0


def test_scan_vault_ignora_cartelle_di_servizio(vault):
    _scrivi(vault / ".obsidian" / "Ignorata.md", "# Ignorata\n")
    _scrivi(vault / ".trash" / "Cestino.md", "# Cestino\n")
    _scrivi(vault / "sottocartella" / "Valida.md", "# Valida\n")

    data = kc.scan_vault(str(vault))
    percorsi = {nota["path"] for nota in data["notes"]}

    assert "sottocartella/Valida.md" in percorsi
    assert not any("Ignorata" in p or "Cestino" in p for p in percorsi)
    assert data["count"] == 9


@pytest.mark.parametrize("valore", ["", "   ", None, "C:/percorso/che/non/esiste"])
def test_scan_vault_rifiuta_percorsi_non_validi(valore, tmp_path):
    if valore == "C:/percorso/che/non/esiste":
        valore = str(tmp_path / "assente")
    with pytest.raises(ValueError):
        kc.scan_vault(valore)


# ── find_duplicates ──────────────────────────────────────────────────────────
def test_find_duplicates_trova_titoli_e_contenuti(vault):
    note = {n["path"]: n for n in kc.scan_vault(str(vault))["notes"]}
    duplicati = kc.find_duplicates(note.values())

    coppie = {(d["a"], d["b"]): d for d in duplicati}
    assert set(coppie) == {("A.md", "B.md"), ("C.md", "D.md")}
    assert coppie[("A.md", "B.md")]["reason"] == "titolo"
    assert coppie[("A.md", "B.md")]["similarity"] == 1.0
    assert coppie[("C.md", "D.md")]["reason"] == "contenuto"
    assert coppie[("C.md", "D.md")]["similarity"] >= kc.SIMILARITY_THRESHOLD


def test_find_duplicates_ignora_testi_corti_e_diversi():
    note = [
        {"path": "x.md", "title": "X", "content": "uno due"},
        {"path": "y.md", "title": "Y", "content": "uno due"},
        {"path": "z.md", "title": "Z", "content": "parole completamente diverse qui"},
    ]
    assert kc.find_duplicates(note) == []


def test_find_duplicates_vuoto_su_lista_vuota():
    assert kc.find_duplicates([]) == []


# ── R7.1: falsi duplicati (titoli generici + contenuto) ──────────────────────
def test_pensiero_del_giorno_con_contenuti_diversi_non_e_duplicato():
    """Il caso reale che generava centinaia di falsi positivi nel vault."""
    notes = [
        {"path": "20_Aree/Identita/2026-08/pensiero_del_giorno.md",
         "title": "pensiero_del_giorno",
         "content": "Oggi ho capito che la disciplina conta piu della motivazione "
                    "e voglio allenarla ogni mattina con piccoli gesti concreti"},
        {"path": "20_Aree/Business/2026-08/pensiero_del_giorno.md",
         "title": "pensiero_del_giorno",
         "content": "Il cliente ha approvato il preventivo per il sito nuovo, "
                    "serve la fattura entro venerdi e poi la consegna finale"},
    ]
    assert kc.find_duplicates(notes) == []


@pytest.mark.parametrize("titolo", [
    "_MOC", "_moc", "MOC", "Agenda", "pensiero_del_giorno",
    "Pensiero del giorno", "Curiosità del giorno", "curiosita_del_giorno",
    "Note", "TODO", "_LEGGIMI", "Template", "Senza titolo",
])
def test_titoli_generici_sono_esclusi(titolo):
    chiave = kc._title_key(titolo)
    assert chiave in kc.EXCLUDED_TITLES, f"{titolo!r} -> {chiave!r} non escluso"


@pytest.mark.parametrize("titolo", ["CLIO", "The Scribe", "Roast Room", "Diario"])
def test_titoli_specifici_non_sono_esclusi(titolo):
    assert kc._title_key(titolo) not in kc.EXCLUDED_TITLES


def test_titolo_uguale_ma_contenuti_diversi_non_e_duplicato():
    notes = [
        {"path": "10_Progetti/CLIO.md", "title": "CLIO",
         "content": "progetto assistente personale memoria control plane policy"},
        {"path": "Projects/CLIO.md", "title": "CLIO",
         "content": "cliente approvato preventivo sito fattura entro venerdi consegna"},
    ]
    assert kc.find_duplicates(notes) == []


def test_titolo_uguale_e_contenuto_quasi_identico_e_duplicato():
    base = "progetto assistente personale memoria control plane policy desktop"
    notes = [
        {"path": "10_Progetti/CLIO.md", "title": "CLIO", "content": base},
        {"path": "Projects/CLIO.md", "title": "CLIO", "content": base + " extra"},
    ]
    duplicati = kc.find_duplicates(notes)

    assert len(duplicati) == 1
    assert duplicati[0]["reason"] == "titolo"
    assert duplicati[0]["similarity"] >= kc.TITLE_SIMILARITY_THRESHOLD


def test_titolo_escluso_ma_contenuto_identico_resta_duplicato():
    """L'esclusione vale per il match sul titolo, non per il contenuto identico."""
    corpo = ("contenuto identico con molte parole ripetute in entrambe le note "
             "del vault e nessuna differenza sostanziale")
    notes = [
        {"path": "20_Aree/Tech/2026-10/_MOC.md", "title": "_MOC", "content": corpo},
        {"path": "50_Giornale/2026-10/_MOC.md", "title": "_MOC", "content": corpo},
    ]
    duplicati = kc.find_duplicates(notes)

    assert len(duplicati) == 1
    assert duplicati[0]["reason"] == "contenuto"
    assert duplicati[0]["similarity"] >= kc.SIMILARITY_THRESHOLD


def test_contenuto_troppo_corto_non_viene_giudicato():
    notes = [
        {"path": "a/Agenda.md", "title": "Agenda", "content": "due parole"},
        {"path": "b/Agenda.md", "title": "Agenda", "content": "due parole"},
    ]
    assert kc.find_duplicates(notes) == []


# ── find_stale_notes ─────────────────────────────────────────────────────────
def test_find_stale_notes_trova_solo_le_vecchie(vault):
    note = kc.scan_vault(str(vault))["notes"]
    vecchie = kc.find_stale_notes(note, days=90)

    assert [v["path"] for v in vecchie] == ["Vecchia.md"]
    assert vecchie[0]["age_days"] >= GIORNI_STALE
    assert vecchie[0]["threshold_days"] == 90
    assert datetime.fromisoformat(vecchie[0]["modified"])


def test_find_stale_notes_soglia_personalizzata(vault):
    note = kc.scan_vault(str(vault))["notes"]
    assert [v["path"] for v in kc.find_stale_notes(note, days=1)] == ["Vecchia.md"]
    assert kc.find_stale_notes(note, days=365) == []


@pytest.mark.parametrize("giorni", [0, -5, "90", None])
def test_find_stale_notes_rifiuta_giorni_non_validi(giorni):
    with pytest.raises(ValueError):
        kc.find_stale_notes([], giorni)


# ── find_broken_links ────────────────────────────────────────────────────────
def test_find_broken_links_solo_i_target_inesistenti(vault):
    note = kc.scan_vault(str(vault))["notes"]
    rotti = kc.find_broken_links(note)

    assert rotti == [{"path": "Rotta.md", "link": "NonEsiste", "target": "NonEsiste"}]


def test_find_broken_links_rispetta_percorsi_e_maiuscole():
    note = [
        {"path": "Cartella/Destinazione.md", "title": "D", "links": []},
        {"path": "Fonte.md", "title": "F",
         "links": ["Cartella/Destinazione", "cartella/destinazione.md",
                   "DESTINAZIONE", "Cartella/Mancante"]},
    ]
    rotti = kc.find_broken_links(note)
    assert [r["target"] for r in rotti] == ["Cartella/Mancante"]


def test_find_broken_links_deduplica_per_nota_e_target():
    note = [{"path": "N.md", "title": "N", "links": ["Boh", "Boh#Sezione", "Boh|alias"]}]
    assert kc.find_broken_links(note) == [
        {"path": "N.md", "link": "Boh", "target": "Boh"}]


# ── propose_actions ──────────────────────────────────────────────────────────
def test_propose_actions_genera_frasi_operative(vault):
    note = kc.scan_vault(str(vault))["notes"]
    problemi = {
        "duplicates": kc.find_duplicates(note),
        "stale": kc.find_stale_notes(note, days=90),
        "broken_links": kc.find_broken_links(note),
    }
    proposte = kc.propose_actions(problemi)

    assert proposte == [
        "Unisci 'A.md' con 'B.md' (titoli uguali, contenuto simile al 100%)",
        f"Unisci 'C.md' con 'D.md' (contenuto simile al "
        f"{round(100 * [d for d in problemi['duplicates'] if d['a'] == 'C.md'][0]['similarity'])}%)",
        "Archivia 'Vecchia.md' (non modificata da "
        f"{problemi['stale'][0]['age_days']} giorni)",
        "Correggi il link '[[NonEsiste]]' in 'Rotta.md'",
    ]
    assert all(isinstance(p, str) for p in proposte)


def test_propose_actions_vuoto_senza_problemi():
    assert kc.propose_actions({}) == []
    assert kc.propose_actions({"duplicates": [], "stale": [], "broken_links": []}) == []


# ── generate_report (fuori dal vault) ────────────────────────────────────────
def test_generate_report_scrive_in_data_e_non_nel_vault(vault, ambiente):
    out = ambiente / "data"
    report = kc.generate_report(str(vault), str(out))

    assert report.parent == out
    assert report.name == f"vault_proposals_{datetime.now().strftime('%Y-%m-%d')}.md"
    testo = report.read_text(encoding="utf-8")
    for atteso in ("# Proposte di pulizia del vault", "Note scansionate: 8",
                   "- Duplicati: 2", "- Note obsolete (> 90 giorni): 1",
                   "- Link rotti: 1", "Unisci 'A.md' con 'B.md' (titoli uguali, contenuto simile al 100%)",
                   "Archivia 'Vecchia.md'", "Correggi il link '[[NonEsiste]]' in 'Rotta.md'",
                   "nessun file del vault è stato"):
        assert atteso in testo, f"manca nel report: {atteso}"    # il report non deve mai finire dentro il vault
    assert not list(vault.rglob("vault_proposals_*.md"))
    # nessun file temporaneo lasciato in giro
    assert list(out.glob(".*.tmp")) == []


def test_generate_report_usa_data_dir_di_default(vault, ambiente):
    report = kc.generate_report(str(vault))
    assert report.parent == kc.DATA_DIR
    assert report.parent == ambiente / "data"
    assert report.exists()


def test_generate_report_rifiuta_output_dentro_il_vault(vault, ambiente):
    for out in (vault, vault / "sub", vault / "sub" / "piu" / "profondo"):
        with pytest.raises(ValueError):
            kc.generate_report(str(vault), str(out))
    assert not list(vault.rglob("vault_proposals_*.md"))


def test_scan_e_report_non_modificano_il_vault(vault, ambiente):
    def stato():
        return {p.relative_to(vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(vault.rglob("*")) if p.is_file()}

    prima = stato()
    data = kc.scan_vault(str(vault))
    problemi = {
        "duplicates": kc.find_duplicates(data["notes"]),
        "stale": kc.find_stale_notes(data["notes"], days=90),
        "broken_links": kc.find_broken_links(data["notes"]),
    }
    kc.propose_actions(problemi)
    kc.generate_report(str(vault), str(ambiente / "data"))

    assert stato() == prima, "il vault è stato modificato: violazione sola lettura"


# ── Azioni CLIO ──────────────────────────────────────────────────────────────
def test_scansiona_vault_azione_riepiloga_i_problemi(vault, ambiente):
    esito = kc.scansiona_vault(str(vault), days=90)

    assert isinstance(esito, str)
    assert not esito.startswith("ERRORE")
    assert "Note scansionate: 8" in esito
    assert "2 duplicati" in esito and "1 note obsolete" in esito and "1 link rotti" in esito
    assert "Unisci 'A.md' con 'B.md'" in esito
    assert not list(vault.rglob("vault_proposals_*.md"))


def test_proponi_pulizia_vault_azione_scrive_il_report(vault, ambiente):
    esito = kc.proponi_pulizia_vault(str(vault), days=90)
    report = kc.DATA_DIR / f"vault_proposals_{datetime.now().strftime('%Y-%m-%d')}.md"

    assert esito.startswith(f"Report proposte: {report}")
    assert "Nessuna modifica eseguita sul vault." in esito
    assert report.exists()
    assert not list(vault.rglob("vault_proposals_*.md"))


def test_azioni_ritornano_errore_leggibile_senza_eccezioni(ambiente):
    inesistente = str(ambiente / "non-esiste")

    assert kc.scansiona_vault(inesistente).startswith("ERRORE:")
    assert kc.proponi_pulizia_vault(inesistente).startswith("ERRORE:")

    os.environ["CLIO_VAULT_PATH"] = inesistente
    try:  # senza vault_path si usa la variabile d'ambiente, che qui è invalida
        assert kc.scansiona_vault("").startswith("ERRORE:")
    finally:
        os.environ.pop("CLIO_VAULT_PATH", None)


def test_vault_da_variabile_d_ambiente(vault, ambiente, monkeypatch):
    monkeypatch.setenv("CLIO_VAULT_PATH", str(vault))
    esito = kc.scansiona_vault()

    assert "Note scansionate: 8" in esito
    assert str(vault.resolve()) in esito


def test_vault_path_esplicito_prevale_sull_ambiente(vault, tmp_path, ambiente, monkeypatch):
    altro = tmp_path / "altro-vault"
    _scrivi(altro / "Sola.md", "# Sola\n")
    monkeypatch.setenv("CLIO_VAULT_PATH", str(altro))

    esito = kc.scansiona_vault(str(vault))
    assert "Note scansionate: 8" in esito


# ── Integrazione col control-plane (policy, registry, LLM) ────────────────────
@pytest.mark.parametrize("azione", ["scansiona_vault", "proponi_pulizia_vault"])
def test_azioni_classificate_read_only(azione):
    assert policy.classify(azione) == "read-only"


@pytest.mark.parametrize("azione", ["scansiona_vault", "proponi_pulizia_vault"])
def test_azioni_registrate_nel_registro(azione):
    assert azione in registry.AZIONI_MAP
    assert callable(registry.AZIONI_MAP[azione])


def test_metadati_llm_espongono_le_azioni():
    import jarvis_brain

    for azione in ("scansiona_vault", "proponi_pulizia_vault"):
        assert azione in jarvis_brain.TOOLS_METADATA
    assert jarvis_brain.TOOLS_METADATA["scansiona_vault"]["params"] == ["vault_path", "days"]


def test_guarded_call_esegue_la_scansione_senza_conferma(vault, ambiente):
    """READ_ONLY: nessuna richiesta di conferma deve essere fatta."""
    richieste = []

    def conferma(nome, args):
        richieste.append(nome)
        return "no"

    esito = policy.guarded_call(
        "scansiona_vault", registry.AZIONI_MAP["scansiona_vault"],
        {"vault_path": str(vault), "days": 90}, confirm=conferma)

    assert richieste == [], "un'azione READ_ONLY non deve chiedere conferma"
    assert "Note scansionate: 8" in esito
    audit = Path(policy.AUDIT_PATH).read_text(encoding="utf-8")
    assert '"scansiona_vault"' in audit and '"read-only"' in audit


def test_guarded_call_scrive_il_report_through_policy(vault, ambiente):
    esito = policy.guarded_call(
        "proponi_pulizia_vault", registry.AZIONI_MAP["proponi_pulizia_vault"],
        {"vault_path": str(vault)}, confirm=lambda n, a: "no")

    report = kc.DATA_DIR / f"vault_proposals_{datetime.now().strftime('%Y-%m-%d')}.md"
    assert report.exists()
    assert str(report) in esito
    audit = Path(policy.AUDIT_PATH).read_text(encoding="utf-8")
    assert '"outcome": "executed"' in audit
    assert '"confirmed": null' in audit      # nessuna conferma per il read-only
