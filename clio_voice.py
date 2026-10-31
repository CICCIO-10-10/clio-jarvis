"""
CLIO - clio_voice.py (Modalità Vocale Interattiva STT + TTS)
Gestisce:
1. Input Vocale: Speech Recognition con Groq Whisper (o Google STT fallback)
2. Cervello: Elaborazione intelligente tramite jarvis_brain (e modulo skills.py)
3. Output Vocale: Sintesi vocale naturale in tempo reale tramite edge-tts + pygame
"""

import os
import sys
import time
import re
import asyncio
import tempfile
import logging
from pathlib import Path

# Fix encoding Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Percorsi base
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config
from jarvis_brain import jarvis

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ClioVoice")

# Librerie audio
import speech_recognition as sr
import edge_tts
import pygame

# Parametri voce da config (FIX 1: voce UNICA centralizzata in config.TTS_VOICE)
LINGUA_TTS = getattr(config, "TTS_VOICE", "it-IT-IsabellaNeural")
VOICE_RATE = getattr(config, "VOICE_RATE", "+8%")
VOICE_PITCH = getattr(config, "VOICE_PITCH", "-3Hz")

# Client Groq Whisper se disponibile
_groq_client = None
try:
    from groq import Groq
    if getattr(config, "GROQ_API_KEY", ""):
        _groq_client = Groq(api_key=config.GROQ_API_KEY)
except Exception as e:
    logger.warning(f"Groq Whisper client non disponibile: {e}")

# ─────────────────────────────────────────────────────────────
# SINTESI VOCALE (TTS)
# ─────────────────────────────────────────────────────────────
def _pulisci_testo_per_voce(testo: str) -> str:
    """Rimuove markdown, backtick e formattazione grezza per una lettura fluida."""
    t = re.sub(r"```.*?```", "", testo, flags=re.DOTALL)
    t = re.sub(r"[*_`#~\[\]\(\)\<\>]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

async def _genera_audio_edge(testo: str, file_path: str):
    """Genera audio mp3 con edge-tts."""
    comm = edge_tts.Communicate(testo, LINGUA_TTS, rate=VOICE_RATE, pitch=VOICE_PITCH)
    await comm.save(file_path)

def parla(testo: str):
    """Sintetizza il testo e lo riproduce tramite gli altoparlanti."""
    testo_pulito = _pulisci_testo_per_voce(testo)
    if not testo_pulito:
        return

    print(f"\n🔊 [Clio parla]: {testo_pulito}")

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # 1. Genera audio con edge-tts — SOLO la voce unica di config.TTS_VOICE.
        #    FIX 1: nessun fallback su gTTS/pyttsx3 (cambierebbero voce/timbro).
        #    In caso di errore si ritenta UNA volta con la stessa voce.
        try:
            asyncio.run(_genera_audio_edge(testo_pulito, tmp_path))
        except Exception as e_edge:
            logger.warning(f"edge-tts non riuscito ({e_edge}), nuovo tentativo con la stessa voce...")
            try:
                asyncio.run(_genera_audio_edge(testo_pulito, tmp_path))
            except Exception as e_retry:
                logger.error(f"TTS non disponibile (voce {LINGUA_TTS}): {e_retry}")
                return

        # 2. Riproduzione audio con pygame.mixer
        try:
            pygame.mixer.init()
            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.05)
        except Exception as e_play:
            logger.error(f"Errore riproduzione audio: {e_play}")
        finally:
            try:
                pygame.mixer.quit()
            except Exception:
                pass
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# RICONOSCIMENTO VOCALE (STT)
# ─────────────────────────────────────────────────────────────
def trascrivi_audio(recognizer: sr.Recognizer, audio: sr.AudioData) -> str:
    """Trascrive l'audio usando prima Groq Whisper V3 (velocissimo) poi Google STT."""
    # 1. Tentativo con Groq Whisper
    if _groq_client:
        try:
            wav_bytes = audio.get_wav_data(convert_rate=16000, convert_width=2)
            transcription = _groq_client.audio.transcriptions.create(
                file=("audio.wav", wav_bytes),
                model="whisper-large-v3-turbo",
                language="it"
            )
            testo = transcription.text.strip()
            if testo:
                return testo
        except Exception as gw_err:
            logger.debug(f"Groq Whisper fallback su Google: {gw_err}")

    # 2. Fallback su Google Speech Recognition
    try:
        return recognizer.recognize_google(audio, language="it-IT").strip()
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        logger.warning(f"Errore trascrizione Google: {e}")
        return ""


