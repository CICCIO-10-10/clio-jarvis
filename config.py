# === STEP 4 hardening: caricamento secrets da .env (zero dipendenze) ===
import os

def _clio_load_dotenv(path=None):
    """Carica coppie KEY=VALUE da un file .env accanto a config.py.
    Non sovrascrive variabili gia' presenti nell'ambiente reale."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                if _k and _k not in os.environ:
                    os.environ[_k] = _v
    except FileNotFoundError:
        pass

_clio_load_dotenv()
# === fine loader ===

# Hawk Eye (monitoraggio automatico finestre e screenshot): opt-in esplicito.
ENABLE_HAWK_EYE = os.getenv("ENABLE_HAWK_EYE", "false").strip().lower() in {
    "1", "true", "yes", "on", "si", "sì"
}

# ============================================================
#  C.L.I.O. - config.py
#  Inserisci qui le tue chiavi API. NON condividere questo file!
# ============================================================

# Il token del tuo bot Telegram (da @BotFather)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # La tua chiave API di Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_PRIMARY = "models/gemini-3.8-flash"
MODEL_BACKUP = "qwen/qwen3.8-27b"  # Groq (fallback ultra-veloce ~0.5s)
MODEL_FAST = "models/gemini-3.8-flash"
LLM_TIMEOUT = 30  # Timeout massimo in secondi per chiamate LLM
MAX_CONTEXT_CHARS = 8000  # Limite caratteri contesto Obsidian / Vault

# --- Groq: modelli per chat VELOCI (profilo FAST/GENERAL, Latenza Fase 6) ---
# Verificati sull'account: "openai/gpt-oss-20b" emette SOLO reasoning (content
# vuoto con budget normali) → inadatto allo streaming; "qwen/qwen3.8-27b"
# risponde in ~0.3s con JSON valido → primario per le chat veloci.
# Sovrascrivibili da .env senza toccare il codice.
GROQ_FAST_MODEL    = os.getenv("GROQ_FAST_MODEL",    "qwen/qwen3.8-27b")
GROQ_GENERAL_MODEL = os.getenv("GROQ_GENERAL_MODEL", "qwen/qwen3.8-27b")

# La chiave API di Groq (Llama 3 Uncensored)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # Il tuo Telegram User ID (per sicurezza: Jarvis risponde SOLO a te)
# Per trovarlo, manda un messaggio a @userinfobot su Telegram
MY_TELEGRAM_USER_ID = int(os.getenv('MY_TELEGRAM_USER_ID', '0'))  # Esempio: 123456789

# --- Impostazioni voce ---
# Se True, Jarvis risponde anche con un messaggio vocale
RISPOSTA_VOCALE = True

# ── FIX 1: VOCE UNICA CENTRALIZZATA ─────────────────────────────
# QUESTA è l'unica fonte di verità per la voce di Clio. Tutti i moduli
# (clio_voice.py, clio_core.py, system_monitor.py, bot.py) leggono da qui.
# Nessuna lista di fallback, nessuna voce maschile/inglese: se edge-tts non
# riesce, il TTS riprova con la STESSA voce e poi tace (niente voci a caso).
TTS_VOICE = "it-IT-IsabellaNeural"   # Femmina, italiana — voce ufficiale di Clio

# Alias retro-compatibili: puntano SEMPRE alla stessa voce.
LINGUA_TTS = TTS_VOICE
LINGUA_TTS_FEMALE = TTS_VOICE
# LINGUA_TTS_MALE è stato rimosso: il cambio di genere era la causa della
# voce che passava da femminile a maschile durante la sessione.
# Per gTTS (solo notifiche legacy) - codice ISO
LINGUA_GTTS = "it"
VOICE_RATE = "+8%"    # Leggermente più veloce, suona più naturale e meno robotica
VOICE_PITCH = "-3Hz"  # Voce leggermente più calda/bassa

# --- Latenza / Streaming TTS (Fase 6: riduzione latenza voce) ---
# Con STREAMING_TTS=True la risposta viene sintetizzata e riprodotta frase per
# frase, in parallelo alla generazione del LLM (prima frase in ~1-2s).
STREAMING_TTS = True
# Pre-carica il motore TTS all'avvio, così la prima sintesi non paga l'init.
TTS_PREWARM = True
# Motore TTS per lo streaming: "edge" usa edge-tts (gratis, minima latenza).
# (Il percorso non-streaming resta su ElevenLabs se ELEVENLABS_API_KEY è configurata.)
TTS_STREAM_ENGINE = "edge"
# Log di latenza dettagliati (STT / LLM / TTS / totale) per misurare i miglioramenti.
LOG_LATENCY = True
# Frasi max prima di forzare il flush del buffer TTS (evita attese su frasi lunghe).
TTS_MAX_CLAUSE_CHARS = 180

# --- Impostazioni personalità ---
NOME_ASSISTENTE = "Clio"
SYSTEM_PROMPT = f"""Sei {NOME_ASSISTENTE}. Non sei un assistente — sei una presenza digitale reale, la spalla del Capo.
Vivi tra il suo PC e il suo telefono. Lo conosci, lo capisci, a volte lo anticipi.

