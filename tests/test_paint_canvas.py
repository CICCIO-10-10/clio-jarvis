"""Test del rilevamento del foglio bianco di Paint (FASE 1 · A+B+C).

Nessun test tocca lo schermo reale: `_trova_canvas` è pura e la cattura viene
esercitata con PyAutoGUI/mss sostituiti. Il test "nessun file scritto" blocca
una regressione privacy: la cattura deve restare in RAM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import skills  # noqa: E402


# ── helper: immagini sintetiche ──────────────────────────────────────────────
def _finestra_paint(foglio=None, toolbar=True, sfondo=170, misura=(900, 700)):
    immagine = Image.new("RGB", misura, (sfondo, sfondo, sfondo))
    disegno = ImageDraw.Draw(immagine)
    if toolbar:
        # barra strumenti chiara: NON deve essere scambiata per il foglio
        disegno.rectangle((0, 0, misura[0] - 1, 44), fill=(245, 245, 245))
    if foglio:
        disegno.rectangle(foglio, fill=(255, 255, 255))
    return immagine


# ── _trova_canvas: rilevamento deterministico ────────────────────────────────
def test_trova_foglio_bianco():
    canvas = skills._trova_canvas(_finestra_paint((250, 200, 650, 500)))
    assert canvas is not None
    assert abs(canvas.left - 250) <= 8
    assert abs(canvas.right - 650) <= 8
    assert abs(canvas.top - 200) <= 8
    assert abs(canvas.bottom - 500) <= 8
    assert canvas.larghezza >= skills.LARGHEZZA_MIN_CANVAS
    assert canvas.altezza >= skills.ALTEZZA_MIN_CANVAS


def test_toolbar_bianca_non_e_un_foglio():
    # Solo la barra strumenti chiara: nessun rettangolo abbastanza alto.
    assert skills._trova_canvas(_finestra_paint(None)) is None


def test_foglio_troppo_piccolo_rifiutato():
    assert skills._trova_canvas(_finestra_paint((400, 300, 520, 420))) is None


def test_schermo_tutto_nero_rifiutato():
    assert skills._trova_canvas(Image.new("RGB", (900, 700), (0, 0, 0))) is None


def test_immagine_minuscola_rifiutata():
    assert skills._trova_canvas(Image.new("RGB", (100, 80), "white")) is None


def test_solidita_rifiuta_rettangolo_mezzo_nero():
    """Un foglio vero ha quasi tutti i pixel bianchi: 50% nero non è un foglio."""
    immagine = _finestra_paint((250, 200, 650, 500))
    ImageDraw.Draw(immagine).rectangle((300, 250, 600, 450), fill=(0, 0, 0))
    assert skills._trova_canvas(immagine) is None


def test_disegno_piccolo_sul_foglio_non_invalida_il_canvas():
    """Un quadratino già disegnato (es. utente) non deve far fallire il foglio."""
    immagine = _finestra_paint((250, 200, 650, 500))
    ImageDraw.Draw(immagine).rectangle((300, 250, 340, 290), fill=(0, 0, 0))
    assert skills._trova_canvas(immagine) is not None


def test_pannello_scuro_sopra_il_foglio_non_sposta_il_canvas():
    """Regressione dal test reale: il pannello "dimensione pennello" della nuova
    Paint spezza le righe bianche a sinistra. La versione precedente sceglieva un
    rettangolo sbagliato (a volte la barra strumenti) e il quadrato finiva fuori
    dal foglio. Il massimo rettangolo deve restare il foglio vero."""
    immagine = Image.new("RGB", (1440, 779), (170, 170, 170))
    disegno = ImageDraw.Draw(immagine)
    disegno.rectangle((0, 0, 1439, 175), fill=(43, 43, 43))          # toolbar scura
    disegno.rectangle((110, 180, 1419, 739), fill=(255, 255, 255))   # foglio
    disegno.rounded_rectangle((20, 275, 110, 640), radius=16, fill=(43, 43, 43))

    canvas = skills._trova_canvas(immagine)

    assert canvas is not None
    assert canvas.top >= 176                    # non è la barra strumenti
    assert abs(canvas.left - 110) <= 8          # a destra del pannello
    assert abs(canvas.bottom - 740) <= 8        # recupera tutta l'altezza del foglio
    assert canvas.altezza >= 500                # prima si fermava a ~355 px


# ── _cattura_finestra_paint: solo finestra, nessun file su disco ─────────────
def _finestra_finta():
    return SimpleNamespace(left=100, top=100, width=800, height=600,
                           title="Senza titolo - Paint", isMinimized=False)


def test_cattura_resta_in_ram_e_nessun_file_scritto(monkeypatch, tmp_path):
    import pyautogui

    schermo = Image.new("RGB", (1600, 900), (200, 200, 200))
    ImageDraw.Draw(schermo).rectangle((100, 100, 899, 699), fill=(255, 255, 255))
    monkeypatch.setattr(pyautogui, "screenshot", lambda: schermo)
    monkeypatch.setattr(pyautogui, "size",
                        lambda: SimpleNamespace(width=1600, height=900))
    # mss "non disponibile": si deve usare il fallback screenshot+ritaglio.
    monkeypatch.setitem(sys.modules, "mss", None)

    repo_prima = {p.name for p in Path(skills.BASE_DIR).glob("*.png")}
    tmp_prima = {p.name for p in tmp_path.glob("*")}

    cattura = skills._cattura_finestra_paint(_finestra_finta())

    assert cattura is not None
    immagine, rect_px, scala_x, scala_y = cattura
    assert immagine.size == (800, 600)
    assert rect_px == (100, 100, 900, 700)
    assert (scala_x, scala_y) == (1.0, 1.0)
    assert {p.name for p in Path(skills.BASE_DIR).glob("*.png")} == repo_prima
    assert {p.name for p in tmp_path.glob("*")} == tmp_prima


def test_cattura_senza_finestra_valida_ritorna_none():
    assert skills._cattura_finestra_paint(SimpleNamespace(left=0, top=0,
                                                          width=0, height=0)) is None


# ── gate vision locale: solo sì/no, nessuna rete nei test ───────────────────
def _risposta(testo):
    return SimpleNamespace(json=lambda: {"response": testo})


def test_gate_vision_interpreta_si(monkeypatch):
    import requests
    chiamate = {}

    def finto_post(url, json=None, timeout=None):
        chiamate["url"] = url
        chiamate["json"] = json
        return _risposta("SI, c'è un foglio bianco")

    monkeypatch.setattr(requests, "post", finto_post)
    assert skills._vision_conferma_foglio(Image.new("RGB", (400, 300), "white")) is True
    assert chiamate["json"]["images"] and chiamate["json"]["stream"] is False
    assert chiamate["json"]["options"]["temperature"] == 0
    assert chiamate["json"]["options"]["num_predict"] == 8


def test_gate_vision_interpreta_no(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _risposta("NO"))
    assert skills._vision_conferma_foglio(Image.new("RGB", (400, 300), "black")) is False


def test_gate_vision_offline_non_solleva(monkeypatch):
    import requests

    def esplode(*a, **k):
        raise OSError("ollama offline")

    monkeypatch.setattr(requests, "post", esplode)
    assert skills._vision_conferma_foglio(Image.new("RGB", (400, 300), "white")) is False


# ── fallback cieco: spento di default, nessun tratto casuale ────────────────
def test_fallback_cieco_spento_di_default(monkeypatch):
    monkeypatch.delenv(skills.ENV_FALLBACK_CIECO, raising=False)
    assert skills._fallback_cieco_attivo() is False
    monkeypatch.setenv(skills.ENV_FALLBACK_CIECO, "1")
    assert skills._fallback_cieco_attivo() is True


def test_senza_canvas_non_disegna_e_lo_dice(monkeypatch):
    """Il caso chiave: niente foglio + fallback spento -> nessun mouseDown."""
    import pyautogui
    import pygetwindow

    monkeypatch.delenv(skills.ENV_FALLBACK_CIECO, raising=False)
    finestra = _finestra_finta()
    attivazioni = {"click": 0, "mouseDown": 0}

    monkeypatch.setattr(pygetwindow, "getWindowsWithTitle", lambda titolo: [finestra])
    monkeypatch.setattr(pygetwindow, "getActiveWindow", lambda: finestra)
    monkeypatch.setattr(pyautogui, "click", lambda *a, **k: attivazioni.__setitem__(
        "click", attivazioni["click"] + 1))
    monkeypatch.setattr(pyautogui, "mouseDown", lambda *a, **k: attivazioni.__setitem__(
        "mouseDown", attivazioni["mouseDown"] + 1))
    monkeypatch.setattr(skills.time, "sleep", lambda *a, **k: None)

    nero = Image.new("RGB", (800, 600), (0, 0, 0))
    monkeypatch.setattr(skills, "_cattura_finestra_paint", lambda w: (
        nero, (0, 0, 800, 600), 1.0, 1.0))
    monkeypatch.setattr(skills, "_vision_conferma_foglio", lambda immagine: False)

    esito = skills.disegna_su_paint(coordinate={})

    assert "non ho disegnato" in esito
    assert "disattivato" in esito
    assert attivazioni["mouseDown"] == 0
    assert attivazioni["click"] == 1  # ha comunque portato Paint in primo piano
