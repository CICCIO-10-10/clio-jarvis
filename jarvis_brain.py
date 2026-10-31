import os
import sys
import inspect
import json
import logging
import re
import requests
import time
from typing import List, Dict, Any, Tuple
from actions import *
from registry import AZIONI_MAP  # registro unico (STEP 5)
import policy
import model_router  # Fase 2: router deterministico dei profili modello

# Cooldown Gemini: se riceve 429 non riprova per N secondi
_gemini_retry_after = 0

# ── ⚡ CACHE RISPOSTE IDENTICHE (Latenza 0ms) ──
_RESPONSE_CACHE = {}  # {query_norm: (timestamp, response_str)}
_RESPONSE_CACHE_TTL = 3600  # 1 ora di cache per risposte identiche
_RESPONSE_CACHE_MAX = 500

# ── SICUREZZA TOOL CALLING (P0) ──────────────────────────────────────────────
# La conferma è una decisione del control-plane (policy.guarded_call): il LLM non
# deve MAI poterla impostare via parametri. Queste chiavi vengono rimosse sempre.
_FORBIDDEN_PARAM_KEYS = {
    "confirmed", "confirm", "confirmation", "force", "forced",
    "skip_confirm", "skip_safety", "approved", "authorized", "bypass",
}

def _sanitize_tool_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Rimuove i parametri che aggirano la conferma imposta da policy."""
    if not isinstance(params, dict):
        return {}
    clean = {}
    for key, value in params.items():
        if not isinstance(key, str):
            continue
        normalized = key.strip().lower()
        if normalized in _FORBIDDEN_PARAM_KEYS or normalized.startswith("__"):
            logger.warning(f"🛡️ [SICUREZZA] Parametro vietato ignorato: '{key}'")
            continue
        clean[key] = value
    return clean

def _filter_kwargs(fn, args: Dict[str, Any]) -> Dict[str, Any]:
    """Tiene solo i kwargs accettati dalla firma (difesa dalle allucinazioni)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return dict(args)
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(args)
    return {k: v for k, v in args.items() if k in params}

def _skill_call_args(azione: str, params: Dict[str, Any], user_request: str) -> Dict[str, Any]:
    """Normalizza gli alias italiani dei parametri in kwargs per skills.py."""
    p = _sanitize_tool_params(params)
    if azione in ("open_app", "apri_app", "apri_applicazione"):
        return {"app_name": p.get("app_name") or p.get("nome_app") or p.get("app") or ""}
    if azione in ("control_media", "media_control", "controllo_media"):
        return {"action": p.get("command") or p.get("action") or p.get("comando") or "playpause"}
    if azione in ("search_youtube", "cerca_su_youtube"):
        return {"query": p.get("query") or p.get("ricerca") or p.get("q") or ""}
    if azione in ("get_system_time", "ora_sistema", "tempo_sistema"):
        return {}
    if azione in ("click_coordinates", "clicca", "clicca_coordinate"):
        return {"x": p.get("x", 0), "y": p.get("y", 0)}
    if azione in ("type_text", "digita", "digita_testo"):
        return {"text": p.get("text") or p.get("testo") or ""}
    if azione in ("press_key", "premi_tasto"):
        return {"key": p.get("key") or p.get("tasto") or ""}
    if azione in ("run_python_code", "esegui_python", "esegui_codice"):
        return {"code_string": p.get("code_string") or p.get("code") or p.get("codice") or ""}
    if azione in ("see_screen", "analizza_schermo", "guarda_schermo"):
        return {"prompt": p.get("prompt") or p.get("richiesta")}
    if azione in ("take_screenshot", "fai_screenshot", "cattura_schermo"):
        return {"filename": p.get("filename")}
    if azione in ("deep_web_research", "ricerca_web", "cerca_web", "cerca_su_internet"):
        return {"query": p.get("query") or p.get("ricerca") or p.get("q") or user_request}
    if azione in ("organize_folder", "organizza_cartella", "riordina_cartella"):
        return {"folder_path": p.get("folder_path") or p.get("cartella") or p.get("path")}
    if azione in ("find_files", "trova_file", "cerca_file"):
        return {
            "extension": p.get("extension") or p.get("estensione") or p.get("ext") or "",
            "folder_path": p.get("folder_path") or p.get("cartella") or p.get("directory"),
        }
    if azione in ("move_file", "sposta_file"):
        return {
            "source": p.get("source") or p.get("sorgente") or p.get("file") or "",
            "destination": p.get("destination") or p.get("destinazione") or p.get("a") or "",
        }
    if azione in ("check_emails", "controlla_email", "leggi_email"):
        return {}
    if azione in ("recupera_resume_progetto", "get_scribe_resume"):
        return {
            "project_id": (
                p.get("project_id") or p.get("progetto") or p.get("project") or ""
            ).strip()
        }
    if azione in ("stato_sessioni_scribe", "get_scribe_status"):
        return {}
    return p


def _classify_media_request(user_request: str):
    """Riconosce i comandi media base senza dipendere dal planner LLM."""
    text = re.sub(r"[^a-z0-9àèéìòù]+", " ", (user_request or "").casefold()).strip()
    if not text:
        return None
    # Lascia al planner le richieste composte che devono prima aprire un'app
    # o cercare/selezionare un contenuto; qui gestiamo solo i tasti globali.
    if re.search(r"\b(?:apri|cerca|trova|seleziona|playlist|album|artista)\b", text):
        return None

    media_item = r"(?:canzon\w*|brano\w*|tracci\w*|pezzo\w*|track\w*|music\w*|spotify|audio|video)"
    next_word = r"(?:successiv\w*|prossim\w*|avanti|dopo|next|skip|salt\w*)"
    previous_word = r"(?:precedent\w*|indietro|prima|previous|prev)"

    if (re.search(rf"\b{media_item}\b.*\b{previous_word}\b", text)
            or re.search(rf"\b{previous_word}\b.*\b{media_item}\b", text)
            or text in {"indietro", "torna indietro", "vai indietro", "precedente", "prev", "previous"}):
        return "previous"
    if (re.search(rf"\b{media_item}\b.*\b{next_word}\b", text)
            or re.search(rf"\b{next_word}\b.*\b{media_item}\b", text)
            or text in {"avanti", "vai avanti", "prossimo", "successivo", "next", "skip", "salta"}):
        return "next"
    if (re.search(r"\b(?:paus\w*|pause)\b", text)
            or text in {"play", "play pausa", "play pause", "metti play", "stop"}
            or re.search(r"\b(?:riprendi|riproduci|fai partire|fai ripartire|avvia|ferma|stop)\b.*\b(?:musica|audio|video|spotify)\b", text)
            or re.search(r"\bmetti\b.*\bplay\b.*\b(?:musica|audio|video|spotify)\b", text)):
        return "play_pause"
    return None


def log_clio_action(action: str, params: Any, success: bool, outcome: str = ""):
    r"""Registra l'azione eseguita da Clio in Sistema\Clio_Log.md di Obsidian."""
    try:
        try:
            from clio_core import VAULT_DIR
        except Exception:
            VAULT_DIR = r"os.path.expanduser("~")\OneDrive\OneSyncFiles"
        log_dir = os.path.join(VAULT_DIR, "Sistema")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "Clio_Log.md")

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        esito_str = "Successo" if success else (f"Errore: {outcome}" if outcome else "Errore")
        params_str = json.dumps(params, ensure_ascii=False) if isinstance(params, (dict, list)) else str(params)
        riga = f"[{now_str}] - Azione: [{action}] - Dettagli: [{params_str}] - Esito: [{esito_str}]\n"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(riga)
        logger.info(f"📓 [OBSIDIAN LOG] Registrata azione in Clio_Log.md: {action}")
    except Exception as e_log:
        logger.warning(f"⚠️ Impossibile salvare log azione in Clio_Log.md: {e_log}")


def invalidate_caches():
    """Invalida _RESPONSE_CACHE e _VAULT_FILE_CACHE e forza la reindicizzazione del Vault."""
    global _RESPONSE_CACHE
    _RESPONSE_CACHE.clear()
    try:
        import clio_core
        if hasattr(clio_core, "_VAULT_FILE_CACHE"):
            clio_core._VAULT_FILE_CACHE.clear()
        if hasattr(clio_core, "build_vault_index"):
            clio_core.build_vault_index(force=True)
        logger.info("🔄 [CACHE] Tutte le cache di Clio invalidate con successo.")
    except Exception as e_inv:
        logger.warning(f"⚠️ Errore durante invalidazione cache vault: {e_inv}")


logger = logging.getLogger("JarvisBrain")

