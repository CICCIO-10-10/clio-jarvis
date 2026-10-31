import subprocess
import os
import sys
import logging

# Configurazione logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [SETUP] - %(levelname)s - %(message)s")
logger = logging.getLogger("ClioSetup")

def setup_task_scheduler():
    """Registra il watchdog di Clio nel Task Scheduler di Windows."""
    task_name = "ClioWatchdog"
    # Percorso assoluto dello script watchdog
    script_path = os.path.abspath("watchdog_clio.py")
    # Comando per schtasks: avvia al login, finestra nascosta (/IT), alta priorità
    # Usiamo 'pythonw.exe' per non mostrare la console se disponibile, altrimenti 'python.exe'
    python_exe = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(python_exe):
        python_exe = sys.executable

    # Comando schtasks
    # /Create: crea nuovo task
    # /SC ONLOGON: al login utente
    # /TN: nome task
    # /TR: comando da eseguire (pythonw script.py)
    # /IT: interactive (necessario per schermi/UI)
    # /F: force overwrite
    # /RL HIGHEST: privilegi massimi se disponibili
    
    cmd = [
        "schtasks", "/Create", 
        "/SC", "ONLOGON", 
        "/TN", task_name, 
        "/TR", f'"{python_exe}" "{script_path}"',
        "/IT", "/F", "/RL", "HIGHEST"
    ]

    try:
        logger.info(f"Registrazione task {task_name}...")
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"✅ Task registrato con successo: {result.stdout}")
        
        # Test immediato del task
        logger.info("Avvio immediato del task per verifica...")
        subprocess.run(["schtasks", "/Run", "/TN", task_name], check=True)
        logger.info("✅ Task avviato correttamente.")
        
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Errore durante la registrazione del task: {e.stderr}")
        print(f"\nAttenzione: Assicurati di eseguire questo script come AMMINISTRATORE.\n")
    except Exception as e:
        logger.error(f"❌ Errore imprevisto: {e}")

if __name__ == "__main__":
    # Verifica che watchdog_clio.py esista
    if not os.path.exists("watchdog_clio.py"):
        logger.error("❌ Errore: 'watchdog_clio.py' non trovato nella cartella corrente!")
        sys.exit(1)
        
    setup_task_scheduler()