# ─────────────────────────────────────────────────────────────
# LOOP PRINCIPALE MODALITÀ VOCALE
# ─────────────────────────────────────────────────────────────
WAKE_WORDS = ("ehi clio", "hey clio", "clio", "ciao clio", "ok clio")

def avvia_modalita_vocale():
    """Loop di ascolto ed elaborazione vocale."""
    print("=" * 65)
    print("🎙️  C.L.I.O. - MODALITÀ VOCALE ATTIVA")
    print("=" * 65)
    print("• Pronuncia 'Ehi Clio' seguito dal comando (es: 'Ehi Clio, apri Spotify')")
    print("• Oppure parla direttamente quando vedi '🎙️  In ascolto...'")
    print("• Premi CTRL+C in qualsiasi momento per uscire")
    print("=" * 65)

    recognizer = sr.Recognizer()
    recognizer.pause_threshold = 0.8
    recognizer.dynamic_energy_threshold = True

    try:
        mic = sr.Microphone()
    except Exception as e:
        print(f"\n⚠️  Microfono non rilevato: {e}")
        print("Passaggio a modalità interattiva da tastiera (con risposta vocale TTS):")
        while True:
            try:
                domanda = input("\n👤 Tu (digita o 'esci'): ").strip()
                if not domanda or domanda.lower() in ("esci", "exit", "quit"):
                    break
                risposta = jarvis.think_and_act(domanda)
                print(f"🤖 Clio: {risposta}")
                parla(risposta)
            except (KeyboardInterrupt, EOFError):
                break
        return

    # Saluto iniziale vocale
    saluto = "Ciao Capo! Sono attiva in modalità vocale. Cosa posso fare per te?"
    print(f"\n🤖 Clio: {saluto}")
    parla(saluto)

    with mic as source:
        print("\n⚙️  Calibrazione rumore ambientale...")
        recognizer.adjust_for_ambient_noise(source, duration=1.0)
        print("✅ Pronto! Inizio ascolto continuo.\n")

        while True:
            try:
                print("\n🎙️  In ascolto... (parla adesso)")
                audio = recognizer.listen(source, timeout=8, phrase_time_limit=10)
                print("⏳ Elaborazione voce...")

                testo = trascrivi_audio(recognizer, audio)
                if not testo:
                    continue

                print(f"🗣️  Hai detto: \"{testo}\"")
                testo_lower = testo.lower().strip()

                # Verifica se contiene la wake word o se è un comando diretto
                ha_wake_word = any(w in testo_lower for w in WAKE_WORDS)
                comando = testo

                # Rimuovi la wake word se presente all'inizio
                if ha_wake_word:
                    for w in WAKE_WORDS:
                        if testo_lower.startswith(w):
                            comando = testo[len(w):].strip().lstrip(",").strip()
                            break

                if not comando:
                    # FIX 2: la wake word attiva SOLO l'ascolto del comando successivo.
                    # Non si chiama il cervello/LLM: niente pippone su CPU/RAM/stato.
                    saluto_wake = "Dimmi"
                    print(f"🤖 Clio: {saluto_wake}")
                    parla(saluto_wake)
                    continue

                # Esegui comando attraverso il cervello di Jarvis
                print(f"🧠 [Jarvis] Elaboro comando: '{comando}'...")
                risposta = jarvis.think_and_act(comando)
                print(f"🤖 Clio: {risposta}")

                # Parla la risposta
                parla(risposta)

            except sr.WaitTimeoutError:
                # Timeout silenzioso: continua ad ascoltare
                continue
            except KeyboardInterrupt:
                print("\n\n👋 Chiusura modalità vocale Clio. A presto Capo!")
                parla("A dopo Capo!")
                break
            except Exception as e:
                logger.error(f"Errore nel ciclo vocale: {e}")
                time.sleep(1)

if __name__ == "__main__":
    avvia_modalita_vocale()
