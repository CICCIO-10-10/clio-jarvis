"""
policy.py — Control-plane MINIMO di Clio: Policy + Confirm + Audit.
Milestone unico. Layer CENTRALE: tutti i dispatch dei tool passano da guarded_call().
Nessun 'if confirm:' sparso nelle azioni.

- Classifica ogni azione: read-only / reversibile / sensibile / irreversibile.
- Sensibili e irreversibili richiedono conferma: Telegram se configurato,
  altrimenti conferma VOCALE locale (TTS "Confermi ...? Di sì o no" + STT
  Whisper) quando l'origine è "voice"/"voce".
- Timeout/rifiuto -> NON esegue.
- Ogni azione (e ogni esito/errore) finisce in logs/security.log (JSON lines).
- Fail-safe: nessun canale conferma disponibile -> non esegue le irreversibili.
"""
import os, re, json, time, uuid, threading, asyncio, datetime, inspect, logging, unicodedata

logger = logging.getLogger("ClioPolicy")

try:
    from logging_setup import _redact as _redact_secrets
except Exception:
    def _redact_secrets(x):
        return x

try:
    import config
except Exception:
    config = None

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
AUDIT_PATH = os.path.join(BASE_DIR, "logs", "security.log")
CONFIRM_TIMEOUT = int(getattr(config, "CONFIRM_TIMEOUT", 60))
CHAT_ID    = getattr(config, "MY_TELEGRAM_USER_ID", None)

