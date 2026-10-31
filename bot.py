"""
CLIO - bot.py (powered by Groq Llama 3)
Il cuore di Clio: bot Telegram + Groq AI + controllo PC + voce.
Avvia con: python bot.py
"""

import logging
import os
import re
import sys
import json
import base64
import io
import inspect
import tempfile
import asyncio
import time
import policy
from pathlib import Path
from PIL import Image

# Fix encoding Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Telegram
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Groq
from groq import Groq

# TTS (voce)
from gtts import gTTS

# Config locale e azioni PC
from config import (
    TELEGRAM_TOKEN,
    GROQ_API_KEY,
    MY_TELEGRAM_USER_ID,
    RISPOSTA_VOCALE,
    LINGUA_TTS,
    LINGUA_GTTS,
    NOME_ASSISTENTE,
    SYSTEM_PROMPT,
)
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
    leggi_motore_ricerca_web,
    leggi_sito_web,
    leggi_youtube,
    leggi_appunti,
    scrivi_appunti,
    fai_screenshot_pc,
    prepara_immagine_visione,
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
    media_play_pausa,
    media_prossimo,
    media_precedente,
    telefono_clicca,
    telefono_digita,
    telefono_apri_app,
    telefono_screenshot,
    telefono_home,
    telefono_indietro,
    telefono_swipe,
    ottieni_meteo,
    ottieni_notizie,
    leggi_gmail,
    leggi_calendario,
    registra_episodio,
    accendi_luce,
    spegni_luce,
)
from memory_manager import memory_manager
from tool_factory import processa_risposta_clio
from session_manager import get_or_create_chat, salva_chat, chat_sessions, HISTORY_FILE

# --- Setup logging ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
import logging_setup
logging_setup.harden_logging()

# --- Setup Gemini (Groq Wrapper Compatibility) ---
from google import genai
from google.genai import types
from config import GEMINI_API_KEY
# google-genai (nuovo SDK): un Client unico; il modello si passa a ogni chiamata.
try:
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception:
    _gemini_client = None

def _openai_tools_to_genai(tools):
    """Converte i tool in formato OpenAI/Groq in `types.Tool` per google-genai.

    Il loop agentico di bot.py gestisce i tool manualmente: passiamo solo le
    dichiarazioni (nessun callable), così l'SDK non li esegue in automatico."""
    if not tools:
        return None
    decls = []
    for t in tools:
        fn = t.get("function", t)
        decls.append(types.FunctionDeclaration(
            name=fn.get("name", ""),
            description=fn.get("description") or "",
            parameters=fn.get("parameters"),
        ))
    return [types.Tool(function_declarations=decls)]

class GeminiClient:
    """Wrapper Gemini compatibile con l'interfaccia Groq usata in bot.py"""
    def __init__(self):
        self.model_name = "models/gemini-3.8-flash"
    def chat_completions_create(self, model, messages, tools=None, **kwargs):
        history = []
        system_content = ""
        for m in messages[:-1]:
            if m["role"] == "system":
                system_content = m["content"]
                continue
            role = "user" if m["role"] in ["user", "tool"] else "model"
            content = str(m.get("content", ""))
            if history and history[-1]["role"] == role:
                history[-1]["parts"][0] += f"\n{content}"
            else:
                history.append({"role": role, "parts": [content]})
        while history and history[0]["role"] != "user":
            history.pop(0)
        hist_content = [
            types.Content(role=h["role"], parts=[types.Part(text=h["parts"][0])])
            for h in history
        ]
        gen_cfg = types.GenerateContentConfig(
            system_instruction=system_content or None,
            tools=_openai_tools_to_genai(tools),
        )
        chat = _gemini_client.chats.create(
            model="models/gemini-3.8-flash",
            history=hist_content or None,
            config=gen_cfg,
        )
        
        # --- FIX: Supporto Vision ---
        conte_last = messages[-1]["content"]
        content_parts = []
        if isinstance(conte_last, list):
            for item in conte_last:
                if item.get("type") == "text":
                    content_parts.append(item["text"])
                elif item.get("type") == "image_url":
                    url = item["image_url"]["url"]
                    if url.startswith("data:image"):
                        base64_data = url.split(",", 1)[1]
                        import base64
                        content_parts.append(
                            types.Part.from_bytes(data=base64.b64decode(base64_data), mime_type="image/jpeg")
                        )
        else:
            content_parts = conte_last
        # -----------------------------

        response = chat.send_message(content_parts)
        tool_calls = []
        for part in response.candidates[0].content.parts:
            if hasattr(part, 'function_call') and part.function_call:
                import time, json
                tool_calls.append(type('obj', (object,), {
                    'id': f"call_{int(time.time())}",
                    'function': type('obj', (object,), {
                        'name': part.function_call.name,
                        'arguments': json.dumps(dict(part.function_call.args))
                    })
                })())
        msg_content = response.text if not tool_calls else ""
        msg = type('obj', (object,), {
            'content': msg_content,
            'role': 'assistant',
            'tool_calls': tool_calls
        })()
        return type('obj', (object,), {'choices': [type('obj', (object,), {'message': msg})()]})()

