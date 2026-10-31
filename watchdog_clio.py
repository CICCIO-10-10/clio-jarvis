import subprocess
import time
import sys
import os
import logging

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [WATCHDOG] - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/watchdog.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ClioWatchdog")

PROCESS_SCRIPTS = ["run_clio.py"]  # STEP 5: processo unico (Flask+Telegram nello stesso processo)
RESTART_DELAY = 30  # Secondi di attesa prima del riavvio

def start_process(script_name):
    """Avvia un processo Python in background."""
    logger.info(f"Avvio di {script_name}...")
    return subprocess.Popen([sys.executable, script_name])

def main():
    os.makedirs("logs", exist_ok=True)
    logger.info("Watchdog Clio avviato.")
    
    processes = {script: start_process(script) for script in PROCESS_SCRIPTS}
    
    try:
        while True:
            time.sleep(5)
            for script, proc in processes.items():
                poll = proc.poll()
                if poll is not None:
                    logger.warning(f"Processo {script} terminato con codice {poll}. Riavvio tra {RESTART_DELAY} secondi...")
                    time.sleep(RESTART_DELAY)
                    processes[script] = start_process(script)
    except KeyboardInterrupt:
        logger.info("Watchdog interrotto manualmente. Terminazione processi...")
        for proc in processes.values():
            proc.terminate()
    except Exception as e:
        logger.error(f"Errore fatale nel watchdog: {e}")

if __name__ == "__main__":
    main()