# --- REGISTRO COMPLETO DEGLI STRUMENTI (Tools) ---
# Tutti i tool disponibili per il cervello JARVIS
TOOLS_METADATA = {
    # ── SKILLS PRINCIPALI PC AGENT (skills.py) ──
    "open_app":               {"desc": "Apre un'app Windows (es: Spotify, Chrome, Notepad, Calc). Esempio: {\"action\": \"open_app\", \"params\": {\"app_name\": \"Spotify\"}}", "params": ["app_name"]},
    "control_media":          {"desc": "Controlla la musica/media (play, pause, next, prev, volume_up, volume_down). Esempio: {\"action\": \"media_control\", \"params\": {\"command\": \"play\"}}", "params": ["command"]},
    "get_system_time":        {"desc": "Restituisce l'ora e data attuali del sistema. Esempio: {\"action\": \"get_system_time\", \"params\": {}}", "params": []},
    "search_youtube":         {"desc": "Cerca e apre un video o brano su YouTube. Esempio: {\"action\": \"search_youtube\", \"params\": {\"query\": \"titolo\"}", "params": ["query"]},
    "take_screenshot":        {"desc": "Cattura lo schermo del PC e lo salva come screenshot.png.", "params": []},
    "see_screen":             {"desc": "Computer Vision: cattura lo schermo e lo analizza con Gemini 3.8 Flash, individuando gli elementi a video e le coordinate pixel (x, y) per cliccarli. Esempio: {\"action\": \"see_screen\", \"params\": {\"prompt\": \"descrivi i bottoni e dammi le coordinate\"}}", "params": ["prompt"]},
    "click_coordinates":      {"desc": "Muove il mouse e clicca alle coordinate specificate (x, y). Esempio: {\"action\": \"click_coordinates\", \"params\": {\"x\": 500, \"y\": 300}}", "params": ["x", "y"]},
    "type_text":              {"desc": "Digita del testo nella finestra attiva. Esempio: {\"action\": \"type_text\", \"params\": {\"text\": \"ricetta carbonara\"}}", "params": ["text"]},
    "press_key":              {"desc": "Preme un tasto specifico o combinazione (enter, esc, tab, space, ctrl+c, ctrl+v). Esempio: {\"action\": \"press_key\", \"params\": {\"key\": \"enter\"}}", "params": ["key"]},
    "disegna_su_paint":       {"desc": "Skill locale e gratuita. Ignora coordinate e non usa tool di screenshot o Vision cloud: trova Paint con pygetwindow, rileva localmente il canvas dai pixel senza salvare o inviare immagini, disegna un quadrato 200x200 e verifica una variazione locale. Chiama con coordinate={}. Se Paint non è aperto, lo avvia localmente.", "params": ["coordinate"]},
    "genera_immagine_pollinations": {"desc": "Genera un'immagine raster tramite Pollinations da un prompt artistico e la salva/apre. Invia solo dettagli necessari all'immagine, mai screenshot o dati personali. Esempio: {\"action\":\"genera_immagine_pollinations\",\"params\":{\"prompt\":\"Razzo stilizzato in un cielo stellato, colori vivaci\"}}", "params": ["prompt"]},
    "run_python_code":        {"desc": "Esegue codice Python reale tramite exec() e restituisce l'output o errore. Esempio: {\"action\": \"run_python_code\", \"params\": {\"code_string\": \"print(2+2)\"}}", "params": ["code_string"]},
    "deep_web_research":      {"desc": "Ricerca web approfondita con browser headless: legge le prime 3 pagine e sintetizza con Gemini. Usa per 'cerca su internet', 'notizie', 'fai una ricerca'. Esempio: {\"action\": \"deep_web_research\", \"params\": {\"query\": \"ultime notizie IA\"}}", "params": ["query"]},
    "organize_folder":        {"desc": "Organizza e ordina i file di una cartella in sottocartelle (Immagini, Documenti, Video, ecc.). Esempio: {\"action\": \"organize_folder\", \"params\": {\"folder_path\": \"os.path.expanduser("~")\\Desktop\"}}", "params": ["folder_path"]},
    "find_files":             {"desc": "Cerca file per estensione (.pdf, .png, ecc.) in una cartella o sul Desktop. Esempio: {\"action\": \"find_files\", \"params\": {\"extension\": \"pdf\", \"folder_path\": \"os.path.expanduser("~")\\Desktop\"}}", "params": ["extension", "folder_path"]},
    "move_file":              {"desc": "Sposta un file da sorgente a destinazione con controllo di sicurezza. Esempio: {\"action\": \"move_file\", \"params\": {\"source\": \"file.txt\", \"destination\": \"C:\\Destinazione\\file.txt\"}}", "params": ["source", "destination"]},
    "check_emails":           {"desc": "Legge le ultime email da Gmail e le riassume per la risposta vocale. Esempio: {\"action\": \"check_emails\", \"params\": {}}", "params": []},
    # ── APP & BROWSER ──
    "apri_applicazione":      {"desc": "Apre un'app Windows (es: chrome, notepad, spotify, discord, code).", "params": ["nome_app"]},
    "apri_sito":              {"desc": "Apre un URL nel browser predefinito.", "params": ["url"]},
    "cerca_su_google":        {"desc": "Apre Google nel browser con la ricerca.", "params": ["query"]},
    "cerca_su_youtube":       {"desc": "Cerca video su YouTube nel browser.", "params": ["query"]},
    # ── WEB & INFORMAZIONI ──
    "leggi_motore_ricerca_web": {"desc": "Cerca su web e restituisce snippet. Usa per domande fattuali o notizie.", "params": ["query"]},
    "leggi_sito_web":         {"desc": "Estrae il testo da un URL. Per leggere articoli o pagine.", "params": ["url"]},
    "ottieni_meteo":          {"desc": "Ottiene meteo attuale per una città.", "params": ["citta"]},
    "ottieni_notizie":        {"desc": "Ultime notizie dall'ANSA.", "params": []},
    "leggi_youtube":          {"desc": "Estrae trascrizione di un video YouTube.", "params": ["url"]},
    # ── EMAIL & CALENDARIO ──
    "leggi_gmail":            {"desc": "Legge le ultime email non lette da Gmail.", "params": ["max_risultati"]},
    "leggi_calendario":       {"desc": "Legge gli eventi del calendario Google.", "params": ["giorni"]},
    # ── FILE & SISTEMA ──
    "scrivi_file_testo":      {"desc": "Crea o sovrascrive un file di testo.", "params": ["percorso", "contenuto"]},
    "leggi_file_testo":       {"desc": "Legge il contenuto di un file.", "params": ["percorso"]},
    "cerca_file":             {"desc": "Cerca file per pattern (es: *.pdf) in una directory.", "params": ["pattern", "directory"]},
    "lista_file_cartella":    {"desc": "Elenca i file in una cartella.", "params": ["percorso"]},
    "crea_cartella":          {"desc": "Crea una cartella nel percorso specificato.", "params": ["percorso"]},
    "apri_cartella":          {"desc": "Apre una cartella in Esplora File.", "params": ["percorso"]},
    "rinomina_file":          {"desc": "Rinomina un file o cartella.", "params": ["vecchio", "nuovo"]},
    "elimina_file":           {"desc": "Elimina definitivamente un file o cartella.", "params": ["percorso"]},
    # ── COMANDI DI SISTEMA ──
    "ottieni_info_sistema":   {"desc": "Restituisce CPU%, RAM%, disco del PC.", "params": []},
    "esegui_comando_shell":   {"desc": "Esegue un comando nel terminale/cmd.", "params": ["comando"]},
    "esegui_powershell_avanzato": {"desc": "Esegue uno script PowerShell avanzato.", "params": ["script"]},
    "esegui_script_python":   {"desc": "Esegue del codice Python e ne restituisce l'output.", "params": ["codice"]},
    "uccidi_processo":        {"desc": "Termina forzatamente un processo per nome.", "params": ["nome_processo"]},
    "finestre_gestione":      {"desc": "Gestisce finestre: azione=lista|focus|chiudi + titolo_parziale.", "params": ["azione", "titolo_parziale"]},
    # ── AUDIO & MEDIA ──
    "imposta_volume":         {"desc": "Imposta il volume di sistema (0-100).", "params": ["percentuale"]},
    "muta_volume":            {"desc": "Muta o de-muta il volume.", "params": []},
    "media_play_pausa":       {"desc": "Play/Pausa musica o video in riproduzione.", "params": []},
    "media_prossimo":         {"desc": "Passa al brano successivo.", "params": []},
    "media_precedente":       {"desc": "Torna al brano precedente.", "params": []},
    # ── PC POWER ──
    "spegni_pc":              {"desc": "Spegne il PC, con ritardo opzionale in minuti.", "params": ["minuti"]},
    "riavvia_pc":             {"desc": "Riavvia il PC.", "params": []},
    "blocca_pc":              {"desc": "Blocca il PC (schermata di login).", "params": []},
    "minimizza_tutto":        {"desc": "Minimizza tutte le finestre e mostra il desktop.", "params": []},
    "svuota_cestino":         {"desc": "Svuota il cestino di Windows.", "params": []},
    # ── APPUNTI & TASTIERA ──
    "scrivi_appunti":         {"desc": "Copia un testo negli appunti del PC (clipboard).", "params": ["testo"]},
    "leggi_appunti":          {"desc": "Legge cosa c'è copiato negli appunti.", "params": []},
    "digita_su_tastiera_fisica": {"desc": "Digita testo nell'applicazione attiva via copia-incolla.", "params": ["testo"]},
    # ── VISIONE ──
    "fai_screenshot_pc":      {"desc": "Scatta uno screenshot del PC.", "params": []},
    "analizza_schermo_pc":    {"desc": "Scatta screenshot e analizza cosa c'è a video.", "params": []},
    # ── HARDWARE ──
    "stato_batteria":         {"desc": "Controlla percentuale batteria e stato di carica.", "params": []},
    # ── MEMORIA & DOMOTICA ──
    "registra_episodio":      {"desc": "Salva un evento importante nella memoria di Clio.", "params": ["titolo", "descrizione"]},
    "accendi_luce":           {"desc": "Accende una luce/dispositivo via Home Assistant.", "params": ["entita_id"]},
    "spegni_luce":            {"desc": "Spegne una luce/dispositivo via Home Assistant.", "params": ["entita_id"]},
    # ── TELEFONO ANDROID ──
    "telefono_apri_app":      {"desc": "Apre un'app sul telefono Android (es: com.whatsapp).", "params": ["nome_pacchetto"]},
    "telefono_screenshot":    {"desc": "Scatta screenshot del telefono Android.", "params": []},
    "telefono_clicca":        {"desc": "Simula un tocco sul telefono alle coordinate x,y.", "params": ["x", "y"]},
    "telefono_home":          {"desc": "Torna alla Home del telefono.", "params": []},
    "telefono_indietro":      {"desc": "Premi tasto Indietro sul telefono.", "params": []},
    # ── CODING AGENT (Fase 4) ──
    "esegui_coding_task":     {"desc": "Coding Agent locale in SANDBOX: delega un task di programmazione (fix bug, feature, test) al profilo CODER locale, SOLO dentro la cartella _clio_sandbox. Nessun fallback cloud. Dopo le modifiche esegue pytest e riporta diff e output dei test. Richiede conferma dell'utente.", "params": ["task_descrizione"]},
    "run_opencode_task":      {"desc": "Alias legacy: delega al Coding Agent locale nella sandbox. Non avvia OpenCode o Gemini e non opera fuori dalla sandbox. Richiede conferma esplicita.", "params": ["prompt", "cwd"]},
    # ── MEMORIA SESSIONI (R1 — The Scribe, sola lettura) ──
    "recupera_resume_progetto": {"desc": "Legge da The Scribe il Resume Markdown dell'ultima sessione di un progetto (dove eravamo rimasti, file modificati, prossimo passo). Usa per 'Dove ero rimasto su X' o 'Cosa stavo facendo su X'. Progetti: cortex, roast-room, clio, tyrion-clips-studio, the_scribe.", "params": ["project_id"]},
    "stato_sessioni_scribe":  {"desc": "Legge da The Scribe lo stato delle sessioni: quale progetto è attivo, dalle/alle e ultima attività.", "params": []},
    # ── ORCHESTRAZIONE MULTI-AGENTE (R5 — Agent Registry + Workflow Engine) ──
    "esegui_workflow":        {"desc": "Orchestra più agenti (Coding Agent in sandbox e/o The Scribe) su un obiettivo, con stati UNDERSTAND/GROUND/PLAN/AUTHORIZE/EXECUTE/VERIFY/PUBLISH/REPORT, checkpoint su SQLite e ripresa dopo un crash. Usa per richieste tipo 'fixa il bug e documentalo con The Scribe': il piano richiede conferma UNA volta, poi ogni passo passa comunque dal control-plane. Richiede conferma dell'utente.", "params": ["goal", "progetto"]},
    "stato_workflow":         {"desc": "Mostra lo stato di un workflow multi-agente (stato macchina, passi, checkpoint, artefatti). Senza parametri elenca gli ultimi workflow.", "params": ["workflow_id"]},
    "ispeziona_agenti":       {"desc": "Elenca gli agenti registrati (CLIO, Coding Agent, The Scribe) con capacità, livello di rischio, timeout e permessi.", "params": []},
    # ── COMPUTER USE CROSS-APP (R6 — UI Automation, whitelist) ──
    "ispeziona_finestra":     {"desc": "Legge i controlli visibili (titolo, testi, pulsanti, id) di un'app desktop CONSENTITA via UI Automation, senza screenshot. App: notepad.exe, Code.exe, Antigravity.exe (o quelle in CLIO_COMPUTER_ALLOWED_APPS). Richiede conferma dell'utente.", "params": ["app_name"]},
    "clicca_elemento":        {"desc": "Clicca un elemento (es. un pulsante) di un'app desktop consentita. Se il nome è ambiguo o assente NON clicca e riporta l'errore. Richiede conferma dell'utente.", "params": ["app_name", "element_name"]},
    "scrivi_testo_app":       {"desc": "Digita testo nell'app desktop consentita (finestra portata in primo piano). Rifiuta Invio/Tab e testi oltre 5000 caratteri. Richiede conferma dell'utente.", "params": ["app_name", "text"]},
    # ── VAULT INTELLIGENCE (R7 — Knowledge Curator, SOLA LETTURA) ──
    "scansiona_vault":        {"desc": "Scansiona il vault Obsidian e riporta i problemi trovate (duplicati, note obsolete, link rotti) SENZA modificare nulla. Usa per 'controlla il vault', 'ci sono note duplicate?'. Senza vault_path usa CLIO_VAULT_PATH o la config di The Scribe.", "params": ["vault_path", "days"]},
    "proponi_pulizia_vault":  {"desc": "Genera un report Markdown di PROPOSTE di pulizia del vault (duplicati, note obsolete, link rotti) e lo salva in clio/data/vault_proposals_AAAA-MM-GG.md. Non modifica il vault: le proposte vanno approvate dall'utente.", "params": ["vault_path", "days"]},
    # ── PROATTIVITÀ QUOTIDIANA (R8 — finestra attiva, privacy-first) ──
    "attiva_proattivita":     {"desc": "Avvia un monitor in background che osserva la finestra in primo piano e propone note pertinenti del vault (tema del titolo nei browser, note di progetto negli editor) con una notifica desktop discreta. Massimo 1 notifica ogni 30 minuti, nessun suggerimento in navigazione privata, nessun log di app o titoli. Senza vault_path usa CLIO_VAULT_PATH o la config di The Scribe.", "params": ["vault_path", "interval_seconds"]},
    # ── DESKTOP AGENT (FASE 2 · C1 — Partner Creativo) ──
    "apri_app_e_inizia_idea": {"desc": "Apre lo strumento di lavoro richiesto e crea una IdeaSession. Per Paint usa app_name=mspaint. In flask_ui è auto-approvato; gli altri canali richiedono conferma.", "params": ["app_name", "obiettivo", "progetto"]},
    "porta_finestra_in_primo_piano": {"desc": "Porta in primo piano la finestra di un'app già aperta (es. mspaint). Usa per 'torna su Paint', 'metti davanti il Blocco Note'. Risponde False se la finestra non esiste.", "params": ["app_name"]},
    # ── VISION AGENT (FASE 2 · C2 — Partner Creativo) ──
    "analyze_sketch_and_update_idea": {"desc": "Guarda la finestra dell'app indicata (cattura SOLO quella finestra), la fa descrivere al profilo VISION (Gemini) e salva la descrizione nella IdeaSession. Usa per 'cosa sto disegnando su Paint?', 'guarda lo schizzo e dimmi che è'. Senza idea_id usa l'ultima idea aperta: se non ce n'è, avviala prima con apri_app_e_inizia_idea.", "params": ["idea_id", "app_name", "prompt"]},
    # ── BLENDER AGENT (FASE 2 · C5 — 2D -> 3D) ──
    "genera_modello_3d": {"desc": "Legacy 3D, mantenuto solo per compatibilità con vecchie IdeaSession. Per i nuovi modelli usa genera_modello_3d_blender: Blender locale, esportazione OBJ reale, nessun fallback fittizio.", "params": ["idea_id"]},
    "genera_modello_3d_blender": {"desc": "Crea localmente un modello OBJ reale con Blender/bpy da una descrizione. In flask_ui è auto-approvato; gli altri canali richiedono conferma. Se Blender non è installato lo dice chiaramente e non genera un modello fittizio.", "params": ["oggetto", "idea_id"]},
    # ── C3 — SESSIONE PERSISTENTE ("Riprendi da qui") ──
    "riprendi_da_qui": {"desc": "Riprende l'idea in corso: legge obiettivo, prossimo passo, decisioni e materiali collegati e chiede se aprire i file. Usa per 'riprendi da qui', 'dove eravamo rimasti'. Senza idea_id usa l'ultima idea aperta; 'progetto' filtra per progetto.", "params": ["progetto", "idea_id"]},
    "aggiungi_decisione_idea": {"desc": "Registra una decisione presa sull'idea in corso e la manda a The Scribe (nota del progetto in Obsidian, categoria 'decisione'). Usa per 'annota che abbiamo deciso X'.", "params": ["testo", "idea_id"]},
    "imposta_prossimo_passo": {"desc": "Imposta il prossimo passo concreto dell'idea e lo manda a The Scribe (categoria 'completamento'). Una sola azione, concreta. Usa per 'il prossimo passo è X'.", "params": ["testo", "idea_id"]},
    "aggiungi_materiale_idea": {"desc": "Collega un file o una nota del PC all'idea in corso (percorso completo). Usa per 'collega questo file all'idea'.", "params": ["path", "idea_id"]},
    "imposta_blocco_idea": {"desc": "Segnala cosa impedisce il progresso dell'idea (o azzera il blocco con testo vuoto). Usa per 'sono bloccato perché X', 'blocco risolto'.", "params": ["testo", "idea_id"]},
    # ── C4 — RESEARCH AGENT (riferimenti web + YouTube) ──
    "cerca_riferimenti": {"desc": "Cerca riferimenti simili all'idea sul web (DuckDuckGo) e su YouTube, li riassume e li salva nell'idea come materiali di ricerca. Usa per 'cerca riferimenti', 'cerca idee simili', 'trova ispirazione su ...'. Senza query usa l'obiettivo dell'idea; senza idea_id usa l'ultima aperta.", "params": ["query", "idea_id"]},
}

# ── Automazioni di Francesco (integrazione Cortex) ─────────────────────────
# I metadati dei tool di automations.py vengono aggiunti al prompt del LLM.
try:
    from automations import TOOLS_METADATA as _AUTOMATIONS_TOOLS_METADATA
    TOOLS_METADATA.update(_AUTOMATIONS_TOOLS_METADATA)
except Exception as _e:
    logger.warning(f"Metadati automazioni non caricati: {_e}")


# ── C3: "RIPRENDI DA QUI" — pre-router deterministico ──────────────────────
# Non passa dal LLM (che potrebbe inventare): legge l'IdeaSession e riassume lo
# stato reale. Le frasi sono volutamente poche e letterali.
_RESUME_C3_PATTERNS = (
    re.compile(r"\briprendi(?:amo)?\s+da\s+qui\b", re.IGNORECASE),
    re.compile(r"\bdove\s+(?:eravamo|ci\s+eravamo|ci\s+siamo)\s+(?:rimast|fermat)[ie]\b",
               re.IGNORECASE),
    re.compile(r"\bdove\s+(?:ero|mi\s+ero)\s+(?:rimast|fermat)[oa]\b", re.IGNORECASE),
    re.compile(r"\bda\s+dove\s+ripart(?:iamo|o)\b", re.IGNORECASE),
    re.compile(r"\briprendi(?:amo)?\s+l['’]?\s?idea\b", re.IGNORECASE),
)

# TTS iniettabile: in produzione resta None (la voce di CLIO legge già la
# risposta restituita), nei test si sostituisce con un doppio per verificare
# che il riassunto venga "letto".
_TTS_HOOK = None


def set_tts_hook(fn):
    """Installa (o rimuove, con None) il motore TTS usato da 'Riprendi da qui'."""
    global _TTS_HOOK
    _TTS_HOOK = fn if callable(fn) else None
    return _TTS_HOOK


def _progetto_dal_resume(testo: str, inizio: int) -> str:
    """Estrae il progetto dalla coda della frase ('... su Clio' -> 'clio')."""
    coda = testo[inizio:]
    try:
        from scribe_control import _clean_project_hint
        return _clean_project_hint(coda)
    except Exception:                    # pragma: no cover - dipende dall'ambiente
        return re.sub(r"^\s*(?:su|del|di)\s+", "", coda.strip(" ?!.,;:"))[:40]


def _riprendi_da_qui_pre_router(user_request: str, on_text=None):
    """Testo del resume se la frase è 'riprendi da qui', altrimenti None.

    Condizione: deve esistere un'IdeaSession aperta (eventualmente del progetto
    citato). Se non c'è, ritorna None e la richiesta prosegue (router di The
    Scribe / LLM), così non si rompe il comportamento precedente.
    """
    testo = user_request if isinstance(user_request, str) else ""
    if not testo.strip():
        return None
    for pattern in _RESUME_C3_PATTERNS:
        match = pattern.search(testo)
        if not match:
            continue
        try:
            import idea_session
        except Exception as e:           # pragma: no cover - dipende dall'ambiente
            logger.warning(f"[C3] idea_session non disponibile: {e}")
            return None
        progetto = _progetto_dal_resume(testo, match.end())
        try:
            sessione = idea_session.find_idea_for_resume(progetto)
            if sessione is None:
                logger.info(f"[C3] nessuna idea aperta per «{progetto or 'qualsiasi progetto'}»: "
                            "lascio la richiesta al router di The Scribe")
                return None
            logger.info(f"⏯️ [C3] Riprendi da qui -> idea {sessione.idea_id[:8]} "
                        f"({sessione.project_id})")
            risposta = idea_session.riprendi_da_qui(progetto=progetto, parla=_TTS_HOOK)
        except idea_session.IdeaSessionError as e:
            logger.error("[C3] resume non disponibile: %s", e)
            risposta = (
                "Capo, lo store delle idee è danneggiato o non leggibile. "
                "Non ho modificato i dati; serve ripristinarli da un backup prima di riprendere."
            )
            if on_text:
                on_text(risposta)
            return risposta
        if on_text:
            on_text(risposta)
        return risposta
    return None