# ── Classificazione ────────────────────────────────────────────────────────────
READ_ONLY = {
    "ottieni_info_sistema","leggi_motore_ricerca_web","leggi_sito_web","leggi_youtube",
    "leggi_appunti","fai_screenshot_pc","telefono_screenshot","stato_batteria",
    "lista_file_cartella","ottieni_meteo","ottieni_notizie","leggi_gmail","leggi_calendario",
    # STEP 5B: monitor puramente osservativi (sole letture psutil, nessun side effect)
    "monitora_connessioni_sospette","monitora_processi_critici",
    # Telemetria hardware: sola lettura psutil (CPU/RAM/batteria), nessun side effect
    "ottieni_telemetria_hardware",
    # Tool di sola lettura: nessun side effect, non richiedono conferma
    "chi_sei","cerca_file","leggi_file_testo","analizza_schermo_pc","ricerca_web_profonda",
    # Automazioni Cortex di sola lettura (automations.py)
    "riepilogo_automazioni","stato_pipeline_notturna","leggi_dashboard_cortex",
    "stato_health_automazioni","leggi_play_store_report","leggi_automazione",
    # ── Skills PC (skills.py / SKILLS_MAP): nessun side effect locale ──
    "check_emails","controlla_email","leggi_email",
    "deep_web_research","ricerca_web","cerca_web","cerca_su_internet",
    "find_files","trova_file",
    "get_system_time","ora_sistema","tempo_sistema",
    "see_screen","analizza_schermo","guarda_schermo",
    # ── R1: Memoria come servizio — The Scribe (lettura DB/config, nessuna scrittura) ──
    "recupera_resume_progetto","get_scribe_resume",
    "stato_sessioni_scribe","get_scribe_status",
    # ── R5: Workflow Engine — ispezione agenti e stato dei workflow ──
    "ispeziona_agenti","stato_workflow",
    # ── R7: Vault Intelligence — scansione del vault in SOLA LETTURA ──
    # knowledge_curator non modifica, sposta o cancella nulla: il report viene
    # scritto fuori dal vault (clio/data), quindi non serve conferma.
    "scansiona_vault","proponi_pulizia_vault",
    # ── R8: Proattività Quotidiana — osserva la finestra attiva e propone note ──
    # Nessun side effect sul sistema: notifiche discreto + stato in memoria,
    # nessun log di app/titoli (privacy). Rate limit 1 notifica / 30 minuti.
    "attiva_proattivita",
    # ── FASE 2 · C2: Vision Agent ──
    # Cattura SOLO la finestra indicata e la fa descrivere al profilo VISION.
    # Come analizza_schermo_pc/see_screen: nessun side effect sul sistema, nessuna
    # app aperta; l'unica scrittura è lo stato dell'IdeaSession di CLIO.
    "analyze_sketch_and_update_idea",
    # ── C3: Sessione persistente ("Riprendi da qui") ──
    # Scrivono solo lo stato dell'IdeaSession di CLIO (come C2) e, per decisione
    # e prossimo passo, un'osservazione per The Scribe (bridge o outbox: CLIO non
    # tocca il vault). Nessun file dell'utente viene aperto o modificato: per
    # questo non serve una conferma a ogni frase ("annota che abbiamo deciso X").
    "riprendi_da_qui",
    "aggiungi_decisione_idea",
    "imposta_prossimo_passo",
    "aggiungi_materiale_idea",
    "imposta_blocco_idea",
    # ── C4: Research Agent ──
    # Apre un browser HEADLESS, legge pagine pubbliche e salva i link nello stato
    # dell'IdeaSession (fonte="research"). Nessun file dell'utente viene aperto o
    # modificato e non si scarica nulla: è una lettura, non un'azione da confermare.
    "cerca_riferimenti",
}
IRREVERSIBLE = {
    "spegni_pc","riavvia_pc","svuota_cestino","elimina_file","rinomina_file",
    "esegui_comando_shell","uccidi_processo",
    # Scrittura di nuovo codice in actions.py + registrazione runtime (tool_factory):
    # azione sensibile -> richiede autorizzazione esplicita.
    "crea_tool",
    # Automazioni Cortex che modificano file/Drive o inviano qualcosa (automations.py)
    "esegui_pipeline_notturna","esegui_dashboard_refresh","invia_welcome_new_users",
    "esegui_ig_token_refresh",
    # Runner generico: può eseguire qualsiasi automazione -> fail-safe irreversibile
    "esegui_automazione",
    # Coding Agent locale (Fase 4): scrive/modifica codice su disco -> irreversibile
    "esegui_coding_task",
    # R5: il Workflow Engine orchestra agenti (può arrivare a scrivere codice) ->
    # l'intero piano richiede una conferma esplicita prima di partire.
    "esegui_workflow",
    "run_opencode_task",
    "opencode_task","esegui_opencode","chiedi_a_opencode",
    # ── Skills PC che modificano dati o eseguono codice ──
    # (prima eseguivano fuori dal control-plane: ora sono classificate e confermate)
    "move_file","sposta_file",
    "organize_folder","organizza_cartella","riordina_cartella",
    "run_python_code","esegui_python","esegui_codice",
    "scrivi_file_testo",
    "esegui_script_python",
    "esegui_powershell_avanzato",
    # ── R6: Computer Use cross-app — azioni con effetti reali sulla UI ──
    # Click e digitazione agiscono su app di terze parti: sempre conferma.
    "clicca_elemento","scrivi_testo_app",
}
SENSITIVE = {
    # ── R6: lettura dei controlli di una finestra (UI Automation) ──
    # Nessun side effect, ma il testo può contenere dati privati (email,
    # password, documenti): richiede conferma come le irreversibili.
    "ispeziona_finestra",
    # Apertura app consentita + nuova IdeaSession. La richiesta esplicita UI
    # ha un'eccezione stretta; gli altri canali mantengono la conferma.
    "apri_app_e_inizia_idea",
    # ── FASE 2 · C5: Blender Agent (schizzo 2D -> modello 3D) ──
    # Resta "sensitive" perché avvia Blender e scrive un nuovo OBJ. La skill
    # usa template bpy statici e limitati; la Web UI ha un'eccezione stretta
    # di auto-approvazione, mentre gli altri canali richiedono conferma.
    "genera_modello_3d",
    "genera_modello_3d_blender",
}
REVERSIBLE = {
    "apri_applicazione","apri_sito","cerca_su_google","cerca_su_youtube","muta_volume",
    "imposta_volume","media_play_pausa","media_prossimo","media_precedente","crea_cartella",
    "apri_cartella","blocca_pc","chiudi_finestra_attiva","minimizza_tutto","digita_su_tastiera_fisica",
    "scrivi_appunti","aggiorna_biografia","registra_episodio","annulla_spegnimento",
    "accendi_luce","spegni_luce","telefono_clicca","telefono_digita","telefono_apri_app",
    "telefono_home","telefono_indietro","telefono_swipe",
    # ── R5: osservazione del workflow nell'outbox di CLIO (il vault di The
    # Scribe NON viene toccato: scrittura locale e reversibile) ──
    "registra_osservazione_scribe",
    # Automazioni Cortex che producono output (report/generazioni) (automations.py)
    "esegui_report_giornaliero","esegui_report_social","esegui_briefing_mattutino",
    "esegui_organizer_vault","esegui_social_stats_sync","esegui_stripe_check",
    "esegui_analytics_scraper","esegui_archivia_social","esegui_smoke_test",
    "esegui_weekly_review",
    # ── Skills PC con effetti limitati/reversibili ──
    "open_app","apri_app",
    # Skill creative a effetti limitati. Pollinations passa comunque dal Cost
    # Guard: il servizio remoto può consumare Pollen.
    "disegna_su_paint","genera_immagine_pollinations",
    "control_media","media_control","controllo_media",
    "search_youtube","cerca_su_youtube",
    "click_coordinates","clicca","clicca_coordinate",
    "type_text","digita","digita_testo",
    "press_key","premi_tasto",
    "take_screenshot","fai_screenshot","cattura_schermo",
    "finestre_gestione",
    # ── FASE 2 · C1: portare una finestra davanti non cambia nessun dato ──
    "porta_finestra_in_primo_piano",
    # Nuova sessione interna, chiudibile senza toccare file dell'utente.
    "crea_idea_session",
}
DEFAULT_CLASS = "irreversibile"  # fail-safe: sconosciuto -> chiede conferma

