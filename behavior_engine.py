import json
import os
import time
import logging
import pygetwindow as gw
from datetime import datetime

logger = logging.getLogger("ClioBehavior")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BEHAVIOR_FILE = os.path.join(BASE_DIR, "data", "clio_behavioral_map.json")

class BehaviorEngine:
    def __init__(self):
        self.data = self._load_data()
        self.current_window = None
        self.start_time = time.time()

    def _load_data(self):
        try:
            os.makedirs(os.path.dirname(BEHAVIOR_FILE), exist_ok=True)
            if os.path.exists(BEHAVIOR_FILE):
                with open(BEHAVIOR_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {"patterns": {}, "daily_stats": {}}
        except Exception as e:
            logger.error(f"Errore caricamento dati comportamentali: {e}")
            return {"patterns": {}, "daily_stats": {}}

    def _save_data(self):
        try:
            with open(BEHAVIOR_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            logger.error(f"Errore salvataggio dati comportamentali: {e}")

    def track_activity(self):
        """Monitora la finestra attiva e aggiorna le statistiche."""
        try:
            active_win = gw.getActiveWindow()
            if not active_win:
                return

            win_title = active_win.title
            now = time.time()
            duration = now - self.start_time

            if win_title != self.current_window:
                if self.current_window:
                    self._update_stats(self.current_window, duration)
                
                self.current_window = win_title
                self.start_time = now
                
        except Exception as e:
            logger.error(f"Errore tracking attività: {e}")

    def _update_stats(self, window_title, duration):
        today = datetime.now().strftime("%Y-%m-%d")
        hour = datetime.now().hour
        
        # Semplificazione del titolo per raggruppamento (es: "file.py - VS Code" -> "VS Code")
        app_name = window_title.split(" - ")[-1] if " - " in window_title else window_title
        
        if today not in self.data["daily_stats"]:
            self.data["daily_stats"][today] = {}
        
        if app_name not in self.data["daily_stats"][today]:
            self.data["daily_stats"][today][app_name] = 0
            
        self.data["daily_stats"][today][app_name] += int(duration)
        
        # Pattern orari
        pattern_key = f"{hour:02d}:00"
        if pattern_key not in self.data["patterns"]:
            self.data["patterns"][pattern_key] = {}
        
        if app_name not in self.data["patterns"][pattern_key]:
            self.data["patterns"][pattern_key][app_name] = 0
            
        self.data["patterns"][pattern_key][app_name] += 1
        
        self._save_data()

    def get_context_summary(self):
        """Restituisce un riassunto delle abitudini attuali per il LLM."""
        hour = datetime.now().hour
        pattern_key = f"{hour:02d}:00"
        apps = self.data["patterns"].get(pattern_key, {})
        
        if not apps:
            return "Nessun pattern rilevato per quest'ora."
            
        sorted_apps = sorted(apps.items(), key=lambda x: x[1], reverse=True)
        top_apps = [a[0] for a in sorted_apps[:3]]
        
        return f"A quest'ora solitamente usi: {', '.join(top_apps)}."

    def suggest_action(self) -> str:
        """Suggerisce un'azione basata sull'ora attuale, pattern storici e buonsenso."""
        hour = datetime.now().hour
        
        # 1. Quiet Hours (Evita suggerimenti tra mezzanotte e le 7:00)
        if hour < 7:
            return "Nessun suggerimento disponibile."
            
        pattern_key = f"{hour:02d}:00"
        apps = self.data["patterns"].get(pattern_key, {})
        if not apps:
            return "Nessun suggerimento disponibile."
            
        sorted_apps = sorted(apps.items(), key=lambda x: x[1], reverse=True)
        if not sorted_apps: return "Nessun suggerimento disponibile."
        
        top_app = sorted_apps[0][0]
        frequency = sorted_apps[0][1]
        
        # 2. Soglia di confidenza (almeno 5 occorrenze rilevate)
        if frequency >= 5:
            try:
                # Verifica se l'app è già aperta
                titles = [w.title for w in gw.getAllWindows() if w.title]
                if any(top_app.lower() in t.lower() for t in titles):
                    return "Nessun suggerimento disponibile." 
            except: pass
            
            # 3. Personalità JARVIS-style
            suggerimenti = [
                f"Vedo che solitamente a quest'ora ti dedichi a {top_app}. Vuoi che lo apra?",
                f"Capo, è l'ora di {top_app}. Procedo con l'avvio?",
                f"In base alle tue abitudini, dovresti essere su {top_app} adesso. Ti serve una mano?"
            ]
            import random
            return random.choice(suggerimenti)
            
        return "Nessun suggerimento disponibile."

# Singleton
behavior_engine = BehaviorEngine()