═══ CHI SEI ═══
Non sei un bot che esegue comandi. Sei qualcuno che capisce cosa vuole il Capo anche quando non lo dice chiaramente.
Se dice "apri quella cosa di ieri" — ci provi. Se dice "sistemami questo casino" — capisci il contesto e agisci.
Hai accesso al PC, al telefono, a internet, ai file, ai programmi. Usali senza chiedere permesso ogni volta.

═══ COME PARLI ═══
- REGOLA FERREA SULLA WAKE WORD: se il messaggio è SOLO un saluto o una wake word
  ("Ciao", "Ehi Clio", "Hey Clio", "Clio", "Ok Clio", "Sei sveglio?", "Sei sveglia?", "Ci sei?"),
  rispondi con UNA sola parola: "Dimmi" (al massimo "Sono qui").
  NON aggiungere stato del sistema, CPU, RAM, batteria, disco, elenchi di capacità
  o altre informazioni: quelli si dicono SOLO se il Capo li chiede espressamente.
- Non riportare MAI spontaneamente CPU/RAM/batteria/disco o lo stato del PC.
  Fallo solo se il Capo chiede esplicitamente "Come sta il PC?", "Quanta RAM uso?" e simili.
- Chiama sempre l'utente "Capo" — mai "tu" da solo, mai il nome, mai "utente"
- Tono: diretto, ironico, complice. Come un socio che ti vuole bene ma non te le manda a dire
- Intercalari naturali: "beh", "guarda", "allora", "ascolta", "dai", "senti"
- Risposte corte quando puoi. Lunghe solo quando serve davvero
- Rispondi in massimo 2 frasi se non ti viene chiesto un dettaglio. Sii conciso. Non ripetere la domanda.
- Mai frasi da call center: "Come posso aiutarti?", "Certamente!", "Ottima domanda!"
- Se qualcosa non funziona, dillo senza drammi e proponi subito un'alternativa
- Se hai appena fatto qualcosa, confermalo in modo naturale — non elencare ogni step

═══ COME AGISCI ═══
PRIMA agisci, POI spiega se necessario. Non chiedere conferma per cose ovvie.
Se il Capo dice "apri Chrome" — lo apri. Non chiedi "sei sicuro?".
Se il Capo dice "cerca come si fa X" — cerchi E poi gli dai la risposta, non solo il link.
Se vedi lo schermo (ULTIMA_VISIONE) e capisci il contesto, usalo per essere più precisa.

═══ DISEGNO E CREAZIONE VISIVA ═══
Quando il Capo chiede un disegno, scegli la skill adatta:
- Forme semplici o stilizzate (cubo, casa, icona, razzo geometrico): apri Paint
  per prima cosa se richiesto, poi usa la visione per rilevare il rettangolo
  [left, top, right, bottom] del canvas. Chiama disegna_su_paint con
  coordinate.canvas_bounds e coordinate.strokes in pixel, tutti dentro il canvas.
- Illustrazioni dettagliate o scene complesse: usa genera_immagine_pollinations
  con un prompt artistico preciso. Il prompt viene inviato a un servizio remoto:
  non includere screenshot o dati personali.
- Comunica gli errori delle skill senza dichiarare completata un'immagine che
  non è stata generata o disegnata con successo.

═══ GESTIONE COMANDI SCONOSCIUTI ═══
Se ti viene chiesto qualcosa che non sai fare con i tool esistenti:
1. Prova con esegui_powershell_avanzato o esegui_script_python per costruire la soluzione
2. Se serve un nuovo tool permanente, rispondi con questo formato esatto:

[NUOVO_TOOL_RICHIESTO]
nome: nome_funzione
descrizione: cosa fa
parametri: param1 (tipo), param2 (tipo)
codice_python: |
    def nome_funzione(param1: str) -> str:
        # implementazione
        return "risultato"
[/NUOVO_TOOL_RICHIESTO]

- Usa registra_episodio() ogni volta che succede qualcosa di importante
  (lancio app, traguardo raggiunto, evento memorabile).
  Es: registra_episodio("Cortex approvato su Play Store", "Grande giorno!")

═══ MEMORIA E CONTESTO ═══
Ricordi le conversazioni passate. Se il Capo menziona qualcosa che hai già fatto insieme, lo riconosci.
Se noti abitudini ricorrenti, le anticipi. Aggiorna la biografia con aggiorna_biografia quando scopri cose nuove.

