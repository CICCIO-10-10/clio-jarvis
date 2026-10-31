"""
CLIO - clio_core.py
Il cuore "Nativo" di C.L.I.O. che gira in background sul PC.
Contiene:
1. Un server Flask locale per ricevere comandi dall'app Android o dal Command Center (Clio Hub).
2. Un loop in background che ascolta la sua Wakeword ("Ehi Clio") dal microfono.
"""

import sys
import io

# Forza encoding UTF-8 per la console su Windows
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except AttributeError:
        pass # Può fallire in alcuni ambienti rari
import os
import time
import json
import secrets
import threading
import logging
import subprocess
import policy
import model_router  # Fase 2: router deterministico dei profili modello
from flask import Flask, request, jsonify
from datetime import datetime
from groq import Groq
import re
import base64
import asyncio
import queue
import edge_tts
from tts_pronuncia import PRONUNCIA_TTS, pronuncia_tts  # fix pronuncia (Paint -> Peint)
from PIL import Image
import speech_recognition as sr
import pygame
import pygetwindow as gw
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from functools import wraps
import tempfile

# Importazioni locali configurazione e azioni
try:
    from config import GROQ_API_KEY, TELEGRAM_TOKEN, MY_TELEGRAM_USER_ID, NOME_ASSISTENTE, SYSTEM_PROMPT, RISPOSTA_VOCALE, TTS_VOICE, VOICE_RATE, VOICE_PITCH, DEVICE_IP, ADB_PATH, ENABLE_HAWK_EYE
    from actions import (
        apri_applicazione,
        apri_sito,
        cerca_su_google,
        cerca_su_youtube,
        spegni_pc,
        annulla_spegnimento,
        riavvia_pc,
        ottieni_info_sistema,
        muta_volume,
        esegui_comando_shell,
        crea_cartella,
        apri_cartella,
        digita_su_tastiera_fisica,
        leggi_appunti,
        scrivi_appunti,
        fai_screenshot_pc,
        analizza_schermo_pc,
        prepara_immagine_visione,
        media_play_pausa,
        media_prossimo,
        media_precedente,
        chiudi_finestra_attiva,
        minimizza_tutto,
        blocca_pc,
        svuota_cestino,
        rinomina_file,
        elimina_file,
        lista_file_cartella,
        stato_batteria,
        uccidi_processo,
        imposta_volume,
        chi_sei,
        telefono_clicca,
        telefono_digita,
        telefono_apri_app,
        telefono_screenshot,
        telefono_home,
        telefono_indietro,
        telefono_swipe,
        ottieni_telemetria_hardware, # Added for proactive monitoring
        monitora_connessioni_sospette,
        monitora_processi_critici,
        cerca_file,
        leggi_file_testo,
        scrivi_file_testo,
        esegui_script_python,
        finestre_gestione,
        ricerca_web_profonda,
        esegui_powershell_avanzato,
        ottieni_meteo,
        ottieni_notizie,
        leggi_gmail,
        leggi_calendario,
        registra_episodio,
        leggi_motore_ricerca_web,
        leggi_sito_web,
        leggi_youtube,
        spegni_luce,
        accendi_luce
    )
    from behavior_engine import behavior_engine
    # Portabilità ADB: prova a usare il comando 'adb' se presente nel PATH, altrimenti usa config
    try:
        import shutil
        ADB_EXEC = shutil.which("adb") or ADB_PATH
    except:
        ADB_EXEC = ADB_PATH
except ImportError as e:
    with open("clio_log_notturno.md", "a") as f:
        f.write(f"\n[ERRORE IMPORT]: {e}")
    print(f"Errore di importazione: {e}")
    exit(1)

def aggiorna_biografia(key: str, value: str):
    """Aggiorna le preferenze dell'utente nella memoria biografica per un'esperienza più personalizzata."""
    from session_manager import carica_chat, salva_chat # Usa session_manager
    bio = carica_biografia()
    if "pref" in key.lower():
        if isinstance(bio["user_profile"].get("preferences"), list):
             bio["user_profile"]["preferences"].append({key: value})
        else:
             bio["user_profile"]["preferences"] = {key: value}
    else:
        bio["user_profile"].update({key: value})
    salva_biografia(bio)
    return f"Biografia aggiornata: {key} = {value}"

# Definizione Mappa Strumenti (Deve essere prima del LLM)
from registry import AZIONI_MAP  # registro unico (STEP 5)
# aggiorna_biografia vive in questo modulo (non in actions): la registriamo nel registro condiviso.
AZIONI_MAP["aggiorna_biografia"] = aggiorna_biografia

def _briefing_call(nome):
    """Esegue un tool read-only del briefing passando dal control-plane (policy+audit)."""
    return policy.guarded_call(nome, AZIONI_MAP[nome], {}, origin="briefing")

import inspect

# Configurazione Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ClioCore")
import logging_setup
logging_setup.harden_logging()

# --- STATO GLOBALE SISTEMA (Per Hub Pulse) ---
SYSTEM_STATE = {
    "mic": "inactive", # active, inactive, listening, muted
    "telegram": "offline", # online, offline
    "adb": "disconnected", # connected, disconnected, healing
    "vision": "inactive", # active (only during interaction), inactive
    "last_speech": "",
    "last_update": "",
    "voice_gender": "female",  # FIX 1: sempre female (voce unica, nessun toggle M/F)
    "current_model": "AUTO"
}

# ── FIX 1: VOCE UNICA ──────────────────────────────────────────
# Voce TTS runtime: SEMPRE e SOLO config.TTS_VOICE (it-IT-IsabellaNeural).
# Non è più modificabile via API: era il toggle M/F a far cambiare voce.
CURRENT_VOICE = TTS_VOICE

TG_APP = None
TG_LOOP = None
LAST_INTERACTION_TIME = time.time()
ULTIMA_VISIONE = "Nessun dato visivo recente."

# ── CRONOLOGIA CONVERSAZIONI (visibile nell'Hub) ──
CONVERSATION_HISTORY: list = []   # [{role, text, ts}]
MAX_HISTORY = 40                  # Mantieni ultimi 40 messaggi

# ── NOTIFICHE PUSH per l'Hub (toasts) ──
HUB_NOTIFICATIONS: list = []      # [{text, type, ts}] — consumate dal frontend

# ── FLAG AUDIO: True mentre Clio sta parlando dal PC (evita l'auto-risveglio sulla propria voce) ──
AUDIO_PLAYING = threading.Event()

# ── Barge-in / stop manuale: azzera la riproduzione e sblocca subito il worker wake-word ──
_SPEECH_STOP = threading.Event()
_CURRENT_TTS = None   # istanza StreamingTTS attiva (per lo stop immediato da API)

# ── Latenza (Fase 6): abilitata da config.LOG_LATENCY ──
try:
    from config import LOG_LATENCY as _LOG_LATENCY
except Exception:
    _LOG_LATENCY = True


def _lat_log(tag, t0, extra=""):
    """Log di latenza uniforme (ms) per misurare i miglioramenti end-to-end."""
    if not _LOG_LATENCY:
        return
    logger.info(f"⏱️ [LATENCY] {tag}: {(time.time() - t0) * 1000:.0f} ms{(' — ' + extra) if extra else ''}")

# --- GESTIONE TASK ASINCRONI ---
PENDING_TASKS = {}
task_lock = threading.Lock()

def get_task_status(task_id):
    with task_lock:
        return PENDING_TASKS.get(task_id)

def set_task_status(task_id, status, response=None):
    with task_lock:
        PENDING_TASKS[task_id] = {"status": status, "response": response, "timestamp": time.time()}

# Cleanup periodico dei task vecchi
def cleanup_tasks_loop():
    while True:
        now = time.time()
        with task_lock:
            to_delete = [tid for tid, data in PENDING_TASKS.items() if now - data["timestamp"] > 3600]
            for tid in to_delete:
                del PENDING_TASKS[tid]
        time.sleep(600)

threading.Thread(target=cleanup_tasks_loop, daemon=True).start()

# ── Stato reale della visione ("Hawk Eye") ───────────────────────────────
# BUG (sync Web UI): il badge restava su "SPENTO" perché SYSTEM_STATE["vision"]
# veniva messo a "active" SOLO dentro vision_loop_func(), che non è mai avviato.
# Il worker di visione realmente attivo è monitor_active_window(): teniamo qui
# lo stato allineato al worker reale, così l'Hub riflette la realtà.
_VISION_WORKER = None  # threading.Thread del worker di visione attivo
_HAWK_EYE_STOP = threading.Event()


def _vision_effettiva():
    """Stato REALE di Hawk Eye: 'active' se il worker è vivo e non in Privacy Mode."""
    if _HAWK_EYE_STOP.is_set():
        return "inactive"
    try:
        if PRIVACY_MODE:
            return "inactive"
    except NameError:
        # PRIVACY_MODE non ancora definito durante l'import: ignora.
        pass
    w = _VISION_WORKER
    if w is not None:
        return "active" if w.is_alive() else "inactive"
    return "active" if SYSTEM_STATE.get("vision") == "active" else "inactive"


def set_vision_state(new_state, reason=""):
    """Aggiorna SYSTEM_STATE['vision'] e logga il cambio (ATTIVO/SPENTO)."""
    old = SYSTEM_STATE.get("vision")
    if new_state == old:
        return
    SYSTEM_STATE["vision"] = new_state
    etichetta = "ATTIVO" if new_state == "active" else "SPENTO"
    logger.info(f"👁️ [STATE] Hawk Eye -> {etichetta}" + (f" ({reason})" if reason else ""))


def analizza_schermo_evento(window_title):
    """Triggered quando cambia la finestra attiva. Esegue uno screenshot silente e aggiorna il contesto."""
    global ULTIMA_VISIONE
    try:
        # 1. Fai screenshot silente tramite la funzione in actions.py
        logger.info(f"📸 Screenshot silente per cambio finestra: {window_title}")
        fai_screenshot_pc()
        
        # 2. Aggiorna il contesto della visione (l'analisi effettiva avviene nel loop o su richiesta)
        img_path = "clio_screenshot.png"
        if os.path.exists(img_path):
            # Eseguiamo un'analisi rapida se possibile per mantenere il contesto "fresco"
            vision_desc = analizza_visione(img_path, f"Cosa c'è in questa finestra chiamata '{window_title}'? Sii estremamente breve.")
            ULTIMA_VISIONE = f"Finestra attiva: {window_title}. Contenuto: {vision_desc}"
            logger.info(f"👁️ Contesto visivo aggiornato per: {window_title}")
    except Exception as e:
        logger.error(f"Errore in analizza_schermo_evento: {e}")

def monitor_active_window():
    """Monitora in background il titolo della finestra attiva per rilevare cambi di contesto.

    È il vero worker di "Hawk Eye" (screenshot + analisi silente al cambio finestra):
    aggiorna SYSTEM_STATE['vision'] così l'Hub mostra lo stato reale. In Privacy Mode
    la visione è sospesa (stato SPENTO) e non vengono catturati screenshot.
    """
    global ULTIMA_VISIONE
    # Registra subito lo stato reale (il worker è vivo): così il cambio viene
    # loggato prima che l'Hub lo sincronizzi via /api/status. Usiamo
    # _vision_effettiva() perché a questo punto PRIVACY_MODE può non essere
    # ancora definito (il thread parte durante l'import del modulo).
    set_vision_state(_vision_effettiva(), "worker visione avviato")
    # Attendi che il modulo finisca l'inizializzazione prima di partire.
    if _HAWK_EYE_STOP.wait(6):
        set_vision_state("inactive", "worker arrestato prima dell'avvio")
        return
    last_title = ""
    while not _HAWK_EYE_STOP.is_set():
        try:
            # Privacy Mode: visione sospesa (nessuno screenshot, stato SPENTO)
            if PRIVACY_MODE:
                set_vision_state("inactive", "privacy mode")
                last_title = ""   # forza una nuova analisi quando la privacy si chiude
                if _HAWK_EYE_STOP.wait(2):
                    break
                continue
            set_vision_state("active", "worker attivo")

            window = gw.getActiveWindow()
            if window and window.title:
                current_title = window.title
                if current_title != last_title:
                    logger.info(f"🪟 Cambio finestra rilevato: {current_title}")
                    last_title = current_title
                    analizza_schermo_evento(current_title)
        except Exception as e:
            logger.debug(f"Errore monitoraggio finestre: {e}")
        if _HAWK_EYE_STOP.wait(2):
            break
    set_vision_state("inactive", "worker arrestato")

def start_hawk_eye(reason="richiesta esplicita"):
    """Avvia Hawk Eye una sola volta; il chiamante deve avere opt-in esplicito."""
    global _VISION_WORKER
    worker = _VISION_WORKER
    if worker is not None and worker.is_alive():
        if not _HAWK_EYE_STOP.is_set():
            return False
        worker.join(timeout=3)
        if worker.is_alive():
            return False
    _VISION_WORKER = None
    _HAWK_EYE_STOP.clear()
    _VISION_WORKER = threading.Thread(
        target=monitor_active_window, daemon=True, name="vision-worker")
    _VISION_WORKER.start()
    logger.info("👁️ [STATE] Hawk Eye avviato (%s).", reason)
    return True


def stop_hawk_eye(reason="richiesta esplicita"):
    """Arresta Hawk Eye e impedisce ulteriori screenshot dopo l'uscita del worker."""
    global _VISION_WORKER
    worker = _VISION_WORKER
    _HAWK_EYE_STOP.set()
    set_vision_state("inactive", reason)
    if worker is not None and worker.is_alive() and worker is not threading.current_thread():
        worker.join(timeout=3)
    if worker is None or not worker.is_alive():
        _VISION_WORKER = None
    return True


# Avvio automatico solo se configurato esplicitamente; default: spento.
if ENABLE_HAWK_EYE:
    start_hawk_eye("ENABLE_HAWK_EYE=true")
else:
    set_vision_state("inactive", "opt-in disattivato")

# Override per attivazione manuale da logo e Modalità Privacy
MIC_OVERRIDE = False
PRIVACY_MODE = False

# ══════════════════════════════════════════════════════════════
#  CONVERSAZIONE ATTIVA (stile Alexa/Google Home)
#  True = Clio ascolta i comandi SENZA ripetere "Ehi Clio", per una
#  finestra di CONVERSAZIONE_TIMEOUT secondi dall'ultima risposta.
#  False = modalità wake-word: risponde solo a "Ehi Clio".
# ══════════════════════════════════════════════════════════════
CONVERSAZIONE_ATTIVA = False
CONVERSAZIONE_SCADENZA = 0.0        # epoch: quando scade la finestra di conversazione
CONVERSAZIONE_TIMEOUT = 10.0        # secondi di inattività prima di tornare in wake-word
# True mentre Clio sta registrando/elaborando (o parlando): sospende il countdown.
# Serve perché il timer deve contare i 10s di inattività DOPO l'ultima risposta di
# Clio, non durante la cattura del comando o l'elaborazione di Jarvis.
CONVERSAZIONE_BUSY = threading.Event()
# Frasi che chiudono SUBITO la conversazione attiva (confronto per frase intera)
CONVERSAZIONE_CHIUSURE = (
    "grazie clio", "grazie cleo", "grazie klio",
    "basta cosi", "basta così", "ok basta", "ok, basta",
    "chiudi conversazione", "chiudi la conversazione", "fine conversazione",
    "basta", "fine",
)

def _conv_apri():
    """Entra in conversazione attiva e avvia la finestra di timeout."""
    global CONVERSAZIONE_ATTIVA, CONVERSAZIONE_SCADENZA
    CONVERSAZIONE_ATTIVA = True
    CONVERSAZIONE_SCADENZA = time.time() + CONVERSAZIONE_TIMEOUT

def _conv_rinova():
    """Rinnova la finestra di conversazione (dopo ogni risposta di Clio)."""
    global CONVERSAZIONE_SCADENZA
    if CONVERSAZIONE_ATTIVA:
        CONVERSAZIONE_SCADENZA = time.time() + CONVERSAZIONE_TIMEOUT

def _conv_chiudi(motivo=""):
    """Chiude la conversazione attiva e torna in modalità wake-word."""
    global CONVERSAZIONE_ATTIVA, CONVERSAZIONE_SCADENZA
    if CONVERSAZIONE_ATTIVA:
        logger.info(f"🔚 [CONVERSAZIONE] Chiusa ({motivo}). Torno in ascolto wake-word.")
    CONVERSAZIONE_ATTIVA = False
    CONVERSAZIONE_SCADENZA = 0.0

