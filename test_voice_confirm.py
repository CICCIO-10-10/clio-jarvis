# -*- coding: utf-8 -*-
"""test_voice_confirm.py — conferma vocale del control-plane (Buco 1).

Verifica `policy._voice_confirm` SENZA microfono, SENZA TTS e SENZA Telegram:
TTS e STT sono mockati, quindi il test è deterministico e offline.

Copre:
  - STT "sì"  -> "yes"  (azione eseguibile)
  - STT "no"  -> "no"   (azione annullata)
  - silenzio  -> "timeout" (azione NON eseguita)
  - Telegram configurato -> priorità a Telegram, niente prompt vocale
  - wiring: guarded_call(origin="voce") usa _voice_confirm
  - parsing delle risposte ambigue/negate (fail-safe)
"""
import pytest

import policy


@pytest.fixture
def voce(monkeypatch):
    """Mock TTS/STT: registra cosa viene detto e restituisce cosa 'sente'."""
    stato = {"detto": [], "risposta": ""}

    def finto_speak(testo):
        stato["detto"].append(testo)
        return True

    def finto_listen(timeout):
        stato["atteso"] = timeout
        return stato["risposta"]

    monkeypatch.setattr(policy, "_telegram_disponibile", lambda: False)
    monkeypatch.setattr(policy, "_voice_speak", finto_speak)
    monkeypatch.setattr(policy, "_voice_listen", finto_listen)
    return stato


# ── 1) Le tre risposte chiave ──────────────────────────────────────────────
def test_risposta_si_esegue(voce):
    voce["risposta"] = "sì"

    esito = policy._voice_confirm("esegui_coding_task", {"task_descrizione": "x"})

    assert esito == "yes"
    assert voce["detto"], "il TTS non ha chiesto nulla"
    assert "Confermi" in voce["detto"][0]
    assert "Di sì o no" in voce["detto"][0]
    assert "esegui coding task" in voce["detto"][0]   # underscore leggibili
    assert voce["atteso"] == policy.VOICE_CONFIRM_TIMEOUT


def test_risposta_no_annulla(voce):
    voce["risposta"] = "No, annulla"

    assert policy._voice_confirm("elimina_file", {}) == "no"


def test_silenzio_timeout(voce):
    voce["risposta"] = ""

    assert policy._voice_confirm("esegui_coding_task", {}) == "timeout"


def test_rumore_non_compreso_timeout(voce):
    voce["risposta"] = "boh, vediamo"

    assert policy._voice_confirm("esegui_coding_task", {}) == "timeout"


# ── 2) Telegram ha la priorità (nessun doppio prompt) ──────────────────────
def test_telegram_configurato_ha_priorita(monkeypatch):
    chiamate = {"tg": [], "voice": []}
    monkeypatch.setattr(policy, "_telegram_disponibile", lambda: True)
    monkeypatch.setattr(policy, "_telegram_confirm",
                        lambda n, a, message=None: chiamate["tg"].append(n) or "yes")
    monkeypatch.setattr(policy, "_voice_listen",
                        lambda t: chiamate["voice"].append(t) or "no")

    assert policy._voice_confirm("elimina_file", {}) == "yes"
    assert chiamate["tg"] == ["elimina_file"]
    assert chiamate["voice"] == []


# ── 3) Errori TTS/STT non devono mai abbattere CLIO ────────────────────────
def test_tts_rotto_non_esplode(monkeypatch):
    monkeypatch.setattr(policy, "_telegram_disponibile", lambda: False)

    def speak_rotto(testo):
        raise RuntimeError("edge-tts offline")

    monkeypatch.setattr(policy, "_voice_speak", speak_rotto)

    assert policy._voice_confirm("elimina_file", {}) == "timeout"


def test_stt_rotto_non_esplode(monkeypatch):
    monkeypatch.setattr(policy, "_telegram_disponibile", lambda: False)
    monkeypatch.setattr(policy, "_voice_speak", lambda testo: True)

    def listen_rotto(timeout):
        raise RuntimeError("microfono assente")

    monkeypatch.setattr(policy, "_voice_listen", listen_rotto)

    assert policy._voice_confirm("elimina_file", {}) == "timeout"


# ── 4) Parsing delle risposte (fail-safe) ──────────────────────────────────
@pytest.mark.parametrize("detto,atteso", [
    ("sì", "yes"),
    ("SÌ", "yes"),
    ("Si, confermo", "yes"),
    ("ok procedi", "yes"),
    ("va bene", "yes"),
    ("no", "no"),
    ("No, annulla", "no"),
    ("stop", "no"),
    ("non confermo", "no"),      # negazione -> non eseguire
    ("non lo so", "no"),         # ambiguo -> non eseguire
    ("boh", None),
    ("", None),
])
def test_normalizza_risposta(detto, atteso):
    assert policy._normalizza_risposta_voce(detto) == atteso


# ── 5) Wiring: l'origine decide il canale di conferma ──────────────────────
def test_guarded_call_origin_voce_usa_voice_confirm(monkeypatch):
    usato = []

    def finto_voice(nome, args):
        usato.append(("voice", nome))
        return "yes"

    def finto_tg(nome, args):
        usato.append(("telegram", nome))
        return "no"

    monkeypatch.setattr(policy, "_voice_confirm", finto_voice)
    monkeypatch.setattr(policy, "_telegram_confirm", finto_tg)

    esito = policy.guarded_call("esegui_coding_task", lambda: "fatto", {},
                                origin="voce")

    assert esito == "fatto"
    assert usato == [("voice", "esegui_coding_task")]


def test_guarded_call_origin_telegram_resta_su_telegram(monkeypatch):
    usato = []

    def finto_voice(nome, args):
        usato.append(("voice", nome))
        return "yes"

    def finto_tg(nome, args):
        usato.append(("telegram", nome))
        return "no"

    monkeypatch.setattr(policy, "_voice_confirm", finto_voice)
    monkeypatch.setattr(policy, "_telegram_confirm", finto_tg)

    esito = policy.guarded_call("esegui_coding_task", lambda: "fatto", {},
                                origin="telegram")

    assert "annullata" in esito.lower()
    assert usato == [("telegram", "esegui_coding_task")]


def test_origin_voice_e_voce_equivalenti():
    assert "voice" in policy.VOICE_ORIGINS
    assert "voce" in policy.VOICE_ORIGINS