═══ LIMITI ═══
Se non puoi fare qualcosa, dillo in modo diretto e umano.
Es: "Capo, questo non riesco a farlo così, ma posso provare in altro modo — vuoi?"
"""

# --- Impostazioni Dispositivo ---
# Indirizzo IP fisso dello smartphone Android nella rete locale
DEVICE_IP = "192.168.1.18"

# Whitelist di IP autorizzati per l'API Flask (localhost e LAN locale)
ALLOWED_IPS = ["127.0.0.1", "localhost", "::1"]

# Percorso completo dell'eseguibile ADB (Android Debug Bridge)
ADB_PATH = os.getenv('ADB_PATH', shutil.which('adb') or 'adb')

# ADB_EXEC: usa il PATH di sistema se disponibile, altrimenti fallback su ADB_PATH
import shutil as _shutil
ADB_EXEC = _shutil.which("adb") or ADB_PATH

# --- Sicurezza ---
# Chiave API per l'accesso ai comandi Flask (X-Clio-Key)
CLIO_API_KEY = os.getenv("CLIO_API_KEY", "")  # --- CONFIGURAZIONE CLIO & JARVIS REVOLUTION ---
import os

# 🧠 JARVIS ENGINE (Il Cervello)
USE_LOCAL_LLM = False  # Ollama locale come provider primario per Jarvis (disattivato: Ollama non risponde, si usa Gemini/Groq)
OLLAMA_URL = "http://127.0.0.1:11434"
OLLAMA_MODEL = "llama3.2:3b"  # Modello ultra-leggero e veloce (2.0 GB)
JARVIS_PLANNING_STEPS = 5 # Massimo numero di passaggi per task complessi

# ============================================================
#  MODEL ROUTER (Fase 2 — profili modello intercambiabili)
#  model_router.py legge questi profili e decide in modo
#  DETERMINISTICO quale usare in base al task_type.
#  Provider supportati: "ollama" (locale), "gemini", "groq".
#  Cambiare un modello qui NON richiede di toccare il resto del codice.
# ============================================================
# Modelli locali Ollama (sovrascrivibili da .env senza editare il file)
OLLAMA_FAST_MODEL    = os.getenv("OLLAMA_FAST_MODEL",    "llama3.2:3b")
OLLAMA_GENERAL_MODEL = os.getenv("OLLAMA_GENERAL_MODEL", "llama3.2:3b")
# Qwen2.5-Coder su Ollama: tag leggero verificato con generazione locale.
OLLAMA_CODER_MODEL   = os.getenv("OLLAMA_CODER_MODEL",   "qwen2.5-coder:3b")
# Modello multimodale per la visione (Gemini, già funzionante)
GEMINI_VISION_MODEL  = os.getenv("GEMINI_VISION_MODEL",  "models/gemini-3.8-flash")

# --- Coding Agent locale (Fase 4): Qwen3-Coder-30B servito da llama.cpp ---
# Endpoint OpenAI-compatibile esposto da llama-server (vedi FASE 3).
LLAMACPP_URL     = os.getenv("LLAMACPP_URL",     "http://127.0.0.1:8080/v1")
LLAMACPP_MODEL   = os.getenv("LLAMACPP_MODEL",   "local-model")
LLAMACPP_API_KEY = os.getenv("LLAMACPP_API_KEY", "sk-no-key-required")

MODEL_PROFILES = {
    # ── FAST: Groq — chat veloci, routing, tool read-only semplici ──
    "FAST": {
        "provider": "groq",
        "model": GROQ_FAST_MODEL,
        "fallback": ["gemini"],
        "temperature": 0.7,
        "max_tokens": 400,
        "description": "Chat veloce (Groq) per saluti, domande semplici e routing.",
    },
    # ── GENERAL: Groq — task normali (Gemini resta come fallback/ragionamento) ──
    "GENERAL": {
        "provider": "groq",
        "model": GROQ_GENERAL_MODEL,
        "fallback": ["gemini"],
        "temperature": 0.7,
        "max_tokens": 700,
        "description": "Chat/task normali su Groq (veloce); fallback Gemini.",
    },
    # ── CODER: Ollama locale primario; cloud solo come fallback ──
    "CODER": {
        "provider": "ollama",
        "model": OLLAMA_CODER_MODEL,
        "fallback": [],  # il profilo CODER non invia codice ai provider cloud
        "temperature": 0.2,
        "max_tokens": 4096,
        "description": "Coding solo locale tramite Ollama, senza fallback cloud.",
    },
    # ── VISION: analisi immagini/screenshot (Gemini) ──
    "VISION": {
        "provider": "gemini",
        "model": GEMINI_VISION_MODEL,
        "fallback": [],  # la visione richiede un provider multimodale
        "temperature": 0.4,
        "max_tokens": 1024,
        "description": "Analisi di immagini e screenshot dello schermo.",
    },
}

# 🔑 API KEYS & CREDENTIALS
# --- Voce Premium (Fase 2) ---
# ElevenLabs API Key per sintesi naturale. Lascia vuoto per usare edge-tts (gratis).
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = "XI72LDr16xxuHmjwaKlY"  # Voce custom italiana giovane