# ── C4: "CERCA RIFERIMENTI" — pre-router deterministico ─────────────────────
# Come C3: nessuna chiamata al LLM per capire *cosa* fare. La ricerca vera la fa
# `research_agent` (browser headless, mai una GUI) e l'esecuzione passa dal
# control-plane (`policy.guarded_call`), così resta tracciata come le altre azioni.
_RESEARCH_C4_PATTERNS = (
    re.compile(r"\bcerc(?:a|ami|o|ate)\s+(?:dei\s+|alcuni\s+|qualche\s+)?riferiment[oi]\b",
               re.IGNORECASE),
    re.compile(r"\bcerc(?:a|ami|o|ate)\s+(?:delle\s+|alcune\s+)?idee\s+simili\b",
               re.IGNORECASE),
    re.compile(r"\bcerc(?:a|ami|o|ate)\s+(?:un po'?\s+di\s+)?ispirazione\b", re.IGNORECASE),
    re.compile(r"\btrov(?:a|ami|ate)\s+(?:un po'?\s+di\s+)?ispirazione\b", re.IGNORECASE),
)

# ── C3: avvio esplicito di una IdeaSession senza richiedere un'app ───────────
_START_IDEA_PATTERNS = (
    re.compile(
        r"\b(?:iniziamo|inizia|creiamo|crea|vorrei\s+iniziare)\s+"
        r"(?:un['’]?idea|una\s+idea)\s+(?:per|su|di)\s+(?P<goal>.+?)\s*[.!?]*$",
        re.IGNORECASE,
    ),
)


def _inizia_idea_pre_router(user_request: str, on_text=None, origin: str = "jarvis"):
    """Crea una IdeaSession quando l'utente avvia un'idea senza nominare un'app."""
    testo = user_request if isinstance(user_request, str) else ""
    for pattern in _START_IDEA_PATTERNS:
        match = pattern.search(testo.strip())
        if not match:
            continue
        goal = match.group("goal").strip(" \t.,;:!?\"'`")
        if not goal:
            return None
        try:
            import idea_session
            import policy
            sessione = policy.guarded_call(
                "crea_idea_session", idea_session.create_idea_session,
                {"goal": goal, "project_id": "clio"}, origin=origin)
        except Exception as e:
            logger.warning(f"[C3] creazione IdeaSession fallita: {type(e).__name__}: {e}")
            return None
        if not getattr(sessione, "idea_id", None):
            return str(sessione)
        risposta = f"💡 Idea avviata, Capo: «{goal}». ID sessione: {sessione.idea_id}."
        logger.info(f"[C3] IdeaSession creata: {sessione.idea_id} (goal={goal!r})")
        if on_text:
            try:
                on_text(risposta)
            except Exception as e:
                logger.warning(f"[C3] on_text non disponibile: {type(e).__name__}: {e}")
        return risposta
    return None

# Parole che introducono l'argomento dopo il verbo ("cerca riferimenti *per* X").
_RESEARCH_PREFISSI = re.compile(
    r"^(?:per|su|di|del|dello|della|dei|degli|delle|sul|sullo|sulla|"
    r"un|uno|una|il|lo|la|i|gli|le)\b\s*",
    re.IGNORECASE)


def _query_e_progetto(testo: str, fine: int) -> Tuple[str, str]:
    """(query, progetto) dalla coda della frase.

    Due parole o più = argomento da cercare ("il poster del concerto"); una sola
    parola = nome del progetto ("su Clio") e la query sarà l'obiettivo dell'idea.
    """
    coda = re.sub(r"^[\s,;:]+", "", testo[fine:])
    for _ in range(3):                      # "per il poster" -> "poster"
        nuova = _RESEARCH_PREFISSI.sub("", coda)
        if nuova == coda:
            break
        coda = nuova
    coda = coda.strip(" ?!.,;:\"'")
    parole = coda.split()
    if len(parole) >= 2:
        return " ".join(parole)[:200], ""
    if len(parole) == 1:
        return "", parole[0].lower()[:40]
    return "", ""


def _cerca_riferimenti_pre_router(user_request: str, on_text=None, origin: str = "jarvis"):
    """Risposta se la frase chiede riferimenti/ispirazione, altrimenti None.

    Condizione: deve esistere un'IdeaSession aperta (anche del progetto citato).
    Senza idea non si risponde: la richiesta prosegue (LLM / router The Scribe),
    così il comportamento precedente resta intatto.
    """
    testo = user_request if isinstance(user_request, str) else ""
    if not testo.strip():
        return None
    for pattern in _RESEARCH_C4_PATTERNS:
        match = pattern.search(testo)
        if not match:
            continue
        try:
            import idea_session
            import research_agent
        except Exception as e:               # pragma: no cover - dipende dall'ambiente
            logger.warning(f"[C4] moduli non disponibili: {e}")
            return None

        query, progetto = _query_e_progetto(testo, match.end())
        sessione = idea_session.find_idea_for_resume(progetto) if progetto else \
            idea_session.find_idea_for_resume()
        if sessione is None and progetto:
            # La parola singola non era un progetto ma l'argomento:
            # "trova ispirazione per poster" -> query="poster", ultima idea aperta.
            query, progetto = progetto, ""
            sessione = idea_session.find_idea_for_resume()
        if sessione is None:
            logger.info(f"[C4] nessuna idea aperta per «{progetto or 'qualsiasi progetto'}»: "
                        "lascio la richiesta al LLM")
            return None

        logger.info(f"🔎 [C4] Ricerca riferimenti -> idea {sessione.idea_id[:8]} "
                    f"(query: {query or sessione.user_goal!r})")
        # La ricerca dura qualche secondo: se la voce è attiva, avvisa subito.
        if on_text:
            try:
                on_text("Cerco riferimenti sul web e su YouTube. Un attimo.")
            except Exception:                # pragma: no cover - dipende dal TTS
                pass
        parametri = {"query": query, "idea_id": sessione.idea_id}
        risposta = None
        try:
            import policy
            import registry
            if "cerca_riferimenti" in registry.AZIONI_MAP:
                # La funzione si risolve a runtime sul modulo (mai un binding
                # stantio): il registro dice solo SE l'azione esiste.
                risposta = policy.guarded_call("cerca_riferimenti",
                                               research_agent.cerca_riferimenti,
                                               parametri, origin=origin)
        except Exception as e:               # pragma: no cover - control-plane assente
            logger.warning(f"[C4] control-plane non disponibile ({e}): chiamo l'agente")
        if risposta is None:
            risposta = research_agent.cerca_riferimenti(**parametri)

        if _TTS_HOOK:
            try:
                _TTS_HOOK(risposta)
            except Exception as e:           # pragma: no cover - dipende dal TTS
                logger.warning(f"[C4] TTS non disponibile: {type(e).__name__}: {e}")
        if on_text:
            try:
                on_text(risposta)
            except Exception as e:           # pragma: no cover - dipende dal TTS
                logger.warning(f"[C4] on_text non disponibile: {type(e).__name__}: {e}")
        return risposta
    return None


# ── "APRI [APP]" — pre-router deterministico ────────────────────────────────
# Il primo test vocale ha mostrato il modello rispondere a voce "Apro Paint"
# SENZA chiamare il tool: l'azione veniva allucinata. Qui il riconoscimento è
# letterale e l'esecuzione è certa, oppure non si risponde affatto.
#
#   "apri Paint"                      -> open_app (nessuna idea)
#   "apri il blocco note"             -> open_app
#   "apri Paint e iniziamo un'idea"   -> apri_app_e_inizia_idea(obiettivo=...)
#
# Se l'app non è nella whitelist del Desktop Agent la richiesta prosegue
# (LLM / altri router): così "apri la cartella Download" o "apri il sito X"
# restano ai tool che sanno gestirle.
_APRI_APP_RE = re.compile(
    r"^\s*(?:(?:ehi|hey|ok|okay|dai|su)\s+)?(?:(?:clio)\s*[,:]?\s*)?"
    r"(?:apri|aprimi|aprite|apriamo|avvia|avviami|avviate|avviamo|"
    r"lancia|lanciami|lanciate|lanciamo)\s+(?P<coda>.+?)\s*[.!?]*\s*$",
    re.IGNORECASE,
)

_ARTICOLO_APP_RE = re.compile(r"^(?:l['’]\s*|il\s+|lo\s+|la\s+|i\s+|gli\s+|le\s+)",
                              re.IGNORECASE)
_PAROLA_APP_RE = re.compile(r"^(?:app|applicazione|programma)\s+", re.IGNORECASE)
_OBIETTIVO_SEP_RE = re.compile(r"\s*(?:[,;!]|\s+e\s+|\s+poi\s+|\s+per\s+)\s*",
                              re.IGNORECASE)
_OBIETTIVO_PREFISSO_RE = re.compile(r"^(?:e|poi|così|cosi)\s+", re.IGNORECASE)
# Coda che NON è un obiettivo: cortesie o chiusure ("apri Paint, grazie").
_OBIETTIVO_NON_VALIDO = frozenset({
    "favore", "per favore", "piacere", "cortesia", "gentilmente", "grazie",
    "grazie mille", "please", "basta", "niente", "nulla", "un attimo",
})
_IDEA_SUFFIX_RE = re.compile(
    r"^(?:(?:inizia(?:mo|re)?|avvia(?:mo|re)?|crea(?:mo|re)?|"
    r"facciamo|apri(?:amo|re)?)\s+)?(?:un[’']idea|idea|una\s+sessione|sessione)\b",
    re.IGNORECASE,
)
MAX_APP_CHARS = 120
MAX_OBIETTIVO_CHARS = 500


def _parse_apri_app(user_request):
    """(app, obiettivo) se la frase è 'apri [app]', altrimenti None.

    L'obiettivo è la coda dopo " e ", ",", " per " (es. "apri Paint e iniziamo
    un'idea" -> app "Paint", obiettivo "iniziamo un'idea"). Una cortesia
    ("per favore", "grazie") non è un obiettivo.
    """
    testo = user_request if isinstance(user_request, str) else ""
    if not testo.strip():
        return None
    match = _APRI_APP_RE.match(testo.strip())
    if not match:
        return None

    coda = _ARTICOLO_APP_RE.sub("", match.group("coda").strip(), count=1)
    coda = _PAROLA_APP_RE.sub("", coda, count=1).strip()
    if not coda:
        return None

    parti = _OBIETTIVO_SEP_RE.split(coda, maxsplit=1)
    app = (parti[0] or "").strip(" \t\"'`").strip()
    obiettivo = (parti[1] if len(parti) > 1 else "").strip(" \t\"'`.,;:!?")
    obiettivo = _OBIETTIVO_PREFISSO_RE.sub("", obiettivo).strip()
    if obiettivo.lower().startswith("per "):
        obiettivo = obiettivo[4:].strip()
    if obiettivo.lower() in _OBIETTIVO_NON_VALIDO:
        obiettivo = ""
    if not app:
        return None
    return app[:MAX_APP_CHARS], obiettivo[:MAX_OBIETTIVO_CHARS]