def _e_chiusura_conversazione(testo):
    """True se il testo è una frase di chiusura esplicita della conversazione."""
    t = _normalizza_testo(testo)
    if not t:
        return False
    for f in CONVERSAZIONE_CHIUSURE:
        if t == f or re.search(rf"(^|\s){re.escape(f)}(\s|$)", t):
            return True
    return False

def conversation_timeout_loop():
    """Timer globale: chiude la conversazione attiva dopo CONVERSAZIONE_TIMEOUT di inattività.

    Non chiude mentre Clio sta parlando (AUDIO_PLAYING) né mentre sta registrando o
    elaborando un comando (CONVERSAZIONE_BUSY): in entrambi i casi la finestra viene
    rinnovata dal worker appena la risposta è terminata.
    """
    while True:
        try:
            if (CONVERSAZIONE_ATTIVA
                    and not AUDIO_PLAYING.is_set()
                    and not CONVERSAZIONE_BUSY.is_set()
                    and CONVERSAZIONE_SCADENZA > 0
                    and time.time() >= CONVERSAZIONE_SCADENZA):
                _conv_chiudi("timeout 10s di inattività")
        except Exception:
            pass
        time.sleep(0.5)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "data", "clio_settings.json")

def carica_impostazioni():
    global PRIVACY_MODE
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                PRIVACY_MODE = data.get("privacy_mode", False)
                logger.info(f"⚙️ Impostazioni caricate: PRIVACY_MODE = {PRIVACY_MODE}")
        else:
            PRIVACY_MODE = False
    except Exception as e:
        logger.error(f"Errore caricamento impostazioni: {e}")

def salva_impostazioni():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"privacy_mode": PRIVACY_MODE}, f)
    except Exception as e:
        logger.error(f"Errore salvataggio impostazioni: {e}")

# ══════════════════════════════════════════════════════════════
#  VAULT OBSIDIAN — indice leggero + retrieval mirato
#  (non si legge mai tutto il vault: solo indice + pochi file rilevanti)
# ══════════════════════════════════════════════════════════════
VAULT_DIR = r"os.path.expanduser("~")\OneDrive\OneSyncFiles"
VAULT_INDEX_FILE = os.path.join(BASE_DIR, "data", "vault_index.json")

VAULT_INDEX = []                 # [{rel, title, excerpt, mtime, size}] — indice leggero
VAULT_PROFILE_REL = ""           # percorso relativo del file profilo (priorità massima)
VAULT_PROFILE_TEXT = ""          # contenuto (troncato) del file profilo

VAULT_MAX_FILES = 4000           # cap di sicurezza per lo scan
VAULT_MAX_CONTEXT_CHARS = 8000   # cap massimo assoluto caratteri Obsidian inviati al LLM
VAULT_CONTEXT_FILE_CHARS = 1000  # max caratteri per file iniettati nel prompt
VAULT_CONTEXT_MAX_FILES = 3      # quanti file rilevanti caricare (3-5)
VAULT_PROFILE_CHARS = 2500       # max caratteri del profilo nel prompt
VAULT_SEMANTIC_MAX = 400         # cap file per l'indice semantico (best-effort)
VAULT_RELOAD_TRIGGERS = (
    "ricarica profilo", "ricarica il profilo", "ricarica mio profilo",
    "aggiorna profilo", "aggiorna il profilo", "reload profilo",
    "ricarica vault", "aggiorna vault", "ricarica il vault",
)
# File di profilo: caricati SEMPRE come contesto primario (ordine = priorità).
# `claude.md` è incluso perché nel vault funge da "chi sono" dell'utente.
VAULT_PROFILE_NAMES = (
    "clio_profilo.md", "profilo.md", "clio_profile.md", "profilo_utente.md",
    "chi_sono.md", "about_me.md", "about.md", "identita.md", "identità.md",
    "claude.md",
)
# Termini che indicano una nota "identitaria" (fallback per query tipo "chi sono io?")
VAULT_IDENTITY_TERMS = (
    "profilo", "chi sono", "about me", "bio", "persona", "identita", "identità",
    "abitudini", "come parlo", "come lavoro", "stile", "routine", "obiettivi", "mindset",
)
# Stopword italiane/inglesi (riducono il rumore nel matching per parole chiave)
_STOPWORDS_IT = {
    "che", "chi", "cosa", "come", "dove", "quando", "perche", "perché", "quale",
    "sono", "sei", "siamo", "siete", "essere", "avere", "hai", "ho", "ha", "abbiamo",
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "dei", "delle", "degli",
    "del", "della", "di", "da", "in", "con", "su", "per", "tra", "fra", "al", "alla",
    "e", "ed", "o", "od", "ma", "se", "non", "mi", "ti", "si", "ci", "vi",
    "mio", "mia", "tuo", "tua", "suo", "sua", "questo", "questa", "quello", "quella",
    "parlami", "dimmi", "fammi", "puoi", "vorrei", "sai", "sul", "sulla", "nel",
    "nella", "the", "and", "for", "you", "your", "about", "what", "who", "with",
}

_VAULT_FILE_CACHE = {}  # {path: (mtime, content)}

def _vault_leggi(path, max_chars=None):
    """Legge un file di testo con cache mtime in memoria (velocizza retrieval ripetuto)."""
    try:
        st = os.stat(path)
        mtime = st.st_mtime
        if path in _VAULT_FILE_CACHE:
            cached_mtime, cached_content = _VAULT_FILE_CACHE[path]
            if cached_mtime == mtime:
                return cached_content[:max_chars] if max_chars else cached_content
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        _VAULT_FILE_CACHE[path] = (mtime, content)
        return content[:max_chars] if max_chars else content
    except Exception:
        return ""

def _vault_strip_frontmatter(testo):
    """Rimuove il blocco frontmatter YAML iniziale (--- ... ---) se presente."""
    righe = (testo or "").splitlines()
    if righe and righe[0].strip() == "---":
        for i in range(1, len(righe)):
            if righe[i].strip() in ("---", "..."):
                return "\n".join(righe[i + 1:])
    return testo or ""

def _vault_titolo(testo, fallback=""):
    """Titolo = prima riga con '#'; altrimenti prima riga utile (salta frontmatter/righe vuote)."""
    corpo = _vault_strip_frontmatter(testo)
    for riga in corpo.splitlines():
        r = riga.strip()
        if not r or r in ("---", "***", "___") or r.startswith("```"):
            continue
        if r.startswith("#"):
            return r.lstrip("#").strip()[:120] or fallback
        return r[:120]
    return fallback

