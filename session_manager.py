import os
import json
import logging
from config import SYSTEM_PROMPT

# Cartella per la memoria persistente
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "data", "clio_memory.json")

# Struttura RAM: { user_id: [{"role": "system"/"user"/"assistant", "content": "..."}] }
chat_sessions: dict[int, list] = {}

logger = logging.getLogger("ClioSessions")

def salva_chat(user_id: int):
    """Salva la cronologia in un JSON locale conservando solo messaggi testuali utili."""
    if user_id not in chat_sessions:
        return
        
    history_to_save = []
    # Salviamo solo User e Assistant per il file di memoria (filtriamo i tool call per evitare loop immensi al prossimo riavvio)
    for msg in chat_sessions[user_id]:
        if msg.get("role") in ["user", "assistant"] and isinstance(msg.get("content"), str) and msg.get("content"):
            history_to_save.append({"role": msg["role"], "content": msg["content"]})
            
    try:
        # Assicuriamoci che la cartella data esista
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        
        data = {}
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura HISTORY_FILE: {e}")
        data = {}
        
    data[str(user_id)] = history_to_save[-20:]
    
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Errore scrittura HISTORY_FILE: {e}")

def carica_chat(user_id: int) -> list:
    """Carica la cronologia salvata e prepende il system prompt."""
    storico = [{"role": "system", "content": SYSTEM_PROMPT}]
    try:
        if not os.path.exists(HISTORY_FILE):
            return storico
            
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        salvato = data.get(str(user_id), [])
        
        for msg in salvato:
            ruolo = msg.get("role", "user")
            if ruolo == "model": ruolo = "assistant"
            
            if "content" in msg:
                storico.append({"role": ruolo, "content": msg["content"]})
            elif "parts" in msg: # Supporto per vecchi formati
                content_str = "".join([p if isinstance(p, str) else "" for p in msg["parts"]])
                if content_str:
                    storico.append({"role": ruolo, "content": content_str})
                    
        return storico
    except Exception as e:
        logger.error(f"Errore caricamento chat: {e}")
        return storico

def get_or_create_chat(user_id: int) -> list:
    """Recupera la sessione attiva o la ricarica dal disco."""
    if user_id not in chat_sessions:
        chat_sessions[user_id] = carica_chat(user_id)
    
    # Limita la dimensione della finestra di contesto (RAM)
    if len(chat_sessions[user_id]) > 22:
        chat_sessions[user_id] = [chat_sessions[user_id][0]] + chat_sessions[user_id][-20:]
    
    return chat_sessions[user_id]