# ── ESTRAZIONE JSON ROBUSTA (30/10/2026) ───────────────────────────────────
# La vecchia regex r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}' non gestiva le graffe
# annidate: con un payload tipo {"action": "run_python_code", "params":
# {"code_string": "... {'a': 1} ..."}} non trovava il match e il loop
# restituiva il JSON dell'azione COME TESTO, senza eseguirlo (nessun file
# prodotto). Qui si usa json.JSONDecoder.raw_decode, che rispetta graffe,
# parentesi e stringhe (anche con graffe escapate dentro il codice).
def _estrai_json(testo):
    """Primo oggetto JSON (dict) valido contenuto in `testo`, altrimenti None."""
    if not isinstance(testo, str) or "{" not in testo:
        return None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(testo):
        if ch != "{":
            continue
        try:
            obj, _fine = decoder.raw_decode(testo, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── "DISEGNA <soggetto>" — generazione immagine con Pillow (30/10/2026) ────
# Bug: il pre-router "apri [app]" si fermava dopo l'apertura, quindi
# "apri Paint e disegna un razzo" apriva Paint + IdeaSession e NON produceva
# nessuna immagine. Qui il comando di disegno viene eseguito per intero:
#   1. app citata (se presente) aperta dal Desktop Agent (origin propagato);
#   2. il LLM genera SOLO codice Python (protocollo minimo: niente JSON
#      azione, che il modello a volte restituisce come testo senza eseguirlo);
#   3. il codice passa da policy.guarded_call("run_python_code") — quindi
#      resta auto-approvato solo per flask/flask_ui, come le altre azioni UI;
#   4. il file prodotto viene verificato; se manca, un solo retry.
# Il pre-router è volutamente ristretto: se la frase parla di script, codice,
# Blender o modelli 3D, restituisce None e lascia il flusso normale.
_DISEGNO_VERBO_RE = re.compile(
    r"\b(?:disegna(?:mi|te)?|dipingi(?:mi)?|abbozza(?:mi)?|"
    r"crea\s+(?:un'|una\s+)?immagine|genera\s+(?:un'|una\s+)?immagine|"
    r"fai\s+(?:un\s+)?disegno)\b",
    re.IGNORECASE,
)
_DISEGNO_ESCLUSI_RE = re.compile(
    r"\b(?:script|codice|coding|programma|programmino|funzione|classe|"
    r"modello\s*3d|blender|\.obj\b)\b",
    re.IGNORECASE,
)
_DISEGNO_APP_RE = re.compile(
    r"^\s*(?:(?:ehi|hey|ok|okay|dai|su|clio)\s*[,:]?\s*)*"
    r"(?:apri|aprimi|avvia|avviami|lancia|lanciami)\s+(?P<app>[^,;]+?)\s+e\s+",
    re.IGNORECASE,
)
_DISEGNO_FILE_RE = re.compile(
    r"[\"'`]?([\w.\-]+\.(?:png|jpg|jpeg|bmp))[\"'`]?",
    re.IGNORECASE,
)
_DISEGNO_PROMPT = (
    "Sei un generatore di codice Python. Rispondi SOLO con codice Python "
    "eseguibile: nessuna spiegazione, nessun markdown, nessun blocco di "
    "backtick.\n"
    "Requisiti tassativi:\n"
    "- usa esclusivamente Pillow: from PIL import Image, ImageDraw\n"
    "- crea un'immagine 512x512 con sfondo bianco\n"
    "- disegna: {soggetto}\n"
    "- salva con img.save(r\"{percorso}\") usando esattamente questo percorso\n"
    "- non usare input(), rete, sottoprocessi o finestre\n"
    "- stampa 'OK' alla fine\n"
)


def _pulisci_codice(testo):
    """Toglie markdown e prosa: tiene solo il codice Python."""
    if not testo:
        return ""
    t = str(testo).strip()
    blocco = re.search(r"```(?:python)?\s*(.+?)```", t, re.DOTALL | re.IGNORECASE)
    if blocco:
        t = blocco.group(1)
    righe = t.splitlines()
    for i, riga in enumerate(righe):
        if riga.lstrip().startswith(("import ", "from ")):
            righe = righe[i:]
            break
    return "\n".join(righe).strip()


def _soggetto_disegno(testo):
    """Soggetto da disegnare ('un razzo') a partire dalla frase utente."""
    soggetto = testo
    m_app = _DISEGNO_APP_RE.match(soggetto)
    if m_app:
        soggetto = soggetto[m_app.end():]
    soggetto = _DISEGNO_VERBO_RE.sub("", soggetto, count=1)
    soggetto = _DISEGNO_FILE_RE.sub("", soggetto)
    soggetto = re.sub(r"\b(?:con|usando|tramite)\s+pillow\b", "", soggetto,
                      flags=re.IGNORECASE)
    soggetto = re.sub(r"\b(?:e\s+)?salva(?:lo|rlo|re|rla)?\b.*$", "", soggetto,
                      flags=re.IGNORECASE)
    return soggetto.strip(" \t.,;:!?\"'`")


def _disegno_pre_router(brain, user_request, on_text=None, origin: str = "jarvis"):
    """Esegue 'disegna X' / 'apri <app> e disegna X'. None se non è un disegno."""
    from pathlib import Path

    testo = user_request if isinstance(user_request, str) else ""
    if not testo.strip() or not _DISEGNO_VERBO_RE.search(testo):
        return None
    if _DISEGNO_ESCLUSI_RE.search(testo):
        return None

    soggetto = _soggetto_disegno(testo)
    if not soggetto:
        return None

    base = Path(__file__).resolve().parent
    m_file = _DISEGNO_FILE_RE.search(testo)
    nome = os.path.basename(m_file.group(1)) if m_file else ""
    if not nome:
        slug = re.sub(r"[^a-z0-9]+", "_", soggetto.lower()).strip("_")[:40]
        nome = f"disegno_{slug or 'clio'}.png"
    if not nome.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
        nome += ".png"
    percorso = base / nome

    messaggi = []
    logger.info(f"[DISEGNO] soggetto={soggetto!r} file={percorso.name}")

    # 1) App citata ("apri Paint e ..."): apertura best-effort, non blocca il disegno.
    m_app = _DISEGNO_APP_RE.match(testo)
    if m_app:
        app = (m_app.group("app") or "").strip(" \t\"'`")
        try:
            import desktop_agent
            if desktop_agent.is_allowed_app(app):
                exe = desktop_agent.normalize_app_name(app)
                aperta = desktop_agent.open_app(app_name=exe, origin=origin)
                if aperta is True:
                    messaggi.append(f"✅ {app} aperto.")
                else:
                    messaggi.append(f"⚠️ {app} non aperto (conferma scaduta o avvio fallito).")
            else:
                logger.info(f"[DISEGNO] '{app}' non in whitelist: salto l'apertura")
        except Exception as e:
            logger.warning(f"[DISEGNO] apertura {app} fallita: {type(e).__name__}: {e}")

    # 2) Codice Pillow dal LLM (solo codice, protocollo minimo) + 3) esecuzione.
    import skills
    prompt = _DISEGNO_PROMPT.format(soggetto=soggetto, percorso=str(percorso))
    esito = ""
    for tentativo in (1, 2):
        codice = _pulisci_codice(brain._call_llm(prompt, task_type="code"))
        if not codice:
            logger.warning(f"[DISEGNO] tentativo {tentativo}: il modello non ha "
                           "restituito codice")
            continue
        logger.info(f"[DISEGNO] tentativo {tentativo}: {len(codice)} caratteri di codice")
        esito = policy.guarded_call(
            "run_python_code", skills.run_python_code,
            {"code_string": codice, "timeout": 20}, origin=origin)
        esito = str(esito)
        if percorso.exists() and percorso.stat().st_size > 0:
            break
        logger.warning(f"[DISEGNO] tentativo {tentativo} senza file. Output: {esito[:300]}")
        prompt = (_DISEGNO_PROMPT.format(soggetto=soggetto, percorso=str(percorso))
                  + f"\nIl tentativo precedente NON ha creato il file. Output:\n{esito[:400]}\n")

    # 4) Verifica finale: nessun file = nessuna dichiarazione di successo.
    if percorso.exists() and percorso.stat().st_size > 0:
        dim = percorso.stat().st_size
        messaggi.append(f"✅ Immagine creata: {percorso} ({dim} byte)")
    elif "Conferma scaduta" in esito or "annullata" in esito:
        messaggi.append(f"⌛ Conferma scaduta: il codice Python non è stato eseguito.")
    else:
        messaggi.append(f"❌ Non sono riuscito a creare {percorso.name}. Output: {esito[:200]}")

    risposta = " ".join(messaggi).strip()
    if on_text:
        try:
            on_text(risposta)
        except Exception as e:           # pragma: no cover - dipende dal TTS
            logger.warning(f"[DISEGNO] on_text non disponibile: {type(e).__name__}: {e}")
    return risposta


# ── "MODELLO 3D" — C5 deterministico (30/10/2026) ─────────────────────────
# Bug: "genera un modello 3d di una sedia" finiva nel loop LLM invece del tool
# dedicato. Qui il comando è deterministico: IdeaSession + skill Blender locale,
# che restituisce un OBJ verificato o un messaggio chiaro se Blender manca.
_MOD3D_RE = re.compile(
    r"\b(?:modello|modellino)\s*(?:3d|3-d|tridimensionale)\b|\bin\s+3d\b",
    re.IGNORECASE,
)
_MOD3D_SOGG_RE = re.compile(
    r"\b(?:di|del|della|dello|dei|delle|per|che\s+rappresenta|che\s+sia)\s+(.+)$",
    re.IGNORECASE,
)
# "dello schizzo", "dell'ultimo disegno": si riferiscono all'idea già aperta.
_MOD3D_RIFERIMENTO_RE = re.compile(
    r"\b(?:schizzo|disegno|idea|progetto|questo|questa|quello|quella|"
    r"ultimo|ultima|osservazione|precedente)\b",
    re.IGNORECASE,
)


def _modello_3d_pre_router(brain, user_request, on_text=None, origin: str = "jarvis"):
    """Esegue 'genera un modello 3d di X'. None se la frase non parla di 3D."""
    testo = user_request if isinstance(user_request, str) else ""
    if not testo.strip() or not _MOD3D_RE.search(testo):
        return None

    m_sogg = _MOD3D_SOGG_RE.search(testo)
    soggetto = (m_sogg.group(1) if m_sogg else "").strip(" \t.,;:!?\"'`")
    if _MOD3D_RIFERIMENTO_RE.search(soggetto):
        soggetto = ""

    idea_id = ""
    if soggetto:
        try:
            import idea_session as idea
            sessione = idea.create_idea_session(goal=soggetto, project_id="clio")
            idea_id = sessione.idea_id
            logger.info(f"[3D] idea {idea_id[:8]} creata per «{soggetto}»")
        except Exception as e:
            logger.warning(f"[3D] idea non creata ({type(e).__name__}: {e}): "
                           "uso l'ultima idea aperta")

    logger.info(f"[3D] modello richiesto: soggetto={soggetto!r} idea_id={idea_id or 'ultima'}")
    try:
        from blender_skills import genera_modello_3d_blender
        esito = policy.guarded_call(
            "genera_modello_3d_blender", genera_modello_3d_blender,
            {"oggetto": soggetto, "idea_id": idea_id}, origin=origin,
        )
    except Exception as e:
        logger.warning(f"[3D] generazione fallita: {type(e).__name__}: {e}")
        risposta = f"❌ Modello 3D non generato: {type(e).__name__}: {e}"
    else:
        risposta = str(esito)

    if on_text:
        try:
            on_text(risposta)
        except Exception as e:           # pragma: no cover - dipende dal TTS
            logger.warning(f"[3D] on_text non disponibile: {type(e).__name__}: {e}")
    return risposta


def _apri_app_pre_router(user_request, on_text=None, origin: str = "jarvis"):
    """Apre una app da sola o avvia un'IdeaSession esplicitamente richiesta.

    L'apertura passa dal Desktop Agent (che a sua volta usa
    `policy.guarded_call`): la conferma per le azioni sensibili resta quella
    del control-plane, non viene aggirata. Se dopo l'apertura c'è un'azione
    (es. "disegna un quadrato"), si lascia l'intera richiesta al planner così
    non si perde il secondo passo.
    """
    analisi = _parse_apri_app(user_request)
    if not analisi:
        return None
    app, obiettivo = analisi

    if obiettivo and not _IDEA_SUFFIX_RE.match(obiettivo):
        logger.info(
            "[PRE-ROUTER] richiesta composta per %s; passo l'intero comando al planner: %s",
            app, obiettivo,
        )
        return None

    _chiave = app.strip().lower()
    if _chiave.endswith(".exe"):
        _chiave = _chiave[:-4]
    # I servizi con app desktop devono passare dal routing desktop-first:
    # eseguibile locale -> protocollo Store/UWP -> sito solo come fallback.
    if _chiave in {"telegram", "discord", "whatsapp"}:  # spotify/netflix -> SITI_NOTI
        fn = AZIONI_MAP.get("apri_applicazione")
        if fn:
            risposta = policy.guarded_call(
                "apri_applicazione", fn, {"nome_app": _chiave}, origin=origin
            )
            if on_text:
                try:
                    on_text(str(risposta))
                except Exception as e:
                    logger.warning(
                        "[PRE-ROUTER] on_text non disponibile: %s: %s",
                        type(e).__name__, e,
                    )
            return risposta

    # Servizi senza un profilo desktop (YouTube, Google, Gmail) restano siti.
    _url_sito = SITI_NOTI.get(_chiave)
    if _url_sito:
        logger.info(f"[PRE-ROUTER] '{_chiave}' è un servizio web: apro {_url_sito}")
        risposta = apri_sito(_url_sito)
        if on_text:
            try:
                on_text(risposta)
            except Exception as e:       # pragma: no cover - dipende dal TTS
                logger.warning(f"[PRE-ROUTER] on_text non disponibile: {type(e).__name__}: {e}")
        return risposta

    try:
        import desktop_agent
    except Exception as e:                   # pragma: no cover - dipende dall'ambiente
        logger.warning(f"[PRE-ROUTER] desktop_agent non disponibile: {e}")
        return None

    if not desktop_agent.is_allowed_app(app):
        logger.info(f"[PRE-ROUTER] apertura app: {app.lower()} -> non in whitelist, "
                    "lascio la richiesta al LLM")
        return None
    try:
        exe = desktop_agent.normalize_app_name(app)
    except Exception as e:
        logger.warning(f"[PRE-ROUTER] nome app non valido ({app!r}): {e}")
        return None

    con_idea = bool(obiettivo)
    logger.info(f"[PRE-ROUTER] apertura app: {app.lower()}, idea: {con_idea}")

    try:
        if con_idea:
            risposta = desktop_agent.apri_app_e_inizia_idea(app_name=exe,
                                                            obiettivo=obiettivo,
                                                            origin=origin)
        else:
            aperta = desktop_agent.open_app(app_name=exe, origin=origin)
            # La whitelist è già stata verificata sopra: se l'apertura non riesce
            # il motivo è la conferma (scaduta/annullata) o l'avvio fallito, MAI
            # "app non consentita".
            risposta = (f"✅ {app} aperto, Capo." if aperta is True else
                        f"⌛ Conferma scaduta: azione NON eseguita ({app}).")
    except Exception as e:                   # pragma: no cover - difesa
        logger.warning(f"[PRE-ROUTER] apertura {app} fallita: {type(e).__name__}: {e}")
        return None

    if not isinstance(risposta, str) or not risposta.strip():
        risposta = (f"✅ {app} aperto con l'idea «{obiettivo}»." if con_idea
                    else f"✅ {app} aperto, Capo.")
    if on_text:
        try:
            on_text(risposta)
        except Exception as e:               # pragma: no cover - dipende dal TTS
            logger.warning(f"[PRE-ROUTER] on_text non disponibile: {type(e).__name__}: {e}")
    return risposta


# ── FASE 5: riconoscimento dei comandi vocali di coding ────────────────────
_CODING_CMD_PATTERNS = (
    r"(?:chiedi|di\'|fai\s+fare)\s+a\s+opencode\s+(?:di\s+)?(.+)",
    r"opencode\s*[:\-]?\s*(.+)",
    r"coding\s+task\s*[:\-]?\s*(.+)",
    r"crea\s+(?:del\s+)?codice\s+per\s+(.+)",
    r"crea\s+(?:del\s+)?codice\s*[:\-]?\s*(.+)",
    r"fixa\s+il\s+bug\s+in\s+(.+)",
    r"fixa\s+(?:il\s+)?bug\s*[:\-]?\s*(.+)",
    r"correggi\s+(?:il\s+)?(?:bug\s+nel\s+)?file\s+(.+)",
)

# Frasi naturali ("Clio, aggiungi una funzione moltiplicazione a main.py"):
# servono DUE segnali insieme — verbo di modifica E bersaglio di codice — per
# non dirottare richieste normali tipo "Clio, aggiungi il latte alla lista".
_CODING_INTENT_RE = re.compile(
    r"(?i)\b(?:aggiungi|aggiungimi|crea|creami|implementa|scrivi|modifica|"
    r"correggi|fixa|sistema|rifattorizza|migliora)\b")
_CODING_TARGET_RE = re.compile(
    r"(?i)(?:\.(?:py|js|mjs|ts|tsx|jsx|json|java|cs|go|rs|rb|php)\b|"
    r"\bsandbox\b|\bcodice\b|\bpytest\b|\bunit\s*test\b|"
    r"\btest\s+unitari?\b|\bscript\b)")

# Una funzione autonoma senza file/workspace richiesti è generazione di codice
# conversazionale: usa il profilo CODER, senza avviare il Coding Agent sandbox.
_SIMPLE_PY_FUNCTION_RE = re.compile(
    r"\b(?:scrivi|crea|dammi)\s+(?:una?\s+)?funzione\s+python\b",
    re.IGNORECASE,
)

def _extract_workspace_hint(text):
    """Estrae una workspace dal testo (cartella di un file citato, o un percorso)."""
    m = re.search(r"([A-Za-z]:\\[^\"'\n]+?\.(?:py|js|mjs|ts|tsx|jsx|json|md|txt|html|css|vue|java|c|cpp|cs|go|rs|rb|php))", text)
    if m:
        return os.path.dirname(m.group(1).strip()) or None
    m = re.search(r"([A-Za-z]:\\[^\"'\n]+)", text)
    if m:
        p = m.group(1).strip().rstrip(".")
        if os.path.isdir(p):
            return p
        base = os.path.basename(p)
        if "." in base:                 # sembra un file -> usa la cartella
            return os.path.dirname(p) or None
        return p                        # sembra una cartella (anche futura)
    return None

def detect_coding_command(user_request):
    """Riconosce i comandi vocali di coding (FASE 5).

    Ritorna (descrizione, workspace_hint) oppure None.
    Formati: 'coding task: ...', 'crea codice per ...', 'fixa il bug in <file>'.
    """
    t = (user_request or "").strip()
    for pat in _CODING_CMD_PATTERNS:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            desc = m.group(1).strip().strip('"').strip()
            # Per "fixa il bug in <file>" il gruppo cattura solo il file: usiamo
            # l'intera richiesta come descrizione così il modello conserva l'intento.
            if re.match(r"(?i)^fixa\s+(?:il\s+)?bug", t):
                desc = t
            if desc:
                return desc, _extract_workspace_hint(t)

    # R4 — voce → codice: frase naturale con doppio segnale (intento + bersaglio).
    if _CODING_INTENT_RE.search(t) and _CODING_TARGET_RE.search(t):
        desc = re.sub(r"(?i)^\s*(?:ehi\s+)?clio\s*[,:]?\s*", "", t).strip()
        return (desc or t), _extract_workspace_hint(t)
    return None


class _JSONFieldStream:
    """Estrae incrementalmente, da un flusso di testo JSON, il valore di stringa
    di una chiave (es. "pensiero").

    Serve per parlare la risposta mentre il modello la genera, senza mai
    esporre al TTS il wrapper JSON né i "pensieri" dei passaggi con tool.

    Uso:
        fs = _JSONFieldStream("pensiero")
        for chunk in stream:
            testo_nuovo = fs.feed(chunk)   # stringa già de-escapata (può essere "")
    Dopo il flusso: fs.text (tutto), fs.complete (se il valore è chiuso).
    """

    _ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\',
                '/': '/', 'b': '\b', 'f': '\f'}

    def __init__(self, key: str):
        self._pat = '"' + key + '"'
        self.buf = ""
        self.i = 0
        self.state = "seek"     # seek -> colon -> quote -> value -> done
        self._esc = False
        self._uni = None
        self.text = ""
        self.complete = False

    def feed(self, chunk: str) -> str:
        if self.complete or not chunk:
            return ""
        self.buf += chunk
        out = []

        if self.state == "seek":
            k = self.buf.find(self._pat, self.i)
            if k == -1:
                # Tieni solo la coda che potrebbe contenere una chiave a cavallo di due chunk
                self.i = max(0, len(self.buf) - (len(self._pat) - 1))
                return ""
            self.i = k + len(self._pat)
            self.state = "colon"

        if self.state == "colon":
            while self.i < len(self.buf) and self.buf[self.i] in ' \t\r\n':
                self.i += 1
            if self.i >= len(self.buf):
                return ""
            if self.buf[self.i] == ':':
                self.i += 1
                self.state = "quote"
            else:
                self.complete = True
                return ""

        if self.state == "quote":
            while self.i < len(self.buf) and self.buf[self.i] in ' \t\r\n':
                self.i += 1
            if self.i >= len(self.buf):
                return ""
            if self.buf[self.i] == '"':
                self.i += 1
                self.state = "value"
            else:
                self.complete = True
                return ""

        if self.state == "value":
            while self.i < len(self.buf):
                c = self.buf[self.i]
                if self._esc:
                    if self._uni is not None:
                        self._uni += c
                        if len(self._uni) == 4:
                            try:
                                out.append(chr(int(self._uni, 16)))
                            except ValueError:
                                pass
                            self._uni = None
                    elif c == 'u':
                        self._uni = ""
                    else:
                        out.append(self._ESCAPES.get(c, c))
                    self._esc = False
                    self.i += 1
                    continue
                if c == '\\':
                    self._esc = True
                    self.i += 1
                    continue
                if c == '"':
                    self.complete = True
                    self.state = "done"
                    self.i += 1
                    break
                out.append(c)
                self.i += 1

        testo = "".join(out)
        self.text += testo
        return testo


