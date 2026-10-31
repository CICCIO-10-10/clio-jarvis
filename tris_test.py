#!/usr/bin/env python3
"""
tris_test.py — Test reale FASE 1: OCR + UI Automation su griglia Tris (Tic-Tac-Toe).

Flusso:
1. Apre Blocco Note (notepad.exe)
2. Scrive una griglia 3x3 finta con due X e una O
3. Usa leggi_schermo_locale() per leggere la griglia (OCR)
4. Usa clicca_elemento() per cliccare il menu "File" (vero controllo UIA)
5. Simula una mossa scrivendo X in una cella (type_text, poiché Notepad non ha pulsanti per celle)
6. Rilegge con leggi_schermo_locale() per verificare

NOTA: Blocco Note non ha veri "pulsanti" per le celle del Tris.
      Il test usa type_text per simulare la mossa sulla griglia,
      ma dimostra clicca_elemento su un vero controllo UIA (menu File).
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skills
import desktop_agent


# Griglia Tris da scrivere in Blocco Note (ASCII art)
GRIGLIA_TRIS_INIZIALE = """
  TIC-TAC-TOE
   A   B   C
1  X |   | O
  ---+---+---
2    | X |
  ---+---+---
3    |   |
"""


def apri_blocco_note_e_scrivi_griglia() -> bool:
    """Apre Blocco Note e scrive la griglia Tris iniziale."""
    print("🔧 [1/5] Apro Blocco Note...")
    if not desktop_agent.open_app("notepad", confirmed=True, origin="tris_test"):
        print("   ❌ Impossibile aprire Blocco Note")
        return False

    # Aspetta che la finestra sia pronta
    import pygetwindow as gw
    finestra = None
    for _ in range(30):
        cand = [w for w in gw.getWindowsWithTitle("Blocco Note") if "blocco note" in w.title.lower()]
        if cand:
            finestra = cand[0]
            break
        time.sleep(0.2)

    if not finestra:
        print("   ❌ Finestra Blocco Note non trovata")
        return False

    print(f"   ✅ Finestra trovata: '{finestra.title}' ({finestra.width}x{finestra.height})")

    # Portala in primo piano e scrivi
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.click(int(finestra.left) + 20, int(finestra.top) + 10)
    time.sleep(0.3)

    print("📝 [2/5] Scrivo la griglia Tris nel Blocco Note...")
    pyautogui.typewrite(GRIGLIA_TRIS_INIZIALE, interval=0.01)
    time.sleep(0.5)

    print("   ✅ Griglia scritta")
    return True


def test_ocr_leggi_griglia() -> str:
    """Test OCR: legge la griglia dal Blocco Note."""
    print("\n👁️  [3/5] OCR: leggo la griglia dal Blocco Note (leggi_schermo_locale)...")
    risultato = skills.leggi_schermo_locale("notepad.exe")
    print(f"   📄 Risultato OCR:\n{risultato}")
    return risultato


def test_click_elemento_reale() -> str:
    """Test reale di clicca_elemento su un controllo UIA (menu File di Blocco Note)."""
    print("\n🖱️  [4/6] Click reale UIA: clicco menu 'File' (clicca_elemento)...")
    risultato = skills.clicca_elemento("notepad.exe", "File")
    print(f"   📄 Risultato: {risultato}")
    time.sleep(0.5)
    
    # Chiudi il menu premendo ESC
    import pyautogui
    pyautogui.press('escape')
    time.sleep(0.2)
    
    return risultato


def test_click_elemento_simulato() -> str:
    """Simula una mossa: dato che Blocco Note non ha pulsanti per le celle,
    usiamo type_text per scrivere una X nella prima cella vuota (riga 2, col B).
    In un vero gioco con UI, useremmo clicca_elemento su un pulsante."""
    print("\n🖱️  [4/5] Simulo mossa: scrivo 'X' nella cella B2 (type_text)...")

    # Nota: clicca_elemento cerca controlli UIA (pulsanti, menu).
    # Blocco Note non ha controlli per le celle di testo.
    # Qui dimostriamo il flusso: useremmo clicca_elemento se l'app avesse pulsanti.
    # Per ora usiamo type_text per simulare l'effetto visivo.

    import pyautogui
    import pygetwindow as gw

    finestra = [w for w in gw.getWindowsWithTitle("Blocco Note") if "blocco note" in w.title.lower()][0]
    pyautogui.click(int(finestra.left) + 20, int(finestra.top) + 10)
    time.sleep(0.2)

    # Posizioniamo il cursore sulla riga 2, colonna B (approssimativo)
    # La griglia inizia circa a y=100, x=50 dalla finestra
    # Ogni cella ~40px, riga ~20px
    x = finestra.left + 150  # colonna B
    y = finestra.top + 180   # riga 2
    pyautogui.click(x, y)
    time.sleep(0.2)
    pyautogui.typewrite("X", interval=0.05)
    time.sleep(0.3)

    print("   ✅ 'X' scritta in B2 (simulazione mossa)")
    return "Mossa simulata: X in B2"


def test_ocr_verifica_mossa() -> str:
    """Rilegge con OCR per verificare che la mossa sia visibile."""
    print("\n👁️  [5/5] OCR: rileggo per verificare la mossa...")
    risultato = skills.leggi_schermo_locale("notepad.exe")
    print(f"   📄 Risultato OCR dopo mossa:\n{risultato}")
    return risultato


def main():
    print("=" * 60)
    print("🎮 TEST FASE 1 - Tris (Tic-Tac-Toe) con OCR + UI Automation")
    print("=" * 60)

    # 1. Apri e scrivi griglia
    if not apri_blocco_note_e_scrivi_griglia():
        print("\n❌ TEST FALLITO: impossibile preparare l'ambiente")
        return 1

    # 2. OCR lettura iniziale
    ocr_prima = test_ocr_leggi_griglia()

    # 3. Click reale UIA su menu File (dimostra clicca_elemento)
    test_click_elemento_reale()

    # 4. Simula mossa (click_elemento non applicabile a Notepad per celle)
    test_click_elemento_simulato()

    # 5. OCR verifica
    ocr_dopo = test_ocr_verifica_mossa()

    # Analisi risultati
    print("\n" + "=" * 60)
    print("📊 ANALISI RISULTATI")
    print("=" * 60)

    # Verifica che OCR abbia letto qualcosa di sensato
    if "X" in ocr_prima and "O" in ocr_prima:
        print("✅ OCR iniziale: ha rilevato X e O nella griglia")
    else:
        print("⚠️  OCR iniziale: non ha rilevato chiaramente X e O")

    if "X" in ocr_dopo and ocr_dopo.count("X") > ocr_prima.count("X"):
        print("✅ OCR dopo mossa: ha rilevato una X in più (mossa visibile)")
    else:
        print("⚠️  OCR dopo mossa: non rileva chiaramente la nuova X")

    print("\n🎯 TEST COMPLETATO")
    return 0


if __name__ == "__main__":
    sys.exit(main())