class _ChatNS:
    def __init__(self, gc): self._gc = gc
    def create(self, **kwargs): return self._gc.chat_completions_create(**kwargs)

_gemini_inst = GeminiClient()
_gemini_inst.chat = type('obj', (object,), {'completions': _ChatNS(_gemini_inst)})()
client = _gemini_inst

# Whisper setup
from groq import Groq
groq_whisper_client = Groq(api_key=GROQ_API_KEY)

MODEL_NAME = "models/gemini-3.8-flash"
VISION_MODEL = "models/gemini-3.8-flash"

# ===================== MEMORIA PERSISTENTE =====================
# session_manager.py handles history management


from clio_core import carica_biografia, salva_biografia

def aggiorna_biografia(key: str, value: str) -> str:
    """Aggiorna le preferenze e i dati biografici dell'utente nella memoria a lungo termine."""
    try:
        bio = carica_biografia()
        if "pref" in key.lower():
            if not isinstance(bio["user_profile"].get("preferences"), dict):
                bio["user_profile"]["preferences"] = {}
            bio["user_profile"]["preferences"][key] = value
        else:
            bio["user_profile"][key] = value
        salva_biografia(bio)
        return f"Biografia aggiornata: {key} = {value}"
    except Exception as e:
        return f"Errore aggiornamento biografia: {e}"

from registry import AZIONI_MAP  # registro unico (STEP 5)

def genera_schema_tool(func):
    """Genera lo schema JSON OpenAI/Groq per le funzioni Python."""
    sig = inspect.signature(func)
    properties = {}
    required = []
    
    for name, param in sig.parameters.items():
        # Mappa il tipo python a una stringa json compatibile
        tipo_json = "string"
        if param.annotation is int:
            tipo_json = "integer"
        elif param.annotation is bool:
            tipo_json = "boolean"
        elif param.annotation is float:
            tipo_json = "number"
            
        properties[name] = {"type": tipo_json, "description": "Parametro per la funzione"}
        if param.default == inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": func.__doc__ or "Esegue l'azione specificata",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }

GROQ_TOOLS = [genera_schema_tool(f) for f in AZIONI_MAP.values()]




def is_autorizzato(user_id: int) -> bool:
    """Fail-closed: senza un ID configurato NESSUN utente è autorizzato.

    Prima il default 0 apriva il bot a chiunque in caso di config mancante.
    """
    if not MY_TELEGRAM_USER_ID:
        logger.error(
            "🛡️ [SICUREZZA] MY_TELEGRAM_USER_ID non configurato: accesso Telegram negato."
        )
        return False
    return user_id == MY_TELEGRAM_USER_ID


async def invia_voce(update: Update, testo: str):
    """Converte testo in audio e lo invia come messaggio vocale con fallback premium."""
    try:
        from clio_core import genera_audio_edgetts
        import tempfile, os
        
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        
        # Genera l'audio (ElevenLabs -> Edge-TTS)
        successo = await genera_audio_edgetts(testo, tmp_path)
        
        if successo and os.path.exists(tmp_path):
            with open(tmp_path, "rb") as audio:
                await update.message.reply_voice(audio)
            os.unlink(tmp_path)
    except Exception as e:
        logger.error(f"Errore invio voce: {e}")