def classify(name: str) -> str:
    name = (name or "").strip()
    if name in IRREVERSIBLE: return "irreversibile"
    if name in SENSITIVE:    return "sensitive"
    if name in READ_ONLY:    return "read-only"
    if name in REVERSIBLE:   return "reversibile"
    return DEFAULT_CLASS

# ── Telegram wiring (impostato da bot.main via post_init) ───────────────────────
_BOT = None
_LOOP = None
_pending = {}   # token -> {"event": Event, "decision": "yes"|"no"|None}
_WEB_COST_PENDING = {}
_WEB_COST_LOCK = threading.Lock()

def bind(bot, loop):
    """Chiamato una volta all'avvio: memorizza bot + event loop principale."""
    global _BOT, _LOOP
    _BOT, _LOOP = bot, loop

# ── Audit ───────────────────────────────────────────────────────────────────────
def _safe_args(args):
    out = {}
    for k, v in (args or {}).items():
        s = _redact_secrets(str(v))
        out[k] = s if len(s) <= 200 else s[:200] + "…"
    return out

def _audit(action, cls, args, outcome, confirmed=None, error=None, dur_ms=None, origin=None):
    rec = {
        "ts": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "action": action, "class": cls, "args": _safe_args(args),
        "outcome": outcome, "confirmed": confirmed, "error": error, "duration_ms": dur_ms,
        "origin": origin,
    }
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # l'audit non deve mai rompere il flusso

# ── Conferma via Telegram (ritorna 'yes' | 'no' | 'timeout') ────────────────────
def _telegram_disponibile():
    """True se esiste un canale Telegram utilizzabile per chiedere conferma."""
    return bool(_BOT and _LOOP and CHAT_ID)

def _telegram_confirm(name, args, message=None):
    if not _telegram_disponibile():
        return "timeout"  # nessun canale -> fail-safe
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except Exception:
        return "timeout"
    token = uuid.uuid4().hex[:12]
    ev = threading.Event()
    _pending[token] = {"event": ev, "decision": None}
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Conferma", callback_data=f"clioconf:{token}:yes"),
        InlineKeyboardButton("❌ Annulla",  callback_data=f"clioconf:{token}:no"),
    ]])
    if message:
        from html import escape
        testo = f"⚠️ {escape(message)}\n\nConfermi? (scade tra {CONFIRM_TIMEOUT}s)"
    else:
        testo = (f"⚠️ <b>Azione irreversibile</b> richiesta:\n"
                 f"<b>{name}</b>\n<code>{_safe_args(args)}</code>\n\n"
                 f"Confermi? (scade tra {CONFIRM_TIMEOUT}s)")
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _BOT.send_message(chat_id=CHAT_ID, text=testo, parse_mode="HTML", reply_markup=kb), _LOOP)
        fut.result(timeout=15)
    except Exception:
        _pending.pop(token, None)
        return "timeout"
    got = ev.wait(CONFIRM_TIMEOUT)
    dec = _pending.pop(token, {}).get("decision")
    if not got or dec is None:
        return "timeout"
    return "yes" if dec == "yes" else "no"


