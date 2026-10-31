"""
registry.py — REGISTRO UNICO dei tool eseguibili di Clio (AZIONI_MAP condiviso).
Fonte unica per bot.py, clio_core.py e jarvis_brain.py.
Nessun tool viene eseguito fuori da questo registro tramite policy.guarded_call().
Contenuto = superset storico di clio_core (comprende gia' tutti i tool di bot e Jarvis).
"""
from actions import *

AZIONI_MAP = {
    "apri_applicazione": apri_applicazione,
    "apri_sito": apri_sito,
    "cerca_su_google": cerca_su_google,
    "cerca_su_youtube": cerca_su_youtube,
    "spegni_pc": spegni_pc,
    "annulla_spegnimento": annulla_spegnimento,
    "riavvia_pc": riavvia_pc,
    "ottieni_info_sistema": ottieni_info_sistema,
    "muta_volume": muta_volume,
    "esegui_comando_shell": esegui_comando_shell,
    "crea_cartella": crea_cartella,
    "apri_cartella": apri_cartella,
    "digita_su_tastiera_fisica": digita_su_tastiera_fisica,
    "leggi_appunti": leggi_appunti,
    "scrivi_appunti": scrivi_appunti,
    "fai_screenshot_pc": fai_screenshot_pc,
    "telefono_clicca": telefono_clicca,
    "telefono_digita": telefono_digita,
    "telefono_apri_app": telefono_apri_app,
    "telefono_screenshot": telefono_screenshot,
    "telefono_home": telefono_home,
    "telefono_indietro": telefono_indietro,
    "telefono_swipe": telefono_swipe,
    "media_play_pausa": media_play_pausa,
    "media_prossimo": media_prossimo,
    "media_precedente": media_precedente,
    "chiudi_finestra_attiva": chiudi_finestra_attiva,
    "minimizza_tutto": minimizza_tutto,
    "blocca_pc": blocca_pc,
    "svuota_cestino": svuota_cestino,
    "rinomina_file": rinomina_file,
    "elimina_file": elimina_file,
    "lista_file_cartella": lista_file_cartella,
    "stato_batteria": stato_batteria,
    "uccidi_processo": uccidi_processo,
    "imposta_volume": imposta_volume,
    "chi_sei": chi_sei,
    "analizza_schermo_pc": analizza_schermo_pc,
    "monitora_connessioni_sospette": monitora_connessioni_sospette,
    "monitora_processi_critici": monitora_processi_critici,
    "ottieni_telemetria_hardware": ottieni_telemetria_hardware,
    "cerca_file": cerca_file,
    "leggi_file_testo": leggi_file_testo,
    "scrivi_file_testo": scrivi_file_testo,
    "esegui_script_python": esegui_script_python,
    "finestre_gestione": finestre_gestione,
    "ricerca_web_profonda": ricerca_web_profonda,
    "esegui_powershell_avanzato": esegui_powershell_avanzato,
    "ottieni_meteo": ottieni_meteo,
    "ottieni_notizie": ottieni_notizie,
    "leggi_gmail": leggi_gmail,
    "leggi_calendario": leggi_calendario,
    "registra_episodio": registra_episodio,
    "leggi_motore_ricerca_web": leggi_motore_ricerca_web,
    "leggi_sito_web": leggi_sito_web,
    "leggi_youtube": leggi_youtube,
    "spegni_luce": spegni_luce,
    "accendi_luce": accendi_luce
}

# ── Automazioni di Francesco (integrazione Cortex) ─────────────────────────
# Merge dei tool di automations.py nel registro unico, così passano da
# policy.guarded_call() come tutti gli altri.
try:
    import automations as _automations
    AZIONI_MAP.update(_automations.AUTOMATION_TOOLS)
except Exception as _e:  # non bloccare l'avvio di Clio se le automazioni non caricano
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Tool automazioni non registrati: {_e}")

# ── Coding Agent in sandbox (R3) ───────────────────────────────────────────
# esegui_coding_task è classificato IRREVERSIBILE in policy.py: richiede SEMPRE
# conferma (nessuna auto-approvazione) e lavora solo dentro _clio_sandbox.
try:
    from coding_agent import esegui_coding_task
    AZIONI_MAP["esegui_coding_task"] = esegui_coding_task
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Tool coding agent non registrato: {_e}")

try:
    from skills import run_opencode_task
    AZIONI_MAP["run_opencode_task"] = run_opencode_task
except Exception as _e:
    pass
# ── R1: Memoria come servizio (The Scribe, sola lettura) ────────────────────
# Il modulo gestisce da solo l'assenza di The Scribe: qui non deve mai bloccare.
try:
    from scribe_control import (
        get_scribe_resume,
        get_scribe_status,
        recupera_resume_progetto,
        stato_sessioni_scribe,
    )
    AZIONI_MAP["recupera_resume_progetto"] = recupera_resume_progetto
    AZIONI_MAP["get_scribe_resume"] = get_scribe_resume
    AZIONI_MAP["stato_sessioni_scribe"] = stato_sessioni_scribe
    AZIONI_MAP["get_scribe_status"] = get_scribe_status
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Tool The Scribe non registrati: {_e}")


# ── R5: Agent Registry + Workflow Engine (orchestrazione multi-agente) ───────
# esegui_workflow è IRREVERSIBILE in policy.py: il piano passa dal control-plane
# e l'utente conferma prima che il primo agente parta.
try:
    from agent_registry import describe_registry as _describe_registry

    AZIONI_MAP["ispeziona_agenti"] = _describe_registry
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Agent Registry non registrato: {_e}")