def _vault_scan():
    """Scansiona il vault e costruisce l'indice leggero (path relativo, titolo, prime righe)."""
    entries = []
    profile_candidati = []   # (priorità, profondità, rel)
    if not os.path.isdir(VAULT_DIR):
        return entries, ""
    for root, dirs, files in os.walk(VAULT_DIR):
        # salta cartelle di servizio / nascoste / trash
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d.lower() not in ("trash", "templates", "template", "attachments")]
        for fn in files:
            if not fn.lower().endswith(".md"):
                continue
            if len(entries) >= VAULT_MAX_FILES:
                break
            full = os.path.join(root, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            inizio = _vault_leggi(full, max_chars=1200)   # legge solo l'inizio (leggero)
            titolo = _vault_titolo(inizio, fallback=os.path.splitext(fn)[0])
            corpo = _vault_strip_frontmatter(inizio)
            righe = [r.strip() for r in corpo.splitlines()
                     if r.strip() and not r.strip().startswith("#")
                     and not r.strip().startswith("```")
                     and r.strip() not in ("---", "***", "___")]
            excerpt = " ".join(righe[:3])[:300]
            rel = os.path.relpath(full, VAULT_DIR).replace("\\", "/")
            entries.append({"rel": rel, "title": titolo, "excerpt": excerpt,
                            "mtime": st.st_mtime, "size": st.st_size})
            # candidato "file profilo": priorità dal nome, profondità dal percorso
            nome = fn.lower()
            if nome in VAULT_PROFILE_NAMES:
                profile_candidati.append((VAULT_PROFILE_NAMES.index(nome), rel.count("/"), rel))
    # profilo = candidato con nome più prioritario e più vicino alla radice
    profile_rel = min(profile_candidati)[2] if profile_candidati else ""
    return entries, profile_rel

def _vault_save_cache():
    """Salva l'indice in JSON locale (evita di rileggere il vault a ogni avvio)."""
    try:
        os.makedirs(os.path.dirname(VAULT_INDEX_FILE), exist_ok=True)
        with open(VAULT_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "entries": VAULT_INDEX,
                "profile_rel": VAULT_PROFILE_REL,
                "profile_text": VAULT_PROFILE_TEXT,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"⚠️ Impossibile salvare cache vault: {e}")

def _vault_semantic_index_async():
    """Costruisce l'indice semantico del vault in background (best-effort, non blocca)."""
    def _work():
        try:
            from memory_manager import memory_manager
            if not VAULT_INDEX:
                return
            docs, metas, ids = [], [], []
            for e in VAULT_INDEX[:VAULT_SEMANTIC_MAX]:
                docs.append(f"{e['title']}\n{e['excerpt']}")
                metas.append({"rel": e["rel"], "title": e["title"]})
                ids.append(e["rel"])
            memory_manager.index_vault_documents(docs, metas, ids)
        except Exception as ex:
            logger.debug(f"[VAULT] Indice semantico non disponibile: {ex}")
    try:
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        pass

def build_vault_index(force=False):
    """Costruisce (o ricarica dalla cache JSON) l'indice del vault. Non solleva MAI eccezioni."""
    global VAULT_INDEX, VAULT_PROFILE_REL, VAULT_PROFILE_TEXT
    # 1) Prova la cache su disco (veloce)
    if not force and os.path.exists(VAULT_INDEX_FILE):
        try:
            with open(VAULT_INDEX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cache = data.get("entries", [])
            if cache:
                VAULT_INDEX = cache
                VAULT_PROFILE_REL = data.get("profile_rel", "")
                VAULT_PROFILE_TEXT = data.get("profile_text", "")
                logger.info(f"📚 Vault indicizzato: {len(VAULT_INDEX)} file trovati (da cache).")
                _vault_semantic_index_async()
                return len(VAULT_INDEX)
        except Exception as e:
            logger.warning(f"⚠️ Cache vault non valida ({e}), rileggo dal disco.")

    # 2) Scan dal disco
    entries, profile_rel = _vault_scan()
    if not entries:
        VAULT_INDEX = []
        VAULT_PROFILE_REL, VAULT_PROFILE_TEXT = "", ""
        logger.info("📚 Vault non trovato o vuoto, uso contesto di default.")
        return 0

    VAULT_INDEX = entries
    VAULT_PROFILE_REL = profile_rel
    VAULT_PROFILE_TEXT = ""
    if profile_rel:
        VAULT_PROFILE_TEXT = _vault_leggi(
            os.path.join(VAULT_DIR, profile_rel.replace("/", os.sep)),
            max_chars=VAULT_PROFILE_CHARS)
    logger.info(f"📚 Vault indicizzato: {len(VAULT_INDEX)} file trovati.")
    _vault_save_cache()
    _vault_semantic_index_async()
    return len(VAULT_INDEX)

def reload_vault_profile():
    """Rilegge l'indice del vault a caldo (comando 'ricarica profilo'). Ritorna il n° di file."""
    logger.info("♻️ [VAULT] Ricarica profilo richiesta — reindicizzo il vault...")
    _VAULT_FILE_CACHE.clear()
    if hasattr(_vault_semantic_paths, "_cache"):
        _vault_semantic_paths._cache.clear()
    return build_vault_index(force=True)

def is_vault_reload_command(testo):
    """True se il testo è il comando di ricarica del profilo."""
    t = (testo or "").lower()
    return any(tr in t for tr in VAULT_RELOAD_TRIGGERS)

# ── Comando "aggiorna profilo: <campo>=<valore>" (BUG 3) ─────────────────────
# Modifica il file profilo del vault (es. CLAUDE.md) e ricarica l'indice, così
# il nuovo valore è visibile subito nel contesto ("chi sono?").
_VAULT_UPDATE_TRIGGERS = (
    "aggiorna profilo:", "aggiorna il profilo:", "aggiorna mio profilo:",
    "aggiorna profilo ", "aggiorna il profilo ", "aggiorna mio profilo ",
    "modifica profilo:", "modifica il profilo:", "update profile:",
)

def parse_vault_update_command(testo):
    """Estrae (campo, valore) da 'aggiorna profilo: campo=valore'. None se non è il comando."""
    t = (testo or "").strip()
    low = t.lower()
    if not any(tr in low for tr in _VAULT_UPDATE_TRIGGERS):
        return None
    m = re.search(r"profilo\s*[:\-]?\s*(.+)$", t, re.IGNORECASE)
    corpo = m.group(1) if m else t
    if "=" not in corpo:
        return None
    campo, _, valore = corpo.partition("=")
    campo = campo.strip().strip("*").strip()
    valore = valore.strip().strip("*").strip()
    if not campo or not valore:
        return None
    return campo, valore

def is_vault_update_command(testo):
    """True se il testo è un comando di aggiornamento del profilo vault."""
    return parse_vault_update_command(testo) is not None

def _vault_profile_path():
    """Percorso assoluto del file profilo del vault (CLAUDE.md o simile)."""
    if VAULT_PROFILE_REL:
        return os.path.join(VAULT_DIR, VAULT_PROFILE_REL.replace("/", os.sep))
    for e in VAULT_INDEX:
        if os.path.basename(e.get("rel", "")).lower() in VAULT_PROFILE_NAMES:
            return os.path.join(VAULT_DIR, e["rel"].replace("/", os.sep))
    for name in VAULT_PROFILE_NAMES:
        p = os.path.join(VAULT_DIR, name)
        if os.path.exists(p):
            return p
    return os.path.join(VAULT_DIR, "CLAUDE.md")

def _vault_norm(s):
    """Normalizza una stringa (minuscolo, senza accenti) per confronti robusti."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()

def update_vault_profile_field(campo, valore):
    """Aggiorna un campo del file profilo del vault. Ritorna (ok, messaggio).

    Supporta il campo età/anni (es: '19 anni' -> '20 anni') e, in generale,
    campi scritti come '**Campo:** valore' o 'campo: valore'. Dopo la scrittura
    ricarica l'indice del vault così il nuovo valore è subito nel contesto.
    """
    path = _vault_profile_path()
    if not os.path.exists(path):
        return False, f"File profilo non trovato: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            testo = f.read()
    except Exception as e:
        return False, f"Impossibile leggere il profilo: {e}"

    orig = testo
    campo_norm = _vault_norm(campo)
    valore = str(valore).strip()

    if campo_norm in ("eta", "anni", "anno", "age"):
        # Campo età: sostituisci '<n> anni' mantenendo il resto della frase.
        testo, n = re.subn(r"\b\d{1,3}\s*anni\b", f"{valore} anni", testo, count=1)
        if n == 0:
            return False, f"Campo '{campo}' non trovato nel profilo (nessun '<n> anni')."
    else:
        # Campo generico: '**Campo:** valore' oppure 'Campo: valore'.
        nuovo, n = re.subn(
            rf"(\*\*\s*{re.escape(campo)}\s*:\s*\*\*)[^\n]*",
            lambda mm: mm.group(1) + " " + valore, testo, count=1, flags=re.IGNORECASE)
        if not n:
            nuovo, n = re.subn(
                rf"(^|\n)(\s*[^\S\n]*(?:[-*]\s*)?{re.escape(campo)}\s*:[^\S\n]*)[^\n]*",
                lambda mm: mm.group(1) + mm.group(2) + valore, testo, count=1, flags=re.IGNORECASE)
        if not n:
            return False, (f"Campo '{campo}' non trovato nel profilo. "
                           f"Campi supportati: età/anni, più qualsiasi '<Campo>:** valore'.")
        testo = nuovo

    if testo == orig:
        return True, f"Nessuna modifica necessaria: {campo} era già '{valore}'."

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(testo)
    except Exception as e:
        return False, f"Impossibile scrivere il profilo: {e}"

    logger.info(f"📝 [VAULT] Profilo aggiornato: {campo} = {valore} ({path})")
    reload_vault_profile()
    return True, f"Profilo aggiornato: {campo} = {valore}."

def _vault_keywords(query):
    """Estrae le parole chiave utili dalla query (minuscole, no stopword, len>=3)."""
    q = re.sub(r"[^\wàèéìòù\s]", " ", (query or "").lower())
    return [w for w in q.split() if len(w) >= 3 and w not in _STOPWORDS_IT]

def _vault_score(entry, keywords):
    """Punteggio di rilevanza di una voce d'indice rispetto alle parole chiave."""
    if not keywords:
        return 0
    titolo = entry.get("title", "").lower()
    excerpt = entry.get("excerpt", "").lower()
    rel = entry.get("rel", "").lower()
    score = 0
    for kw in keywords:
        if kw in titolo:
            score += 5
        if kw in rel:
            score += 2
        if kw in excerpt:
            score += 1
    return score

def _vault_semantic_paths(query, k=VAULT_CONTEXT_MAX_FILES):
    """Best-effort: usa ChromaDB (se attivo) per i file più simili semanticamente.

    Il risultato è memoizzato (le query vocali si ripetono): la prima chiamata
    può pagare il caricamento del modello di embedding, le successive no.
    """
    if not hasattr(_vault_semantic_paths, "_cache"):
        _vault_semantic_paths._cache = {}
    cache = _vault_semantic_paths._cache
    key = (query or "").strip().lower(), k
    if key in cache:
        return cache[key]
    try:
        from memory_manager import memory_manager
        hits = memory_manager.query_vault(query, n_results=k)
        res = [h.get("meta", {}).get("rel") for h in hits if h.get("meta", {}).get("rel")]
    except Exception:
        res = []
    if len(cache) > 256:
        cache.clear()
    cache[key] = res
    return res

def _vault_read_rel(rel, max_chars=VAULT_CONTEXT_FILE_CHARS):
    return _vault_leggi(os.path.join(VAULT_DIR, rel.replace("/", os.sep)), max_chars=max_chars)

def get_vault_index_summary(max_titles=15):
    """Riassunto leggero dell'indice: i titoli delle note principali (ultime note)."""
    if not VAULT_INDEX:
        return ""
    recenti = sorted(VAULT_INDEX, key=lambda e: e.get("mtime", 0), reverse=True)[:max_titles]
    return "; ".join((e.get("title") or e.get("rel")) for e in recenti)

def get_vault_context_for_prompt(query):
    """Contesto vault da iniettare nel prompt: profilo (priorità) + note rilevanti.

    Non legge mai tutto il vault: parte dall'indice, sceglie i file più pertinenti
    (parole chiave + eventuale similarità semantica) e ne carica solo un estratto.
    """
    if not VAULT_INDEX and not VAULT_PROFILE_TEXT:
        return ""
    parti = []
    if VAULT_PROFILE_TEXT.strip():
        parti.append("PROFILO DELL'UTENTE (priorità massima):\n" + VAULT_PROFILE_TEXT.strip())

    keywords = _vault_keywords(query)
    scelti = []

    # 1) match per parole chiave sull'indice
    if keywords:
        scored = sorted(((_vault_score(e, keywords), e) for e in VAULT_INDEX),
                        key=lambda x: x[0], reverse=True)
        for s, e in scored:
            if s > 0:
                scelti.append(e["rel"])
            if len(scelti) >= VAULT_CONTEXT_MAX_FILES:
                break
        # 2) arricchimento semantico (opzionale, best-effort)
        for rel in _vault_semantic_paths(query):
            if rel not in scelti and any(e["rel"] == rel for e in VAULT_INDEX):
                scelti.append(rel)
            if len(scelti) >= VAULT_CONTEXT_MAX_FILES + 1:
                break

    # 3) fallback "identità": note che parlano dell'utente (query tipo "chi sono io?")
    if not scelti:
        for e in sorted(VAULT_INDEX, key=lambda e: e.get("mtime", 0), reverse=True):
            blob = (e.get("title", "") + " " + e["rel"]).lower()
            if any(t in blob for t in VAULT_IDENTITY_TERMS):
                scelti.append(e["rel"])
            if len(scelti) >= 2:
                break

    # 4) ultimo fallback: note alla radice (più vicine) o le più recenti
    if not scelti:
        radice = sorted((e for e in VAULT_INDEX if "/" not in e["rel"]),
                        key=lambda e: e.get("mtime", 0), reverse=True)
        pool = radice or sorted(VAULT_INDEX, key=lambda e: e.get("mtime", 0), reverse=True)
        scelti = [e["rel"] for e in pool[:2]]

    current_chars = sum(len(p) for p in parti)
    for rel in scelti[:VAULT_CONTEXT_MAX_FILES]:
        if rel == VAULT_PROFILE_REL:
            continue   # già incluso come profilo prioritario
        rem = VAULT_MAX_CONTEXT_CHARS - current_chars
        if rem <= 150:
            break
        chars_to_read = min(VAULT_CONTEXT_FILE_CHARS, rem)
        testo = _vault_read_rel(rel, max_chars=chars_to_read)
        if testo.strip():
            blocco = f"NOTA DAL VAULT ({rel}):\n{testo.strip()}"
            parti.append(blocco)
            current_chars += len(blocco)

    if not parti:
        return ""
    res = "\n\n".join(parti)
    return res[:VAULT_MAX_CONTEXT_CHARS]

def set_system_mic_mute(mute=True):
    """Tenta di mutare il microfono a livello di sistema operativo."""
    try:
        import soundcard as sc
        mics = sc.all_microphones()
        for mic in mics:
            try:
                # SoundCard non ha un metodo 'mute' diretto universale affidabile su tutti i backend,
                # ma possiamo provare a disabilitare l'acquisizione o usare PowerShell come fallback.
                pass
            except: pass
        
        # Fallback PowerShell molto robusto per Windows
        status = "1" if mute else "0"
        # Questo comando richiede l'ID del device o agisce sul default. 
        # Per semplicità usiamo un comando che muta il volume di registrazione o l'input.
        val = 0 if mute else 100
        cmd = f"powershell -command \"(Get-WmiObject -Query 'Select * from Win32_PnPEntity where Name like \"*Microphone*\"').Caption\""
        # Dato che mutare il volume di sistema è più semplice e testato:
        subprocess.run(["powershell", "-Command", f"$m = New-Object -ComObject MMDeviceEnumerator; $d = $m.GetDefaultAudioEndpoint(1, 0); $d.SetMute({ '1' if mute else '0' })"], capture_output=True)
        logger.info(f"🎤 [SYSTEM] Mic {'MUTATO' if mute else 'ATTIVATO'} via PowerShell.")
    except Exception as e:
        logger.error(f"Errore muting sistema: {e}")

def update_state(key, value):
    global PRIVACY_MODE
    
    if key == "mic":
        if PRIVACY_MODE and value != "muted":
            return
            
    if SYSTEM_STATE.get(key) != value:
        logger.info(f"🔄 [STATE] {key} -> {value}")
        
    SYSTEM_STATE[key] = value
    SYSTEM_STATE["last_update"] = datetime.now().strftime("%H:%M:%S")

# Inizializzazione Flask e Groq
app = Flask(__name__, static_folder='clio_hub', static_url_path='')

# --- SECRETS & SECURITY SETUP ---
from config import CLIO_API_KEY, ALLOWED_IPS
os.makedirs("logs", exist_ok=True)
security_logger = logging.getLogger("ClioSecurity")
security_logger.setLevel(logging.WARNING)
sec_handler = logging.FileHandler("logs/security.log", encoding="utf-8")
sec_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
security_logger.addHandler(sec_handler)

# --- SESSIONI WEB UI: token temporanei in memoria (la chiave master non esce mai dal server) ---
CLIO_VERSION = "2.0"
SESSION_COOKIE = "clio_session"
SESSION_TTL = 12 * 3600  # 12h, rinnovato (sliding) a ogni richiesta autenticata
_SESSIONS = {}           # token -> scadenza (epoch)
_sessions_lock = threading.Lock()

def _new_session_token():
    """Genera e registra un token di sessione, ripulendo quelli scaduti."""
    now = time.time()
    tok = secrets.token_urlsafe(24)
    with _sessions_lock:
        for k in [k for k, exp in _SESSIONS.items() if exp < now]:
            _SESSIONS.pop(k, None)
        _SESSIONS[tok] = now + SESSION_TTL
    return tok

def _valid_session_token(tok):
    """True se il token esiste e non è scaduto; se valido ne rinnova la scadenza."""
    if not tok:
        return False
    now = time.time()
    with _sessions_lock:
        exp = _SESSIONS.get(tok)
        if exp is None:
            return False
        if exp < now:
            _SESSIONS.pop(tok, None)
            return False
        _SESSIONS[tok] = now + SESSION_TTL  # sliding expiration
        return True

def _attach_session_cookie(resp):
    """Emette un nuovo token di sessione nel cookie e lo consegna al browser."""
    resp.set_cookie(SESSION_COOKIE, _new_session_token(),
                    max_age=SESSION_TTL, httponly=True, samesite="Strict", path="/")
    return resp

def require_clio_key(f):
    """Decoratore per proteggere le rotte con API Key e Rate Limiting."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        remote = request.remote_addr
        
        # 1. IP Whitelisting (Solo localhost e LAN se configurate)
        is_local = remote in ALLOWED_IPS or remote == "127.0.0.1"
        if not is_local:
            security_logger.warning(f"🚫 IP BLOCCATO: {remote} ha tentato di accedere a {request.path}")
            return jsonify({"error": "Forbidden: Non-authorized network source"}), 403

        # 2. Autenticazione: chiave master (header X-Clio-Key) OPPURE token di sessione (header/cookie).
        #    La chiave master non è mai esposta al client: la Web UI usa un token temporaneo.
        header_key = request.headers.get("X-Clio-Key")
        cookie_key = request.cookies.get(SESSION_COOKIE)
        master_ok = bool(CLIO_API_KEY) and header_key == CLIO_API_KEY
        if not master_ok and not _valid_session_token(header_key) and not _valid_session_token(cookie_key):
            security_logger.warning(f"🔑 CHIAVE ERRATA: {remote} ha fornito una chiave non valida per {request.path}")
            return jsonify({"error": "Unauthorized: Invalid API Key"}), 401

        # 3. Rate Limiting — escludi endpoint di polling (status, ping, task_status)
        _exempt = ('/api/status', '/api/ping', '/api/task_status', '/api/cost-confirmations')
        if not any(request.path.startswith(p) for p in _exempt):
            if not hasattr(app, 'rate_limit_db'):
                app.rate_limit_db = {}
            now = time.time()
            if remote not in app.rate_limit_db:
                app.rate_limit_db[remote] = []
            app.rate_limit_db[remote] = [t for t in app.rate_limit_db[remote] if now - t < 60]
            if len(app.rate_limit_db[remote]) >= 60:
                security_logger.warning(f"⏳ RATE LIMIT: {remote} ha superato le 60 req/min")
                return jsonify({"error": "Rate limit exceeded"}), 429
            app.rate_limit_db[remote].append(now)
        return f(*args, **kwargs)
    return decorated_function
client = None  # Client Groq (chat + Whisper). Resta None se l'init fallisce.
try:
    client = Groq(api_key=GROQ_API_KEY)
except Exception as e:
    logger.error(f"Errore inizializzazione Groq: {e}")
    with open("clio_log_notturno.md", "a") as f:
        f.write(f"\n[ERRORE GROQ]: {e}")

# Configurazione Modelli JARVIS (Gemini 2.0 Primario)
MODEL_PRIMARY = "models/gemini-3.8-flash"
MODEL_BACKUP = "qwen/qwen3.8-27b"  # Groq — allineato a config.MODEL_BACKUP (ultra-veloce)
MODEL_FAST = "qwen/qwen3.8-27b"

gemini_client = None
gemini_vision = None
safe_client = None

_vision_unavailable = None  # motivo per cui la vision è disattivata (es. chiave non valida)

def analizza_visione(immagine_path, prompt_testo):
    """Esegue l'analisi visiva usando Gemini. Se Gemini non è disponibile NON solleva eccezioni."""
    global _vision_unavailable
    if not gemini_client or not gemini_vision or _vision_unavailable:
        return "Vision non disponibile: " + (_vision_unavailable or "Gemini non configurato.")
    try:
        jpeg_bytes = prepara_immagine_visione(immagine_path)
        logger.info(f"👁️ Vision: immagine pronta per Gemini ({len(jpeg_bytes)//1024} KB, max 1280px, JPEG q85)")
        # Il router decide il modello del profilo VISION (di default Gemini).
        vision_model = model_router.get_profile(model_router.route_task("vision")).get("model") or gemini_vision
        response = gemini_client.models.generate_content(
            model=vision_model,
            contents=[prompt_testo, types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")],
        )
        return response.text
    except Exception as e:
        msg = str(e)
        if "API key not valid" in msg or "API_KEY_INVALID" in msg:
            _vision_unavailable = "chiave Gemini non valida."
            logger.warning("⚠️ Vision disattivata: chiave Gemini non valida.")
        else:
            logger.error(f"Errore analisi Gemini Vision: {e}")
        return "Vision non disponibile: " + (_vision_unavailable or msg)

def stima_token(messages, tools=None):
    """Stima ultra-conservativa dei token (caratteri / 2) includendo i tool."""
    text = json.dumps(messages)
    if tools:
        text += json.dumps(tools)
    return len(text) // 2  # Massima prudenza (2 invece di 3)

def comprimi_contesto(messages, tools=None, max_tokens=3500):
    """Riduce la dimensione del contesto in modo drastico per restare nei mini-limiti di Groq (6000 TPM)."""
    if not messages: return []
    
    # Se siamo sotto il limite di sicurezza estremo, non facciamo nulla
    if stima_token(messages, tools) <= max_tokens:
        return messages
    
    logger.warning(f"🔍 Compressione DRASICA ({stima_token(messages, tools)} token stimati)...")
    
    new_messages = []
    # 1. Troncamento violento dei risultati dei tool (che spesso sono i colpevoli)
    for msg in messages:
        m_copy = msg.copy()
        # Se è un tool result o un messaggio molto lungo, taglia a 300 caratteri
        content_str = str(m_copy.get("content", ""))
        if len(content_str) > 600:
            m_copy["content"] = content_str[:200] + "... [TAGLIATO PER SPAZIO] ..." + content_str[-50:]
        new_messages.append(m_copy)
    
    # 2. Se ancora sopra, rimuovi i messaggi centrali della cronologia (mantenendo System [0] e gli ultimi due)
    while len(new_messages) > 3 and stima_token(new_messages, tools) > max_tokens:
        new_messages.pop(1) # Rimuove il messaggio più vecchio subito dopo il system
        
    return new_messages

class SafeLLM:
    """Wrapper universale che prioritizza Gemini Pro e usa Groq come failover."""
    def __init__(self, groq_client, gemini_client):
        self.groq = groq_client
        self.gemini = gemini_client

    def chat(self, messages, tools=None, task_type="general"):
        """Esegue una chat completion con failover automatico.

        Il profilo (provider/modello primario) è scelto dal router. Qui restano i
        client cloud (Gemini/Groq); il provider locale Ollama è gestito da JarvisBrain.
        """
        profile_name = model_router.route_task("fast" if task_type == "proactive" else task_type)
        profile = model_router.get_profile(profile_name)
        cloud_chain = [p for p in model_router.get_provider_chain(profile_name)
                       if p in ("gemini", "groq")]
        if not cloud_chain:
            cloud_chain = ["gemini", "groq"]
        client_map = {"gemini": self.gemini, "groq": self.groq}
        tentativi = [(nome, client_map[nome]) for nome in cloud_chain]
        gem_model = profile.get("model") if profile.get("provider") == "gemini" else MODEL_PRIMARY
        groq_model = profile.get("model") if profile.get("provider") == "groq" else MODEL_BACKUP
        if task_type == "proactive":
            # Dedicated selection; other chat/coding profiles remain unchanged.
            groq_model = "llama-3.1-8b-instant"
            gem_model = "gemini-3.8-flash"
            # llama-3.1-8b-instant absent from this Groq account (checked 2026-10-16).
            tentativi = [("gemini", self.gemini)]
            cloud_chain = ["gemini"]
        logger.info(f"🧭 [ROUTER] SafeLLM task='{task_type}' → profilo {profile_name}; catena cloud={cloud_chain}")

        ultimo_errore = None
        for nome, c in tentativi:
            if not c: continue
            try:
                if nome == "gemini":
                    logger.info(f"💎 [LLM] Tentativo con Gemini ({gem_model})")
                    # Conversione history (già testata e funzionante)
                    history = []
                    last_role = None
                    for m in messages[:-1]:
                        if m["role"] == "system": continue
                        role = "user" if m["role"] in ["user", "tool"] else "model"
                        content = str(m.get("content", ""))
                        if role == last_role:
                            if history: history[-1]["parts"][0] += f"\n\n{content}"
                        else:
                            history.append({"role": role, "parts": [content]})
                            last_role = role
                    while history and history[0]["role"] != "user":
                        history.pop(0)

                    hist_content = [
                        types.Content(role=h["role"], parts=[types.Part(text=h["parts"][0])])
                        for h in history
                    ]
                    gem_cfg = types.GenerateContentConfig(
                        tools=[] if task_type == "proactive" else [f for f in AZIONI_MAP.values()],
                        temperature=0.1 if task_type == "proactive" else None,
                        system_instruction="\n\n".join(
                            str(m.get("content", "")) for m in messages
                            if m.get("role") == "system"
                        ) or SYSTEM_PROMPT,
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    )
                    chat = c.chats.create(model=gem_model, history=hist_content or None, config=gem_cfg)
                    response = chat.send_message(messages[-1]["content"])
                    
                    # Mock Response
                    class MockChoice:
                        def __init__(self, msg): self.message = msg
                    class MockResponse:
                        def __init__(self, msg): self.choices = [MockChoice(msg)]
                    
                    tool_calls = []
                    for part in response.candidates[0].content.parts:
                        if part.function_call:
                            tool_calls.append(type('obj', (object,), {
                                'id': f"call_{int(time.time())}",
                                'function': type('obj', (object,), {
                                    'name': part.function_call.name,
                                    'arguments': json.dumps(dict(part.function_call.args))
                                })
                            }))
                    try:
                        msg_content = response.text if not tool_calls else ""
                    except ValueError:
                        msg_content = ""
                    msg = type('obj', (object,), {'content': msg_content, 'role': 'assistant', 'tool_calls': tool_calls})
                    return MockResponse(msg)
                
                else:  # GROQ
                    model_to_use = groq_model
                    logger.info(f"🔥 [LLM] Tentativo con Groq ({model_to_use})")
                    ctx = comprimi_contesto(messages, tools=tools, max_tokens=5000)
                    # Non inviare tool se la richiesta è solo testo (es. riflessione proattiva)
                    # per non sforare il limite token di Groq free tier
                    usa_tools = tools is not None and len(ctx) > 1
                    if usa_tools:
                        return c.chat.completions.create(
                            model=model_to_use,
                            messages=ctx,
                            tools=GROQ_TOOLS,
                            tool_choice="auto"
                        )
                    else:
                        return c.chat.completions.create(
                            model=model_to_use,
                            messages=ctx,
                            **({"temperature": 0.1, "max_tokens": 120, "response_format": {"type": "json_object"}} if task_type == "proactive" else {})
                        )
            except Exception as e:
                logger.error(f"❌ [LLM FAIL] {nome.upper()}: {e}")
                ultimo_errore = e
                continue
        
        raise Exception(f"Entrambi i motori AI hanno fallito. Ultimo errore: {ultimo_errore}")

    def create_chat_completion(self, model, messages, **kwargs):
        """Alias per compatibilità con il loop di riflessione proattiva."""
        return self.chat(messages, tools=kwargs.get("tools"), task_type=kwargs.get("task_type", "general"))

# Inizializzazione AI (Dopo che AZIONI_MAP è pronta)
try:
    from config import GEMINI_API_KEY
    # google-genai (nuovo SDK): un Client unico; il modello si passa a ogni chiamata.
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    logger.info(f"🧬 [LLM] Inizializzazione Gemini Pro con {len(AZIONI_MAP)} tools...")
    # Modello primario per ragionamento e tool calling con System Instructions
    gemini_model_name = MODEL_PRIMARY if MODEL_PRIMARY.startswith("models/") else MODEL_BACKUP
    for f in AZIONI_MAP.values():
         logger.info(f"🔧 [Gemini Tool] Preparazione: {getattr(f, '__name__', 'unknown')}")
    logger.info("✅ [LLM] Gemini Pro inizializzato con successo.")
    # Modello per visione veloce — letto dal profilo VISION del router
    gemini_vision = model_router.get_profile(model_router.route_task("vision")).get("model") or 'models/gemini-3.8-flash'
    logger.info(f"✅ [LLM] Gemini Vision inizializzato ({gemini_vision}).")
except Exception as e:
    logger.error(f"❌ [LLM] Errore configurazione Gemini: {e}")
    import traceback
    logger.error(traceback.format_exc())

safe_client = SafeLLM(client, gemini_client)

# Cleanup rimosso

def genera_schema_tool(name, func):
    sig = inspect.signature(func)
    properties = {}
    required = []
    for p_name, param in sig.parameters.items():
        tipo_json = "string"
        if param.annotation is int: tipo_json = "integer"
        elif param.annotation is bool: tipo_json = "boolean"
        properties[p_name] = {"type": tipo_json, "description": "Parametro"}
        if param.default == inspect.Parameter.empty: required.append(p_name)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": func.__doc__ or "Azione",
            "parameters": {"type": "object", "properties": properties, "required": required}
        }
    }

GROQ_TOOLS = [genera_schema_tool(n, f) for n, f in AZIONI_MAP.items()]
WAKE_WORD = "ehi clio"
USER_ID = 0 # Dummy ID for local memory

# --- MEMORIA BIOGRAFICA ---
BIOGRAPHY_FILE = os.path.join(BASE_DIR, "data", "clio_biography.json")

def carica_biografia():
    try:
        with open(BIOGRAPHY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"user_profile": {"name": "User", "preferences": {}}}

def salva_biografia(bio):
    with open(BIOGRAPHY_FILE, "w", encoding="utf-8") as f:
        json.dump(bio, f, indent=4)

# Inizializza audio (pygame mixer)
try:
    pygame.mixer.init()
except Exception as e:
    logger.error(f"Errore inizializzazione audio: {e}")


async def genera_audio_elevenlabs(testo, percorso_file):
    """Genera audio usando l'API ElevenLabs. Costo a consumo."""
    import requests
    try:
        from config import ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID
        if not ELEVENLABS_API_KEY: return False
        
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": ELEVENLABS_API_KEY
        }
        data = {
            "text": testo,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.45,          # Meno stabile = più espressiva e naturale
                "similarity_boost": 0.80,   # Alta fedeltà alla voce originale
                "style": 0.35,              # Stile leggermente accentuato
                "use_speaker_boost": True   # Audio più chiaro e presente
            }
        }
        response = requests.post(url, json=data, headers=headers)
        if response.status_code == 200:
            with open(percorso_file, "wb") as f:
                f.write(response.content)
            return True
        logger.error(f"Errore ElevenLabs API: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"Errore ElevenLabs: {e}")
        return False

async def genera_audio_edgetts(testo, percorso_file):
    """Genera audio usando SOLO edge-tts con la voce unica di Clio.

    FIX 1: rimossi ElevenLabs e gTTS dal percorso TTS. Erano fonti di voci
    diverse (timbro/genere differente) e rendevano la voce incoerente.
    Il testo passa da `pronuncia_tts`: "Paint" si legge "Peint", non "Pa-ìnt".
    """
    try:
        communicate = edge_tts.Communicate(pronuncia_tts(testo), CURRENT_VOICE,
                                           rate=VOICE_RATE, pitch=VOICE_PITCH)
        await communicate.save(percorso_file)
        return True
    except Exception as e:
        logger.error(f"Errore edge-tts (voce {CURRENT_VOICE}): {e}")
        return False

def invia_notifica_push(titolo: str, messaggio: str) -> bool:
    """Invia una notifica push allo smartphone Android tramite Firebase (FCM)."""
    import requests
    try:
        from config import FCM_SERVER_KEY, FCM_DEVICE_TOKEN
        if not FCM_SERVER_KEY or not FCM_DEVICE_TOKEN:
            return False
            
        url = "https://fcm.googleapis.com/fcm/send"
        headers = {
            "Authorization": f"key={FCM_SERVER_KEY}",
            "Content-Type": "application/json"
        }
        data = {
            "to": FCM_DEVICE_TOKEN,
            "notification": { "title": titolo, "body": messaggio, "sound": "default" },
            "priority": "high"
        }
        res = requests.post(url, json=data, headers=headers)
        return res.status_code == 200
    except Exception as e:
        logger.error(f"Errore invio Push FCM: {e}")
        return False

def riproduci_beep():
    """Riproduce un bip veloce per confermare l'ascolto senza latenza di rete."""
    try:
        import ctypes
        # Frequenza 1000Hz, durata 100ms
        threading.Thread(target=lambda: ctypes.windll.kernel32.Beep(1000, 100), daemon=True).start()
    except:
        pass 
def riproduci_audio(percorso_file):
    """Riproduce un file audio locale in modo bloccante usando pygame."""
    try:
        if not os.path.exists(percorso_file):
             return
        AUDIO_PLAYING.set()  # Clio sta parlando: il loop wakeword sospende l'ascolto
        pygame.mixer.music.load(percorso_file)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(15)
        pygame.mixer.music.unload() # Libera il file
    except Exception as e:
        logger.error(f"Errore riproduzione audio: {e}")
    finally:
        AUDIO_PLAYING.clear()

def riproduci_audio_umanizzato(testo):
    """Spezza il testo in frasi e le riproduce con pause naturali e dizione ottimizzata."""
    import random
    import re
    import tempfile
    
    # Ottimizzazione testo (dizione e cadenza)
    testo_migliorato = pre_process_testo_tts(testo)
    
    # Pulizia testo da tag markdown per la lettura (se rimasti)
    testo_pulito = re.sub(r'\*|_|#|`|>', '', testo_migliorato)
    
    # Per risposte brevi o medie (< 500 caratteri), non spezziamo affatto.
    # Questo permette ad Ava di gestire la punteggiatura con prosodia naturale senza lag.
    if len(testo_pulito) < 500:
        frasi = [testo_pulito]
    else:
        # Se è molto lungo, spezziamo sui paragrafi o punti seguiti da spazi
        frasi = re.split(r'(?<=[.!?])\s+', testo_pulito)
    
    logger.info(f"Clio dice (Human-First): {testo_pulito}")
    
    for i, frase in enumerate(frasi):
        if not frase.strip(): continue
        if len(frase.strip()) < 2: continue # Salta pezzettini inutili
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
            
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Usiamo parametri di velocità leggermente variabili per simulare l'emozione/enfasi
        rate_var = VOICE_RATE # Potremmo variarlo leggermente qui
        successo = loop.run_until_complete(genera_audio_edgetts(frase, tmp_path))
        loop.close()
        
        if successo:
            riproduci_audio(tmp_path)
            # Nessuna pausa artificiale: lasciamo che pygame carichi il file successivo istantaneamente
            # se però ci sono più chunk per testi lunghi.
            pass
        
        try:
            if os.path.exists(tmp_path): os.remove(tmp_path)
        except: pass


# ─────────────────────────────────────────────────────────────────────────
#  STREAMING TTS (Fase 6): sintetizza e riproduce frase per frase in parallelo
#  alla generazione del LLM. La prima frase parte in ~1-2s.
# ─────────────────────────────────────────────────────────────────────────
async def _synth_edge(testo, percorso_file, voce):
    """Sintesi edge-tts di una singola frase con la voce unica (nessun fallback)."""
    try:
        communicate = edge_tts.Communicate(pronuncia_tts(testo), voce,
                                           rate=VOICE_RATE, pitch=VOICE_PITCH)
        await communicate.save(percorso_file)
        return True
    except Exception as e:
        logger.error(f"🔊 edge-tts fallito (voce {voce}): {e}")
        return False


class StreamingTTS:
    """Sintetizza e riproduce la risposta a frase mentre il LLM la genera.

    - `feed(delta)`: riceve i token di testo man mano (buffer di frase).
    - Appena una frase è completa (`.`/`!`/`?`/`…`/newline) viene messa in coda
      a un thread di sintesi (edge-tts) e riprodotta da un thread di playback.
    - `close()`: flush finale + attende la fine della riproduzione.
    - `stop()`: interruzione immediata (barge-in / stop manuale).

    `AUDIO_PLAYING` resta alzato per tutta la durata: il worker wake-word non
    ascolta la voce di Clio. `stop()` lo azzera subito per far riprendere il worker.
    """

    _BOUNDARY = re.compile(r'[.!?…]+(?:\s+|$)|[\r\n]+')

    def __init__(self, voce=None, max_clause=None, engine=None):
        self.voce = voce or CURRENT_VOICE
        try:
            from config import TTS_MAX_CLAUSE_CHARS as _mc
        except Exception:
            _mc = 180
        try:
            from config import TTS_STREAM_ENGINE as _eng
        except Exception:
            _eng = "edge"
        self.engine = engine or _eng
        self.max_clause = int(max_clause or _mc)
        self._buf = ""
        self._stop = threading.Event()
        self._sent_q = queue.Queue()
        self._play_q = queue.Queue()
        self._t0 = time.time()
        self.t_first_sentence = None
        self.t_first_audio = None
        self.t_done = None
        self._synth_thread = threading.Thread(target=self._synth_loop, daemon=True)
        self._play_thread = threading.Thread(target=self._play_loop, daemon=True)
        self._synth_thread.start()
        self._play_thread.start()
        AUDIO_PLAYING.set()

    # ── Input testo ──
    def feed(self, delta_text):
        if not delta_text or self._stop.is_set():
            return
        self._buf += delta_text
        self._drain()

    def _drain(self):
        while not self._stop.is_set():
            m = self._BOUNDARY.search(self._buf)
            if not m:
                # Frase troppo lunga senza punteggiatura: taglia all'ultimo spazio.
                if len(self._buf) > self.max_clause:
                    cut = self._buf.rfind(" ", 0, self.max_clause)
                    if cut <= 0:
                        cut = self.max_clause
                    self._enqueue(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    continue
                return
            sent = self._buf[:m.end()].strip()
            self._buf = self._buf[m.end():]
            if sent:
                self._enqueue(sent)

    def _enqueue(self, frase):
        if self.t_first_sentence is None:
            self.t_first_sentence = time.time() - self._t0
            logger.info(f"🔊 [LATENCY] prima frase pronta per il TTS dopo {self.t_first_sentence:.2f}s")
        self._sent_q.put(frase)

    # ── Chiusura / stop ──
    def close(self, wait=True):
        """Flush del buffer e attende la fine della riproduzione."""
        if not self._stop.is_set():
            resto = self._buf.strip()
            self._buf = ""
            if resto:
                self._enqueue(resto)
        self._sent_q.put(None)          # sentinella → il synth inoltra la sentinella al play
        if wait:
            self._synth_thread.join()
            self._play_thread.join()
        AUDIO_PLAYING.clear()
        self.t_done = time.time() - self._t0
        if self.t_first_sentence is not None:
            logger.info(f"🔊 [LATENCY] TTS completato in {self.t_done:.2f}s "
                        f"(prima frase {self.t_first_sentence:.2f}s, "
                        f"primo audio {self.t_first_audio if self.t_first_audio is not None else -1:.2f}s)")
        else:
            logger.info("🔊 [LATENCY] TTS completato (nessun testo da pronunciare).")

    def stop(self):
        """Interruzione immediata: ferma l'audio, svuota le code e libera il worker."""
        self._stop.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        for q in (self._sent_q, self._play_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        AUDIO_PLAYING.clear()

    def wait(self):
        """Attende (senza chiudere) la fine della riproduzione corrente."""
        self._synth_thread.join()
        self._play_thread.join()

    # ── Thread interni ──
    def _synth_loop(self):
        while True:
            frase = self._sent_q.get()
            if frase is None:
                self._play_q.put(None)
                return
            if self._stop.is_set():
                continue
            path = self._synth(frase)
            if path:
                self._play_q.put(path)

    def _synth(self, frase):
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                path = tmp.name
            testo = pre_process_testo_tts(frase)
            testo = re.sub(r'[*_`#\[\]()>]', '', testo).strip()
            if not testo:
                raise ValueError("frase vuota dopo la pulizia")
            loop = asyncio.new_event_loop()
            try:
                if self.engine == "edge":
                    ok = loop.run_until_complete(_synth_edge(testo, path, self.voce))
                else:
                    # Motore alternativo (es. ElevenLabs se configurato): riusa il
                    # percorso TTS classico, frase per frase.
                    ok = loop.run_until_complete(genera_audio_edgetts(testo, path))
            finally:
                loop.close()
            if ok:
                return path
        except Exception as e:
            logger.error(f"🔊 [TTS] sintesi fallita: {e}")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return None

    def _play_loop(self):
        while True:
            path = self._play_q.get()
            if path is None:
                return
            if self._stop.is_set():
                self._rm(path)
                continue
            if self.t_first_audio is None:
                self.t_first_audio = time.time() - self._t0
                logger.info(f"🔊 [LATENCY] primo audio in riproduzione dopo {self.t_first_audio:.2f}s")
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self._stop.is_set():
                        pygame.mixer.music.stop()
                        break
                    pygame.time.Clock().tick(15)
                pygame.mixer.music.unload()
            except Exception as e:
                logger.error(f"🔊 [TTS] riproduzione fallita: {e}")
            self._rm(path)

    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass


def prewarm_tts():
    """Pre-carica il motore TTS all'avvio: la prima sintesi non paga l'inizializzazione."""
    try:
        from config import TTS_PREWARM
        if not TTS_PREWARM:
            return
    except Exception:
        pass
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            path = tmp.name
        loop = asyncio.new_event_loop()
        try:
            ok = loop.run_until_complete(_synth_edge("Pronto.", path, CURRENT_VOICE))
        finally:
            loop.close()
        try:
            os.remove(path)
        except OSError:
            pass
        if ok:
            logger.info("🔊 TTS pre-warmed")
    except Exception as e:
        logger.warning(f"🔊 pre-warm TTS non riuscito: {e}")


def _append_conversation(role: str, text: str):
    """Aggiunge un messaggio alla cronologia conversazioni in memoria."""
    global CONVERSATION_HISTORY
    CONVERSATION_HISTORY.append({
        "role": role,
        "text": text,
        "ts": datetime.now().strftime("%H:%M")
    })
    if len(CONVERSATION_HISTORY) > MAX_HISTORY:
        CONVERSATION_HISTORY.pop(0)

# ── FIX 2: parole chiave che autorizzano la telemetria nel prompt ──
_STATO_PC_KEYWORDS = (
    "come sta il pc", "come sta il computer", "stato del pc", "stato del sistema",
    "stato sistema", "cpu", "ram", "memoria ram", "batteria", "disco",
    "spazio libero", "temperatura", "surriscald", "prestazioni", "risorse",
    "quanta memoria", "processi", "task manager", "usa la cpu", "quanta ram",
)


def _richiede_stato_sistema(testo: str) -> bool:
    """True solo se l'utente chiede esplicitamente lo stato del PC."""
    t = (testo or "").lower()
    return any(k in t for k in _STATO_PC_KEYWORDS)


def _telemetria_se_richiesta(testo: str, origin: str = "core") -> str:
    """FIX 2: restituisce CPU/RAM/Batteria SOLO se richiesti esplicitamente.

    Prima la telemetria veniva iniettata nel prompt a OGNI interazione: era
    questo a far partire il "pippone" su CPU/RAM anche dopo un semplice "Ehi Clio".
    """
    if not _richiede_stato_sistema(testo):
        return ""
    try:
        tel = policy.guarded_call("ottieni_telemetria_hardware",
                                  AZIONI_MAP["ottieni_telemetria_hardware"], {}, origin=origin)
        return (f"CPU {tel.get('cpu_percent','?')}%, RAM {tel.get('ram_percent','?')}%, "
                f"Batt {tel.get('battery_percent','?')}%")
    except Exception:
        return ""


def elabora_richiesta(testo_richiesta, origin="core"):
    """Elabora una richiesta testuale passandola al cervello Jarvis (Planning & Action)."""
    logger.info(f"🧠 [JARVIS] Elaborazione richiesta: {testo_richiesta}")

    # Aggiorna il tempo dell'ultima interazione
    global LAST_INTERACTION_TIME
    LAST_INTERACTION_TIME = time.time()

    # Salva il messaggio dell'utente nella cronologia
    _append_conversation("user", testo_richiesta)

    try:
        from jarvis_brain import jarvis

        # ── Contesto visivo + telemetria per JARVIS (FIX 2: solo se richiesta) ──
        sistema_str = _telemetria_se_richiesta(testo_richiesta, origin)

        context = {
            "visione": ULTIMA_VISIONE,
            "sistema": sistema_str
        }

        # Jarvis gestisce internamente pianificazione, tools e risposta finale
        risposta = jarvis.think_and_act(testo_richiesta, context=context, origin=origin)

        # Salva la risposta nella cronologia
        _append_conversation("clio", risposta)

        # Salvataggio in memoria semantica (per ricordi futuri)
        try:
            from memory_manager import memory_manager
            memory_manager.store_interaction(USER_ID, testo_richiesta, risposta, visual_context=ULTIMA_VISIONE)
        except Exception as me:
            logger.error(f"Errore salvataggio memoria: {me}")

        return risposta

    except Exception as e:
        errore = f"Capo, il mio cervello (Jarvis) ha avuto un intoppo: {e}"
        logger.error(errore)
        _append_conversation("clio", errore)
        log_path = os.path.join(BASE_DIR, "logs", "clio_log_notturno.md")
        with open(log_path, "a") as f:
             f.write(f"\n[ERRORE JARVIS]: {e}")
        return errore

def chiedi_a_clio(user_id, prompt):
    """Alias di compatibilità per elabora_richiesta (Legacy)."""
    return elabora_richiesta(prompt)

def elabora_richiesta_vocale(testo_richiesta, origin="voice", t_e2e0=None):
    """Come elabora_richiesta ma PARLA la risposta in streaming (Fase 6).

    La voce parte sulla prima frase mentre il LLM continua a generare: l'utente
    sente l'inizio in ~1-2s invece di aspettare l'intera risposta. Il testo
    completo resta disponibile per la Web UI e la memoria.
    """
    logger.info(f"🧠 [JARVIS] Elaborazione vocale (streaming): {testo_richiesta}")
    global LAST_INTERACTION_TIME
    LAST_INTERACTION_TIME = time.time()

    if (origin or "").strip().lower() in {"voice", "voce"}:
        testo_norm = (testo_richiesta or "").strip().lower()
        if re.search(r"\b(?:disattiva|spegni|ferma|stop)\s+(?:hawk\s*eye|occhio\s+di\s+falco|visione)\b|"
                     r"\bsmetti\s+di\s+(?:guardare|monitorare)\s+(?:lo\s+)?schermo\b",
                     testo_norm):
            stop_hawk_eye("disattivato da comando vocale")
            risposta = "Hawk Eye spento, Capo. Non monitoro più lo schermo."
            _append_conversation("user", testo_richiesta)
            _append_conversation("clio", risposta)
            clio_parla(risposta)
            return risposta
        if re.search(r"\b(?:attiva|avvia|accendi|inizia)\s+(?:hawk\s*eye|occhio\s+di\s+falco|"
                     r"monitoraggio\s+(?:dello\s+)?schermo|visione\s+(?:dello\s+)?schermo)\b|"
                     r"\b(?:guarda|osserva|monitora)\s+(?:lo\s+)?schermo\b",
                     testo_norm):
            avviato = start_hawk_eye("attivato da comando vocale esplicito")
            risposta = ("Hawk Eye attivato su tua richiesta, Capo."
                        if avviato else "Hawk Eye è già attivo, Capo.")
            _append_conversation("user", testo_richiesta)
            _append_conversation("clio", risposta)
            clio_parla(risposta)
            return risposta

    # Fallback reversibile: se lo streaming è disattivato da config, usa il
    # percorso classico (risposta completa → TTS).
    try:
        from config import STREAMING_TTS as _stream_enabled
    except Exception:
        _stream_enabled = True
    if not _stream_enabled:
        risposta = elabora_richiesta(testo_richiesta, origin=origin)
        pulito = re.sub(r"[*_`#\[\]()>]", "", risposta)
        clio_parla(pulito[:600] if len(pulito) < 600 else "Operazione completata.")
        return risposta

    _append_conversation("user", testo_richiesta)

    from jarvis_brain import jarvis

    # FIX 2: telemetria solo su richiesta esplicita (niente pippone ad ogni attivazione)
    sistema_str = _telemetria_se_richiesta(testo_richiesta, origin)
    context = {"visione": ULTIMA_VISIONE, "sistema": sistema_str}

    tts = StreamingTTS()
    global _CURRENT_TTS
    _SPEECH_STOP.clear()
    _CURRENT_TTS = tts
    latency = {}
    t_start = time.time()
    try:
        risposta = jarvis.think_and_act(testo_richiesta, context=context, origin=origin,
                                        on_text=tts.feed, latency=latency)
    except Exception as e:
        logger.error(f"Errore streaming vocale: {e}")
        tts.stop()
        risposta = f"Capo, ho avuto un intoppo: {e}"
        tts.close(wait=False)
        _CURRENT_TTS = None
        _append_conversation("clio", risposta)
        return risposta

    tts.close()   # attende la fine della riproduzione dell'ultima frase
    _CURRENT_TTS = None

    # ── Metriche di latenza end-to-end ──
    if latency.get("llm_first_token") is not None:
        logger.info(f"⏱️ [LATENCY] LLM primo token: {latency['llm_first_token']*1000:.0f} ms "
                    f"(completamento {latency.get('llm_total', 0)*1000:.0f} ms, "
                    f"{latency.get('llm_steps', 0)} passaggi)")
    if tts.t_first_audio is not None:
        logger.info(f"⏱️ [LATENCY] end-to-end (richiesta → primo audio): {tts.t_first_audio*1000:.0f} ms")
        if t_e2e0 is not None:
            e2e = (tts._t0 + tts.t_first_audio) - t_e2e0
            logger.info(f"⏱️ [LATENCY] end-to-end (fine voce utente → primo audio Clio): {e2e*1000:.0f} ms")
    logger.info(f"⏱️ [LATENCY] totale risposta+voce: {(time.time()-t_start)*1000:.0f} ms")

    _append_conversation("clio", risposta)
    try:
        from memory_manager import memory_manager
        memory_manager.store_interaction(USER_ID, testo_richiesta, risposta, visual_context=ULTIMA_VISIONE)
    except Exception as me:
        logger.error(f"Errore salvataggio memoria: {me}")
    return risposta

def pre_process_testo_tts(testo):
    """Migliora la dizione e la cadenza aggiungendo pause e correggendo pronunce critiche."""
    # 1. Pulizia Markdown residua
    testo = re.sub(r'\*|_|#|`|>', '', testo)
    
    # 2. Correzioni di dizione tecnica (Italiano JARVIS)
    dizionario_fonetico = {
        "LLM": "el-el-emme",
        "SDK": "es-di-kappa",
        "API": "a-pì-ì",
        "VS Code": "vi-esse code",
        "ADB": "a-di-bi",
        "PWA": "pi-double-u-a",
        "URL": "u-erre-elle",
        "JSON": "ge-ison",
        "Python": "paiton",
        "GitHub": "ghit-ab",
        "Chrome": "crom",
        "WhatsApp": "uozz-app",
        "PC": "pì-cì",
        "AI": "a-ì",
        "JARVIS": "Jarvis",
        "Capo": "Capo",
        "Sì": "Sì"
    }
    
    for word, phon in dizionario_fonetico.items():
        # Case insensitive replace
        pattern = re.compile(re.escape(word), re.IGNORECASE)
        testo = pattern.sub(phon, testo)

    # 3. Pacing naturale (Pause e intercalari se mancano)
    testo = testo.replace("...", "... ")

    # 4. Pulizia punteggiatura duplicata (evita doppi punti che confondono TTS)
    testo = re.sub(r'\.{4,}', '...', testo)
    testo = re.sub(r'\s{2,}', ' ', testo)

    # 5. Umanizzazione punteggiatura (senza virgole forzate che suonano robotiche)
    testo = testo.replace(":", ": ")

    return testo

def clio_parla(testo):
    """Sintesi vocale umanizzata con pacing naturale."""
    SYSTEM_STATE["last_speech"] = testo
    try:
        riproduci_audio_umanizzato(testo)
    except Exception as e:
         logger.error(f"Errore nella riproduzione umanizzata: {e}")

# ── WAKE-WORD: varianti accettate (match esatto) ──
# Whisper trascrive spesso "Ehi Clio" come "E' Cleo" / "E Cleo" / "Hey Klio":
# includiamo le varianti più comuni e, sotto, un match fuzzy sul token "clio".
WAKE_VARIANTS = [
    "ehi clio", "ehi cleo", "ehi clie", "ehi klio", "ehi khlio", "ehi cloe",
    "hey clio", "hey cleo", "hey klio", "ei clio", "ei cleo", "ei klio",
    "ok clio", "ok cleo", "ok klio", "oh clio", "ehi chio",
    "e clio", "e cleo", "e klio", "eh clio", "eh cleo",
]

def _normalizza_testo(testo: str) -> str:
    """Normalizza il testo per il matching della wake-word (minuscole, no punteggiatura)."""
    t = (testo or "").lower()
    t = re.sub(r"[^\wàèéìòù\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def rileva_wakeword(testo: str):
    """Rileva "Ehi Clio" con match esatto + fuzzy (Whisper a volte scrive varianti).

    Ritorna (trovata: bool, resto_comando: str) — il resto è l'eventuale comando
    pronunciato nella stessa frase (one-shot: "ehi clio apri chrome").
    """
    import difflib
    t = _normalizza_testo(testo)
    if not t:
        return False, ""

    # 1) Varianti multi-parola: match esatto
    for v in WAKE_VARIANTS:
        if v in t:
            return True, t.split(v, 1)[-1].strip()

    # 2) Match fuzzy sul token simile a "clio" (Whisper scrive "cleo", "klio", "cloe").
    #    Vincoli per evitare falsi positivi (italiano):
    #      - inizio "cl/kl/chl/khl"  → esclude "ciao", "chilo", "Kilo"
    #      - rapporto ≥ 0.70          → "cleo"/"clio"=0.75, "klio"=0.75
    #      - finisce con vocale       → esclude "clic"
    VOCALI = "aeiouàèéìòù"
    for i, w in enumerate(t.split()):
        if len(w) < 4 or not w.startswith(("cl", "kl", "chl", "khl")):
            continue
        if w[-1] not in VOCALI:
            continue
        if difflib.SequenceMatcher(None, w, "clio").ratio() >= 0.70:
            return True, " ".join(t.split()[i + 1:]).strip()

    return False, ""

def _jarvis_display_wake():
    """Effetto "Jarvis": sveglia il monitor e porta la Web UI in primo piano.

    Chiamato al rilevamento della wake-word, PRIMA di dire "Dimmi". Eseguito in
    un thread daemon separato: le operazioni (avvio di Chrome, focus finestra)
    possono richiedere qualche centinaio di ms e non devono ritardare la voce
    di conferma. Cosi' l'utente vede la UI aprirsi *mentre* Clio parla.

    Non solleva mai eccezioni: un display o un browser che non risponde non deve
    bloccare il loop di ascolto.
    """
    def _worker():
        try:
            import display_control
            display_control.wake_screen()
            # Se la UI e' gia' aperta la portiamo in primo piano, altrimenti la
            # apriamo (open_clio_ui gestisce internamente il caso "gia' aperta"
            # per non creare finestre duplicate).
            if not display_control.focus_clio_window():
                display_control.open_clio_ui()
            logger.info("🖥️ [JARVIS] Schermo svegliato e UI in primo piano.")
        except Exception as e:
            logger.warning(f"[JARVIS] Risveglio display/UI fallito: {e}")

    threading.Thread(target=_worker, daemon=True, name="jarvis-display").start()

def ascolto_wakeword_loop():
    """Loop continuo di ascolto della wake-word "Ehi Clio" (stile Alexa/Jarvis).

    Flusso quando la wake-word viene rilevata:
      1. Risponde vocalmente "Dimmi" (conferma breve).
      2. Registra l'audio del Capo (max 10s o fino a ~1s di silenzio).
      3. Trascrive con Whisper (Groq).
      4. Invia il testo a Jarvis (think_and_act).
      5. Risponde con TTS (edge-tts) e mostra il testo in Web UI.

    Rumore/esclusi: PRIVACY_MODE muta il mic; mentre Clio parla (AUDIO_PLAYING)
    l'ascolto è sospeso per evitare che si ri-attivi con la propria voce.
    """
    global MIC_OVERRIDE, PRIVACY_MODE
    recognizer = sr.Recognizer()
    microfono = sr.Microphone()

    recognizer.dynamic_energy_threshold = True
    recognizer.pause_threshold = 1.0        # ~1s di silenzio chiude la frase
    recognizer.non_speaking_duration = 0.6
    recognizer.energy_threshold = 300       # soglia base sensibile

    # ── ANTI-FALSI-POSITIVI: soglie, filtri e cooldown ──
    RMS_SOGLIA = 0.01               # gate wake-word: sensibile (deve cogliere "Ehi Clio")
    RMS_SOGLIA_COMANDO = 0.05       # gate comando: solo voce chiara, non rumore di fondo
    COOLDOWN_NON_MATCH = 3.0        # pausa dopo una trascrizione che NON è la wake-word
    SETTLE_DOPO_TTS = 0.6           # attesa dopo che Clio smette di parlare (coda eco)
    PAUSA_POST_TTS = 0.5            # pausa dopo "Dimmi" prima di registrare il comando
    SILENZIO_STOP_COMANDO = 1.5     # il comando si chiude dopo 1.5s di silenzio consecutivo
    MAX_COMANDO_S = 10.0            # durata massima della registrazione del comando
    # Parole/rumore che Whisper allucina spesso dal silenzio: da ignorare
    PAROLE_RUMORE = {
        "grazie", "ciao", "ok", "sì", "si", "no", "ehi", "hey", "e", "eh",
        "oh", "ah", "mm", "mh", "sottotitoli", "grazie mille", "il", "la",
    }

    def _rms_raw(raw):
        """RMS normalizzato (0..1) di un buffer PCM 16-bit. Campiona a passi per restare leggero."""
        try:
            n = len(raw)
            if n < 2:
                return 0.0
            n -= n % 2                       # allinea a 16 bit (2 byte/campione)
            campioni = memoryview(raw)[:n].cast("h")
            totale = len(campioni)
            passo = max(1, totale // 2000)   # ~2000 campioni bastano per stimare il livello
            somma = 0
            conta = 0
            for i in range(0, totale, passo):
                s = campioni[i]
                somma += s * s
                conta += 1
            if conta == 0:
                return 0.0
            return ((somma / conta) ** 0.5) / 32768.0
        except Exception:
            return 1.0                       # in caso di dubbio non bloccare l'ascolto

    def _livello_rms(audio):
        """RMS normalizzato (0..1) di un blocco sr.AudioData."""
        return _rms_raw(audio.get_raw_data())

    def _e_rumore(testo):
        """True se la trascrizione è rumore: vuota, 1-2 caratteri o parola comune di rumore."""
        t = _normalizza_testo(testo)
        if len(t) < 3:
            return True
        if t in PAROLE_RUMORE:
            return True
        return False

    def _testo_utile(testo):
        """True se la trascrizione merita attenzione (né vuota, né <3 char, né parola di rumore)."""
        return not _e_rumore(testo)

    def _attendi_fine_eco():
        """Anti-eco: sospende il worker finché Clio parla e per la breve coda successiva.

        Se la riproduzione è stata interrotta manualmente (barge-in/stop) il worker
        riprende SUBITO, senza la pausa anti-eco.
        """
        while AUDIO_PLAYING.is_set():
            time.sleep(0.1)
        if _SPEECH_STOP.is_set():
            _SPEECH_STOP.clear()
            return
        time.sleep(SETTLE_DOPO_TTS)

    def _trascrivi(audio, contesto="wakeword", log=True):
        """Trascrive un blocco audio: Whisper (Groq) primario, Google come fallback.

        log=False durante l'ascolto continuo della wake-word: si evita di
        stampare una riga per ogni trascrizione e si riduce lo spam nei log.
        """
        _t_stt = time.time()
        # 1) Whisper via Groq (molto più affidabile di Google sull'italiano)
        if client is not None:
            try:
                wav = audio.get_wav_data(convert_rate=16000, convert_width=2)
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    tmp.write(wav)
                    tmp_path = tmp.name
                try:
                    with open(tmp_path, "rb") as f:
                        tr = client.audio.transcriptions.create(
                            file=(os.path.basename(tmp_path), f.read()),
                            model="whisper-large-v3",
                            language="it",
                            temperature=0.0,
                        )
                    testo = (getattr(tr, "text", "") or "").strip()
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                if log and testo:
                    logger.info(f"📝 [WHISPER/{contesto}] \"{testo}\"")
                _lat_log(f"STT({contesto})", _t_stt)
                return testo
            except Exception as e:
                logger.warning(f"⚠️ [WHISPER/{contesto}] errore ({type(e).__name__}: {e}) — uso Google.")
        # 2) Fallback: Google Speech Recognition
        testo = recognizer.recognize_google(audio, language="it-IT")
        if log and testo:
            logger.info(f"📝 [GOOGLE/{contesto}] \"{testo}\"")
        return testo

    def _ascolta_blocco(timeout_s, phrase_limit):
        """Registra un blocco dal microfono. Solleva WaitTimeoutError se silenzio prolungato."""
        with microfono as source:
            return recognizer.listen(source, timeout=timeout_s, phrase_time_limit=phrase_limit)

    def _registra_comando(max_s=MAX_COMANDO_S, timeout_avvio=6.0,
                          silenzio_stop=SILENZIO_STOP_COMANDO, soglia=RMS_SOGLIA_COMANDO):
        """Registra il comando con voice-activity detection (silence detection reale).

        - parte alla PRIMA voce sopra `soglia` (entro `timeout_avvio`);
        - si chiude dopo `silenzio_stop` secondi di silenzio consecutivo;
        - dura al massimo `max_s` secondi.
        Solleva sr.WaitTimeoutError se non arriva voce (niente registrazione di solo rumore).
        """
        with microfono as source:
            rate = getattr(source, "SAMPLE_RATE", 16000)
            width = getattr(source, "SAMPLE_WIDTH", 2)
            blocco = max(256, int(rate * 0.05))   # ~50ms per lettura
            frames = []
            iniziato = False
            silenzio_dal = None
            avvio = time.time()
            while True:
                try:
                    buf = source.stream.read(blocco, exception_on_overflow=False)
                except Exception:
                    break
                frames.append(buf)
                if _rms_raw(buf) >= soglia:
                    iniziato = True
                    silenzio_dal = None
                elif iniziato:
                    if silenzio_dal is None:
                        silenzio_dal = time.time()
                    elif time.time() - silenzio_dal >= silenzio_stop:
                        break
                ora = time.time()
                if not iniziato:
                    if ora - avvio >= timeout_avvio:
                        raise sr.WaitTimeoutError("nessuna voce rilevata")
                    continue
                if ora - avvio >= max_s:
                    break
            # Nessuna voce sopra soglia → non trascrivere (evita le allucinazioni "Grazie")
            if not frames or not iniziato:
                raise sr.WaitTimeoutError("nessuna voce rilevata")
            return sr.AudioData(b"".join(frames), rate, width)

    with microfono as source:
        logger.info("🎤 Calibrazione microfono (1.5s)...")
        recognizer.adjust_for_ambient_noise(source, duration=1.5)

    logger.info(f"🟢 CLIO PRONTA. In ascolto wake-word: '{WAKE_WORD}' (Whisper Groq)")
    update_state("mic", "active")
    set_system_mic_mute(PRIVACY_MODE)

    _conv_chiudi()   # stato pulito all'avvio del worker

    while True:
        try:
            # Nuovo giro del worker: nessun comando in corso → il timer di conversazione può ripartire
            CONVERSAZIONE_BUSY.clear()

            # ── Privacy Mode: microfono mutato ──
            if PRIVACY_MODE:
                update_state("mic", "muted")
                time.sleep(1)
                continue

            # ── Mentre Clio parla non ascoltare (evita l'auto-risveglio) ──
            if AUDIO_PLAYING.is_set():
                time.sleep(0.2)
                continue

            # ── Attivazione manuale (click logo o API) ──
            if MIC_OVERRIDE:
                MIC_OVERRIDE = False
                logger.info("⚡ [WAKEWORD] Attivazione manuale da Web UI.")
                testo_riconosciuto = WAKE_WORD
                resto_comando = ""
                wakeword_trovata = True
            # ── CONVERSAZIONE ATTIVA: ascolto senza wake-word (finestra di 10s) ──
            elif CONVERSAZIONE_ATTIVA:
                rimanente = CONVERSAZIONE_SCADENZA - time.time()
                if rimanente <= 0:
                    _conv_chiudi("timeout 10s")
                    update_state("mic", "active")
                    continue
                update_state("mic", "listening")
                CONVERSAZIONE_BUSY.set()   # alza il gate: stiamo catturando un comando
                try:
                    try:
                        audio_conv = _registra_comando(
                            max_s=min(MAX_COMANDO_S, max(2.0, rimanente)),
                            timeout_avvio=rimanente)
                    except sr.WaitTimeoutError:
                        _conv_chiudi("nessun seguito entro 10s")
                        update_state("mic", "active")
                        continue
                    try:
                        # log=False: in conversazione logghiamo SOLO i comandi reali (riga sotto),
                        # così una trascrizione di rumore non finisce nei log come comando.
                        comando_testo = _trascrivi(audio_conv, "conversazione", log=False)
                    except sr.UnknownValueError:
                        update_state("mic", "listening")
                        continue

                    # FILTRO RUMORE: vuoto, 1-2 caratteri o parole comuni ("grazie", "ciao", "ok"...)
                    # → ignora e resta in attesa, senza inviare nulla a Jarvis e senza loggare.
                    if _e_rumore(comando_testo):
                        update_state("mic", "listening")
                        continue

                    # Chiusura esplicita della conversazione?
                    if _e_chiusura_conversazione(comando_testo):
                        _conv_chiudi(f"frase di chiusura: '{comando_testo}'")
                        update_state("mic", "active")
                        clio_parla("Prego, a dopo!")
                        continue

                    logger.info(f"🗣️ [CONVERSAZIONE] Seguito: '{comando_testo}'")
                    t_cmd = time.time()
                    risposta = elabora_richiesta_vocale(comando_testo)
                    _lat_log("comando → fine risposta (streaming)", t_cmd)
                    update_state("mic", "active")
                    _conv_rinova()   # rinnova la finestra di 10s dopo la risposta
                    continue
                finally:
                    CONVERSAZIONE_BUSY.clear()
            else:
                # ── Ascolto wake-word (blocchi brevi, gated da volume + anti-eco) ──
                _attendi_fine_eco()   # anti-eco: l'intero worker attende la fine della voce di Clio
                update_state("mic", "active")
                audio = _ascolta_blocco(timeout_s=6, phrase_limit=6)

                # 1) GATE DI VOLUME: se è silenzio/rumore debole, salta la trascrizione
                #    (non chiama Whisper → niente allucinazioni tipo "Grazie" sul silenzio).
                if _livello_rms(audio) < RMS_SOGLIA:
                    continue

                # 2) Trascrizione SENZA log (loggheremo solo su wake-word reale).
                testo_riconosciuto = _trascrivi(audio, "wakeword", log=False)

                # 3) FILTRO trascrizioni inutili: vuote, <3 caratteri o parole comuni di rumore.
                #    Ignorate silenziosamente (nessun log) + cooldown per non martellare Whisper.
                if not _testo_utile(testo_riconosciuto):
                    time.sleep(COOLDOWN_NON_MATCH)
                    continue

                wakeword_trovata, resto_comando = rileva_wakeword(testo_riconosciuto)
                if not wakeword_trovata:
                    # 4) COOLDOWN: nessuna nuova trascrizione per qualche secondo.
                    time.sleep(COOLDOWN_NON_MATCH)
                    continue

            # ══ WAKE-WORD RILEVATA! ══
            logger.info(f"🎤 Wake-word rilevata! (\"{testo_riconosciuto}\")")
            update_state("mic", "listening")
            SYSTEM_STATE["last_wake"] = datetime.now().strftime("%H:%M:%S")
            _conv_apri()   # entra in conversazione attiva (pronta per i seguiti senza wake-word)
            CONVERSAZIONE_BUSY.set()   # sospende il timer durante cattura+elaborazione+risposta

            # Effetto "Jarvis": sveglia il monitor e mette la UI in primo piano.
            # Parte in un thread separato PRIMA di "Dimmi", cosi' la UI si apre
            # mentre Clio parla, senza aggiungere latenza alla voce.
            _jarvis_display_wake()

            # a. Conferma vocale breve — parte SUBITO (nessun delay prima del TTS)
            clio_parla("Dimmi")
            # Attende la fine REALE di qualsiasi audio e poi una pausa anti-eco,
            # così la registrazione non capta la voce di Clio ("Dimmi").
            while AUDIO_PLAYING.is_set():
                time.sleep(0.1)
            time.sleep(PAUSA_POST_TTS)

            if resto_comando:
                # b. One-shot: wake-word + comando nella stessa frase
                comando_testo = resto_comando
                t_rec_done = time.time()
                logger.info(f"⚡ [WAKEWORD] One-shot: '{comando_testo}'")
            else:
                # b. Registra il comando con VAD: parte alla prima voce sopra soglia
                #    (0.05) e si ferma dopo 1.5s di silenzio consecutivo (max 10s).
                try:
                    logger.info("🔴 [WAKEWORD] Registrazione comando (VAD, max 10s)...")
                    audio_cmd = _registra_comando()
                    t_rec_done = time.time()          # fine voce utente (base per la latenza)
                    logger.info("⏹️ [WAKEWORD] Registrazione terminata.")
                except sr.WaitTimeoutError:
                    logger.info("🔕 [WAKEWORD] Nessun comando ricevuto dopo la wake-word.")
                    _conv_chiudi("nessun comando dopo 'Dimmi'")
                    update_state("mic", "active")
                    continue
                # c. Trascrizione con Whisper (Groq)
                _lat_log("fine registrazione → inizio STT", t_rec_done)
                try:
                    comando_testo = _trascrivi(audio_cmd, "comando")
                except sr.UnknownValueError:
                    logger.info("🔕 [WAKEWORD] Audio non comprensibile dopo la wake-word.")
                    update_state("mic", "active")
                    continue
                # d. Filtro rumore: "Grazie"/"Ciao"/1-2 caratteri → ignora e NON inviare a Jarvis
                if _e_rumore(comando_testo):
                    logger.info("🔇 [WAKEWORD] Trascrizione ignorata (rumore).")
                    update_state("mic", "active")
                    continue
                logger.info(f"🗣️ [WAKEWORD] Comando trascritto: '{comando_testo}'")

            # d. Rete di sicurezza anti-rumore (vale anche per l'one-shot):
            #    nessuna trascrizione tipo "Grazie"/"Ciao" deve arrivare a Jarvis.
            if _e_rumore(comando_testo):
                logger.info("🔇 [WAKEWORD] Trascrizione ignorata (rumore).")
                update_state("mic", "active")
                continue

            # d-bis. Chiusura esplicita della conversazione ("Grazie Clio", "Basta", "Fine"...)
            if _e_chiusura_conversazione(comando_testo):
                _conv_chiudi(f"frase di chiusura: '{comando_testo}'")
                update_state("mic", "active")
                clio_parla("Prego, a dopo!")
                continue

            # e. Invia il testo a Jarvis: risposta parlata IN STREAMING (Fase 6)
            _lat_log("fine registrazione → inizio elaborazione", t_rec_done)
            risposta = elabora_richiesta_vocale(comando_testo, t_e2e0=t_rec_done)
            update_state("mic", "active")

            # ── Resta in conversazione attiva per un eventuale seguito (finestra 10s) ──
            _conv_rinova()

        except (sr.WaitTimeoutError, sr.UnknownValueError):
            # Silenzio o audio non riconosciuto: torna subito in ascolto
            continue
        except sr.RequestError as e:
            logger.warning(f"⚠️ [WAKEWORD] Errore servizio STT: {e}")
            time.sleep(5)
        except Exception as e:
            logger.error(f"❌ [WAKEWORD] Errore loop audio: {e}", exc_info=True)
            time.sleep(1)

def wakeword_supervisor_loop():
    """Supervisore del thread wake-word: lo mantiene vivo e lo riavvia dopo 2s se crasha."""
    while True:
        try:
            logger.info("🎧 [WAKEWORD] Avvio worker di ascolto...")
            ascolto_wakeword_loop()
            logger.warning("⚠️ [WAKEWORD] Worker terminato inaspettatamente. Riavvio tra 2s.")
        except Exception as e:
            logger.error(f"❌ [WAKEWORD] Crash del worker: {e}", exc_info=True)
            try:
                update_state("mic", "inactive")
            except Exception:
                pass
            logger.warning("♻️ [WAKEWORD] Riavvio tra 2 secondi...")
        time.sleep(2)

def behavioral_tracking_loop():
    """Thread separato per mappare le abitudini senza bloccare altri loop."""
    logger.info("🧬 Behavioral Engine in ascolto...")
    while True:
        try:
            behavior_engine.track_activity()
        except: pass
        time.sleep(10) # Campionamento ogni 10s

def adb_self_healing_loop():
    """Tenta periodicamente di ricollegare lo smartphone via ADB se disconnesso."""
    device_ip = DEVICE_IP
    adb_path = ADB_PATH
    
    logger.info(f"Avvio monitoraggio ADB Self-Healing per {device_ip}...")
    
    while True:
        try:
            import subprocess
            res = subprocess.run(f'"{adb_path}" devices', shell=True, capture_output=True, text=True)
            if device_ip not in res.stdout:
                logger.warning(f"Bridge ADB perso. Tentativo di riconnessione a {device_ip}...")
                update_state("adb", "healing")
                subprocess.run(f'"{adb_path}" connect {device_ip}:5555', shell=True, capture_output=True)
            else:
                # Se connesso, controlla se è 'unauthorized' o 'offline'
                if f"{device_ip}:5555\toffline" in res.stdout or "unauthorized" in res.stdout:
                     logger.warning("Device presente ma non pronto. Reset connessione...")
                     update_state("adb", "healing")
                     subprocess.run(f'"{adb_path}" disconnect {device_ip}:5555', shell=True, capture_output=True)
                else:
                     update_state("adb", "connected")
        except Exception as e:
            logger.error(f"Errore loop self-healing: {e}")
            update_state("adb", "disconnected")
        
        time.sleep(60) # Controlla ogni minuto

# ==================== ROTTE API LOCALI ====================

@app.route("/api/ping", methods=["GET"])
@require_clio_key
def ping():
    return jsonify({"status": "online", "message": "Clio Core is active"}), 200

@app.route("/api/lite_agent_sync", methods=["POST"])
@require_clio_key
def lite_agent_sync():
    """Riceve telemetria/attività dal PC Portatile (Agente leggero)."""
    try:
        data = request.json
        pc_name = data.get("pc_name", "Portatile")
        active_window = data.get("active_window", "")
        
        if "laptop_activity" not in SYSTEM_STATE:
            SYSTEM_STATE["laptop_activity"] = {}
            
        SYSTEM_STATE["laptop_activity"][pc_name] = {
            "active_window": active_window,
            "last_seen": time.time()
        }
        return jsonify({"status": "ok", "message": "Sincronizzato"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

@app.route("/api/debug", methods=["POST"])
@require_clio_key
def debug_log():
    data = request.json
    msg = data.get("msg", "No message")
    logger.info(f"🌐 [BROWSER_DEBUG] {msg}")
    return jsonify({"status": "ok"}), 200

# --- PROACTIVE MONITORING (Terzo Occhio) ---
async def _invia_tg_async(testo):
    """Funzione helper asincrona per inviare messaggi su Telegram."""
    global LAST_INTERACTION_TIME
    LAST_INTERACTION_TIME = time.time() # L'interazione via TG conta come attività
    if TG_APP:
        try:
            await TG_APP.bot.send_message(chat_id=MY_TELEGRAM_USER_ID, text=testo)
        except Exception as e:
            logger.error(f"Errore invio messaggio TG: {e}")

def invia_notifica_clio(testo, parla=True, tipo="proactive"):
    """Invia una notifica proattiva all'utente, gestendo la continuità cross-dispositivo."""
    global HUB_NOTIFICATIONS
    logger.info(f"📣 [NOTIFICA] {testo}")

    # Accoda notifica per il frontend (Hub toasts)
    HUB_NOTIFICATIONS.append({
        "text": testo,
        "type": tipo,
        "ts": datetime.now().strftime("%H:%M")
    })
    HUB_NOTIFICATIONS = HUB_NOTIFICATIONS[-15:]  # Mantieni solo le ultime 15

    # Salva in cronologia conversazioni
    _append_conversation("clio", f"[🔔 Proattivo] {testo}")
    
    is_afk = (time.time() - LAST_INTERACTION_TIME) > 600 # 10 minuti di inattività
    
    # 1. Telegram (Sempre per continuità)
    if TG_APP and TG_LOOP:
        asyncio.run_coroutine_threadsafe(_invia_tg_async(testo), TG_LOOP)
    
    # 2. Voce (Solo se non AFK e non in privacy)
    if parla and not PRIVACY_MODE and SYSTEM_STATE["mic"] != "muted" and not is_afk:
        # Avviamo la voce in un thread per non bloccare il loop proattivo
        threading.Thread(target=lambda: riproduci_audio_umanizzato(testo), daemon=True).start()
    elif is_afk:
        logger.info("👤 [AFK] Utente lontano dal PC. Notifica inviata su Telegram e Push Mobile.")
        # 3. Notifica Mobile Push (FCM) - Solo in caso di assenza al PC
        invia_notifica_push("Clio", testo)

PROACTIVE_INTERVAL_SECONDS = 20 * 60


def _proactive_issues(telemetry):
    """Only measured, concrete conditions can produce a proactive notification."""
    if not isinstance(telemetry, dict):
        return {}
    def number(key):
        value = telemetry.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100 else None
    issues = {}
    ram = number("ram_percent")
    cpu = number("cpu_percent")
    battery = number("battery_percent")
    if battery is not None and battery < 15 and telemetry.get("power_plugged") is False:
        issues["battery"] = f"Batteria al {battery:g}% e alimentatore scollegato. Collega il PC all'alimentazione."
    if ram is not None and ram >= 90:
        issues["ram"] = f"RAM utilizzata al {ram:g}%. Se il PC rallenta, controlla le applicazioni piu pesanti in Gestione attivita."
    if cpu is not None and cpu >= 95:
        issues["cpu"] = f"CPU al {cpu:g}% nella rilevazione attuale. Se il carico non e previsto, verifica i processi in Gestione attivita."
    return issues


def proactive_intelligence():
    """Sobria, opzionale e vincolata a misure reali; nessuna azione autonoma."""
    telemetry = policy.guarded_call("ottieni_telemetria_hardware", AZIONI_MAP["ottieni_telemetria_hardware"], {}, origin="proactive")
    issues = _proactive_issues(telemetry)
    if not issues:
        logger.info("[PROACTIVE] Nessun problema concreto: nessun messaggio e nessuna chiamata LLM.")
        return
    now = time.monotonic()
    last_notice = getattr(proactive_intelligence, "last_notice", None)
    if last_notice is not None and now - last_notice < PROACTIVE_INTERVAL_SECONDS:
        return
    prompt = """Sei un assistente proattivo. Non inventare attivita, non fare battute,
non usare un tono troppo confidenziale. Basati SOLO sui dati di telemetria reali
forniti (CPU, RAM, batteria). Processi e finestra attiva non forniti sono sconosciuti.
Fornisci un suggerimento solo se c'e un problema concreto o un'azione utile e pertinente.
Se non c'e nulla di rilevante, NON generare alcun messaggio: restituisci {}.
Non suggerire di aprire applicazioni se non e chiaramente richiesto o non c'e un problema.
Non dedurre intenzioni, stress o attivita dall'orario. Non eseguire azioni.
Rispondi SOLO in JSON: {} oppure {"problema": "uno degli identificatori forniti"}.
Il testo della notifica e gia verificato dal programma: seleziona solo il problema pertinente.
"""
    try:
        response = safe_client.create_chat_completion(
            model="gemini-3.8-flash",
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": "Valuta questi problemi misurati: " + json.dumps(issues, ensure_ascii=False)}],
            task_type="proactive", max_tokens=120, temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        issue = result.get("problema") if isinstance(result, dict) else None
        if isinstance(issue, str) and issue in issues:
            # Never relay free-form model text or execute suggested tools.
            invia_notifica_clio(issues[issue])
            proactive_intelligence.last_notice = time.monotonic()
            logger.info("[PROACTIVE] Notifica basata su misura reale: %s", issue)
        else:
            logger.info("[PROACTIVE] Nessuna segnalazione pertinente selezionata.")
    except Exception as exc:
        logger.error("Errore riflessione proattiva: %s", exc)


def proactive_loop_func():
    """Controlli ogni minuto; riflessioni e notifiche distanziate di 20 minuti."""
    last_intel_check = None
    while True:
        try:
            now = time.monotonic()
            telemetry = policy.guarded_call("ottieni_telemetria_hardware", AZIONI_MAP["ottieni_telemetria_hardware"], {}, origin="proactive")
            issues = _proactive_issues(telemetry)
            last_notice = getattr(proactive_intelligence, "last_notice", None)
            if "battery" in issues and (last_notice is None or now - last_notice >= PROACTIVE_INTERVAL_SECONDS):
                invia_notifica_clio(issues["battery"], parla=True)
                proactive_intelligence.last_notice = time.monotonic()
            # No unsolicited application-opening suggestions based on habits.
            if last_intel_check is None or now - last_intel_check >= PROACTIVE_INTERVAL_SECONDS:
                last_intel_check = now
                proactive_intelligence()
        except Exception as exc:
            logger.error("Errore Proactive Loop: %s", exc)
        time.sleep(60)

# --- OCCHIO DI FALCO (Vision Loop) ---

def vision_loop_func():
    """LEGACY / NON AVVIATO.

    Era il vecchio loop periodico della visione (unico punto che impostava
    SYSTEM_STATE["vision"]="active"), ma non viene più avviato da nessuna parte:
    il worker di visione attivo è monitor_active_window(), che ora gestisce anche
    lo stato. Conservata solo come riferimento.
    """
    global ULTIMA_VISIONE
    logger.info("👁️ Occhio di Falco attivo (Reattivo ai cambi finestra).")
    
    import pygetwindow as gw
    last_window_title = ""
    last_periodic_check = 0
    
    SYSTEM_STATE["vision"] = "active"
    while True:
        try:
            # Condizioni per l'analisi:
            tempo_attuale = time.time()
            is_afk = (tempo_attuale - LAST_INTERACTION_TIME) > 1800 # 30 min AFK
            
            # Cambio finestra detection
            current_window = gw.getActiveWindow()
            current_title = current_window.title if current_window else ""
            window_changed = current_title != last_window_title and current_title != ""
            
            should_analyze = False
            if SYSTEM_STATE.get("vision") == "active":
                should_analyze = True
            elif not PRIVACY_MODE and not is_afk:
                if window_changed:
                    should_analyze = True
                    logger.info(f"👁️ [VISION] Cambio finestra rilevato: '{current_title}'")
                    last_window_title = current_title # Aggiorna buffer
                elif (tempo_attuale - last_periodic_check > 600):
                    should_analyze = True
                    last_periodic_check = tempo_attuale
                    logger.info("👁️ [VISION] Avvio analisi periodica silenziosa...")

            if should_analyze:
                logger.info(f"👁️ [VISION] Cattura schermo: {fai_screenshot_pc()}")
                path_img = "clio_screenshot.png"
                
                if os.path.exists(path_img):
                    try:
                        # Analisi con Gemini Vision (Sostituisce Groq decommissionato)
                        logger.info(f"👁️ Avvio analisi periodica con Gemini Vision...")
                        vision_text = analizza_visione(path_img, "Analizza questo/i schermo/i (l'immagine può contenere più monitor affiancati, es. 2). Descrivi brevemente (max 100 parole) cosa sta facendo l'utente, quali app sono aperte e se ci sono elementi critici (errori, notifiche), distinguendo i monitor se presenti.")
                        
                        ULTIMA_VISIONE = vision_text
                        logger.info(f"👁️ [VISION_UPDATE]: {ULTIMA_VISIONE[:100]}...")
                    except Exception as ve:
                        logger.error(f"Errore Gemini Vision in loop periodico: {ve}")
                
                # Mantieni lo stato sincronizzato
                if PRIVACY_MODE:
                    SYSTEM_STATE["vision"] = "inactive"
                else:
                    SYSTEM_STATE["vision"] = "active"
            
        except Exception as e:
            logger.error(f"Errore Vision Loop: {e}")
            time.sleep(10)
        
        # Evita loop saturo
        time.sleep(3) # Controlla ogni 3 secondi per reattività ideale

# --- Threads initialization removed from top level ---

@app.route("/api/status", methods=["GET"])
@require_clio_key
def get_status():
    """Restituisce lo stato globale del sistema per l'Hub."""
    # Sincronizzazione forzata: se PRIVACY_MODE è True, lo stato DEVE essere muted
    if PRIVACY_MODE:
        SYSTEM_STATE["mic"] = "muted"

    # Visione ("Hawk Eye"): riporta lo stato REALE del worker di visione, non un
    # flag mai aggiornato. Così il badge dell'Hub non resta bloccato su "SPENTO".
    set_vision_state(_vision_effettiva(), "sync /api/status")

    # Creiamo una copia per aggiungere il flag booleano esplicito
    response_state = SYSTEM_STATE.copy()
    response_state["privacy_mode"] = PRIVACY_MODE
    
    # Aggiungi telemetria hardware e visione
    telemetria = ottieni_telemetria_hardware()
    response_state["telemetria"] = telemetria
    response_state["ultima_visione"] = ULTIMA_VISIONE

    # Consegna notifiche pendenti e svuota la coda
    global HUB_NOTIFICATIONS
    response_state["notifications"] = list(HUB_NOTIFICATIONS)
    HUB_NOTIFICATIONS = []  # Svuota dopo consegna

    # Automazioni disponibili
    try:
        from automations import get_registered_automations
        response_state["automations"] = get_registered_automations()
    except Exception:
        response_state["automations"] = []

    return jsonify(response_state), 200
@app.route("/api/task_status", methods=["GET"])
@require_clio_key
def api_task_status():
    """Restituisce lo stato di un task asincrono."""
    task_id = request.args.get("task_id")
    if not task_id:
        return jsonify({"error": "Nessun task_id fornito"}), 400
        
    status_info = get_task_status(task_id)
    if not status_info:
        return jsonify({"status": "not_found", "error": f"Task {task_id} non trovato"}), 404
        
    return jsonify(status_info), 200



@app.route("/api/command", methods=["POST"])
@require_clio_key
def on_command():
    data = request.json
    if not data or "text" not in data:
        return jsonify({"error": "Nessun testo fornito"}), 400
        
    testo_comando = data["text"]
    
    # Se il comando inizia con "coding task:", imposta il modello su CODER
    if isinstance(testo_comando, str) and testo_comando.strip().lower().startswith("coding task:"):
        SYSTEM_STATE["current_model"] = "CODER"
        origin = "flask_ui"  # per l'auto-approve del coding task
    else:
        origin = "flask"
        
    # Gestione immagine allegata
    image_base64 = data.get("image_base64")
    image_type = data.get("image_type", "image/jpeg")
    if image_base64:
        import base64, io
        from PIL import Image
        img_bytes = base64.b64decode(image_base64)
        img = Image.open(io.BytesIO(img_bytes))
        testo_comando = [testo_comando, img]
        
    logger.info(f"Ricevuto comando da remoto: {testo_comando}")
    global LAST_INTERACTION_TIME
    LAST_INTERACTION_TIME = time.time()
    
    import uuid
    task_id = str(uuid.uuid4())
    set_task_status(task_id, "processing")
    
    def run_task(tid, cmd, orig):
        try:
            logger.info(f"Avvio elaborazione asincrona per task {tid}: '{cmd}'")
            # origin viene passato a elabora_richiesta che deve poi propagarlo a jarvis_brain/policy
            risposta = elabora_richiesta(cmd, origin=orig)
            logger.info(f"Elaborazione completata per task {tid}. Risposta generata.")
            set_task_status(tid, "completed", risposta)
            # Trigger speech
            threading.Thread(target=clio_parla, args=(risposta,), daemon=True).start()
        except Exception as e:
            logger.error(f"Errore asincrono task {tid}: {e}", exc_info=True)
            import traceback
            log_path = os.path.join(BASE_DIR, "logs", "clio_log_notturno.md")
            with open(log_path, "a") as f:
                 f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERRORE ASINCRONO]: {e}\n{traceback.format_exc()}")
            set_task_status(tid, "error", str(e))

    threading.Thread(target=run_task, args=(task_id, testo_comando, origin), daemon=True).start()
    
    return jsonify({
        "status": "accepted",
        "task_id": task_id
    }), 202

@app.route("/api/model/force", methods=["POST"])
@require_clio_key
def force_model():
    data = request.json
    model = data.get("model", "AUTO")
    # Il router gestisce già AUTO fallback
    SYSTEM_STATE["current_model"] = model.upper()
    return jsonify({"status": "success", "model": model}), 200

@app.route("/api/task_status/<task_id>", methods=["GET"])
@require_clio_key
def task_status(task_id):
    status_data = get_task_status(task_id)
    if not status_data:
        return jsonify({"error": "Task non trovato"}), 404
    return jsonify(status_data), 200


@app.route("/api/cost-confirmations", methods=["GET"])
@require_clio_key
def api_cost_confirmations():
    """Consegna alla Web UI le richieste di consenso economico pendenti."""
    return jsonify({"pending": policy.get_pending_web_cost_confirmations()}), 200


@app.route("/api/cost-confirmations/<token>", methods=["POST"])
@require_clio_key
def api_resolve_cost_confirmation(token):
    """Riceve la scelta esplicita Sì/No dell'utente dalla Web UI."""
    data = request.get_json(silent=True) or {}
    decision = data.get("decision")
    if not policy.resolve_web_cost_confirmation(token, decision):
        return jsonify({"error": "Richiesta scaduta o decisione non valida"}), 404
    return jsonify({"status": "resolved", "decision": str(decision).lower()}), 200

@app.route("/api/connect_adb", methods=["POST"])
@require_clio_key
def connect_adb():
    data = request.json
    ip = data.get("ip")
    if not ip: return jsonify({"error": "IP mancante"}), 400
    
    adb_path = ADB_PATH
    try:
        import subprocess
        subprocess.run(f'"{adb_path}" connect {ip}:5555', shell=True)
        return jsonify({"status": "success", "message": f"Tentativo di connessione a {ip} inviato."}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/mic/toggle", methods=["POST"])
@require_clio_key
def toggle_mic():
    """Attiva, disattiva o muta il microfono manualmente."""
    global MIC_OVERRIDE, PRIVACY_MODE
    data = request.json
    action = data.get("action") # 'mute', 'unmute'
    
    logger.info(f"🔄 [API] toggle_mic: {action}")
    
    if action == "mute":
        PRIVACY_MODE = True
        set_system_mic_mute(True)
        SYSTEM_STATE["mic"] = "muted"
        set_vision_state("inactive", "privacy mode")
        salva_impostazioni()
        logger.info("🛑 [SERVER] Privacy Mode ON")
        return jsonify({"status": "success", "mic": "muted"}), 200
    elif action == "unmute":
        # Se eravamo in Privacy Mode, la togliamo
        if PRIVACY_MODE:
            PRIVACY_MODE = False
            set_system_mic_mute(False)
            SYSTEM_STATE["mic"] = "active"
            salva_impostazioni()
            logger.info("🟢 [SERVER] Privacy Mode OFF")
        
        # Il click sul logo attiva l'ascolto immediato. Hawk Eye resta opt-in
        # tramite flag o richiesta vocale esplicita.
        MIC_OVERRIDE = True
        logger.info("⚡ [SERVER] MIC_OVERRIDE attivato; Hawk Eye non viene avviato.")
        return jsonify({"status": "success", "mic": "active"}), 200
    else:
        return jsonify({"status": "error", "message": "Azione non valida"}), 400

@app.route("/api/jarvis/think", methods=["POST"])
@require_clio_key
def jarvis_think():
    """Endpoint per inviare richieste complesse al cervello di Jarvis."""
    data = request.json
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "Nessun testo fornito"}), 400
    
    from jarvis_brain import jarvis
    logger.info(f"🧠 [JARVIS] Pensiero attivo: {text}")
    risultato = jarvis.think_and_act(text)
    return jsonify({"status": "success", "response": risultato}), 200


@app.route("/api/tasks", methods=["GET", "POST"])
@require_clio_key
def handle_tasks():
    TASKS_FILE = os.path.join(BASE_DIR, "data", "clio_tasks.json")
    if request.method == "GET":
        if os.path.exists(TASKS_FILE):
             with open(TASKS_FILE, "r") as f: return jsonify(json.load(f)), 200
        return jsonify({"pending": []}), 200
    
    # POST: Aggiungi task
    new_task = request.json
    try:
        data = {"pending": []}
        if os.path.exists(TASKS_FILE):
            with open(TASKS_FILE, "r") as f: data = json.load(f)
        
        new_task["id"] = f"task_{int(time.time())}"
        new_task["creazione"] = time.time()
        data["pending"].append(new_task)
        
        with open(TASKS_FILE, "w") as f: json.dump(data, f, indent=4)
        return jsonify({"status": "ok", "task_id": new_task["id"]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/speech/stop", methods=["POST"])
@require_clio_key
def stop_speech():
    """Interrompe immediatamente la riproduzione audio di Clio (barge-in/stop)."""
    try:
        _SPEECH_STOP.set()                 # sblocca eventuali stream TTS in corso
        if _CURRENT_TTS is not None:
            _CURRENT_TTS.stop()
        pygame.mixer.music.stop()
        AUDIO_PLAYING.clear()              # il worker wake-word riprende subito
        SYSTEM_STATE["last_speech"] = ""
        logger.info("⏹️ [SERVER] Riproduzione vocale interrotta: worker wake-word sbloccato.")
        return jsonify({"status": "success", "message": "Riproduzione interrotta"}), 200
    except Exception as e:
        logger.error(f"Errore interruzione audio: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/voice/toggle", methods=["POST"])
@require_clio_key
def toggle_voice_gender():
    """FIX 1: endpoint mantenuto per compatibilità UI, ma la voce è FISSA.

    Prima alternava Isabella (F) ↔ Diego (M): era questa la causa della voce
    che cambiava tra uomo e donna. Ora risponde sempre con la voce unica.
    """
    global CURRENT_VOICE
    CURRENT_VOICE = TTS_VOICE
    SYSTEM_STATE["voice_gender"] = "female"
    logger.info(f"🎙️ [VOICE] Voce unica forzata: {CURRENT_VOICE}")
    return jsonify({"status": "ok", "voice": CURRENT_VOICE, "gender": "female", "label": "Isabella (F)"}), 200

@app.route('/')
def serve_index():
    logger.info("Richiesta ricevuta per: /")
    return _attach_session_cookie(app.send_static_file('index.html'))

@app.route('/brain')
def serve_brain():
    """Serve la visualizzazione 3D della mente."""
    clio_hub_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clio_hub')
    from flask import send_from_directory
    return _attach_session_cookie(send_from_directory(clio_hub_folder, 'brain.html'))

@app.route('/3d_preview.html')
def serve_3d_preview():
    """Anteprima 3D dei modelli generati (imposta il cookie per /3d_models)."""
    clio_hub_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clio_hub')
    from flask import send_from_directory
    return _attach_session_cookie(send_from_directory(clio_hub_folder, '3d_preview.html'))

@app.route('/3d_models/<path:filename>', methods=['GET'])
@require_clio_key
def serve_3d_model(filename):
    """Serve un modello di data/3d_models. 'latest' = l'ultimo .obj generato."""
    from flask import send_from_directory
    cartella = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '3d_models')
    if filename == 'latest':
        try:
            modelli = [f for f in os.listdir(cartella) if f.lower().endswith('.obj')]
        except OSError:
            modelli = []
        if not modelli:
            return jsonify({"error": "nessun modello .obj generato"}), 404
        filename = max(modelli, key=lambda f: os.path.getmtime(os.path.join(cartella, f)))
    if not filename.lower().endswith(('.obj', '.glb')):
        return jsonify({"error": "formato non supportato"}), 404
    return send_from_directory(cartella, filename)

@app.route("/api/memory_graph", methods=["GET"])
@require_clio_key
def get_memory_graph():
    """Trasforma gli episodi in un formato Nodi/Link per Three.js."""
    from episodic_memory import episodic_memory
    episodes = episodic_memory.episodes
    nodes = []
    links = []
    
    # --- DATI REALI ---
    if episodes:
        # Nodo centrale (Clio)
        nodes.append({"id": "clio", "name": "Clio Core", "val": 20, "color": "#00f3ff"})
        
        for i, ep in enumerate(episodes):
            node_id = f"ep_{i}"
            nodes.append({
                "id": node_id,
                "name": ep.get("titolo", "Ricordo"),
                "desc": ep.get("descrizione", ""),
                "val": ep.get("importanza", 3) * 2,
                "color": "#ff007a" if ep.get("importanza", 3) > 3 else "#00BFFF"
            })
            # Tutti i ricordi sono connessi a Clio
            links.append({"source": "clio", "target": node_id})
            
    # --- DATI DEMO (Se vuoto) ---
    else:
        nodes.append({"id": "core", "name": "Neural Nexus", "val": 15, "color": "#00f3ff"})
        # Palette ciano/viola/magenta — niente bianco (bianco non visibile con MeshBasicMaterial su sfondo scuro)
        PALETTE = ["#00f3ff", "#00BFFF", "#a855f7", "#ec4899", "#7c3aed", "#06b6d4", "#3b82f6"]
        for i in range(50):
            n_id = str(i)
            nodes.append({
                "id": n_id,
                "name": f"Neurone_{i}",
                "desc": "Connessione sinaptica in attesa di dati...",
                "val": 2 + (i % 5),
                "color": PALETTE[i % len(PALETTE)]
            })
            links.append({"source": "core", "target": n_id})
    
    return jsonify({"nodes": nodes, "links": links}), 200

@app.route("/api/chat_history", methods=["GET"])
@require_clio_key
def get_chat_history():
    """Restituisce la cronologia conversazioni recente per l'Hub."""
    limit = int(request.args.get("limit", 30))
    return jsonify({"history": CONVERSATION_HISTORY[-limit:]}), 200

@app.route("/api/vision", methods=["GET"])
@require_clio_key
def get_vision_frame():
    """Restituisce l'ultimo screenshot come base64 per il Vision Panel dell'Hub."""
    try:
        img_path = os.path.join(BASE_DIR, "clio_screenshot.png")
        if not os.path.exists(img_path):
            return jsonify({"vision": None, "desc": ULTIMA_VISIONE, "age": -1}), 200
        age = int(time.time() - os.path.getmtime(img_path))
        with open(img_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return jsonify({
            "vision": b64,
            "desc": ULTIMA_VISIONE[:250] if ULTIMA_VISIONE else "",
            "age": age  # Secondi fa
        }), 200
    except Exception as e:
        return jsonify({"vision": None, "desc": str(e), "age": -1}), 200

@app.route("/api/briefing", methods=["GET"])
@require_clio_key
def get_briefing():
    """Genera un briefing mattutino istantaneo: meteo + notizie + calendario."""
    try:
        meteo = policy.guarded_call("ottieni_meteo", AZIONI_MAP["ottieni_meteo"], {}, origin="flask")
        notizie = policy.guarded_call("ottieni_notizie", AZIONI_MAP["ottieni_notizie"], {}, origin="flask")
        calendario = policy.guarded_call("ottieni_notizie", AZIONI_MAP["ottieni_notizie"], {}, origin="flask")  # Può essere sostituito da leggi_calendario se auth OK
        ora = datetime.now().strftime("%H:%M")
        risposta = f"Briefing delle {ora}:\n\n🌤 {meteo}\n\n{notizie}"
        return jsonify({"briefing": risposta}), 200
    except Exception as e:
        return jsonify({"briefing": f"Errore briefing: {e}"}), 200

@app.route("/api/quick_command", methods=["POST"])
@require_clio_key
def quick_command():
    """Esegue un comando rapido direttamente (bypass async task, risposta immediata per azioni veloci)."""
    data = request.json
    cmd = data.get("cmd", "")
    if not cmd:
        return jsonify({"error": "cmd mancante"}), 400
    global LAST_INTERACTION_TIME
    LAST_INTERACTION_TIME = time.time()
    _append_conversation("user", cmd)
    try:
        from jarvis_brain import jarvis
        tel = policy.guarded_call("ottieni_telemetria_hardware", AZIONI_MAP["ottieni_telemetria_hardware"], {}, origin="flask")
        ctx = {"visione": ULTIMA_VISIONE, "sistema": f"CPU {tel.get('cpu_percent','?')}%"}
        risposta = jarvis.think_and_act(cmd, context=ctx)
        _append_conversation("clio", risposta)
        threading.Thread(target=clio_parla, args=(risposta,), daemon=True).start()
        return jsonify({"response": risposta}), 200
    except Exception as e:
        return jsonify({"response": f"Errore: {e}"}), 200

@app.route('/api/config.js')
@require_clio_key
def serve_config_js():
    """Config pubblica NON segreta per la Web UI (porta/versione).

    La chiave master non è mai esposta al client: l'autenticazione avviene col
    token di sessione (cookie clio_session / header X-Clio-Key)."""
    cfg = {"port": 5000, "version": CLIO_VERSION}
    return f"window.CLIO_CONFIG = {json.dumps(cfg)};", 200, {'Content-Type': 'application/javascript'}

@app.route('/android/<path:filename>')
def serve_android(filename):
    """Serve i file dell'app Android PWA (es. /android/index.html)"""
    import os
    android_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clio_android')
    from flask import send_from_directory
    return send_from_directory(android_folder, filename)

def start_core_services(run_flask_blocking=True):
    """Avvia wakeword + loop proattivo + Flask.
    run_flask_blocking=False => Flask parte in un thread (uso nel processo unico run_clio.py)."""
    # ── Vault Obsidian: indice leggero all'avvio (mai bloccante in caso di errore) ──
    try:
        build_vault_index()
    except Exception as e:
        logger.warning(f"⚠️ Vault non inizializzato: {e}")

    # ── TTS pre-warm (Fase 6): la prima sintesi non paga l'inizializzazione ──
    threading.Thread(target=prewarm_tts, daemon=True).start()

    wakeword_thread = threading.Thread(target=wakeword_supervisor_loop, daemon=True)
    wakeword_thread.start()
    logger.info("🎙️ Thread wakeword avviato (supervisionato: riavvio automatico dopo 2s in caso di crash).")

    # Timer globale che chiude la conversazione attiva dopo 10s di inattività
    threading.Thread(target=conversation_timeout_loop, daemon=True).start()
    logger.info(f"⏱️ Timer conversazione attiva avviato (timeout {int(CONVERSAZIONE_TIMEOUT)}s).")

    proactive_thread = threading.Thread(target=proactive_loop_func, daemon=True)
    proactive_thread.start()
    logger.info("🤖 Thread proattivo avviato.")

    # threaded=True: nel processo unico Flask condivide l'interprete con il loop asyncio di
    # Telegram + wakeword + monitor; un worker singolo (default Werkzeug) serializza i poll
    # concorrenti della Web UI e una richiesta I/O/LLM on-demand li congela tutti. Con più
    # worker i poll di stato (CPU/RAM/MIC/...) non vengono starvati.
    if run_flask_blocking:
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
    else:
        threading.Thread(
            target=lambda: app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True),
            daemon=True).start()
        logger.info("🌐 Flask avviato in thread (processo unico, threaded).")

if __name__ == "__main__":
    start_core_services(run_flask_blocking=True)