def _web_cost_confirm(name, args, message):
    """Attende un sì/no esplicito dalla Web UI, con scadenza fail-safe."""
    token = uuid.uuid4().hex
    event = threading.Event()
    item = {
        "token": token, "action": name, "message": message,
        "event": event, "decision": None,
        "created_at": time.time(), "expires_in": CONFIRM_TIMEOUT,
    }
    with _WEB_COST_LOCK:
        _WEB_COST_PENDING[token] = item
    received = event.wait(CONFIRM_TIMEOUT)
    with _WEB_COST_LOCK:
        pending = _WEB_COST_PENDING.pop(token, None)
    if not received or pending is None or pending.get("decision") not in ("yes", "no"):
        return "timeout"
    return pending["decision"]


def get_pending_web_cost_confirmations():
    """Snapshot privo di oggetti sincronizzazione per la Web UI."""
    now = time.time()
    with _WEB_COST_LOCK:
        expired = [token for token, item in _WEB_COST_PENDING.items()
                   if now - item["created_at"] >= item["expires_in"]]
        for token in expired:
            _WEB_COST_PENDING.pop(token, None)
        return [{
            "token": item["token"], "action": item["action"],
            "message": item["message"],
            "expires_in": max(0, int(item["expires_in"] - (now - item["created_at"]))),
        } for item in _WEB_COST_PENDING.values()]


def resolve_web_cost_confirmation(token, decision):
    """Risolve una conferma economica aperta dalla Web UI."""
    choice = str(decision or "").strip().lower()
    if choice in {"si", "sì", "yes", "y", "true"}:
        choice = "yes"
    elif choice in {"no", "n", "false"}:
        choice = "no"
    else:
        return False
    with _WEB_COST_LOCK:
        item = _WEB_COST_PENDING.get(token)
        if not item:
            return False
        item["decision"] = choice
        item["event"].set()
    return True

async def on_confirm_callback(update, context):
    """CallbackQueryHandler per i bottoni di conferma (pattern ^clioconf:)."""
    q = update.callback_query
    try:
        _, token, dec = q.data.split(":")
    except Exception:
        return
    p = _pending.get(token)
    if p:
        p["decision"] = dec
        p["event"].set()
    try:
        await q.answer("Ricevuto")
        await q.edit_message_text("✅ Confermato — eseguo." if dec == "yes" else "❌ Annullato.")
    except Exception:
        pass


# ── Conferma VOCALE (fallback quando Telegram non è configurato) ───────────────
# Flusso: TTS locale ("Confermi <azione>? Di sì o no.") -> STT (Whisper/Groq,
# fallback Google) -> "yes" | "no" | "timeout" (timeout = NON esegue).
VOICE_CONFIRM_TIMEOUT = 15          # secondi di ascolto
VOICE_ORIGINS = {"voice", "voce"}   # origini che usano la conferma vocale

_YES_TOKENS = {"si", "yes", "y", "ok", "okay", "confermo", "conferma",
               "procedi", "certo", "esatto", "affermativo", "vai", "vero"}
_NO_TOKENS = {"no", "n", "annulla", "annullare", "annullato", "stop",
              "fermati", "ferma", "negativo", "basta"}
_YES_PHRASES = ("va bene", "d accordo", "sono sicuro", "vai avanti", "fai pure")


def _normalizza_risposta_voce(testo):
    """Mappa una trascrizione STT in 'yes' / 'no' / None (None = non capito).

    Conservativa: in caso di negazione o ambiguità NON esegue.
    """
    t = (testo or "").strip().lower()
    if not t:
        return None
    # rimuove accenti: "sì" -> "si"
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    parole = set(t.split())

    if parole & _NO_TOKENS:
        return "no"
    if "non" in parole:                     # "non confermo", "non lo so"
        return "no"
    if parole & _YES_TOKENS:
        return "yes"
    if any(frase in t for frase in _YES_PHRASES):
        return "yes"
    return None


def _voice_speak(testo):
    """TTS locale (edge-tts di clio_voice). Best-effort: non solleva mai."""
    try:
        from clio_voice import parla
        parla(testo)
    except Exception as e:
        logger.warning(f"[VOICE] TTS non disponibile: {type(e).__name__}: {e}")
        return False
    finally:
        # clio_voice.parla chiude pygame.mixer a fine riproduzione: lo riapriamo
        # perché il TTS in streaming della risposta (clio_core) usa lo stesso mixer.
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception:
            pass
    return True


