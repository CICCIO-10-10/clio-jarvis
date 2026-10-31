import os
import time
import socket
import requests
import pygetwindow as gw

# --- CONFIGURAZIONE ---
HUB_IP = "192.168.1.15" # INSERISCI IP DEL PC FISSO (HUB)
CLIO_API_KEY = os.getenv("CLIO_API_KEY", "")  # Deve coincidere con CLIO_API_KEY dell'Hub (.env) — non hardcodare
PC_NAME = socket.gethostname() # Es: "Portatile"

HUB_URL = f"http://{HUB_IP}:5000/api/lite_agent_sync"

def get_active_window():
    try:
        win = gw.getActiveWindow()
        return win.title if win else ""
    except:
        return ""

def main():
    print(f"🚀 Clio Agent Lite avviato su: {PC_NAME}")
    print(f"📡 Invio dati a: {HUB_URL}")
    last_window = ""

    while True:
        try:
            current_win = get_active_window()
            if current_win and current_win != last_window:
                headers = {"X-Clio-Key": CLIO_API_KEY, "Content-Type": "application/json"}
                data = {
                    "pc_name": PC_NAME,
                    "active_window": current_win
                }
                res = requests.post(HUB_URL, json=data, headers=headers, timeout=3)
                if res.status_code == 200:
                    print(f"✅ Sincronizzato: {current_win}")
                    last_window = current_win
                else:
                    print(f"❌ Errore Sync: {res.status_code}")
        except Exception as e:
            print(f"⚠️ Errore loop: {e}")
            
        time.sleep(5) # Controlla ogni 5 secondi

if __name__ == "__main__":
    main()