try:
    from workflow_engine import esegui_workflow, stato_workflow

    AZIONI_MAP["esegui_workflow"] = esegui_workflow
    AZIONI_MAP["stato_workflow"] = stato_workflow
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Workflow Engine non registrato: {_e}")


# ── R6: Computer Use cross-app (UI Automation, whitelist, conferma) ──────────
# ispeziona_finestra è SENSITIVE, clicca_elemento/scrivi_testo_app sono
# IRREVERSIBILI in policy.py: ogni azione passa da policy.guarded_call.
try:
    from computer_agent import click_element, inspect_window, type_text

    AZIONI_MAP["ispeziona_finestra"] = inspect_window
    AZIONI_MAP["clicca_elemento"] = click_element
    AZIONI_MAP["scrivi_testo_app"] = type_text
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Computer Agent non registrato: {_e}")


# ── R7: Vault Intelligence (Knowledge Curator, sola lettura) ─────────────────
# Entrambe le azioni sono READ_ONLY in policy.py: leggono il vault e (solo la
# seconda) scrivono un report in clio/data, mai dentro il vault.
try:
    from knowledge_curator import proponi_pulizia_vault, scansiona_vault

    AZIONI_MAP["scansiona_vault"] = scansiona_vault
    AZIONI_MAP["proponi_pulizia_vault"] = proponi_pulizia_vault
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Knowledge Curator non registrato: {_e}")


# ── R8: Proattività Quotidiana (sola osservazione, READ_ONLY) ────────────────
# Osserva la finestra in primo piano e propone note pertinenti con una notifica
# discreta: nessun log di app/titoli, stato solo in memoria, max 1 notifica/30m.
try:
    from proactive_assistant import attiva_proattivita

    AZIONI_MAP["attiva_proattivita"] = attiva_proattivita
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Proactive Assistant non registrato: {_e}")


# ── FASE 2 · C1: Desktop Agent (apre lo strumento e apre l'idea) ─────────────
# apri_app_e_inizia_idea è SENSITIVE in policy.py (avvia un processo + scrive
# stato): la conferma la chiede il control-plane. porta_finestra_in_primo_piano
# è REVERSIBLE (nessun dato cambia).
try:
    from desktop_agent import apri_app_e_inizia_idea, focus_window

    AZIONI_MAP["apri_app_e_inizia_idea"] = apri_app_e_inizia_idea
    AZIONI_MAP["porta_finestra_in_primo_piano"] = focus_window
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Desktop Agent non registrato: {_e}")


# ── FASE 2 · C2: Vision Agent (guarda la finestra e aggiorna l'idea) ────────
# analyze_sketch_and_update_idea è READ_ONLY in policy.py: cattura solo il
# rettangolo della finestra indicata (nessuna app aperta) e aggiorna lo stato
# dell'IdeaSession. Senza idea_id usa l'ultima idea ancora aperta.
try:
    from vision_agent import analyze_sketch_and_update_idea

    AZIONI_MAP["analyze_sketch_and_update_idea"] = analyze_sketch_and_update_idea
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Vision Agent non registrato: {_e}")


# ── FASE 2 · C5: Blender skill (modello OBJ reale con bpy) ────────────────────
# Il nuovo tool è il percorso primario dei comandi 3D. Il vecchio nome resta
# registrato per compatibilità con chiamanti esterni già esistenti.
try:
    from blender_agent import generate_3d_from_idea
    from blender_skills import genera_modello_3d_blender

    AZIONI_MAP["genera_modello_3d_blender"] = genera_modello_3d_blender
    AZIONI_MAP["genera_modello_3d"] = generate_3d_from_idea
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Blender skill non registrata: {_e}")


# ── C3: Sessione persistente ("Riprendi da qui") ─────────────────────────────
# Queste azioni aggiornano SOLO lo stato dell'IdeaSession di CLIO (come C2) e,
# per decisione/prossimo passo, mandano un'osservazione a The Scribe via
# scribe_control. Nessun side effect su app/file dell'utente: READ_ONLY in policy.
try:
    from idea_session import (
        aggiungi_decisione_idea,
        aggiungi_materiale_idea,
        imposta_blocco_idea,
        imposta_prossimo_passo,
        riprendi_da_qui,
    )

    AZIONI_MAP["riprendi_da_qui"] = riprendi_da_qui
    AZIONI_MAP["aggiungi_decisione_idea"] = aggiungi_decisione_idea
    AZIONI_MAP["imposta_prossimo_passo"] = imposta_prossimo_passo
    AZIONI_MAP["aggiungi_materiale_idea"] = aggiungi_materiale_idea
    AZIONI_MAP["imposta_blocco_idea"] = imposta_blocco_idea
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Azioni C3 non registrate: {_e}")


# ── C4: Research Agent (riferimenti web + YouTube per l'idea) ────────────────
# Apre un browser headless, cerca e salva i link nell'IdeaSession con
# fonte="research". Nessun file dell'utente viene toccato: READ_ONLY in policy.
try:
    from research_agent import cerca_riferimenti

    AZIONI_MAP["cerca_riferimenti"] = cerca_riferimenti
except Exception as _e:
    import logging as _logging
    _logging.getLogger("Registry").warning(f"Research Agent non registrato: {_e}")