def _voice_listen(timeout):
    """Ascolta il microfono e trascrive (Groq Whisper -> Google). '' se nulla."""
    try:
        import speech_recognition as sr
        from clio_voice import trascrivi_audio
    except Exception as e:
        logger.warning(f"[VOICE] STT non disponibile: {type(e).__name__}: {e}")
        return ""
    try:
        rec = sr.Recognizer()
        rec.pause_threshold = 0.6
        rec.dynamic_energy_threshold = True
        with sr.Microphone() as source:
            rec.adjust_for_ambient_noise(source, duration=0.3)
            audio = rec.listen(source, timeout=max(1, int(timeout)), phrase_time_limit=5)
        return (trascrivi_audio(rec, audio) or "").strip()
    except Exception as e:
        logger.warning(f"[VOICE] ascolto fallito: {type(e).__name__}: {e}")
        return ""


def _voice_confirm(name, args, timeout=None, message=None):
    """Conferma di un'azione irreversibile per via vocale.

    1. Se Telegram è configurato lo usa (comportamento storico, nessun doppio prompt).
    2. Altrimenti chiede a voce e ascolta la risposta.
    Ritorna sempre 'yes' | 'no' | 'timeout'; non solleva mai eccezioni.
    """
    timeout = VOICE_CONFIRM_TIMEOUT if timeout is None else timeout
    try:
        if _telegram_disponibile():
            return _telegram_confirm(name, args, message=message)

        azione = str(name).replace("_", " ")
        if name == "esegui_workflow" and not message:
            obiettivo = str((args or {}).get("goal") or "")[:160]
            passi = str((args or {}).get("steps") or "")[:200]
            costo = str((args or {}).get("cost_summary") or "Costo API non verificato.")
            message = (
                f"Confermi il workflow per {obiettivo}. Passi: {passi}. "
                f"{costo} Di sì o no."
            )
        domanda = message or f"Confermi {azione}? Di sì o no."
        logger.info(f"[VOICE] conferma richiesta per {name}")
        _voice_speak(domanda)               # best-effort (parla già stampa il testo)

        t0 = time.time()
        risposta = _voice_listen(timeout)
        decisione = _normalizza_risposta_voce(risposta)
        logger.info(f"[VOICE] risposta={risposta!r} -> {decisione} "
                    f"({time.time() - t0:.1f}s)")
        return decisione or "timeout"
    except Exception as e:                  # qualunque problema -> non eseguire
        logger.warning(f"[VOICE] conferma vocale fallita: {type(e).__name__}: {e}")
        return "timeout"

# ── Guard centrale ───────────────────────────────────────────────────────────────
# Chiavi che il LLM (o un payload esterno) non deve MAI poter impostare: la conferma
# è una decisione del control-plane, non un parametro del tool.
_FORBIDDEN_ARG_KEYS = {
    "confirmed", "confirm", "confirmation", "force", "forced",
    "skip_confirm", "skip_safety", "approved", "authorized", "bypass",
}

# Origini che possono ricevere un'auto-approvazione. VUOTO per decisione R3:
# nessuna azione irreversibile (nemmeno i coding task) è più auto-approvata,
# nemmeno dalla Web UI locale. Resta come costante per compatibilità.
AUTO_APPROVE_ORIGINS = set()

# ── Auto-approvazione UI: reversibili + eccezioni strette ────────────────────
# Le azioni sensibili restano confermate salvo le eccezioni esplicite più sotto.
UI_AUTO_APPROVE_ORIGINS = {"flask", "flask_ui"}
UI_AUTO_APPROVE_ACTIONS = {
    "apri_applicazione", "open_app", "apri_app", "apri_sito", "apri_cartella",
    "crea_idea_session", "apri_app_e_inizia_idea",
    "disegna_su_paint", "genera_immagine_pollinations",
    "genera_modello_3d_blender",
}
# Compatibilità: eventuali azioni che richiedono il consenso Web esplicito.
UI_WEB_CONFIRM_ACTIONS = {"esegui_workflow"}

