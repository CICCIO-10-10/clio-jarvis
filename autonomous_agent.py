import os
import json
import time
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger("ClioAutonomous")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BASE_DIR, "data", "clio_tasks.json")

# Flag di controllo globale per l'agente
IS_PAUSED = False

def load_tasks():
    try:
        if os.path.exists(TASKS_FILE):
            with open(TASKS_FILE, "r") as f:
                return json.load(f)
        return {"pending": [], "completed": [], "failed": []}
    except Exception as e:
        logger.error(f"Errore caricamento task: {e}")
        return {"pending": [], "completed": [], "failed": []}

def save_tasks(tasks):
    try:
        with open(TASKS_FILE, "w") as f:
            json.dump(tasks, f, indent=4)
    except Exception as e:
        logger.error(f"Errore salvataggio task: {e}")

def autonomous_loop():
    """Loop infinito che esegue task in background se l'utente è AFK."""
    from clio_core import LAST_INTERACTION_TIME, elabora_richiesta, invia_notifica_clio
    
    logger.info("🤖 Loop Autonomo avviato.")
    
    while True:
        try:
            if IS_PAUSED:
                logger.debug("🤖 [AUTONOMO] Agente in pausa...")
                time.sleep(30)
                continue

            # 1. Verifica stato AFK (più di 15 minuti di inattività)
            is_afk = (time.time() - LAST_INTERACTION_TIME) > 900
            
            if is_afk:
                tasks = load_tasks()
                if tasks["pending"]:
                    # Prendi il primo task
                    task = tasks["pending"].pop(0)
                    logger.info(f"🦾 [AUTONOMO] Inizio task: {task['titolo']}")
                    
                    # Notifica su Telegram l'inizio dell'operazione
                    invia_notifica_clio(f"🤖 Sto lavorando in autonomia su: '{task['titolo']}'...", parla=False)
                    
                    try:
                        # Esecuzione tramite il LLM (collaborazione tra riflessione e azione)
                        risultato = elabora_richiesta(f"Esegui questo task autonomo: {task['titolo']}. Descrizione: {task['descrizione']}")
                        
                        task["risultato"] = risultato
                        task["completato_il"] = time.time()
                        tasks["completed"].append(task)
                        
                        # Notifica completamento
                        invia_notifica_clio(f"✅ Task completato: {task['titolo']}\nRisultato: {risultato[:200]}...", parla=False)
                        
                    except Exception as te:
                        logger.error(f"Errore esecuzione task {task['id']}: {te}")
                        task["errore"] = str(te)
                        tasks["failed"].append(task)
                    
                    save_tasks(tasks)
            
        except Exception as e:
            logger.error(f"Errore Autonomous Loop: {e}")
            
        time.sleep(60) # Controlla ogni minuto
