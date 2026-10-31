# -*- coding: utf-8 -*-
"""test_computer_agent.py — R6: Computer Use cross-app con MOCK.

Nessuna app viene aperta: `_open_window` e il backend pywinauto sono sostituiti
da finestre finte. Copre:
  * whitelist (default, override env, normalizzazione, rifiuto);
  * inspect_window: controlli, dedup, troncamento, redazione secret, errori puliti;
  * click_element: successo, elemento assente, elemento ambiguo, input invalido;
  * type_text: successo, escaping metacaratteri, rifiuto Invio/Tab/testo lungo;
  * policy: ispezione SENSITIVE, click/digitazione IRREVERSIBLE, conferma sempre;
  * registro AZIONI_MAP e metadati del LLM;
  * assenza di catture schermo nel modulo.
"""
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import computer_agent as ca
import policy


# ─────────────────────────────────────────────────────────────────────────────
#  Doppioni di pywinauto (nessuna finestra reale)
# ─────────────────────────────────────────────────────────────────────────────
class FakeControl:
    def __init__(self, name="", control_type="", automation_id=""):
        self.element_info = SimpleNamespace(
            name=name, control_type=control_type, automation_id=automation_id)
        self.clicks = 0
        self.click_error = None

    def click_input(self):
        if self.click_error:
            raise self.click_error
        self.clicks += 1


class FakeWindow:
    def __init__(self, title="", controls=None):
        self.title = title
        self.controls = list(controls or [])
        self.typed = []
        self.focus_count = 0
        self.type_error = None

    def window_text(self):
        return self.title

    def descendants(self):
        return list(self.controls)

    def set_focus(self):
        self.focus_count += 1

    def type_keys(self, keys, **kwargs):
        if self.type_error:
            raise self.type_error
        self.typed.append((keys, kwargs))


class FakeSpec:
    """WindowSpecification finta: exists() controllato, resto delegato."""

    def __init__(self, window, visible=True):
        self._window = window
        self._visible = visible

    def exists(self, timeout=None):
        return self._visible

    def __getattr__(self, name):
        return getattr(self._window, name)


class FakeApp:
    def __init__(self, window, visible=True):
        self._window = window
        self._visible = visible

    def top_window(self):
        return FakeSpec(self._window, self._visible)

    def window(self, **kwargs):
        self.window_kwargs = kwargs
        return FakeSpec(self._window, self._visible)


class FakeApplication:
    """Sostituisce pywinauto.Application e registra i criteri di connect.

    connect(process=...) -> nessuna finestra UIA (come un'app UWP);
    connect(title_re=...) -> finestra trovata.
    """

    def __init__(self, window, log):
        self._window = window
        self.log = log

    def connect(self, **kwargs):
        self.log.append(dict(kwargs))
        return FakeApp(self._window, visible="title_re" in kwargs)


@pytest.fixture
def nota(monkeypatch):
    """Blocco Note finto + whitelist di default, nessun processo reale."""
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    window = FakeWindow("Senza titolo - Blocco note", [
        FakeControl("File", "MenuItem"),
        FakeControl("Ciao Clio", "Document"),
        FakeControl("", "Pane"),                     # senza nome: ignorato
    ])
    monkeypatch.setattr(
        ca, "_open_window",
        lambda app: ca._WindowAdapter(window, ca.normalize_app_name(app)))
    return window


