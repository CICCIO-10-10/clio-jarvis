"""
CLIO - vault_watcher.py
Sincronizzazione Continua in Tempo Reale tra Obsidian Vault e Clio AI.

Monitora la cartella del Vault Obsidian (os.path.expanduser("~")\\OneDrive\\OneSyncFiles).
Quando un file Markdown (.md) viene creato, modificato, eliminato o rinominato:
1. Invalida immediatamente la cache delle risposte (_RESPONSE_CACHE) di jarvis_brain.py.
2. Invalida la cache dei file del vault (_VAULT_FILE_CACHE) in clio_core.py.
3. Riscansiona e aggiorna l'indice del vault (vault_index.json).
4. Aggiorna il timestamp globale di invalidazione per sincronizzare qualsiasi processo attivo.
"""

import os
import sys
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import time
import logging
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configura logger dedicato
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [VaultWatcher] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("VaultWatcher")

# Percorsi principali
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
TIMESTAMP_FILE = os.path.join(DATA_DIR, "vault_invalidated.timestamp")

# Determina percorso del Vault
try:
    import config
    VAULT_DIR = getattr(config, "VAULT_DIR", r"os.path.expanduser("~")\OneDrive\OneSyncFiles")
except Exception:
    VAULT_DIR = r"os.path.expanduser("~")\OneDrive\OneSyncFiles"

# Cartelle da ignorare (file interni di sistema o cartelle temporanee)
IGNORED_DIRS = {".obsidian", ".git", ".trash", ".copilot", ".pytest_cache", "__pycache__"}


def trigger_cache_invalidation(event_type: str = "change", src_path: str = ""):
    """Invalida tutte le cache di Clio in memoria e su disco."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # 1. Scrivi il timestamp su file (permette il sync cross-processo)
        now_ts = time.time()
        with open(TIMESTAMP_FILE, "w", encoding="utf-8") as f:
            f.write(f"{now_ts}\n{event_type}\n{src_path}\n")
        logger.info(f"🔄 Cache invalidation timestamp aggiornato: {time.strftime('%H:%M:%S', time.localtime(now_ts))}")
    except Exception as e:
        logger.warning(f"Errore scrittura timestamp: {e}")

    # 2. Invalida la cache in-memory se i moduli sono importati nello stesso processo
    try:
        if "jarvis_brain" in sys.modules:
            jb = sys.modules["jarvis_brain"]
            if hasattr(jb, "_RESPONSE_CACHE"):
                jb._RESPONSE_CACHE.clear()
                logger.info("⚡ In-process: _RESPONSE_CACHE di jarvis_brain svuotata.")

        if "clio_core" in sys.modules:
            cc = sys.modules["clio_core"]
            if hasattr(cc, "_VAULT_FILE_CACHE"):
                cc._VAULT_FILE_CACHE.clear()
            if hasattr(cc, "build_vault_index"):
                count = cc.build_vault_index(force=True)
                logger.info(f"📚 In-process: _VAULT_FILE_CACHE svuotata, indice ricostruito ({count} file).")
        else:
            # Import lazy per ricostruire l'indice su disco
            sys.path.insert(0, SCRIPT_DIR)
            import clio_core
            clio_core._VAULT_FILE_CACHE.clear()
            count = clio_core.build_vault_index(force=True)
            logger.info(f"📚 Indice Obsidian ricaricato dal watcher ({count} note).")
    except Exception as ex:
        logger.warning(f"Nota durante invalidazione in-process: {ex}")


class VaultChangeHandler(FileSystemEventHandler):
    """Intercetta modifiche al Vault con debouncing."""

    def __init__(self, debounce_seconds: float = 1.0):
        super().__init__()
        self.debounce_seconds = debounce_seconds
        self._timer: threading.Timer = None
        self._lock = threading.Lock()
        self._pending_changes = set()

    def _is_relevant(self, path: str) -> bool:
        """Verifica se il file è una nota Markdown non ignorata."""
        if not path:
            return False
        # Solo file Markdown
        if not path.lower().endswith(".md"):
            return False
        
        # Ignora cartelle interne
        parts = Path(path).parts
        for part in parts:
            if part in IGNORED_DIRS or part.startswith("."):
                return False

        # Ignora i log di Clio per evitare loop infiniti di reindicizzazione
        if "Clio_Log.md" in path:
            return False

        return True

    def _debounce_trigger(self, event_type: str, path: str):
        if not self._is_relevant(path):
            return

        with self._lock:
            self._pending_changes.add(f"{event_type}:{os.path.basename(path)}")
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._fire_invalidation)
            self._timer.daemon = True
            self._timer.start()

    def _fire_invalidation(self):
        with self._lock:
            changes = list(self._pending_changes)
            self._pending_changes.clear()

        if changes:
            summary = ", ".join(changes[:4])
            if len(changes) > 4:
                summary += f" e altri {len(changes)-4} file"
            logger.info(f"📂 Rilevata modifica Vault Obsidian [{summary}] -> Invalido cache...")
            trigger_cache_invalidation(event_type="batch", src_path=summary)

    def on_created(self, event):
        if not event.is_directory:
            self._debounce_trigger("CREATO", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._debounce_trigger("MODIFICATO", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._debounce_trigger("ELIMINATO", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._debounce_trigger("SPOSTATO", event.dest_path)


def start_vault_watcher_thread(debounce: float = 1.0) -> Observer:
    """Avvia il watcher come thread in background all'interno di un'altra applicazione."""
    if not os.path.exists(VAULT_DIR):
        logger.error(f"❌ Percorso Vault non trovato: {VAULT_DIR}")
        return None

    event_handler = VaultChangeHandler(debounce_seconds=debounce)
    observer = Observer()
    observer.schedule(event_handler, path=VAULT_DIR, recursive=True)
    observer.daemon = True
    observer.start()
    logger.info(f"👁️ Vault Watcher attivo in background su: {VAULT_DIR}")
    return observer


def run_watcher():
    """Esegue il watcher come processo autonomo a ciclo continuo."""
    if not os.path.exists(VAULT_DIR):
        print(f"❌ Errore: Cartella Vault Obsidian non trovata in: {VAULT_DIR}")
        sys.exit(1)

    print("=" * 65)
    print("👁️ CLIO VAULT WATCHER - SINCRONIZZAZIONE CONTINUA OBSIDIAN")
    print(f"📁 Monitoraggio attivo su: {VAULT_DIR}")
    print("⚡ Auto-invalidation di _RESPONSE_CACHE e _VAULT_FILE_CACHE attiva.")
    print("=" * 65)

    event_handler = VaultChangeHandler(debounce_seconds=1.0)
    observer = Observer()
    observer.schedule(event_handler, path=VAULT_DIR, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArresto del Vault Watcher in corso...")
        observer.stop()
    observer.join()
    print("Vault Watcher terminato.")


if __name__ == "__main__":
    run_watcher()