# ── Limite contesto Groq (errore 413: ITPM 7000 token) ──────────────────────
# Groq rifiuta le richieste grosse ("Request too large ... Limit 7000"). Il
# prompt è uno solo e contiene storia + contesto vault + strumenti: prima di
# inviarlo si tengono al massimo 10 turni e, se serve, si scartano i turni più
# vecchi (poi il vault). La richiesta attuale e gli strumenti restano sempre.
GROQ_MAX_HISTORY_TURNS = 10
GROQ_MAX_PROMPT_CHARS = 14000       # ~4000 token: margine sotto il limite ITPM
_GROQ_STORIA_INIZIO = "═══ STORIA CONVERSAZIONE"
_GROQ_STORIA_FINE = "═══ FINE STORIA ═══"
_GROQ_VAULT_INIZIO = "═══ CONTESTO DAL VAULT OBSIDIAN DEL CAPO ═══"
_GROQ_VAULT_FINE = "═══ FINE CONTESTO VAULT ═══"
_GROQ_TAGLIO = "\n[...contesto ridotto per il limite di Groq...]\n"


def _tronca_turni_groq(prompt: str, max_turns: int = GROQ_MAX_HISTORY_TURNS) -> str:
    """Lascia nella storia del prompt solo gli ultimi `max_turns` scambi."""
    if not isinstance(prompt, str) or max_turns <= 0:
        return prompt
    inizio = prompt.find(_GROQ_STORIA_INIZIO)
    if inizio == -1:
        return prompt
    fine = prompt.find(_GROQ_STORIA_FINE, inizio + 1)
    if fine == -1:
        return prompt

    righe = prompt[inizio:fine].splitlines()
    # righe[0] è l'intestazione; poi coppie "Capo: ..." / "Clio: ...".
    indici = [i for i, riga in enumerate(righe) if riga.startswith("Capo:")]
    da_togliere = set()
    for i in indici[:-max_turns]:
        da_togliere.update((i, i + 1))
    if not da_togliere:
        return prompt

    tenute = [riga for i, riga in enumerate(righe) if i not in da_togliere]
    if not any(riga.startswith("Capo:") for riga in tenute):
        return prompt[:inizio] + prompt[fine:]
    return prompt[:inizio] + "\n".join(tenute) + "\n" + prompt[fine:]


def _turni_nella_storia(testo: str) -> int:
    """Quanti scambi ("Capo: ...") ci sono nel blocco storia del prompt."""
    inizio = testo.find(_GROQ_STORIA_INIZIO)
    fine = testo.find(_GROQ_STORIA_FINE, inizio + 1) if inizio != -1 else -1
    if inizio == -1 or fine == -1:
        return 0
    return sum(1 for riga in testo[inizio:fine].splitlines()
               if riga.startswith("Capo:"))


def _togli_turno_piu_vecchio(testo: str):
    """Rimuove il primo scambio della storia. None se non c'è nulla da togliere."""
    inizio = testo.find(_GROQ_STORIA_INIZIO)
    if inizio == -1:
        return None
    fine = testo.find(_GROQ_STORIA_FINE, inizio + 1)
    if fine == -1:
        return None
    righe = testo[inizio:fine].splitlines()
    indice = next((i for i, riga in enumerate(righe) if riga.startswith("Capo:")), None)
    if indice is None:
        return None
    del righe[indice:indice + 2]
    if not any(riga.startswith("Capo:") for riga in righe):
        return testo[:inizio] + testo[fine:]
    return testo[:inizio] + "\n".join(righe) + "\n" + testo[fine:]


def _limita_prompt_groq(prompt: str, max_chars: int = GROQ_MAX_PROMPT_CHARS) -> str:
    """Prompt ridotto per Groq: max 10 turni, poi si scarta la storia più vecchia.

    Ordine dei tagli: (1) turni oltre il decimo, (2) turni più vecchi (tenendo
    almeno i 2 più recenti), (3) contesto del vault, (4) i turni rimasti,
    (5) taglio nel mezzo come ultima difesa. La coda (richiesta attuale +
    strumenti) resta intatta.
    """
    if not isinstance(prompt, str):
        return prompt
    testo = _tronca_turni_groq(prompt)

    def _togli_vecchi(minimo_turni: int) -> None:
        nonlocal testo
        while len(testo) > max_chars and _turni_nella_storia(testo) > minimo_turni:
            nuovo = _togli_turno_piu_vecchio(testo)
            if nuovo is None or nuovo == testo:
                return
            testo = nuovo

    _togli_vecchi(minimo_turni=2)

    if len(testo) > max_chars:
        inizio = testo.find(_GROQ_VAULT_INIZIO)
        fine = testo.find(_GROQ_VAULT_FINE, inizio + 1) if inizio != -1 else -1
        if inizio != -1 and fine != -1:
            resto = testo[fine + len(_GROQ_VAULT_FINE):]
            testo = (testo[:inizio]
                     + "[contesto vault omesso: limite di Groq]"
                     + resto)

    _togli_vecchi(minimo_turni=0)

    if len(testo) > max_chars:
        testa = int(max_chars * 0.55)
        coda = max(0, max_chars - testa - len(_GROQ_TAGLIO))
        testo = testo[:testa] + _GROQ_TAGLIO + (testo[-coda:] if coda else "")
    return testo


