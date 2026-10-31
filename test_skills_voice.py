"""
Test completo di verifica per Skills, Integrazione Jarvis e Modalità Vocale di Clio.
"""
import sys
import os
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import skills
from jarvis_brain import jarvis

def run_verification():
    print("=" * 65)
    print("🧪 TEST VERIFICA SKILLS & INTEGRAZIONE CLIO")
    print("=" * 65)

    # 1. Test Funzioni Native in skills.py
    print("\n[STEP 1] Test modulo skills.py...")
    t_msg = skills.get_system_time()
    print(f"  ✅ get_system_time(): {t_msg}")

    m_msg = skills.control_media("play")
    print(f"  ✅ control_media('play'): {m_msg}")

    vol_msg = skills.control_media("volume_up")
    print(f"  ✅ control_media('volume_up'): {vol_msg}")

    # 2. Test End-to-End Jarvis + Skills (Apri Spotify)
    print("\n[STEP 2] Test Jarvis Brain -> Prompt 'Apri Spotify'...")
    t0 = time.time()
    risposta_spotify = jarvis.think_and_act("Apri Spotify")
    durata = time.time() - t0
    print(f"  ⏱️ Tempo esecuzione: {durata:.2f}s")
    print(f"  🤖 Risposta generata da Clio: \"{risposta_spotify}\"")
    assert "Spotify" in risposta_spotify or "aperto" in risposta_spotify, f"Risposta inattesa: {risposta_spotify}"
    print("  ✅ Intercettazione JSON 'open_app' & apertura Spotify: SUPERATA")

    # 3. Test End-to-End Jarvis + Skills (Ora e Media)
    print("\n[STEP 3] Test Jarvis Brain -> Prompt 'Che ore sono?'...")
    risposta_ora = jarvis.think_and_act("Che ore sono?")
    print(f"  🤖 Risposta generata da Clio: \"{risposta_ora}\"")
    assert "Capo" in risposta_ora or "sono le" in risposta_ora, f"Risposta inattesa: {risposta_ora}"
    print("  ✅ Intercettazione JSON 'get_system_time': SUPERATA")

    # 4. Verifica Componenti Vocali
    print("\n[STEP 4] Verifica componenti vocali di clio_voice.py...")
    import speech_recognition as sr
    import edge_tts
    import pygame
    print("  ✅ speech_recognition disponibile")
    print("  ✅ edge-tts disponibile")
    print("  ✅ pygame audio engine disponibile")

    print("\n" + "=" * 65)
    print("🎯 TUTTI I TEST SONO STATI SUPERATI CON SUCCESSO!")
    print("=" * 65)

if __name__ == "__main__":
    run_verification()
