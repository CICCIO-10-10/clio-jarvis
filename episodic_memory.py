import json
import os
import logging
from datetime import datetime

logger = logging.getLogger("ClioEpisodes")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EPISODES_FILE = os.path.join(BASE_DIR, "data", "clio_episodes.json")

class EpisodicMemory:
    def __init__(self):
        self.episodes = self._load_episodes()

    def _load_episodes(self):
        try:
            os.makedirs(os.path.dirname(EPISODES_FILE), exist_ok=True)
            if os.path.exists(EPISODES_FILE):
                with open(EPISODES_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"Errore caricamento memoria episodica: {e}")
            return []

    def _save_episodes(self):
        try:
            with open(EPISODES_FILE, "w", encoding="utf-8") as f:
                json.dump(self.episodes, f, indent=4)
        except Exception as e:
            logger.error(f"Errore salvataggio memoria episodica: {e}")

    def save_episode(self, titolo: str, descrizione: str = "", importanza: int = 3, emozione: str = "neutra"):
        """Registra un evento importante/milestone."""
        now = datetime.now()
        episode = {
            "timestamp": now.timestamp(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
            "titolo": titolo,
            "descrizione": descrizione,
            "importanza": importanza,  # 1-5
            "emozione": emozione
        }
        self.episodes.append(episode)
        self._save_episodes()
        logger.info(f"💾 Episodio salvato: {titolo}")
        return f"✅ Episodio '{titolo}' memorizzato con successo."

    def check_anniversaries(self) -> str:
        """Controlla se oggi ricorre un anniversario di un evento passato."""
        today_md = datetime.now().strftime("%m-%d")
        matches = []
        
        for ep in self.episodes:
            ep_date = ep["date"]
            if ep_date[5:] == today_md:
                years_ago = datetime.now().year - int(ep_date[:4])
                if years_ago > 0:
                    matches.append(f"- {years_ago} anni fa: '{ep['titolo']}' ({ep['descrizione']})")
                else:
                    matches.append(f"- Oggi: '{ep['titolo']}'")
                    
        if not matches:
            return ""
            
        return "🎗️ RICORRENZE DI OGGI:\n" + "\n".join(matches)

# Singleton
episodic_memory = EpisodicMemory()
