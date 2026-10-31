try:
    import chromadb
except ImportError:
    chromadb = None
import os
import logging

# Configurazione logger
logger = logging.getLogger("ClioMemory")

# Cartella per il database persistente
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "chroma_db")

class MemoryManager:
    def __init__(self):
        self.vault_collection = None   # collezione dedicata all'indice del vault Obsidian
        try:
            if not chromadb:
                logger.warning("⚠️ chromadb non installato. Memoria semantica disabilitata.")
                self.collection = None
                return

            # Assicuriamoci che la cartella data esista
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            
            # Inizializziamo il client persistente
            self.client = chromadb.PersistentClient(path=DB_PATH)
            
            # Creiamo o recuperiamo la collezione principale
            # Nota: ChromaDB scaricherà un modello di embedding locale al primo avvio (all-MiniLM-L6-v2)
            self.collection = self.client.get_or_create_collection(name="clio_memory")
            logger.info("🧠 Memoria Semantica (ChromaDB) inizializzata.")
        except Exception as e:
            logger.error(f"Errore inizializzazione ChromaDB: {e}")
            self.collection = None

    def store_interaction(self, user_id, user_msg, assistant_msg, visual_context=""):
        """Salva un'interazione nel database vettoriale con contesto visivo."""
        if not self.collection: return
        
        try:
            import uuid
            from datetime import datetime
            doc_id = str(uuid.uuid4())
            
            # Arricchiamo il testo con la visione se disponibile
            vision_suffix = f"\n[Contesto Visivo]: {visual_context}" if visual_context else ""
            text = f"Utente: {user_msg}\nClio: {assistant_msg}{vision_suffix}"
            
            self.collection.add(
                documents=[text],
                ids=[doc_id],
                metadatas=[{
                    "user_id": str(user_id),
                    "timestamp": datetime.now().timestamp(),
                    "has_vision": bool(visual_context)
                }]
            )
            logger.info(f"💾 Interazione {'+ visione ' if visual_context else ''}salvata in memoria semantica.")
        except Exception as e:
            logger.error(f"Errore salvataggio memoria: {e}")

    def query_relevant_context(self, user_id, query, n_results=3):
        """Cerca nel passato i contesti più simili alla richiesta attuale."""
        if not self.collection: return ""
        
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
                where={"user_id": str(user_id)}
            )
            
            docs = results.get('documents', [[]])[0]
            if not docs:
                return ""
                
            context = "\n---\n".join(docs)
            return f"\n[MEMORIA PASSATA RILEVANTE]:\n{context}\n"
        except Exception as e:
            logger.error(f"Errore query memoria: {e}")
            return ""

    # ── VAULT OBSIDIAN: indice semantico opzionale (best-effort) ──
    def index_vault_documents(self, documents, metadatas, ids):
        """Indicizza (upsert) le note del vault in una collezione dedicata.

        Non solleva eccezioni: se ChromaDB non è disponibile ritorna False e
        il retrieval per parole chiave resta comunque attivo.
        """
        if not self.collection:
            return False
        try:
            col = self.client.get_or_create_collection(name="clio_vault")
            # Upsert a blocchi per non sovraccaricare il modello di embedding
            step = 128
            for i in range(0, len(ids), step):
                col.upsert(documents=documents[i:i+step],
                           metadatas=metadatas[i:i+step],
                           ids=ids[i:i+step])
            self.vault_collection = col
            logger.info(f"📚 Indice semantico vault aggiornato ({len(ids)} note).")
            return True
        except Exception as e:
            logger.error(f"Errore indicizzazione semantica vault: {e}")
            return False

    def query_vault(self, query, n_results=4):
        """Cerca semanticamente le note del vault più simili alla query.

        Ritorna una lista di dict {doc, meta}. Lista vuota se non disponibile.
        """
        try:
            col = self.vault_collection
            if col is None:
                if not self.collection:
                    return []
                col = self.client.get_or_create_collection(name="clio_vault")
                self.vault_collection = col
            if col.count() == 0:
                return []
            res = col.query(query_texts=[query], n_results=n_results)
            docs = res.get("documents", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            return [{"doc": d, "meta": m or {}} for d, m in zip(docs, metas)]
        except Exception as e:
            logger.error(f"Errore query semantica vault: {e}")
            return []

# Singleton
memory_manager = MemoryManager()