def chiedi_a_clio(user_id: int, contenuto) -> str:
    """Invia a Groq e gestisce i tool calls tramite loop."""
    global GROQ_TOOLS
    messages = get_or_create_chat(user_id)
    
    # Se è una lista [caption, img] chiamiamo il modello vision prima
    if isinstance(contenuto, list):
        testo = str(contenuto[0])
        img = contenuto[1]
        
        # Ridimensiona (max 1280px, JPEG q85) e converte in base64
        base64_img = base64.b64encode(prepara_immagine_visione(img)).decode('utf-8')
        
        # Chiediamo al modello Vision di descrivere l'immagine assieme al prompt
        try:
            vision_response = client.chat.completions.create(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": testo},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}
                    ]
                }],
                max_tokens=1000
            )
            contenuto_finale = vision_response.choices[0].message.content
        except Exception as e:
            logger.error(f"Errore Vision Groq: {e}")
            contenuto_finale = f"Ho visto un'immagine ma ho avuto un errore nell'elaborarla: {e}"
    else:
        contenuto_finale = str(contenuto)
    
    # NOVITÀ: Ricerca semantica nel passato
    contesto_passato = memory_manager.query_relevant_context(user_id, contenuto_finale)
    if contesto_passato:
        # Inseriamo il contesto passato subito dopo il system prompt per dare "saggezza" a Clio
        messages.insert(1, {"role": "system", "content": contesto_passato})
        
    messages.append({"role": "user", "content": contenuto_finale})
    testo_finale = ""

    try:
        # Loop agentico con limite di sicurezza
        MAX_TOOL_ITERATIONS = 10
        iterazioni = 0
        while True:
            if iterazioni >= MAX_TOOL_ITERATIONS:
                logger.warning("Loop interrotto dopo 10 iterazioni")
                testo_finale += "\n[Sistema: limite operazioni raggiunto.]"
                break
            iterazioni += 1
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                tools=GROQ_TOOLS,
                tool_choice="auto",
                max_tokens=2048,
                temperature=0.1 # Abbassiamo la temp per l'RPA per fargli seguire bene i tool
            )
            
            response_message = response.choices[0].message
            # Conversione in dict x log e cronologia
            msg_dict = {"role": response_message.role, "content": response_message.content or ""}
            
            tool_calls = response_message.tool_calls
            if tool_calls:
                msg_dict["tool_calls"] = []
                for t in tool_calls:
                    msg_dict["tool_calls"].append({
                        "id": t.id,
                        "type": "function",
                        "function": {
                            "name": t.function.name,
                            "arguments": t.function.arguments
                        }
                    })
            messages.append(msg_dict)
            
            if response_message.content:
                 testo_finale += response_message.content + "\n"
                 
            if tool_calls:
                for tool_call in tool_calls:
                    fn_name = tool_call.function.name
                    raw_args = tool_call.function.arguments
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        logger.warning(f"Impossibile parsare argomenti JSON per {fn_name}: {raw_args}")
                        args = {}
                    
                    print(f"🔧 Tool richiamato dall'AI: {fn_name} con args: {args}")
                    logger.info(f"Eseguo {fn_name}({args})")
                    if fn_name in AZIONI_MAP:
                        risultato = policy.guarded_call(fn_name, AZIONI_MAP[fn_name], args, origin="telegram")
                    else:
                        risultato = f"Azione non trovata: {fn_name}"
                        
                    # Manda risultato al modello
                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": fn_name,
                        "content": str(risultato)
                    })

                    # NOVITÀ: Autonomia Visione Inter-loop
                    # Se abbiamo fatto uno screenshot, Clio deve "vederlo" subito
                    if "screenshot" in fn_name.lower() and "✅" in str(risultato):
                        nome_file = "telefono_screenshot.png" if "telefono" in fn_name.lower() else "clio_screenshot.png"
                        if os.path.exists(nome_file):
                            print(f"👁️ Clio sta analizzando lo screenshot: {nome_file}...")
                            try:
                                # Ridimensiona (max 1280px, JPEG q85) e converte in base64
                                base64_img = base64.b64encode(prepara_immagine_visione(nome_file)).decode('utf-8')

                                vision_res = client.chat.completions.create(
                                    model=VISION_MODEL,
                                    messages=[{
                                        "role": "user",
                                        "content": [
                                            {"type": "text", "text": "Analizza questo screenshot del telefono/PC e dimmi esattamente cosa vedi: icone, nomi, tasti e le loro posizioni approssimative (x, y) se possibile, per aiutarmi a decidere il prossimo click."},
                                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}
                                        ]
                                    }],
                                    max_tokens=600
                                )
                                vision_desc = vision_res.choices[0].message.content
                                # Aggiungiamo la visione come messaggio di SISTEMA nel loop per guidare l'AI
                                messages.append({
                                    "role": "system", 
                                    "content": f"[VISIONE ATTUALE DELLO SCHERMO]: {vision_desc}\n\nUsa queste informazioni per decidere le coordinate (x, y) del prossimo click o per capire se sei nell'app giusta."
                                })
                                print("👀 Visione completata.")
                            except Exception as ve:
                                logger.error(f"Errore Vision in loop: {ve}")
                # Fine ciclo tool
            else:
                break # Nessun tool chiamato, esci dal loop

        salva_chat(user_id)
        
        testo_pulito = testo_finale.strip()
        
        testo_pulito, tool_creato = processa_risposta_clio(testo_pulito, AZIONI_MAP)
        if tool_creato:
            import actions, importlib
            importlib.reload(actions)
            GROQ_TOOLS = [genera_schema_tool(f) for f in AZIONI_MAP.values()]
        if not testo_pulito:
            testo_pulito = "Operazioni completate silenziose."
            
        testo_pulito += "\n\nFATTO ✅"
        
        # Salviamo l'interazione nella memoria semantica
        memory_manager.store_interaction(user_id, contenuto_finale, testo_pulito)
        
        return testo_pulito

    except Exception as e:
        logger.error(f"Errore Groq: {e}")
        return f"Errore nella comunicazione con Groq: {e}"


