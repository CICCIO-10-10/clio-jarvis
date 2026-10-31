"""test_proactive_assistant.py — R8: Proattività Quotidiana.

Regole rispettate:
  * nessuna notifica reale: `plyer` è sostituito da un finto backend (autouse);
  * vault finto in `tmp_path`, mai il vault reale;
  * nessun popup, nessuna app aperta, nessun log di ciò che l'utente guarda.
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import knowledge_curator as kc     # noqa: E402
import policy                      # noqa: E402
import proactive_assistant as pa   # noqa: E402
import registry                    # noqa: E402


# ── Fixture: backend notifiche finto (nessun popup reale) ────────────────────
@pytest.fixture(autouse=True)
def notifiche_finte(monkeypatch):
    """Sostituisce `plyer.notification` in sys.modules: zero popup reali."""
    inviate = []

    def notify(**kwargs):
        inviate.append(kwargs)

    finto = SimpleNamespace(notification=SimpleNamespace(notify=notify))
    monkeypatch.setitem(sys.modules, "plyer", finto)
    pa.reset_rate_limit()
    pa.clear_index_cache()
    yield inviate
    pa.reset_rate_limit()
    pa.clear_index_cache()


# ── Fixture: vault finto ─────────────────────────────────────────────────────
def _nota(root: Path, relativo: str, contenuto: str) -> Path:
    path = root / relativo
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contenuto, encoding="utf-8")
    return path


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    _nota(root, "30_Risorse/Obsidian - Sync e Second Brain.md",
          "# Obsidian - Sync e Second Brain\n\nCome sincronizzare il vault con "
          "OneDrive e usare i plugin.\n")
    _nota(root, "30_Risorse/Visual Studio Code tips.md",
          "# Visual Studio Code tips\n\nScorciatoie e impostazioni utili.\n")
    _nota(root, "10_Progetti/CLIO.md",
          "# CLIO\n\nCLIO è l'assistente personale, con memoria e control plane.\n")
    _nota(root, "Projects/Roast Room.md",
          "# Roast Room\n\nProgetto di clip e montaggi video.\n")
    _nota(root, "50_Giornale/Diario 2026-10.md",
          "# Diario 2026-10\n\nAppunti sparsi del mese.\n")
    return root


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    monkeypatch.delenv("CLIO_VAULT_PATH", raising=False)
    return tmp_path


# ── Finestra in primo piano ─────────────────────────────────────────────────
def test_get_foreground_window_ritorna_dizionario_di_stringhe():
    finestra = pa.get_foreground_window()

    assert set(finestra) == {"app", "title"}
    assert isinstance(finestra["app"], str)
    assert isinstance(finestra["title"], str)


def test_get_foreground_app_delega_alla_finestra(monkeypatch):
    monkeypatch.setattr(pa, "get_foreground_window",
                        lambda: {"app": "chrome.exe", "title": "X - Google Chrome"})
    assert pa.get_foreground_app() == "chrome.exe"


def test_get_foreground_app_vuota_se_non_determinabile(monkeypatch):
    monkeypatch.setattr(pa, "get_foreground_window",
                        lambda: {"app": "", "title": ""})
    assert pa.get_foreground_app() == ""


# ── Trigger ─────────────────────────────────────────────────────────────────
def test_browser_suggerisce_note_sul_tema(vault):
    suggerimenti = pa.check_triggers(
        "chrome.exe", str(vault), "Obsidian Sync - Google Chrome")

    assert suggerimenti, "atteso almeno un suggerimento"
    assert all(s.startswith("Nota sul tema") for s in suggerimenti)
    assert "Obsidian - Sync e Second Brain" in suggerimenti[0]
    assert "30_Risorse/Obsidian - Sync e Second Brain.md" in suggerimenti[0]
    assert len(suggerimenti) <= pa.MAX_SUGGESTIONS


def test_browser_argomento_non_presente_nessun_suggerimento(vault):
    assert pa.check_triggers("chrome.exe", str(vault),
                             "Meteo domani a Milano - Google Chrome") == []


def test_browser_titolo_vuoto_nessun_suggerimento(vault):
    assert pa.check_triggers("chrome.exe", str(vault), "") == []


def test_altro_browser_riconosciuto(vault):
    for app in ("msedge.exe", "firefox.exe", "brave.exe"):
        assert pa.check_triggers(app, str(vault), "Obsidian Sync"), f"{app} ignorato"


def test_editor_suggerisce_solo_note_di_progetto(vault):
    suggerimenti = pa.check_triggers(
        "code.exe", str(vault), "CLIO.md - Visual Studio Code")

    assert suggerimenti
    assert all(s.startswith("Nota di progetto") for s in suggerimenti)
    percorsi = " ".join(suggerimenti)
    assert "10_Progetti/CLIO.md" in percorsi
    # la nota fuori dai progetti non deve comparire, anche se il titolo combacia
    assert "30_Risorse" not in percorsi


def test_browser_invece_puo_suggerire_note_fuori_dai_progetti(vault):
    suggerimenti = pa.check_triggers(
        "chrome.exe", str(vault), "Visual Studio Code tips - Google Chrome")
    assert any("30_Risorse/Visual Studio Code tips.md" in s for s in suggerimenti)


def test_app_non_riconosciuta_non_produce_suggerimenti(vault):
    assert pa.check_triggers("explorer.exe", str(vault), "CLIO - Esplora file") == []
    assert pa.check_triggers("", str(vault), "CLIO") == []


@pytest.mark.parametrize("titolo", [
    "Obsidian Sync - Google Chrome (Incognito)",
    "Obsidian Sync - Navigazione privata",
    "Obsidian Sync - Private browsing",
    "Obsidian Sync - Modalità privata",
    "Obsidian Sync - InPrivate",
])
def test_privacy_nessun_suggerimento_in_incognito(vault, titolo):
    assert pa.check_triggers("chrome.exe", str(vault), titolo) == []


@pytest.mark.parametrize("titolo", [
    "Obsidian Sync - Google Chrome",
    "CLIO.md - Visual Studio Code",
    "Diario - Blocco note",
])
def test_titoli_normali_non_sono_trattati_come_privati(titolo):
    assert pa._is_private(titolo) is False


def test_incognito_non_finisce_nei_log(vault, caplog):
    caplog.set_level("DEBUG")
    pa.check_triggers("chrome.exe", str(vault),
                      "Obsidian Sync - Google Chrome (Incognito)")
    assert "Obsidian Sync" not in caplog.text
    assert "incognito" not in caplog.text.casefold()


def test_vault_inesistente_non_solleva(vault, tmp_path, caplog):
    caplog.set_level("DEBUG")
    assert pa.check_triggers("chrome.exe", str(tmp_path / "assente"),
                             "Obsidian Sync") == []
    assert "Obsidian" not in caplog.text     # nessun contenuto nei log


def test_titolo_letto_dalla_finestra_se_non_passato(vault, monkeypatch):
    monkeypatch.setattr(
        pa, "get_foreground_window",
        lambda: {"app": "chrome.exe", "title": "Obsidian Sync - Google Chrome"})
    assert pa.check_triggers("chrome.exe", str(vault))


def test_titolo_di_un_altra_app_non_viene_usato(vault, monkeypatch):
    monkeypatch.setattr(
        pa, "get_foreground_window",
        lambda: {"app": "explorer.exe", "title": "Obsidian Sync"})
    assert pa.check_triggers("chrome.exe", str(vault)) == []


def test_indice_del_vault_in_cache(vault, monkeypatch):
    assert pa._vault_notes(str(vault))

    def esplode(_vault):
        raise AssertionError("scan_vault non deve essere richiamato con cache calda")

    monkeypatch.setattr(kc, "scan_vault", esplode)
    assert pa.check_triggers("chrome.exe", str(vault), "Obsidian Sync")

    pa.clear_index_cache()
    assert pa.check_triggers("chrome.exe", str(vault), "Obsidian Sync") == []


def test_vault_non_modificato_dai_trigger(vault):
    def stato():
        return {p.relative_to(vault).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in sorted(vault.rglob("*")) if p.is_file()}

    prima = stato()
    pa.check_triggers("chrome.exe", str(vault), "Obsidian Sync - Google Chrome")
    pa.check_triggers("code.exe", str(vault), "CLIO.md - Visual Studio Code")
    assert stato() == prima


# ── Notifiche ───────────────────────────────────────────────────────────────
def test_show_notification_usa_plyer_e_tronca_il_messaggio(notifiche_finte):
    esito = pa.show_notification("nota " * 100)

    assert esito is True
    assert len(notifiche_finte) == 1
    inviata = notifiche_finte[0]
    assert inviata["title"] == "CLIO"
    assert inviata["app_name"] == "CLIO"
    assert len(inviata["message"]) <= pa.MAX_NOTIFICATION_CHARS


def test_show_notification_fallisce_senza_eccezioni(monkeypatch, caplog):
    caplog.set_level("DEBUG")

    class Rotto:
        @staticmethod
        def notify(**kwargs):
            raise RuntimeError("backend notifiche assente")

    monkeypatch.setitem(sys.modules, "plyer",
                        SimpleNamespace(notification=Rotto))
    assert pa.show_notification("Obsidian Sync") is False
    assert "Obsidian Sync" not in caplog.text   # mai il contenuto nei log


def test_show_notification_vuota_non_invia(notifiche_finte):
    assert pa.show_notification("   ") is False
    assert notifiche_finte == []


# ── Rate limit ──────────────────────────────────────────────────────────────
def test_rate_limit_una_notifica_ogni_30_minuti():
    inviate = []
    notifier = lambda testo: inviate.append(testo) or True
    t0 = 1000.0

    assert pa.notify_if_allowed("prima", now=t0, notifier=notifier) is True
    assert pa.notify_if_allowed("subito dopo", now=t0 + 60, notifier=notifier) is False
    assert pa.notify_if_allowed("quasi", now=t0 + pa.RATE_LIMIT_SECONDS - 1,
                                notifier=notifier) is False
    assert inviate == ["prima"]
    assert pa.notify_if_allowed("dopo", now=t0 + pa.RATE_LIMIT_SECONDS,
                                notifier=notifier) is True
    assert inviate == ["prima", "dopo"]


def test_rate_limit_30_minuti_di_default():
    assert pa.RATE_LIMIT_SECONDS == 30 * 60


def test_notifica_fallita_non_consuma_il_budget():
    notifier = lambda testo: False
    t0 = 5000.0

    assert pa.notify_if_allowed("ko", now=t0, notifier=notifier) is False
    assert pa.should_notify(now=t0) is True
    assert pa.notify_if_allowed("ok", now=t0, notifier=lambda testo: True) is True
    assert pa.should_notify(now=t0 + 10) is False


def test_notifica_vuota_non_consuma_il_budget():
    assert pa.notify_if_allowed("", now=100.0, notifier=lambda t: True) is False
    assert pa.should_notify(now=100.0) is True


def test_secondi_al_prossimo_suggerimento():
    t0 = 42.0
    assert pa.seconds_until_next_notification(now=t0) == 0.0
    assert pa.notify_if_allowed("x", now=t0, notifier=lambda t: True)
    assert pa.seconds_until_next_notification(now=t0 + 60) == pa.RATE_LIMIT_SECONDS - 60
    assert pa.seconds_until_next_notification(now=t0 + pa.RATE_LIMIT_SECONDS) == 0.0


# ── Monitor (tick, avvio, stop) ─────────────────────────────────────────────
def test_monitor_tick_notifica_una_volta_sola(vault):
    inviate = []
    notifier = lambda testo: inviate.append(testo) or True
    finestra = {"app": "chrome.exe", "title": "Obsidian Sync - Google Chrome"}

    primo = pa.monitor_tick(str(vault), window=finestra, notifier=notifier)
    secondo = pa.monitor_tick(str(vault), window=finestra, notifier=notifier)

    assert primo["suggestions"] and primo["notified"] is True
    assert len(inviate) == 1
    assert secondo["suggestions"] and secondo["notified"] is False


def test_monitor_tick_su_app_ignota_non_notifica(vault):
    inviate = []
    esito = pa.monitor_tick(str(vault), window={"app": "explorer.exe", "title": "CLIO"},
                            notifier=lambda testo: inviate.append(testo) or True)
    assert esito["suggestions"] == [] and esito["notified"] is False
    assert inviate == []


def test_monitor_tick_in_incognito_non_notifica_ne_logga(vault, caplog):
    caplog.set_level("DEBUG")
    inviate = []
    esito = pa.monitor_tick(
        str(vault),
        window={"app": "chrome.exe", "title": "Obsidian Sync - Google Chrome (Incognito)"},
        notifier=lambda testo: inviate.append(testo) or True)

    assert esito["suggestions"] == [] and esito["notified"] is False
    assert inviate == []
    assert "Obsidian" not in caplog.text


def test_monitor_tick_usa_la_finestra_corrente_se_non_indicata(vault, monkeypatch):
    monkeypatch.setattr(
        pa, "get_foreground_window",
        lambda: {"app": "chrome.exe", "title": "Obsidian Sync - Google Chrome"})
    inviate = []
    esito = pa.monitor_tick(str(vault), notifier=lambda testo: inviate.append(testo) or True)

    assert esito["app"] == "chrome.exe"
    assert esito["notified"] is True and len(inviate) == 1


def test_attiva_proattivita_avvia_un_solo_thread(vault, ambiente):
    try:
        esito = pa.attiva_proattivita(str(vault), interval_seconds=3600)
        assert esito.startswith("Proattività attiva")
        assert "30 minuti" in esito
        assert pa.proattivita_attiva() is True

        secondo = pa.attiva_proattivita(str(vault), interval_seconds=3600)
        assert "già attiva" in secondo
    finally:
        esito_stop = pa.ferma_proattivita(timeout=5)
    assert esito_stop == "Proattività fermata."
    assert pa.proattivita_attiva() is False


def test_ferma_proattivita_idempotente(vault, ambiente):
    assert pa.ferma_proattivita() == "Proattività non attiva."


def test_attiva_proattivita_valida_i_parametri(vault, ambiente, tmp_path):
    assert pa.attiva_proattivita(str(tmp_path / "assente")).startswith("ERRORE:")
    assert pa.attiva_proattivita(str(vault), interval_seconds=0).startswith("ERRORE:")
    assert pa.attiva_proattivita(str(vault), interval_seconds="boh").startswith("ERRORE:")
    assert pa.proattivita_attiva() is False


def test_attiva_proattivita_senza_vault_esplicito_usa_l_ambiente(vault, ambiente,
                                                                monkeypatch):
    monkeypatch.setenv("CLIO_VAULT_PATH", str(vault))
    try:
        assert pa.attiva_proattivita(interval_seconds=3600).startswith("Proattività attiva")
    finally:
        pa.ferma_proattivita(timeout=5)


# ── Integrazione col control-plane ──────────────────────────────────────────
def test_azione_classificata_read_only():
    assert policy.classify("attiva_proattivita") == "read-only"


def test_azione_registrata_e_richiamabile():
    assert "attiva_proattivita" in registry.AZIONI_MAP
    assert registry.AZIONI_MAP["attiva_proattivita"] is pa.attiva_proattivita


def test_metadati_llm():
    import jarvis_brain

    meta = jarvis_brain.TOOLS_METADATA["attiva_proattivita"]
    assert meta["params"] == ["vault_path", "interval_seconds"]
    assert "30 minuti" in meta["desc"]


def test_guarded_call_non_chiede_conferma_e_non_scarica_log_di_titoli(
        vault, ambiente, caplog):
    caplog.set_level("DEBUG")
    richieste = []

    def conferma(nome, args):
        richieste.append(nome)
        return "no"

    try:
        esito = policy.guarded_call(
            "attiva_proattivita", registry.AZIONI_MAP["attiva_proattivita"],
            {"vault_path": str(vault), "interval_seconds": 3600}, confirm=conferma)
    finally:
        pa.ferma_proattivita(timeout=5)

    assert richieste == [], "un'azione READ_ONLY non deve chiedere conferma"
    assert esito.startswith("Proattività attiva")
    audit = Path(policy.AUDIT_PATH).read_text(encoding="utf-8")
    assert '"attiva_proattivita"' in audit and '"read-only"' in audit
    assert '"confirmed": null' in audit
    # l'audit contiene il percorso del vault, mai titoli di finestre o note
    assert "Obsidian Sync" not in audit
    assert "Obsidian Sync" not in caplog.text