def _sanitize_args(args):
    """Rimuove dagli argomenti le chiavi che aggirano la conferma."""
    clean = {}
    for k, v in (args or {}).items():
        if not isinstance(k, str):
            continue
        if k.strip().lower() in _FORBIDDEN_ARG_KEYS or k.startswith("__"):
            continue
        clean[k] = v
    return clean

def _invoke(fn, args, confirmed, origin=None):
    """Invoca fn propagando confirmed=True SOLO se la firma lo accetta.

    Le skill 'confirmed=True' (move_file, organize_folder, run_python_code) ricevono
    la conferma dal control-plane: senza questo passaggio chiederebbero una seconda
    conferma e l'azione non verrebbe mai eseguita.
    """
    call_args = dict(args)
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if confirmed and "confirmed" in params:
        call_args["confirmed"] = True
    if "origin" in params:
        # Sovrascrive eventuali argomenti omonimi: la provenienza arriva dal
        # control plane, mai dal payload prodotto dal modello.
        call_args["origin"] = origin
    return fn(**call_args)


def _call_confirmation_callback(callback, name, args, message=None):
    """Chiama un provider di conferma, includendo sempre l'avviso di costo.

    I callback storici accettano solo (name, args); quelli nuovi possono
    dichiarare anche `message`. Per i primi il messaggio viene aggiunto solo
    alla copia destinata al callback, mai agli argomenti della skill.
    """
    if message is None:
        return callback(name, args)
    try:
        parameters = inspect.signature(callback).parameters.values()
        accepts_message = any(
            parameter.name == "message"
            or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_message = False
    if accepts_message:
        return callback(name, args, message=message)
    callback_args = dict(args or {})
    callback_args["confirmation_message"] = message
    return callback(name, callback_args)


def _workflow_confirmation_message(args):
    """Riassume passi e costo prima di autorizzare un workflow."""
    args = args or {}
    goal = str(args.get("goal") or "")[:160]
    steps = str(args.get("steps") or "")[:200]
    cost_summary = str(args.get("cost_summary") or "").strip()
    if not steps or not cost_summary:
        try:
            import workflow_engine
            plan = workflow_engine.build_plan(
                goal, {"project": args.get("progetto") or args.get("project") or ""}
            )
            if not steps:
                steps = ", ".join(
                    f"{step.get('agent')}:{step.get('action')}" for step in plan
                )[:200]
            if not cost_summary:
                cost_summary = workflow_engine._plan_cost_summary(plan)
        except Exception as exc:
            logger.warning("Costo del workflow non calcolabile: %s", exc)
            cost_summary = cost_summary or "Costo API non verificato."
    return (
        f"CLIO sta per eseguire il workflow: {goal}. Passi: {steps}. "
        f"{cost_summary} Confermi? [S/N]"
    )


def guarded_call(name, fn, args, confirm=None, origin=None):
    """UNICO punto di esecuzione dei tool. Applica policy + conferma + audit."""
    args = _sanitize_args(args)
    cls = classify(name)
    is_danger = cls in ("irreversibile", "sensitive")
    invoke_confirmed = False
    custom_confirm_supplied = confirm is not None
    try:
        import cost_guard
        cost_is_free = cost_guard.check_cost(name, origin)
        needs_cost_confirmation = not cost_is_free
        cost_estimate = cost_guard.estimated_cost(name)
    except Exception as exc:
        # Se il modulo Cost Guard non e' disponibile, non interrompere i tool
        # locali gia' protetti dalla policy; fallire chiuso solo per i tool non noti.
        logger.error("Cost Guard non disponibile per %s: %s", name, exc)
        needs_cost_confirmation = True
        cost_estimate = None

    if confirm is None:
        voce = (origin or "").strip().lower() in VOICE_ORIGINS
        confirm = _voice_confirm if voce else _telegram_confirm

    def _ask_explicit_confirmation():
        if needs_cost_confirmation:
            estimate = (f"${cost_estimate:.2f}" if cost_estimate is not None
                        else "variabile/non determinabile")
            message = f"Sto per usare un'API a pagamento o a costo non verificato per '{name}' (costo stimato: {estimate}). Confermi? [S/N]"
            if is_danger:
                message = f"L'azione '{name}' e' sensibile/irreversibile. {message}"
            normalized_origin = (origin or "").strip().lower()
            # Un callback esplicito può rappresentare l'autorizzazione di un
            # piano già approvato. Quel piano deve aver mostrato il costo e il
            # grant deve essere legato al suo hash (come nel Workflow Engine).
            if custom_confirm_supplied:
                return _call_confirmation_callback(confirm, name, args, message)
            if normalized_origin in UI_AUTO_APPROVE_ORIGINS:
                return _web_cost_confirm(name, args, message)
            if normalized_origin in VOICE_ORIGINS:
                return _call_confirmation_callback(confirm, name, args, message)
            return _call_confirmation_callback(confirm, name, args, message)
        if confirm is not None and confirm is not _voice_confirm and confirm is not _telegram_confirm:
            if name == "esegui_workflow":
                return _call_confirmation_callback(
                    confirm, name, args, _workflow_confirmation_message(args)
                )
            return confirm(name, args)
        normalized_origin = (origin or "").strip().lower()
        if (is_danger and normalized_origin in UI_AUTO_APPROVE_ORIGINS
                and name in UI_WEB_CONFIRM_ACTIONS):
            if name == "esegui_workflow":
                message = _workflow_confirmation_message(args)
            else:
                message = (
                    f"L'azione '{name}' avvierà Blender e scriverà un nuovo OBJ "
                    "nella cartella dei modelli CLIO. Non modifica file esistenti. "
                    "Confermi? [S/N]"
                )
            return _web_cost_confirm(name, args, message)
        if name == "esegui_workflow":
            return _call_confirmation_callback(
                confirm, name, args, _workflow_confirmation_message(args)
            )
        return confirm(name, args)

    # Il Cost Guard e la policy di rischio sono indipendenti: una chiamata
    # fatturabile richiede sempre consenso, anche per tool reversibili/UI.
    normalized_origin = (origin or "").strip().lower()
    ui_auto_approved = (
        cls == "reversibile"
        and normalized_origin in UI_AUTO_APPROVE_ORIGINS
        and name in UI_AUTO_APPROVE_ACTIONS
    ) or (
        # Eccezioni UI strette: apertura/sessione Paint e generazione Blender
        # locale tramite template statici, con solo nuovi output sotto data/.
        cls == "sensitive"
        and normalized_origin in UI_AUTO_APPROVE_ORIGINS
        and name in {"apri_app_e_inizia_idea", "genera_modello_3d_blender"}
        and name in UI_AUTO_APPROVE_ACTIONS
    )

    if needs_cost_confirmation or (is_danger and not ui_auto_approved):
        _audit(name, cls, args, "confirm_requested", origin=origin)
        decision = _ask_explicit_confirmation()
        if decision == "no":
            _audit(name, cls, args, "refused", confirmed=False, origin=origin)
            return "❌ Azione annullata dall'utente."
        if decision != "yes":  # timeout o qualunque non-yes
            logger.warning("TIMEOUT: %s - Origine: %s", name, origin)
            _audit(name, cls, args, "timeout", confirmed=False, origin=origin)
            logger.warning(f"TIMEOUT ESATTO: {name} | Origine: {origin} | Motivo: Costo/Conferma")
            return f"⌛ Conferma scaduta per {name}: azione NON eseguita."
        confirmed = True
        invoke_confirmed = True
    elif ui_auto_approved:
        _audit(name, cls, args, "auto_approved_ui", confirmed=None, origin=origin)
        logger.info("AUTO-APPROVE UI: %s - Origine: %s", name, origin)
        confirmed = None
        # Propaga l'autorizzazione UI alle skill composte che invocano un
        # secondo tool protetto (es. disegna_su_paint -> apertura di Paint).
        # L'audit mantiene confirmed=None: non è stato chiesto un secondo sì.
        invoke_confirmed = True
    else:
        confirmed = None

    t0 = time.time()
    try:
        if needs_cost_confirmation:
            try:
                import cost_guard
                cost_guard.record_paid_action(name, origin)
            except Exception as exc:
                logger.error("Impossibile registrare il costo per %s: %s", name, exc)
        res = _invoke(fn, args, confirmed or invoke_confirmed, origin=origin)
        _audit(name, cls, args, "executed", confirmed=confirmed, dur_ms=int((time.time()-t0)*1000), origin=origin)
        return res
    except Exception as e:
        _audit(name, cls, args, "error", confirmed=confirmed, error=str(e), dur_ms=int((time.time()-t0)*1000), origin=origin)
        return f"❌ Errore durante {name}: {e}"