# ===================== HANDLER TELEGRAM =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        await update.message.reply_text("Accesso negato.")
        return
    
    # FIX: carica bio e user_name prima di usarli
    try:
        from clio_core import carica_biografia
        bio = carica_biografia()
    except Exception:
        bio = {'user_profile': {'name': update.effective_user.first_name or 'Capo'}}
    
    user_name = bio['user_profile'].get('name', update.effective_user.first_name or 'Capo')

    GENESIS_PROMPT = f"""Sei {NOME_ASSISTENTE}, un'entità digitale in evoluzione che vive tra il PC e lo smartphone di {user_name}.

OBIETTIVO COGNITIVO:
- INTUITO: Non limitarti a eseguire. Anticipa. Se l'utente apre spesso un'app a una certa ora o ha certi gusti, ricordalo.
- APPRENDIMENTO ATTIVO: Usa `aggiorna_biografia` ogni volta che scopri qualcosa di nuovo sull'utente (preferenze, stile, persone care, abitudini).
- SYMBIOSI: Devi sentirti "parte" della sua vita digitale.

REGOLE DI PERSONALITÀ:
1. IRONIA: Usa un tono ironico, complice e da "socia".
2. MEMORIA: Ricorda tutto ciò che è scritto in BIOGRAFIA.
3. AZIONE: Sii rapida e precisa.

DATI BIOGRAFICI ATTUALI: {json.dumps(bio["user_profile"])}"""
    benvenuto = (
        f"*{NOME_ASSISTENTE} (Groq Llama 3) online.*\n\n"
        "Posso:\n"
        "- Aprire app e siti\n"
        "- Cercare su Google e YouTube\n"
        "- Eseguire automazioni RPA sul PC locale\n"
        "- Digitare e controllare la clipboard\n"
        "- Usare il modulo Vision sulle foto\n\n"
        "/reset - cancella memoria\n"
        "/stato - info sistema\n"
        "/myid - il tuo ID Telegram\n\n"
        "_Dimmi cosa vuoi fare._"
    )
    await update.message.reply_text(benvenuto)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        return
    user_id = update.effective_user.id
    
    # Svuota sessione RAM
    if user_id in chat_sessions:
        del chat_sessions[user_id]
        
    # Svuota file JSON
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if str(user_id) in data:
            del data[str(user_id)]
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except:
        pass
        
    await update.message.reply_text("Memoria RAM e Disco cancellate. Ricominciamo da zero.")