@pytest.fixture
def audit_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "AUDIT_PATH", str(tmp_path / "security.log"))
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
#  Whitelist
# ─────────────────────────────────────────────────────────────────────────────
def test_whitelist_default(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    assert ca.allowed_apps() == frozenset(
        {"code.exe", "notepad.exe", "antigravity.exe"})


def test_whitelist_da_env(monkeypatch):
    monkeypatch.setenv(ca.ENV_ALLOWED_APPS, " Notepad.EXE , calc.exe ,,")
    assert ca.allowed_apps() == frozenset({"notepad.exe", "calc.exe"})
    assert ca.is_allowed_app("notepad") is True
    assert ca.is_allowed_app("NOTEPAD.EXE") is True
    assert ca.is_allowed_app("calc") is True
    assert ca.is_allowed_app("evil.exe") is False


def test_whitelist_vuota_nega_tutto(monkeypatch):
    monkeypatch.setenv(ca.ENV_ALLOWED_APPS, ",, ,")
    assert ca.allowed_apps() == frozenset()
    assert ca.is_allowed_app("notepad.exe") is False


def test_normalizza_app_name(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    assert ca.normalize_app_name("Notepad") == "notepad.exe"
    assert ca.normalize_app_name(" C:/Windows/System32/NOTEPAD.EXE ") == "notepad.exe"
    assert ca.normalize_app_name('"Code.exe"') == "code.exe"
    assert ca.normalize_app_name("windowsapps.notepad") == "notepad.exe"
    with pytest.raises(ca.ComputerUseError):
        ca.normalize_app_name("   ")
    with pytest.raises(ca.ComputerUseError):
        ca.normalize_app_name(None)


def test_alias_calcolatrice_e_whitelist(monkeypatch):
    monkeypatch.setenv(ca.ENV_ALLOWED_APPS, "calculator")
    assert ca.allowed_apps() == frozenset({"calc.exe"})
    assert ca.normalize_app_name("Calculator.exe") == "calc.exe"
    assert ca.normalize_app_name("calcolatrice") == "calc.exe"
    assert ca.is_allowed_app("calc.exe") is True
    assert ca.is_allowed_app("calcolatrice") is True
    assert ca.is_allowed_app("notepad.exe") is False  # whitelist sostituita


def test_pattern_titolo_per_notepad_e_calcolatrice():
    notepad = ca._title_patterns("notepad.exe")
    assert any("Blocco Note" in pattern for pattern in notepad)
    assert any("Notepad" in pattern for pattern in notepad)
    calcolatrice = ca._title_patterns("calc.exe")
    assert any("Calcolatrice" in pattern for pattern in calcolatrice)
    assert any("Calculator" in pattern for pattern in calcolatrice)
    for pattern in notepad + calcolatrice:
        re.compile(pattern)  # sintassi regex valida per pywinauto


def test_candidate_pids_vuoto_se_processo_assente(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil",
                        SimpleNamespace(process_iter=lambda attrs: []))
    assert ca._candidate_pids("notepad.exe") == []


def test_candidate_pids_accetta_lo_stem_senza_exe(monkeypatch):
    processi = [SimpleNamespace(info={"name": "Notepad", "create_time": 10},
                                pid=7),
                SimpleNamespace(info={"name": "notepad.exe", "create_time": 5},
                                pid=9)]
    monkeypatch.setitem(sys.modules, "psutil",
                        SimpleNamespace(process_iter=lambda attrs: processi))
    assert ca._candidate_pids("notepad.exe") == [7, 9]


def test_inspect_fallback_per_titolo_se_psutil_non_trova(monkeypatch):
    """Caso Windows 11/UWP: psutil non trova nulla, si connette per titolo."""
    monkeypatch.setenv(ca.ENV_ALLOWED_APPS, "notepad")
    monkeypatch.setattr(ca, "_candidate_pids", lambda exe: [])
    window = FakeWindow("Senza titolo - Blocco note",
                        [FakeControl("Testo", "Document")])
    log = []
    monkeypatch.setattr(
        ca, "_backend", lambda: (lambda backend="uia": FakeApplication(window, log)))

    esito = ca.inspect_window("notepad.exe")

    assert "Blocco note" in esito and "- Testo [Document]" in esito
    assert len(log) == 1 and "title_re" in log[0]
    assert "Blocco Note" in log[0]["title_re"]
    assert log[0]["timeout"] == ca.CONNECT_TIMEOUT
    assert "process" not in log[0]


def test_fallback_per_titolo_dopo_pid_senza_finestra(monkeypatch):
    """Il PID esiste ma non espone una finestra UIA: si ripiega sul titolo."""
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(ca, "_candidate_pids", lambda exe: [4242])
    window = FakeWindow("Senza titolo - Blocco note", [FakeControl("Testo")])
    log = []
    monkeypatch.setattr(
        ca, "_backend", lambda: (lambda backend="uia": FakeApplication(window, log)))

    assert ca.type_text("notepad.exe", "ciao") is True

    assert log[0] == {"process": 4242, "timeout": ca.CONNECT_TIMEOUT}
    assert "title_re" in log[1]
    assert window.typed and window.typed[-1][0] == "ciao"


def test_fallback_per_titolo_fallito_errore_pulito(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(ca, "_candidate_pids", lambda exe: [])

    class BoomApplication:
        def __init__(self, backend="uia"):
            self.backend = backend

        def connect(self, **kwargs):
            raise RuntimeError("finestra assente")

    monkeypatch.setattr(
        ca, "_backend", lambda: (lambda backend="uia": BoomApplication(backend)))

    esito = ca.inspect_window("notepad.exe")
    assert esito.startswith("ERRORE:") and "non raggiungibile" in esito
    assert "Traceback" not in esito


@pytest.mark.parametrize("azzione,invoca", [
    ("inspect", lambda app: ca.inspect_window(app)),
    ("click", lambda app: ca.click_element(app, "File")),
    ("type", lambda app: ca.type_text(app, "ciao")),
])
def test_app_fuori_whitelist_rifiutata_senza_toccare_il_backend(
        monkeypatch, azzione, invoca):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    chiamate = []
    monkeypatch.setattr(ca, "_open_window", lambda app: chiamate.append(app))

    esito = invoca("calc.exe")

    assert chiamate == [], f"{azzione}: il backend non deve essere aperto"
    if azzione == "inspect":
        assert esito.startswith("ERRORE:") and "non consentita" in esito.lower()
    else:
        assert esito is False


def test_inspect_non_esegue_la_whitelist_del_chiamante(monkeypatch):
    """Difesa in profondita': anche chiamando _open_window l'app resta filtrata."""
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    with pytest.raises(ca.ComputerUseError, match="non consentita"):
        ca._open_window("calc.exe")


# ─────────────────────────────────────────────────────────────────────────────
#  inspect_window
# ─────────────────────────────────────────────────────────────────────────────
def test_inspect_legge_titolo_e_controlli(nota):
    esito = ca.inspect_window("notepad.exe")
    assert "App: notepad.exe" in esito
    assert "Finestra: Senza titolo - Blocco note" in esito
    assert "- File [MenuItem]" in esito
    assert "- Ciao Clio [Document]" in esito
    assert "Pane" not in esito          # controllo senza nome: escluso


def test_inspect_deduplica_i_controlli(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    window = FakeWindow("x", [FakeControl("Salva", "Button", "save"),
                              FakeControl("salva", "Button", "save")])
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    esito = ca.inspect_window("notepad.exe")
    assert esito.count("Salva") == 1


def test_inspect_tronca_oltre_il_limite(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    controlli = [FakeControl(f"Voce {i}", "ListItem") for i in range(ca.MAX_CONTROLS + 20)]
    window = FakeWindow("grande", controlli)
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    esito = ca.inspect_window("notepad.exe")
    assert f"({ca.MAX_CONTROLS}+)" in esito
    assert "troncato" in esito
    assert "Voce " + str(ca.MAX_CONTROLS) not in esito


def test_inspect_redige_i_secret(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    segreto = "sk_" + "a" * 24
    window = FakeWindow("x", [FakeControl(f"token {segreto}")])
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    esito = ca.inspect_window("notepad.exe")
    assert segreto not in esito
    assert "REDACTED" in esito


def test_inspect_app_non_avviata_errore_pulito(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(ca, "_open_window", lambda app: (_ for _ in ()).throw(
        ca.ComputerUseError("notepad.exe non e' in esecuzione: avvia l'app e riprova")))
    esito = ca.inspect_window("notepad.exe")
    assert esito.startswith("ERRORE:")
    assert "non e' in esecuzione" in esito
    assert "Traceback" not in esito


def test_inspect_pywinauto_mancante_errore_pulito(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(ca, "_backend", lambda: (_ for _ in ()).throw(
        ca.ComputerUseError("pywinauto non disponibile: installa pywinauto")))
    esito = ca.inspect_window("notepad.exe")
    assert esito.startswith("ERRORE:") and "pywinauto" in esito


# ─────────────────────────────────────────────────────────────────────────────
#  click_element
# ─────────────────────────────────────────────────────────────────────────────
def test_click_successo(nota):
    assert ca.click_element("notepad.exe", "File") is True
    assert nota.controls[0].clicks == 1
    assert nota.focus_count >= 1


def test_click_elemento_assente_non_clicca(nota):
    assert ca.click_element("notepad.exe", "Salva") is False
    assert all(control.clicks == 0 for control in nota.controls)


def test_click_elemento_ambiguo_non_clicca(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    doppi = [FakeControl("OK", "Button"), FakeControl("OK", "Button")]
    window = FakeWindow("x", doppi)
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))

    assert ca.click_element("notepad.exe", "OK") is False
    assert all(control.clicks == 0 for control in doppi)
    with pytest.raises(ca.ComputerUseError, match="ambiguo"):
        ca._click("notepad.exe", "OK")


def test_click_per_automation_id(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    pulsante = FakeControl("", "Button", "save-button")
    window = FakeWindow("x", [pulsante])
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    assert ca.click_element("notepad.exe", "save-button") is True
    assert pulsante.clicks == 1


def test_click_nome_elemento_non_valido(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    chiamate = []
    monkeypatch.setattr(ca, "_open_window", lambda app: chiamate.append(app))
    assert ca.click_element("notepad.exe", "   ") is False
    assert ca.click_element("notepad.exe", "x" * (ca.MAX_ELEMENT_NAME + 1)) is False
    assert chiamate == []


def test_click_errore_del_controllo_ritorna_false(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    controlli = [FakeControl("File", "MenuItem")]
    controlli[0].click_error = RuntimeError("elemento non cliccabile")
    window = FakeWindow("x", controlli)
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    assert ca.click_element("notepad.exe", "File") is False


# ─────────────────────────────────────────────────────────────────────────────
#  type_text
# ─────────────────────────────────────────────────────────────────────────────
def test_type_text_successo(nota):
    assert ca.type_text("notepad.exe", "Ciao da Clio, sono pronto.") is True
    keys, kwargs = nota.typed[-1]
    assert keys == "Ciao da Clio, sono pronto."
    assert kwargs.get("with_spaces") is True
    assert nota.focus_count >= 1


def test_type_text_escapa_i_metacaratteri(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    window = FakeWindow("x", [])
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    assert ca.type_text("notepad.exe", "a+b{c}") is True
    assert window.typed[-1][0] == "a{+}b{{}c{}}"
    assert ca._escape_for_keys("(x)[y]") == "{(}x{)}{[}y{]}"


@pytest.mark.parametrize("testo", ["", "   ", "riga1\nriga2", "tab\tqui",
                                   "x" * (ca.MAX_TEXT_LENGTH + 1)])
def test_type_text_input_rifiutato_senza_aprire_il_backend(monkeypatch, testo):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    chiamate = []
    monkeypatch.setattr(ca, "_open_window", lambda app: chiamate.append(app))
    assert ca.type_text("notepad.exe", testo) is False
    assert chiamate == []


def test_type_text_errore_backend_ritorna_false(monkeypatch):
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    window = FakeWindow("x", [])
    window.type_error = RuntimeError("finestra non attiva")
    monkeypatch.setattr(ca, "_open_window",
                        lambda app: ca._WindowAdapter(window, "notepad.exe"))
    assert ca.type_text("notepad.exe", "ciao") is False


# ─────────────────────────────────────────────────────────────────────────────
#  Policy (control-plane)
# ─────────────────────────────────────────────────────────────────────────────
def test_policy_classifica_le_azioni_computer():
    assert policy.classify("ispeziona_finestra") == "sensitive"
    assert policy.classify("clicca_elemento") == "irreversibile"
    assert policy.classify("scrivi_testo_app") == "irreversibile"
    # fail-safe invariato per azioni sconosciute
    assert policy.classify("azione_ignota_xyz") == "irreversibile"


@pytest.mark.parametrize("azione,args", [
    ("ispeziona_finestra", {"app_name": "notepad.exe"}),
    ("clicca_elemento", {"app_name": "notepad.exe", "element_name": "File"}),
    ("scrivi_testo_app", {"app_name": "notepad.exe", "text": "ciao"}),
])
def test_guarded_call_chiede_conferma_e_esegue(audit_tmp, azione, args):
    richieste, eseguite = [], []

    def finto(**kwargs):
        eseguite.append(kwargs)
        return "ESEGUITO"

    def conferma(nome, argomenti):
        richieste.append(nome)
        return "yes"

    assert policy.guarded_call(azione, finto, args, confirm=conferma) == "ESEGUITO"
    assert richieste == [azione]
    assert eseguite == [args]


def test_guarded_call_rifiuto_non_esegue(audit_tmp):
    eseguite = []

    def finto(**kwargs):
        eseguite.append(kwargs)
        return "ESEGUITO"

    esito = policy.guarded_call(
        "clicca_elemento", finto,
        {"app_name": "notepad.exe", "element_name": "File"},
        confirm=lambda nome, argomenti: "no")

    assert "annullata" in esito.lower()
    assert eseguite == []


def test_guarded_call_timeout_non_esegue(audit_tmp):
    eseguite = []

    def finto(**kwargs):
        eseguite.append(kwargs)
        return "ESEGUITO"

    esito = policy.guarded_call(
        "scrivi_testo_app", finto,
        {"app_name": "notepad.exe", "text": "ciao"},
        confirm=lambda nome, argomenti: "timeout")

    assert "scaduta" in esito.lower()
    assert eseguite == []


def test_guarded_call_computer_rispetta_la_whitelist(audit_tmp, monkeypatch):
    """Il control-plane non bypassa il modulo: app fuori whitelist -> nessun click."""
    monkeypatch.delenv(ca.ENV_ALLOWED_APPS, raising=False)
    monkeypatch.setattr(ca, "_open_window", lambda app: (_ for _ in ()).throw(
        AssertionError("non deve aprire finestre")))
    esito = policy.guarded_call(
        "clicca_elemento", ca.click_element,
        {"app_name": "calc.exe", "element_name": "7"},
        confirm=lambda nome, argomenti: "yes")
    assert esito is False


# ─────────────────────────────────────────────────────────────────────────────
#  Integrazione con registro e LLM
# ─────────────────────────────────────────────────────────────────────────────
def test_registro_espone_le_azioni_computer():
    import registry

    assert registry.AZIONI_MAP["ispeziona_finestra"] is ca.inspect_window
    assert registry.AZIONI_MAP["clicca_elemento"] is ca.click_element
    assert registry.AZIONI_MAP["scrivi_testo_app"] is ca.type_text


def test_metadati_llm_espongono_le_azioni_computer():
    jarvis = pytest.importorskip("jarvis_brain")
    for nome in ("ispeziona_finestra", "clicca_elemento", "scrivi_testo_app"):
        assert nome in jarvis.TOOLS_METADATA
        assert jarvis.TOOLS_METADATA[nome]["desc"]


# ─────────────────────────────────────────────────────────────────────────────
#  Vincoli del task
# ─────────────────────────────────────────────────────────────────────────────
def test_modulo_senza_catture_schermo_o_video():
    sorgente = Path(ca.__file__).read_text(encoding="utf-8")
    for vietato in ("ImageGrab", "pyautogui", "import mss", "cv2",
                    ".mp4", "VideoCapture", "screenshot("):
        assert vietato not in sorgente


def test_nessun_backend_reale_all_import():
    """L'import del modulo non carica pywinauto: l'import e' pigro."""
    assert not hasattr(ca, "Application")
    assert "pywinauto" not in getattr(ca, "__dict__", {})