class JarvisBrain:
    def __init__(self):
        from config import USE_LOCAL_LLM, OLLAMA_URL, OLLAMA_MODEL
        self.use_local = USE_LOCAL_LLM
        self.ollama_url = OLLAMA_URL
        self.model = OLLAMA_MODEL
        # ── MEMORIA SESSIONE: Clio ricorda la conversazione ──
        self.session_history: List[Dict[str, str]] = []
        self.max_history_turns = 10  # Mantieni ultimi 10 scambi
        # ── Carica sessione precedente dal disco ──
        self._pending_safety_action = None
        self._last_vault_sync = 0.0
        self._load_session()

    def _session_file(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'jarvis_session.json')

    def _load_session(self):
        """Ripristina la memoria della sessione precedente dal disco."""
        try:
            path = self._session_file()
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    storia = data.get('history', [])
                    if isinstance(storia, list):
                        # La sessione su disco può essere vecchia: in memoria
                        # restano solo gli ultimi `max_history_turns` scambi.
                        self.session_history = storia[-self.max_history_turns:]
                    saved_at = data.get('saved_at', '?')
                    logger.info(f"📚 Sessione ripristinata: {len(self.session_history)} turni (salvata {saved_at})")
        except Exception as e:
            logger.warning(f"Impossibile caricare sessione: {e}")

    def _trim_history(self):
        """Mantiene in memoria solo gli ultimi `max_history_turns` scambi."""
        limite = max(1, int(getattr(self, "max_history_turns", GROQ_MAX_HISTORY_TURNS) or 1))
        if len(self.session_history) > limite:
            self.session_history = self.session_history[-limite:]

    def _save_session(self):
        """Persiste la memoria sul disco — sopravvive ai riavvii."""
        try:
            path = self._session_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({
                    'history': self.session_history,
                    'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Impossibile salvare sessione: {e}")

    def _history_to_str(self) -> str:
        """Converte la storia della sessione in formato leggibile dal prompt."""
        self._trim_history()
        if not self.session_history:
            return ""
        lines = ["\n═══ STORIA CONVERSAZIONE (ricorda questi scambi) ═══"]
        for turn in self.session_history[-6:]:  # Ultimi 6 turni nel prompt
            lines.append(f"Capo: {turn['user']}")
            lines.append(f"Clio: {turn['assistant']}")
        lines.append("═══ FINE STORIA ═══")
        return "\n".join(lines)

    def _call_ollama(self, prompt: str, profile: Dict[str, Any]) -> str:
        """Provider locale. Il modello viene dal profilo scelto dal router."""
        # Per i profili che usano un provider primario diverso (es. CODER -> llama.cpp)
        # il fallback Ollama può richiedere un modello diverso ("ollama_model").
        model = profile.get("ollama_model") or profile.get("model") or self.model
        import config as _cfg
        timeout = getattr(_cfg, "LLM_TIMEOUT", 30)
        response = requests.post(
            f"{self.ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "keep_alive": "10m"},
            timeout=timeout
        )
        response.raise_for_status()
        testo = response.json().get("response", "").strip()
        if testo:
            logger.info(f"✅ Ollama ({model}) risponde ({len(testo)} chars)")
            return testo
        logger.warning(f"Ollama ({model}) ha restituito una risposta vuota.")
        return None

    def _call_llamacpp(self, prompt: str, profile: Dict[str, Any]) -> str:
        """Provider locale llama.cpp (OpenAI-compatibile) — FASE 4.

        Usato dal profilo CODER (Qwen3-Coder-30B). Timeout contenuto: se il
        server è lento/irraggiungibile si prosegue con il fallback Ollama.
        """
        import config as _cfg
        base = (profile.get("base_url") or getattr(_cfg, "LLAMACPP_URL", "http://127.0.0.1:8080/v1")).rstrip("/")
        model = profile.get("model") or getattr(_cfg, "LLAMACPP_MODEL", "local-model")
        key = profile.get("api_key") or getattr(_cfg, "LLAMACPP_API_KEY", "sk-no-key-required")
        timeout = int(getattr(_cfg, "LLAMACPP_TIMEOUT", 90))
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": profile.get("temperature", 0.2),
                "max_tokens": profile.get("max_tokens", 2048),
                "stream": False,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        testo = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        if testo:
            logger.info(f"✅ llama.cpp ({model}) risponde ({len(testo)} chars)")
            return testo
        logger.warning(f"llama.cpp ({model}) ha restituito una risposta vuota.")
        return None

    def _call_gemini(self, prompt: str, profile: Dict[str, Any]) -> str:
        """Provider cloud Gemini (con cooldown automatico se quota 429)."""
        global _gemini_retry_after
        if time.time() < _gemini_retry_after:
            secs = int(_gemini_retry_after - time.time())
            logger.info(f"⏳ Gemini in cooldown, riprova tra {secs}s — proseguo la catena")
            return None
        try:
            from config import GEMINI_API_KEY, MODEL_PRIMARY
            from google import genai
            from google.genai import types
            # Se il profilo è nativo Gemini usa il suo modello, altrimenti il primario storico.
            model = profile.get("model") if profile.get("provider") == "gemini" else MODEL_PRIMARY
            client = genai.Client(api_key=GEMINI_API_KEY)
            import config as _cfg
            timeout_ms = getattr(_cfg, "LLM_TIMEOUT", 30) * 1000
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=profile.get("temperature", 0.7),
                    max_output_tokens=profile.get("max_tokens", 1024),
                    http_options={"timeout": timeout_ms},
                ),
            )
            testo = (resp.text or "").strip()
            if testo:
                logger.info(f"✅ Gemini ({model}) risponde ({len(testo)} chars)")
                return testo
            return None
        except Exception as e:
            err_str = str(e)
            # Estrai retry_delay dal messaggio 429 di Gemini
            if "429" in err_str or "quota" in err_str.lower():
                import re
                match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err_str)
                wait = int(match.group(1)) if match else 60
                _gemini_retry_after = time.time() + wait
                logger.warning(f"⚠️ Gemini quota 429 — cooldown {wait}s (fino alle {time.strftime('%H:%M:%S', time.localtime(_gemini_retry_after))})")
            elif "API key not valid" in err_str or "API_KEY_INVALID" in err_str:
                # Chiave non valida: riprovare ad ogni chiamata è inutile. Disabilita Gemini
                # per 1h e prosegui col fallback (Groq) senza crash.
                _gemini_retry_after = time.time() + 3600
                logger.warning("⚠️ Gemini: chiave API non valida — provider disabilitato per 1h, uso Groq.")
            else:
                logger.warning(f"⚠️ Gemini fallito: {type(e).__name__}: {e}")
            return None

    def _call_groq(self, prompt: str, profile: Dict[str, Any]) -> str:
        """Provider cloud Groq (ultimo fallback)."""
        try:
            from config import GROQ_API_KEY, MODEL_BACKUP
            from groq import Groq
            model = profile.get("model") if profile.get("provider") == "groq" else MODEL_BACKUP
            groq_client = Groq(api_key=GROQ_API_KEY)
            import config as _cfg
            timeout = getattr(_cfg, "LLM_TIMEOUT", 30)
            # Limite ITPM di Groq: max 10 turni e prompt entro il budget.
            prompt = _limita_prompt_groq(prompt)
            completion = groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "user", "content": "Sei Clio, l'assistente AI del Capo. Rispondi in italiano, diretto e naturale.\n\n" + prompt}
                ],
                max_tokens=profile.get("max_tokens", 1024),
                temperature=profile.get("temperature", 0.7),
                timeout=timeout,
            )
            testo = completion.choices[0].message.content.strip()
            logger.info(f"✅ Groq ({model}) risponde ({len(testo)} chars)")
            return testo
        except Exception as e:
            logger.error(f"⚠️ Groq fallito: {type(e).__name__}: {e}")
            return None

    def _call_llm(self, prompt: str, task_type: str = "general") -> str:
        """Sceglie il profilo col router e prova la catena provider del profilo.

        Il router decide provider/modello primario; se non risponde si prosegue
        col fallback storico (Ollama → Gemini → Groq, o quello del profilo).
        """
        profile_name = model_router.route_task(task_type)
        profile = model_router.get_profile(profile_name)
        chain = model_router.get_provider_chain(profile_name)
        logger.info(
            f"🧭 [ROUTER] task='{task_type}' → profilo {profile_name} "
            f"(primario {profile.get('provider')}:{profile.get('model')}); catena={chain}"
        )

        handlers = {
            "llamacpp": self._call_llamacpp,
            "ollama": self._call_ollama,
            "gemini": self._call_gemini,
            "groq": self._call_groq,
        }
        for provider in chain:
            handler = handlers.get(provider)
            if not handler:
                continue
            try:
                testo = handler(prompt, profile)
                if testo:
                    return testo
            except Exception as e:
                logger.warning(f"Provider {provider} non disponibile, proseguo: {type(e).__name__}: {e}")

        logger.error("⚠️ Nessun provider ha risposto entro il timeout di 30s per il profilo %s", profile_name)
        return '{"pensiero": "Capo, il modello non ha risposto entro il tempo limite (timeout di 30 secondi). Controlla la connessione o il servizio AI.", "azione": "rispondi", "parametri": {}, "finito": true}'

    # ─────────────────────────────────────────────────────────────────────
    #  STREAMING (Fase 6 — riduzione latenza voce)
    # ─────────────────────────────────────────────────────────────────────
    def _stream_groq(self, prompt: str, profile: Dict[str, Any]):
        """Genera token per token da Groq (OpenAI-compatibile)."""
        import config
        from config import GROQ_API_KEY, MODEL_BACKUP
        from groq import Groq
        model = profile.get("model") if profile.get("provider") == "groq" else MODEL_BACKUP
        client = Groq(api_key=GROQ_API_KEY)
        # Limite ITPM di Groq: max 10 turni e prompt entro il budget.
        prompt = _limita_prompt_groq(prompt)
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Sei Clio, l'assistente AI del Capo. Rispondi in italiano, diretto e naturale."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=profile.get("max_tokens", 1024),
            temperature=profile.get("temperature", 0.7),
            timeout=getattr(config, "LLM_TIMEOUT", 30),
            stream=True,
        )
        for ch in stream:
            delta = getattr(ch.choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                yield content

    def _stream_gemini(self, prompt: str, profile: Dict[str, Any]):
        """Genera token per token da Gemini (col cooldown 429 come _call_gemini)."""
        global _gemini_retry_after
        if time.time() < _gemini_retry_after:
            secs = int(_gemini_retry_after - time.time())
            logger.info(f"⏳ Gemini in cooldown, riprova tra {secs}s — proseguo la catena")
            return
        try:
            from config import GEMINI_API_KEY, MODEL_PRIMARY
            from google import genai
            from google.genai import types
            model = profile.get("model") if profile.get("provider") == "gemini" else MODEL_PRIMARY
            client = genai.Client(api_key=GEMINI_API_KEY)
            import config as _cfg
            timeout_ms = getattr(_cfg, "LLM_TIMEOUT", 30) * 1000
            for chunk in client.models.generate_content_stream(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=profile.get("temperature", 0.7),
                    max_output_tokens=profile.get("max_tokens", 1024),
                    http_options={"timeout": timeout_ms},
                ),
            ):
                testo = getattr(chunk, "text", None)
                if testo:
                    yield testo
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "quota" in err_str.lower():
                match = re.search(r'retry_delay\s*\{\s*seconds:\s*(\d+)', err_str)
                wait = int(match.group(1)) if match else 60
                _gemini_retry_after = time.time() + wait
                logger.warning(f"⚠️ Gemini quota 429 — cooldown {wait}s")
            else:
                logger.warning(f"⚠️ Gemini stream fallito: {type(e).__name__}: {e}")
            return

    def _stream_ollama(self, prompt: str, profile: Dict[str, Any]):
        """Genera token per token da Ollama (/api/generate, stream NDJSON)."""
        model = profile.get("ollama_model") or profile.get("model") or self.model
        import config as _cfg
        timeout = getattr(_cfg, "LLM_TIMEOUT", 30)
        with requests.post(
            f"{self.ollama_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": True, "keep_alive": "10m"},
            stream=True, timeout=timeout,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                testo = obj.get("response")
                if testo:
                    yield testo

    def _stream_llamacpp(self, prompt: str, profile: Dict[str, Any]):
        """Genera token per token da llama.cpp (OpenAI SSE)."""
        import config as _cfg
        base = (profile.get("base_url") or getattr(_cfg, "LLAMACPP_URL", "http://127.0.0.1:8080/v1")).rstrip("/")
        model = profile.get("model") or getattr(_cfg, "LLAMACPP_MODEL", "local-model")
        key = profile.get("api_key") or getattr(_cfg, "LLAMACPP_API_KEY", "sk-no-key-required")
        timeout = int(getattr(_cfg, "LLAMACPP_TIMEOUT", 60))
        with requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": profile.get("temperature", 0.2),
                  "max_tokens": profile.get("max_tokens", 2048), "stream": True},
            stream=True, timeout=timeout,
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content

    def _stream_llm(self, prompt: str, task_type: str = "general"):
        """Genera token per token col profilo scelto dal router, con fallback.

        Prova il primario del profilo; se non emette nulla prova i successivi.
        """
        profile_name = model_router.route_task(task_type)
        profile = model_router.get_profile(profile_name)
        chain = model_router.get_provider_chain(profile_name)
        logger.info(
            f"🧭 [ROUTER/stream] task='{task_type}' → profilo {profile_name} "
            f"(primario {profile.get('provider')}:{profile.get('model')}); catena={chain}"
        )
        streamers = {
            "llamacpp": self._stream_llamacpp,
            "ollama": self._stream_ollama,
            "gemini": self._stream_gemini,
            "groq": self._stream_groq,
        }
        for provider in chain:
            fn = streamers.get(provider)
            if not fn:
                continue
            yielded = False
            try:
                for chunk in fn(prompt, profile):
                    if chunk:
                        yielded = True
                        yield chunk
            except Exception as e:
                logger.warning(f"Provider {provider} (stream) non disponibile, proseguo: {type(e).__name__}: {e}")
            if yielded:
                return

    def _stream_answer(self, prompt: str, task_type: str, on_text, on_first=None):
        """Trasmette la risposta del modello come TESTO DELL'UTENTE.

        Streamma il JSON, estrae incrementalmente `azione` e `pensiero`; parla
        il `pensiero` SOLO se l'azione è "rispondi" (mai i pensieri dei tool).
        Ritorna il testo grezzo completo (per il parsing del loop chiamante).
        """
        pens = _JSONFieldStream("pensiero")
        thought = _JSONFieldStream("thought")
        azione = _JSONFieldStream("azione")
        action_field = _JSONFieldStream("action")
        raw = ""
        action = None
        pending = []            # testo pensiero in attesa che si sappia l'azione
        speaking = False        # azione == rispondi: parliamo in diretta
        silent = False          # è un tool: non parliamo in questo passaggio

        for chunk in self._stream_llm(prompt, task_type=task_type):
            if raw == "" and on_first:
                on_first()
            raw += chunk
            nuovo = pens.feed(chunk) or thought.feed(chunk)
            azione.feed(chunk)
            action_field.feed(chunk)

            if action is None and (azione.complete or action_field.complete):
                action = (azione.text or action_field.text or "").strip()
                if action in ("rispondi", "reply", "chat"):
                    speaking = True
                    for s in pending:
                        on_text(s)
                    pending = []
                else:
                    silent = True
                    pending = []

            # Se i primi caratteri non iniziano per '{', è testo libero: trasmetti subito
            if action is None and not silent and not speaking:
                s_strip = raw.lstrip()
                if s_strip and not s_strip.startswith("{"):
                    speaking = True
                    for s in pending:
                        on_text(s)
                    pending = []

            if nuovo:
                if speaking:
                    on_text(nuovo)
                elif not silent:
                    pending.append(nuovo)

        # Flusso finito: nessuna azione esplicita → è una risposta diretta.
        if action is None and not silent:
            if pens.text:
                for s in pending:
                    on_text(s)
            elif raw.strip():
                # Nessun JSON: il modello ha risposto in testo libero → leggilo.
                on_text(raw.strip())
        return raw

    def think_and_act(self, user_request: str, context: Dict[str, Any] = None, origin: str = "jarvis", task_type: str = None, on_text=None, latency=None) -> str:
        """Loop di pianificazione ed esecuzione: PENSA -> AGISCI -> RISPONDI.

        PC Agent: Visione schermo, Mouse/Tastiera, Esecuzione Codice Python,
        Chain of Thought multi-step e Safety Guardrail [S/N]."""
        req_norm = (user_request or "").strip().lower()

        # ── FIX 2: WAKE WORD PURA → nessuna chiamata LLM, nessun report di sistema ──
        # Se il Capo dice solo "Ciao" / "Ehi Clio" / "Sei sveglio?" il cervello non
        # deve generare un pippone su CPU/RAM: risponde con una sola parola.
        import re  # locale: nel resto della funzione `re` è già importato più avanti
        _req_wake = re.sub(r"[^a-zàèéìòù']+", " ", req_norm).strip()
        _WAKE_ONLY = {
            "ciao", "ehi", "hey", "ehi clio", "hey clio", "ok clio", "clio",
            "ciao clio", "sei sveglio", "sei sveglia", "ci sei", "ci sei clio",
            "dimmi", "buongiorno", "buonasera",
        }
        if _req_wake in _WAKE_ONLY:
            logger.info("👋 [WAKEWORD] Solo attivazione: rispondo 'Dimmi' senza chiamare il LLM.")
            if on_text:
                on_text("Dimmi")
            return "Dimmi"

        # Le richieste di disegno passano dal planner LLM, che sceglie tra
        # disegna_su_paint (tratti UI) e genera_immagine_pollinations.

        # ── "MODELLO 3D" — deterministico, prima del pre-router app ──
        esito_3d = _modello_3d_pre_router(self, user_request, on_text=on_text,
                                          origin=origin)
        if esito_3d:
            self.session_history.append({"user": user_request[:200],
                                         "assistant": str(esito_3d)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return esito_3d

        # ── "APRI [APP]" — deterministico, prima del LLM ──
        # Il LLM rispondeva "Apro Paint" senza eseguire il tool (function calling
        # mancato). Qui l'app viene aperta davvero; con un obiettivo ("e iniziamo
        # un'idea") si apre anche la IdeaSession.
        esito_app = _apri_app_pre_router(user_request, on_text=on_text, origin=origin)
        if esito_app:
            self.session_history.append({"user": user_request[:200],
                                         "assistant": str(esito_app)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return esito_app

        # C3: un'idea può partire senza prima aprire uno strumento desktop.
        esito_idea = _inizia_idea_pre_router(user_request, on_text=on_text, origin=origin)
        if esito_idea:
            self.session_history.append({"user": user_request[:200],
                                         "assistant": str(esito_idea)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return esito_idea

        # ── C3: "RIPRENDI DA QUI" (idea in corso) — deterministico, prima del LLM ──
        # Se c'è un'idea aperta, "riprendi da qui" / "dove eravamo rimasti" parla
        # dello stato dell'idea (obiettivo, prossimo passo, decisioni, materiali).
        # Senza idea aperta si lascia lavorare il router di The Scribe (R1), così
        # il comportamento precedente resta intatto.
        esito_resume = _riprendi_da_qui_pre_router(user_request, on_text=on_text)
        if esito_resume:
            self.session_history.append({"user": user_request[:200],
                                         "assistant": str(esito_resume)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return esito_resume

        # ── C4: "CERCA RIFERIMENTI" (ispirazione per l'idea) — prima del LLM ──
        # Determina l'azione (web + YouTube + riassunto + salvataggio nell'idea),
        # non il contenuto: nessuna invenzione da parte del modello.
        esito_ricerca = _cerca_riferimenti_pre_router(user_request, on_text=on_text,
                                                     origin=origin)
        if esito_ricerca:
            self.session_history.append({"user": user_request[:200],
                                         "assistant": str(esito_ricerca)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return esito_ricerca

        # ── R1: INTENTO DETERMINISTICO "Dove ero rimasto su X" → The Scribe ──
        # Non dipende dal LLM (che potrebbe allucinare). L'esecuzione passa
        # comunque da policy.guarded_call: lettura autorizzata, niente conferma.
        try:
            from scribe_control import detect_scribe_intent
            scribe_intent = detect_scribe_intent(user_request)
        except Exception as e_scribe:
            scribe_intent = None
            logger.warning(f"Router The Scribe non disponibile: {e_scribe}")
        if scribe_intent:
            azione = scribe_intent.get("action")
            parametri = scribe_intent.get("params") or {}
            logger.info(f"🗂️ [SCRIBE] Intento deterministico: {azione}({parametri})")
            import skills
            fn = skills.SKILLS_MAP.get(azione) or AZIONI_MAP.get(azione)
            if not fn:
                logger.error(f"[SCRIBE] Azione '{azione}' non registrata.")
                esito = "Capo, la skill di The Scribe non è disponibile in questo momento."
            else:
                esito = policy.guarded_call(azione, fn, parametri, origin=origin)
            log_clio_action(azione, parametri, success=True, outcome=str(esito)[:200])
            self.session_history.append({"user": user_request[:200], "assistant": str(esito)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            if on_text:
                on_text(str(esito))
            return esito

        # ── CONTROLLO WATCHDOG VAULT (Sync real-time Ossidiana -> Clio) ──
        ts_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vault_invalidated.timestamp")
        if os.path.exists(ts_file):
            try:
                mtime_ts = os.path.getmtime(ts_file)
                if not hasattr(self, "_last_vault_sync") or mtime_ts > self._last_vault_sync:
                    self._last_vault_sync = mtime_ts
                    invalidate_caches()
            except Exception as e_ts:
                logger.warning(f"Errore controllo timestamp vault: {e_ts}")

        # ── MODALITÀ DI SICUREZZA: Controllo risposta a conferma pendente [S/N] ──
        if hasattr(self, "_pending_safety_action") and self._pending_safety_action:
            pending_act, pending_par, pending_motivo = self._pending_safety_action
            import skills
            if req_norm in ("s", "si", "sì", "yes", "y", "confermo", "procedi", "ok", "conferma"):
                self._pending_safety_action = None
                logger.info(f"🛡️ [SICUREZZA] Conferma 'S' ricevuta. Eseguo azione: {pending_act}")
                try:
                    if pending_act in skills.SKILLS_MAP:
                        fn = skills.SKILLS_MAP[pending_act]
                        call_args = _filter_kwargs(
                            fn, _skill_call_args(pending_act, pending_par, user_request)
                        )
                        # L'utente ha già confermato con "S": la conferma è acquisita
                        # su questo canale, ma l'esecuzione passa comunque dal control-plane.
                        esito = policy.guarded_call(
                            pending_act, fn, call_args, origin=origin,
                            confirm=lambda *_a, **_k: "yes",
                        )
                    elif pending_act in AZIONI_MAP:
                        esito = policy.guarded_call(
                            pending_act, AZIONI_MAP[pending_act],
                            _sanitize_tool_params(pending_par), origin=origin,
                            confirm=lambda *_a, **_k: "yes",
                        )
                    else:
                        esito = f"Azione {pending_act} non disponibile nel registro."
                    log_clio_action(pending_act, pending_par, success=True, outcome=str(esito)[:200])
                    msg = f"Azione confermata ed eseguita, Capo: {esito}"
                except Exception as e_conf:
                    log_clio_action(pending_act, pending_par, success=False, outcome=str(e_conf))
                    msg = f"Capo, errore durante l'esecuzione dell'azione confermata: {e_conf}"
                self.session_history.append({"user": user_request[:200], "assistant": msg[:300]})
                self._save_session()
                return msg
            elif req_norm in ("n", "no", "annulla", "abort", "fermati", "lascia stare", "rifiuta"):
                self._pending_safety_action = None
                logger.info("🛡️ [SICUREZZA] Azione annullata dall'utente.")
                msg = "Operazione annullata per motivi di sicurezza, Capo."
                self.session_history.append({"user": user_request[:200], "assistant": msg[:300]})
                self._save_session()
                return msg
        # I comandi media base vengono riconosciuti localmente e inviati ai tasti
        # multimediali globali di Windows: non dipendono dal modello, da Vision o
        # dall'app in primo piano (Spotify, browser e player compatibili).
        media_command = _classify_media_request(user_request)
        if media_command:
            import skills
            media_fn = skills.SKILLS_MAP.get("control_media")
            if media_fn:
                try:
                    media_result = policy.guarded_call(
                        "control_media", media_fn, {"action": media_command}, origin=origin
                    )
                except Exception as media_err:
                    logger.exception("Invio del comando multimediale fallito")
                    media_result = f"Capo, non sono riuscita a controllare la riproduzione: {media_err}"
                log_clio_action(
                    "control_media", {"action": media_command},
                    success=not str(media_result).lower().startswith(
                        ("capo, non sono riuscita", "errore", "❌", "⚠️")
                    ),
                    outcome=str(media_result)[:200],
                )
                self.session_history.append({
                    "user": user_request[:200], "assistant": str(media_result)[:300]
                })
                if len(self.session_history) > self.max_history_turns:
                    self.session_history.pop(0)
                self._save_session()
                if on_text:
                    try:
                        on_text(str(media_result))
                    except Exception as text_err:
                        logger.warning("Risposta media non riprodotta: %s", text_err)
                return str(media_result)

        is_action_request = any(req_norm.startswith(cmd) or f" {cmd} " in f" {req_norm} " for cmd in (
            "apri", "chiudi", "spegni", "riavvia", "esegui", "crea", "elimina",
            "modifica", "aggiorna", "ricarica", "scrivi", "invia", "cancella", "task",
            "play", "pause", "pausa", "metti", "volume", "next", "salta", "avanti", "indietro",
            "spotify", "youtube", "cerca", "orario", "ora", "che ore", "internet", "web", "notizie", "email", "mail", "organizza", "ordina", "sposta", "trova"
        ))
        if not is_action_request and not (context and context.get("visione")):
            if req_norm in _RESPONSE_CACHE:
                cached_time, cached_res = _RESPONSE_CACHE[req_norm]
                if time.time() - cached_time < _RESPONSE_CACHE_TTL:
                    logger.info(f"⚡ [CACHE HIT] Risposta servita istantaneamente dalla cache per: '{req_norm[:40]}'")
                    if on_text:
                        on_text(cached_res)
                    self.session_history.append({"user": user_request[:200], "assistant": cached_res[:300]})
                    self._save_session()
                    return cached_res


        # ── Il router decide il profilo modello per questa richiesta ──
        # Le richieste di disegno vengono instradate localmente a Ollama/CODER:
        # Paint usa una skill deterministica e non richiede visione remota.
        paint_draw_request = (
            bool(re.search(r"\b(?:paint|mspaint)\b", req_norm))
            and bool(re.search(r"\bdisegn\w*\b", req_norm))
        )
        visual_task_request = bool(
            re.search(r"\b(?:paint|mspaint|disegn\w*)\b", req_norm)
        )
        if visual_task_request:
            # Paint/disegno usa solo il planner locale; CODER punta a Ollama
            # senza fallback cloud, così non vengono chiamati Gemini o Groq.
            task_type = "coder"
            logger.info("🧭 [ROUTER] richiesta Paint/disegno → profilo CODER locale (forzato)")
        else:
            task_type = task_type or model_router.classify_task(user_request)
            logger.info(f"🧭 [ROUTER] richiesta classificata come '{task_type}'")

        if (_SIMPLE_PY_FUNCTION_RE.search(user_request)
                and not re.search(r"\b\w+\.(?:py|js|ts|java)\b", user_request, re.IGNORECASE)):
            prompt_codice = (
                "Sei un generatore di codice Python. Restituisci solo la funzione richiesta, "
                "con una breve docstring se utile. Non chiamare strumenti e non restituire JSON.\n\n"
                f"Richiesta: {user_request}"
            )
            codice = self._call_llm(prompt_codice, task_type="code")
            if codice:
                self.session_history.append({"user": user_request[:200],
                                             "assistant": codice[:300]})
                if len(self.session_history) > self.max_history_turns:
                    self.session_history.pop(0)
                self._save_session()
                if on_text:
                    on_text(codice)
                return codice
            return "Capo, il profilo CODER locale non ha restituito codice."

        # ── FASE 5 / R3: comandi vocali di coding → Coding Agent in SANDBOX ──
        coding_cmd = detect_coding_command(user_request)
        if coding_cmd:
            desc, ws_hint = coding_cmd
            if ws_hint:
                logger.info(f"[CODING] workspace richiesta ignorata ({ws_hint}): si lavora solo nella sandbox.")
            logger.info(f"[CODING] Task: {desc} (sandbox)")
            try:
                from coding_agent import esegui_coding_task
                # Azione irreversibile: passa dal control-plane (conferma utente).
                # Nessun workspace reale: la sandbox e' l'unica destinazione ammessa.
                risultato = policy.guarded_call(
                    "esegui_coding_task", esegui_coding_task,
                    {"task_descrizione": desc}, origin=origin)
            except Exception as e:
                logger.error(f"Coding Agent non disponibile: {e}")
                risultato = f"⚠️ Coding Agent non disponibile: {e}"
            # R4: in modalità vocale parla l'ESITO (non il diff, illeggibile a voce).
            if on_text:
                try:
                    on_text("Fatto, Capo. Task di coding completato, i test passano."
                            if "✅" in str(risultato)
                            else "Capo, non sono riuscito a completare il task di coding.")
                except Exception as e_tts:
                    logger.warning(f"[CODING] risposta vocale non riprodotta: {e_tts}")
            self.session_history.append({"user": user_request[:200], "assistant": str(risultato)[:300]})
            if len(self.session_history) > self.max_history_turns:
                self.session_history.pop(0)
            self._save_session()
            return risultato

        # ── Costruisci lista tools ──
        tools_str = "Strumenti disponibili:\n"
        for name, info in TOOLS_METADATA.items():
            tools_str += f"- {name}: {info['desc']} (Parametri: {info['params']})\n"

        # ── Contesto visivo e di sistema ──
        context_str = ""
        if context:
            if context.get("visione") and context["visione"] and "Nessun" not in str(context["visione"]):
                context_str += f"\nSCHERMO ATTUALE: {str(context['visione'])[:350]}"
            if context.get("sistema"):
                context_str += f"\nSISTEMA: {context['sistema']}"

        # ── Storia della conversazione ──
        history_str = self._history_to_str()

        # ── CONTESTO VAULT OBSIDIAN (profilo + note rilevanti) ──
        # Lazy import per evitare cicli: clio_core importa jarvis_brain solo a runtime.
        vault_str = ""
        try:
            from clio_core import (
                get_vault_context_for_prompt,
                get_vault_index_summary,
                is_vault_reload_command,
                reload_vault_profile,
                parse_vault_update_command,
                update_vault_profile_field,
            )
            # Comando a caldo: "ricarica profilo" rilegge l'indice senza riavviare Clio
            if is_vault_reload_command(user_request):
                n = reload_vault_profile()
                msg = (f"Fatto Capo, ho ricaricato il profilo dal vault: {n} note indicizzate."
                       if n else "Vault non trovato o vuoto, uso contesto di default.")
                self.session_history.append({"user": user_request[:200], "assistant": msg[:300]})
                if len(self.session_history) > self.max_history_turns:
                    self.session_history.pop(0)
                self._save_session()
                return msg

            # Comando a caldo: "aggiorna profilo: campo=valore" (BUG 3)
            upd = parse_vault_update_command(user_request)
            if upd:
                campo, valore = upd
                logger.info(f"📝 [VAULT] Comando aggiorna profilo: {campo} = {valore}")
                ok, msg = update_vault_profile_field(campo, valore)
                self.session_history.append({"user": user_request[:200], "assistant": msg[:300]})
                if len(self.session_history) > self.max_history_turns:
                    self.session_history.pop(0)
                self._save_session()
                return msg

            contesto_vault = get_vault_context_for_prompt(user_request)
            if contesto_vault:
                riassunto = get_vault_index_summary(max_titles=15)
                vault_str = "\n═══ CONTESTO DAL VAULT OBSIDIAN DEL CAPO ═══\n"
                if riassunto:
                    vault_str += f"Indice note principali: {riassunto}\n\n"
                vault_str += contesto_vault
                vault_str += "\n═══ FINE CONTESTO VAULT ═══\n"
                import config as _cfg
                max_chars = getattr(_cfg, "MAX_CONTEXT_CHARS", 8000)
                if len(vault_str) > max_chars:
                    vault_str = vault_str[:max_chars] + "\n[...contesto limitato a 8000 caratteri...]\n═══ FINE CONTESTO VAULT ═══\n"
        except Exception as ve:
            logger.warning(f"⚠️ Contesto vault non disponibile: {ve}")

        prompt = f"""Sei Clio, l'assistente AI e PC Agent autonoma del Capo. Rispondi in italiano.

═══ REGOLA FERREA: WAKE WORD E STATO SISTEMA ═══
- Se la richiesta è SOLO un saluto o una wake word ("Ciao", "Ehi Clio", "Hey Clio",
  "Sei sveglio?", "Sei sveglia?", "Ci sei?"), rispondi con UNA sola parola: "Dimmi"
  (al massimo "Sono qui"). NON aggiungere nient'altro.
- NON riportare MAI spontaneamente stato del sistema, CPU, RAM, batteria, disco o
  elenchi di capacità. Se la sezione SISTEMA è presente, usala SOLO per rispondere
  a una domanda esplicita del Capo sullo stato del PC ("Come sta il PC?").
- Risposte corte: massimo 2 frasi se non viene chiesto un dettaglio. Niente pipponi.

{history_str}
{context_str}
{vault_str}
Richiesta attuale: {user_request}

═══ RUOLO PC AGENT & CHAIN OF THOUGHT ═══
Sei un agente autonomo capace di usare il PC come un umano: guardi lo schermo (Computer Vision), muovi il mouse, digiti sulla tastiera, apri programmi ed esegui codice.
Ragioni con logica "Chain of Thought" (passo dopo passo).

═══ REGOLE PER LE AZIONI JSON ═══
Rispondi SEMPRE E SOLO con un oggetto JSON valido.

═══ APERTURA APPLICAZIONI: REGOLA CATEGORICA ═══
Se il Capo chiede di aprire un'applicazione (Paint, Blocco Note/Notepad,
Chrome o altre), la PRIMA azione deve essere l'uso del tool
"apri_applicazione" oppure "apri_app_e_inizia_idea", scegliendo quello
più adatto alla richiesta.
Per una richiesta composta come "apri Paint e disegna ...", chiama direttamente
"disegna_su_paint": la skill apre Paint se manca. Non chiamare prima
"apri_app_e_inizia_idea" o "apri_applicazione" per questa richiesta.
Usa "apri_applicazione" per una richiesta che chiede soltanto di aprire Paint.
Distinzione app/sito: "apri il sito di X" richiede "apri_sito" con il relativo
URL; "apri X" richiede "apri_applicazione" e deve cercare prima l'app desktop.
Per servizi come Spotify, Netflix, Telegram, Discord e WhatsApp non aprire il
sito quando l'utente nomina solo l'app: il tool fa il fallback web soltanto se
non trova l'app locale o il relativo protocollo UWP.
Per le altre richieste di apertura, non avviare azioni successive prima del
tool di apertura. Eccezione: per "apri Paint e disegna ..." chiama direttamente
"disegna_su_paint"; la skill apre Paint localmente se necessario ed esegue il
disegno in un solo passaggio.
Se una richiesta composta richiede altri tool, continua il loop finché non è
completata; non fermarti alla sola apertura dell'app.

1. TASK COMPLESSI A PIÙ STEP (Chain of Thought):
Esegui un'azione alla volta impostando "finito": false finché il task non è completato. Quando finisci, imposta "finito": true o usa "action": "rispondi".
Esempio: Se il Capo dice "Apri Chrome e cerca su Google la ricetta della carbonara":
- Step 1: {{"action": "open_app", "params": {{"app_name": "chrome"}}, "finito": false, "pensiero": "Apro Google Chrome"}}
- Step 2: {{"action": "type_text", "params": {{"text": "ricetta carbonara"}}, "finito": false, "pensiero": "Digito la ricerca"}}
- Step 3: {{"action": "press_key", "params": {{"key": "enter"}}, "finito": false, "pensiero": "Premo invio per cercare"}}
- Step 4: {{"action": "rispondi", "pensiero": "Ho aperto Chrome e avviato la ricerca della ricetta della carbonara per te, Capo.", "params": {{}}, "finito": true}}

2. COMPUTER VISION & CONTROLLO MOUSE/TASTIERA:
- Per vedere lo schermo e individuare elementi/coordinate (x, y):
  {{"action": "see_screen", "params": {{"prompt": "descrivi lo schermo e dammi le coordinate dei bottoni"}}, "finito": false, "pensiero": "Analizzo cosa c'è a video"}}
- Per cliccare sulle coordinate individuate:
  {{"action": "click_coordinates", "params": {{"x": 500, "y": 300}}, "finito": false, "pensiero": "Clicco sul bottone"}}
- Per digitare testo:
  {{"action": "type_text", "params": {{"text": "testo da inserire"}}, "finito": false, "pensiero": "Digito"}}
- Per premere un tasto o combinazione:
  {{"action": "press_key", "params": {{"key": "enter"}}, "finito": false, "pensiero": "Premo il tasto"}} (anche "ctrl+c", "esc", "tab", "space")
- Per eseguire codice Python:
  {{"action": "run_python_code", "params": {{"code_string": "print(2+2)"}}, "finito": false, "pensiero": "Eseguo codice Python"}}

═══ DISEGNO E CREAZIONE VISIVA ═══
- Se il Capo chiede di disegnare o creare un'immagine, scegli il metodo adatto.
- REGOLA FERREA — DISEGNO SU PAINT: se il Capo chiede di disegnare su Paint,
  NON usare "fai_screenshot_pc", "take_screenshot", "see_screen",
  "analizza_schermo_pc" o altri tool di visione: costano denaro e non servono.
  Chiama DIRETTAMENTE la skill "disegna_su_paint" passando coordinate vuote:
  {{"action":"disegna_su_paint","params":{{"coordinate":{{}}}},"finito":true}}
  La skill trova Paint con pygetwindow e individua localmente il canvas tramite
  una cattura pixel temporanea, mai salvata né inviata in rete; disegna un
  quadrato 200x200 e verifica localmente che i pixel siano cambiati.
  Non scrivere testo di avanzamento al posto della chiamata tool e non usare
  coordinate inventate o fornite dal modello. Solo dopo l'esecuzione della skill
  puoi comunicare l'esito.
- Forme semplici o stilizzate (cubo, casa, icona, razzo geometrico): usa questa
  skill locale quando Paint è richiesto; non sostituirla con la sola
  generazione di immagini.
- Illustrazioni dettagliate, scene o immagini complesse: usa
  genera_immagine_pollinations con un prompt artistico preciso. Questo invia
  il prompt testuale a un servizio remoto: non includere screenshot, dati
  personali o informazioni non pertinenti all'immagine.
- Se una skill restituisce un errore, comunicalo chiaramente. Non dichiarare
  completato un disegno o un'immagine senza conferma della skill.

3. APP, MEDIA, DATA E ORA:
- Apri app: {{"action": "open_app", "params": {{"app_name": "Spotify"}}, "finito": true}}
- Controlli media base (play/pausa, canzone successiva/precedente): usa "media_control".
  Questi comandi usano i tasti multimediali globali di Windows e valgono per
  qualsiasi player compatibile; non usare screenshot, Vision o UI Automation.
  Esempi: {{"action": "media_control", "params": {{"command": "play_pause"}}, "finito": true}},
  {{"action": "media_control", "params": {{"command": "next"}}, "finito": true}},
  {{"action": "media_control", "params": {{"command": "previous"}}, "finito": true}}.
- Data e ora: {{"action": "get_system_time", "params": {{}}, "finito": true}}
- Ricerca YouTube: {{"action": "search_youtube", "params": {{"query": "termine"}}, "finito": true}}

4. RICERCA WEB & ULTIME NOTIZIE:
- Se l'utente dice "Cerca su internet", "Fai una ricerca", "Leggi le ultime notizie", "Chi è...", o chiede info recenti/web:
  {{"action": "deep_web_research", "params": {{"query": "<cosa cercare>"}}, "finito": true, "pensiero": "Eseguo ricerca web con browser"}}

5. FILE MANAGER AUTONOMO:
- Se l'utente chiede di organizzare o riordinare una cartella (es: "Organizza la cartella Desktop"):
  {{"action": "organize_folder", "params": {{"folder_path": "<cartella o Desktop>"}}, "finito": true, "pensiero": "Organizzo i file in sottocartelle"}}
- Per cercare file per estensione (.pdf, .png, ecc.):
  {{"action": "find_files", "params": {{"extension": "pdf", "folder_path": "<cartella o Desktop>"}}, "finito": true, "pensiero": "Cerco i file"}}
- Per spostare un file:
  {{"action": "move_file", "params": {{"source": "<file>", "destination": "<cartella_destinazione>"}}, "finito": true, "pensiero": "Sposto il file"}}

6. GESTIONE EMAIL GMAIL:
- Se l'utente dice "Controlla le email", "Ho nuove email?", "Leggi la posta":
  {{"action": "check_emails", "params": {{}}, "finito": true, "pensiero": "Controllo la casella Gmail"}}

7. CONVERSAZIONE PURA O RISPOSTE FINALI:
- {{"action": "rispondi", "pensiero": "la tua risposta in italiano", "params": {{}}, "finito": true}}

8. MEMORIA DELLE SESSIONI (THE SCRIBE):
- Se il Capo dice "Dove ero rimasto su <progetto>", "Cosa stavo facendo su <progetto>", "Cosa ho fatto su <progetto>":
  {{"action": "recupera_resume_progetto", "params": {{"project_id": "<progetto>"}}, "finito": true, "pensiero": "Leggo il Resume da The Scribe"}}
- Se chiede "Stato sessioni", "Quali sessioni sono attive", "Cosa sta girando":
  {{"action": "stato_sessioni_scribe", "params": {{}}, "finito": true, "pensiero": "Leggo lo stato delle sessioni da The Scribe"}}
- Progetti validi: cortex, roast-room, clio, tyrion-clips-studio, the_scribe. Non inventare progetti.

5. TONO & STILE:
- Rivolgiti all'utente come "Capo", tono diretto, ironico, complice. Conciso ed efficace.
- Rispondi SOLO con JSON valido. Nessun testo prima o dopo il JSON.

Strumenti disponibili:
{tools_str}
"""

        steps = 0
        max_steps = 6  # PC Agent: fino a 6 passaggi sequenziali per task complessi
        history = []
        risposta_finale = None
        paint_draw_completed = False
        paint_json_retry_used = False

        def _esegui_paint_locale():
            import skills
            fn_paint = skills.SKILLS_MAP.get("disegna_su_paint")
            if not fn_paint:
                return "Capo, la skill locale disegna_su_paint non è disponibile."
            return policy.guarded_call(
                "disegna_su_paint", fn_paint, {"coordinate": {}}, origin=origin
            )

        while steps < max_steps:
            steps += 1
            if history:
                # BUG 1 FIX: l'output REALE dei tool deve arrivare al modello nel
                # follow-up, con un'istruzione esplicita a integrarlo nella risposta.
                # (Prima veniva troncato a 400 char e senza istruzioni: il LLM
                # rispondeva solo con un'introduzione generica.)
                output_block = "\n\n".join(history)
                if paint_draw_request and not paint_draw_completed:
                    full_prompt = prompt + (
                        "\n\n═══ OUTPUT DEI TOOL APPENA ESEGUITI ═══\n"
                        f"{output_block}\n"
                        "═══ FINE OUTPUT TOOL ═══\n\n"
                        "DISEGNO PAINT LOCALE: chiama direttamente il tool "
                        "disegna_su_paint con coordinate={} . Non chiamare screenshot, "
                        "see_screen o analisi visive. Restituisci solo JSON valido e non "
                        "concludere prima dell'esecuzione della skill."
                    )
                else:
                    full_prompt = prompt + (
                        "\n\n═══ OUTPUT DEI TOOL APPENA ESEGUITI ═══\n"
                        f"{output_block}\n"
                        "═══ FINE OUTPUT TOOL ═══\n\n"
                        "ISTRUZIONE OBBLIGATORIA: i tool qui sopra sono già stati eseguiti e "
                        "hanno prodotto dei DATI REALI. Ora integrali nella tua risposta "
                        "all'utente in modo naturale, mostrando i dati concreti (nomi, valori, "
                        "percorsi, frequenze, stati, elenchi). NON limitarti a un'introduzione "
                        "generica tipo \"ecco il riassunto\": riporta il contenuto effettivo. "
                        "Se l'output è un elenco, riporta le voci. Se contiene un errore, dillo.\n"
                        "Rispondi SOLO con JSON valido: "
                        '{"azione": "rispondi", "pensiero": "<risposta completa con i dati reali>", '
                        '"parametri": {}, "finito": true}'
                    )
            else:
                full_prompt = prompt

            t_step = time.time()
            # ── STREAMING DI DEFAULT: la risposta arriva parola per parola in tempo reale ──
            def _on_first():
                if latency is not None and "llm_first_token" not in latency:
                    latency["llm_first_token"] = time.time() - t_step

            # Se non viene fornito un callback on_text personalizzato, usa stdout
            stream_cb = on_text if on_text is not None else (lambda chunk: (sys.stdout.write(chunk), sys.stdout.flush()))
            raw_ai = self._stream_answer(full_prompt, task_type, stream_cb, on_first=_on_first)
            if not raw_ai or not raw_ai.strip():
                # Fallback bloccante se lo streaming non ha prodotto output
                raw_ai = self._call_llm(full_prompt, task_type=task_type)
            if latency is not None:
                latency["llm_total"] = latency.get("llm_total", 0.0) + (time.time() - t_step)
                latency["llm_steps"] = latency.get("llm_steps", 0) + 1

            if not raw_ai or raw_ai.strip() == "":
                if paint_draw_request:
                    logger.warning("[PAINT] Ollama senza risposta: eseguo la skill locale senza altri provider.")
                    risposta_finale = _esegui_paint_locale()
                    paint_draw_completed = True
                    break
                risposta_finale = "Capo, nessuna risposta dal modello, riprova."
                break

            try:
                # Estrai JSON anche con testo aggiuntivo, backtick e graffe
                # annidate nel payload (es. codice Python dentro code_string).
                decision = _estrai_json(raw_ai)
                if decision is None:
                    # Paint: una risposta di avanzamento non esegue alcun tool.
                    # Riprova una sola volta con il vincolo operativo esplicito.
                    if paint_draw_request and not paint_json_retry_used:
                        paint_json_retry_used = True
                        logger.warning(
                            "[PAINT] Testo libero senza tool: riprovo imponendo la skill locale."
                        )
                        prompt += (
                            "\n\nRECUPERO PAINT OBBLIGATORIO: non scrivere testo. Chiama "
                            "direttamente disegna_su_paint con coordinate={} in un JSON valido. "
                            "È una skill locale; non chiamare screenshot o Vision."
                        )
                        continue
                    if paint_draw_request:
                        logger.warning("[PAINT] Fallback deterministico alla skill locale dopo il retry.")
                        risposta_finale = _esegui_paint_locale()
                        paint_draw_completed = True
                        break
                    logger.warning("Jarvis: nessun JSON trovato, uso testo diretto.")
                    risposta_finale = raw_ai.strip()
                    break

                pensiero = decision.get("pensiero") or decision.get("thought", "")
                azione = decision.get("action") or decision.get("azione", "rispondi")
                # P0: il LLM non può impostare 'confirmed' o altre chiavi di bypass.
                if paint_draw_request and not paint_draw_completed:
                    if azione != "disegna_su_paint":
                        logger.info(
                            "[PAINT] Sostituisco action=%s con la skill locale diretta.",
                            azione,
                        )
                    azione = "disegna_su_paint"
                    decision["finito"] = True
                    params = {"coordinate": {}}
                else:
                    params = _sanitize_tool_params(
                        decision.get("params") or decision.get("parametri", {})
                    )

                # ── MODALITÀ DI SICUREZZA: Controllo preventivo azioni distruttive ──
                import skills
                is_danger, motivo_danger = skills.check_safety_action(azione, params)
                user_pre_confirmed = any(w in req_norm for w in ("confermo", "forza", "procedi pure", "sono sicuro"))
                if is_danger and not user_pre_confirmed:
                    self._pending_safety_action = (azione, params, motivo_danger)
                    logger.warning(f"🛡️ [SICUREZZA] Azione critica '{azione}' intercettata. Chiedo conferma.")
                    risposta_finale = f"Sto per fare {motivo_danger}. Sei sicuro? [S/N]"
                    break

                # ── INTERCETTAZIONE SKILLS (skills.py) ──
                skill_fn = skills.SKILLS_MAP.get(azione)
                if skill_fn:
                    logger.info(f"⚡ [SKILLS] Esecuzione skill '{azione}' con parametri: {params}")
                    try:
                        # P0: UNICO punto di esecuzione. Anche le skill di skills.py
                        # passano da policy.guarded_call (classificazione + conferma + audit).
                        call_args = _filter_kwargs(
                            skill_fn, _skill_call_args(azione, params, user_request)
                        )
                        # Se l'utente ha già scritto "confermo/procedi" nella richiesta,
                        # la conferma è acquisita; altrimenti decide il control-plane.
                        confirm_fn = (lambda *_a, **_k: "yes") if user_pre_confirmed else None
                        conferma = policy.guarded_call(
                            azione, skill_fn, call_args, origin=origin, confirm=confirm_fn
                        )
                        if paint_draw_request and azione == "disegna_su_paint":
                            paint_draw_completed = True

                        finito_esplicito = decision.get("finito")
                        if finito_esplicito is True:
                            risposta_finale = conferma
                            break
                        elif finito_esplicito is False:
                            # Multi-step: salva in history e continua il loop per il prossimo step
                            history.append(f"Azione {azione} completata con successo: {conferma}")
                            continue
                        else:
                            # Se non specificato: per azioni singole termina con conferma
                            risposta_finale = conferma
                            break
                    except Exception as skill_err:
                        log_clio_action(azione, params, success=False, outcome=str(skill_err))
                        logger.error(f"Errore esecuzione skill {azione}: {skill_err}")
                        
                        # --- FIX IDEA SESSION ERRORI ---
                        import datetime
                        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        errore_msg = f"{type(skill_err).__name__}: {str(skill_err)}"
                        if hasattr(self, "session") and self.session:
                            if "errori_esecuzione" not in self.session:
                                self.session["errori_esecuzione"] = []
                            self.session["errori_esecuzione"].append({
                                "timestamp": timestamp,
                                "skill": azione,
                                "messaggio": errore_msg
                            })
                            if hasattr(self, "_save_session"):
                                self._save_session()
                        
                        risposta_finale = f"Capo, c'è stato un problema: {skill_err}"
                        break

                    log_clio_action(azione, params, success=True, outcome=str(conferma)[:200])

                if azione in ("rispondi", "reply", "chat") or decision.get("finito", False):
                    risposta_finale = pensiero if pensiero else raw_ai.strip()
                    # Cache risposte conversazionali prive di tool
                    if not is_action_request and steps == 1 and risposta_finale:
                        if len(_RESPONSE_CACHE) >= _RESPONSE_CACHE_MAX:
                            _RESPONSE_CACHE.pop(next(iter(_RESPONSE_CACHE)))
                        _RESPONSE_CACHE[req_norm] = (time.time(), risposta_finale)
                    break

                # ── Esecuzione strumento ──
                if azione in AZIONI_MAP:
                    func = AZIONI_MAP[azione]
                    logger.info(f"🛠️ Jarvis esegue: {azione}({params})")
                    try:
                        risultato = policy.guarded_call(azione, func, params, origin=origin)
                        log_clio_action(azione, params, success=True, outcome=str(risultato)[:200])
                        # BUG 1 FIX: mantieni l'output reale (non più tagliato a 400 char)
                        out_str = str(risultato)
                        if len(out_str) > 6000:
                            out_str = out_str[:6000] + "… [troncato]"
                        history.append(f"Ecco l'output del tool {azione}: {out_str}")
                    except Exception as tool_err:
                        log_clio_action(azione, params, success=False, outcome=str(tool_err))
                        history.append(f"Ecco l'output del tool {azione}: ERRORE: {tool_err}")
                else:
                    logger.warning(f"Strumento '{azione}' non nel registro AZIONI_MAP.")
                    history.append(f"Strumento {azione} non disponibile.")

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"JSON non valido da LLM, uso risposta grezza: {e}")
                clean = raw_ai.strip().strip('`').strip()
                risposta_finale = clean if clean else "Capo, ho avuto un problema nel risponderti."
                break
            except Exception as e:
                logger.error(f"Errore nel ragionamento di Jarvis: {e}")
                risposta_finale = f"Capo, qualcosa è andato storto: {e}"
                break

        if risposta_finale is None:
            risposta_finale = "Capo, il task era troppo complesso, ho raggiunto il limite di passaggi."

        # ── Salva in memoria sessione ──
        self.session_history.append({
            "user": user_request[:200],
            "assistant": risposta_finale[:300]
        })
        if len(self.session_history) > self.max_history_turns:
            self.session_history.pop(0)

        # ── Persisti sul disco (sopravvive ai riavvii) ──
        self._save_session()

        return risposta_finale


# Singleton
jarvis = JarvisBrain()