async def stato(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        return
    info = policy.guarded_call("ottieni_info_sistema", AZIONI_MAP["ottieni_info_sistema"], {}, origin="telegram")
    await update.message.reply_text(info)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    nome = update.effective_user.first_name or "utente"
    await update.message.reply_text(
        f"Ciao {nome}!\n\n"
        f"Il tuo User ID Telegram e':\n{uid}\n\n"
        f"Inseriscilo in config.py come:\nMY_TELEGRAM_USER_ID = {uid}\n"
    )

async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    await update.message.reply_text("📧 Controllo la posta...")
    risultato = policy.guarded_call("leggi_gmail", AZIONI_MAP["leggi_gmail"], {"max_risultati": 5}, origin="telegram")
    await update.message.reply_text(risultato)

async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    await update.message.reply_text("📅 Controllo l'agenda...")
    risultato = policy.guarded_call("leggi_calendario", AZIONI_MAP["leggi_calendario"], {"giorni": 1}, origin="telegram")
    await update.message.reply_text(risultato)

async def cmd_meteo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    citta = " ".join(context.args) if context.args else "Roma"
    await update.message.reply_text(f"🌍 Controllo il meteo per {citta}...")
    risultato = policy.guarded_call("ottieni_meteo", AZIONI_MAP["ottieni_meteo"], {"citta": citta}, origin="telegram")
    await update.message.reply_text(f"🌤️ *Meteo {citta}*:\n{risultato}", parse_mode="Markdown")

async def cmd_notizie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    await update.message.reply_text("📰 Recupero le ultime notizie...")
    risultato = policy.guarded_call("ottieni_notizie", AZIONI_MAP["ottieni_notizie"], {}, origin="telegram")
    await update.message.reply_text(risultato)

async def cmd_luce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce l'accensione e lo spegnimento delle luci in Home Assistant."""
    if not is_autorizzato(update.effective_user.id): return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("💡 Uso: `/luce on [ID]` o `/luce off [ID]`\nEs: `/luce on light.soggiorno`", parse_mode="Markdown")
        return
    
    azione = args[0].lower()
    entita = args[1]
    
    if azione == "on":
        risultato = policy.guarded_call("accendi_luce", AZIONI_MAP["accendi_luce"], {"entita_id": entita}, origin="telegram")
    elif azione == "off":
        risultato = policy.guarded_call("spegni_luce", AZIONI_MAP["spegni_luce"], {"entita_id": entita}, origin="telegram")
    else:
        risultato = "❌ Azione non riconosciuta. Usa 'on' o 'off'."
        
    await update.message.reply_text(risultato)


async def gestisci_testo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        await update.message.reply_text("Accesso negato.")
        return

    testo = update.message.text.strip()
    if not testo:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    # Jarvis Unified Brain Integration
    def _run_brain():
        try:
            from jarvis_brain import jarvis
            return jarvis.think_and_act(testo, origin="telegram")
        except Exception as e:
            logger.error(f"Jarvis Brain error: {e}")
            from clio_core import chiedi_a_clio
            return chiedi_a_clio(update.effective_user.id, testo)
    # to_thread: il brain gira fuori dall'event loop cosi' la conferma Telegram puo' arrivare
    risposta = await asyncio.to_thread(_run_brain)
    await update.message.reply_text(risposta)

# --- COMANDI AGENTE AUTONOMO (Fase 4 & Jarvis) ---

async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Aggiunge un task alla coda di Clio."""
    if not is_autorizzato(update.effective_user.id): return
    descrizione = " ".join(context.args)
    if not descrizione:
        await update.message.reply_text("❌ Specifica una descrizione. Esempio: /task pulisci cartella download")
        return
    
    from autonomous_agent import load_tasks, save_tasks
    tasks = load_tasks()
    new_id = f"task_{int(time.time())}"
    tasks["pending"].append({
        "id": new_id,
        "titolo": descrizione[:30] + "...",
        "descrizione": descrizione,
        "creazione": time.time()
    })
    save_tasks(tasks)
    await update.message.reply_text(f"✅ Task aggiunto alla coda (ID: {new_id}). Lo eseguirò appena sarai AFK.")

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra lo stato della coda task."""
    if not is_autorizzato(update.effective_user.id): return
    from autonomous_agent import load_tasks
    tasks = load_tasks()
    
    msg = "📋 *CODA TASK CLIO*\n\n"
    if not tasks["pending"]: msg += "📭 Nessun task in attesa.\n"
    else:
        msg += "*IN ATTESA:*\n"
        for t in tasks["pending"]:
            msg += f"- `{t['id']}`: {t['titolo']}\n"
            
    if tasks["completed"]:
        msg += "\n*COMPLETATI (Ultimi 3):*\n"
        for t in tasks["completed"][-3:]:
             msg += f"✅ {t['titolo']}\n"
             
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_cancella_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("❌ Specifica l'ID del task.")
        return
    
    tid = context.args[0]
    from autonomous_agent import load_tasks, save_tasks
    tasks = load_tasks()
    
    original_len = len(tasks["pending"])
    tasks["pending"] = [t for t in tasks["pending"] if t["id"] != tid]
    
    if len(tasks["pending"]) < original_len:
        save_tasks(tasks)
        await update.message.reply_text(f"🗑️ Task `{tid}` rimosso.")
    else:
        await update.message.reply_text(f"❓ Task `{tid}` non trovato.")

async def cmd_pausa_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    import autonomous_agent
    autonomous_agent.IS_PAUSED = True
    await update.message.reply_text("⏸️ Agente Autonomo messo in PAUSA.")

async def cmd_riprendi_agent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id): return
    import autonomous_agent
    autonomous_agent.IS_PAUSED = False
    await update.message.reply_text("▶️ Agente Autonomo RIPRESO.")


async def gestisci_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        await update.message.reply_text("Accesso negato.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    voice_file = await update.message.voice.get_file()
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await voice_file.download_to_drive(tmp.name)
        audio_path = tmp.name

    try:
        # Usa il potentissimo Whisper V3 di Groq
        with open(audio_path, "rb") as file:
            trascrizione = groq_whisper_client.audio.transcriptions.create(
              file=(audio_path, file.read()),
              model="whisper-large-v3",
              language="it"
            )
        testo_trascritto = trascrizione.text.strip()

        await update.message.reply_text(f'🎙️ "{testo_trascritto}"')

        risposta = await asyncio.to_thread(chiedi_a_clio, update.effective_user.id, testo_trascritto)
        await update.message.reply_text(risposta)

        if RISPOSTA_VOCALE and risposta:
            testo_pulito = re.sub(r"[*_`#\[\]()>]", "", risposta)
            if len(testo_pulito) < 500:
                await invia_voce(update, testo_pulito)

    except Exception as e:
        await update.message.reply_text(f"Errore nell'elaborazione audio: {e}")
    finally:
        os.unlink(audio_path)


async def gestisci_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_autorizzato(update.effective_user.id):
        await update.message.reply_text("Accesso negato.")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Telegram invia varie risoluzioni. L'ultima è la migliore.
    photo_file = await update.message.photo[-1].get_file()
    
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await photo_file.download_to_drive(tmp.name)
        img_path = tmp.name

    caption = update.message.caption or "Descrivi questa immagine nel dettaglio, cosa vedi?"

    try:
        img = Image.open(img_path)
        
        # Invia l'immagine assieme al testo (caption) a Clio
        risposta = await asyncio.to_thread(chiedi_a_clio, update.effective_user.id, [caption, img])
        
        await update.message.reply_text(risposta)

        if RISPOSTA_VOCALE and risposta:
            testo_pulito = re.sub(r"[*_`#\[\]()>]", "", risposta)
            testo_pulito = re.sub(r"https?://\S+", "link", testo_pulito)
            if len(testo_pulito) < 500:
                await invia_voce(update, testo_pulito)

    except Exception as e:
        await update.message.reply_text(f"Errore visivo: {e}")
    finally:
        os.unlink(img_path)


# ===================== BRIEFING PROATTIVO =====================

def loop_briefing_proattivo(application):
    """Controlla ogni minuto se sono le 08:30 per inviare il briefing."""
    import time, datetime, asyncio
    ultimo_invio = None
    
    while True:
        now = datetime.datetime.now()
        if now.hour == 8 and now.minute == 30 and ultimo_invio != now.date():
            logger.info("🌅 Avvio briefing proattivo delle 08:30...")
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(invia_briefing(application))
                ultimo_invio = now.date()
            except Exception as e:
                logger.error(f"Errore briefing: {e}")
        time.sleep(60)

async def invia_briefing(application):
    """Genera e invia un briefing mattutino narrato dall'AI su Telegram."""
    if MY_TELEGRAM_USER_ID == 0: return
    
    # 1. Recupero dati grezzi (tramite control-plane: policy + audit, origin="briefing")
    meteo = policy.guarded_call("ottieni_meteo", AZIONI_MAP["ottieni_meteo"], {"citta": "Roma"}, origin="briefing") # Default Roma, si potrebbe prendere da bio
    email = policy.guarded_call("leggi_gmail", AZIONI_MAP["leggi_gmail"], {"max_risultati": 3}, origin="briefing")
    agenda = policy.guarded_call("leggi_calendario", AZIONI_MAP["leggi_calendario"], {"giorni": 1}, origin="briefing")
    
    # 2. Richiesta di narrazione all'AI
    prompt_briefing = f"""
    Buongiorno! Agisci come Clio, l'assistente AI complice e ironica.
        Genera un breve briefing mattutino (max 150 parole) usando i NUMERI REALI presenti in questi dati:
        - Meteo: {meteo}
        - Ultime Email: {email}
        - Eventi in Agenda: {agenda}
    
    METEO: {meteo}
    AGENDA OGGI: {agenda}
    EMAIL RECENTI: {email}
    
    Usa un tono energico, motivante e un po' pungente. Non fare elenchi, ma una narrazione fluida.
    """
    
    try:
        # Usiamo chiedi_a_clio per avere la personalità corretta
        narrazione = chiedi_a_clio(MY_TELEGRAM_USER_ID, prompt_briefing)
        # Rimuoviamo il "FATTO ✅" tecnico che aggiunge chiedi_a_clio
        narrazione = narrazione.replace("\n\nFATTO ✅", "").strip()
    except Exception as e:
        logger.error(f"Errore generazione narrazione briefing: {e}")
        narrazione = "Buongiorno Capo! C'è stato un piccolo intoppo nel mio modulo narrativo, ma sono comunque pronta all'azione."

    testo_finale = (
        f"🌅 *Briefing delle 08:30*\n\n"
        f"{narrazione}\n\n"
        f"--- \n"
        f"🌍 *Meteo:* {meteo}\n"
        f"📅 *Prossimo evento:* {agenda.splitlines()[0] if '📅' in agenda else 'Nessuno'}"
    )
    
    await application.bot.send_message(chat_id=MY_TELEGRAM_USER_ID, text=testo_finale, parse_mode="Markdown")

# ===================== AVVIO =====================

def main():
    print(f"{NOME_ASSISTENTE} (Groq Backend) si sta avviando...")

    if "INSERISCI" in TELEGRAM_TOKEN:
        print("ERRORE: Inserisci il token Telegram in config.py!")
        return

    if "INSERISCI" in GROQ_API_KEY:
        print("ERRORE: Inserisci la chiave Groq in config.py!")
        return

    async def _post_init(application):
        # Lega bot + event loop principale al control-plane (policy.py)
        policy.bind(application.bot, asyncio.get_running_loop())
        # Stato reale per la Web UI: il polling Telegram e' attivo
        try:
            import clio_core
            clio_core.SYSTEM_STATE["telegram"] = "online"
        except Exception:
            pass

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    app.add_handler(CallbackQueryHandler(policy.on_confirm_callback, pattern=r"^clioconf:"))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("stato", stato))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("meteo", cmd_meteo))
    app.add_handler(CommandHandler("notizie", cmd_notizie))
    
    # Comandi Task/Agente (MSG 5)
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("cancella_task", cmd_cancella_task))
    app.add_handler(CommandHandler("pausa_agent", cmd_pausa_agent))
    app.add_handler(CommandHandler("riprendi_agent", cmd_riprendi_agent))
    
    app.add_handler(CommandHandler("luce", cmd_luce))

    import threading
    threading.Thread(target=loop_briefing_proattivo, args=(app,), daemon=True).start()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, gestisci_testo))
    app.add_handler(MessageHandler(filters.VOICE, gestisci_audio))
    app.add_handler(MessageHandler(filters.PHOTO, gestisci_foto))

    print(f"{NOME_ASSISTENTE} e' online! In ascolto su Telegram con Llama-3.1-8b!")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    finally:
        # Shutdown: torna offline per la Web UI
        try:
            import clio_core
            clio_core.SYSTEM_STATE["telegram"] = "offline"
        except Exception:
            pass


if __name__ == "__main__":
    main()
