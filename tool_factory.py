"""
C.L.I.O. - tool_factory.py
Sistema di auto-generazione comandi.
Quando Clio incontra una richiesta che non sa gestire,
può creare automaticamente un nuovo tool e aggiungerlo ad actions.py
"""

import os
import re
import json
import logging
import importlib

import policy  # control-plane: la creazione di un tool passa da policy + audit

logger = logging.getLogger("ClioToolFactory")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ACTIONS_FILE = os.path.join(BASE_DIR, "actions.py")
CUSTOM_TOOLS_FILE = os.path.join(BASE_DIR, "data", "custom_tools.json")


def parse_new_tool_request(risposta_clio: str) -> dict | None:
    pattern = r'\[NUOVO_TOOL_RICHIESTO\](.*?)\[/NUOVO_TOOL_RICHIESTO\]'
    match = re.search(pattern, risposta_clio, re.DOTALL)
    if not match:
        return None
    contenuto = match.group(1).strip()
    tool = {}
    nome_match = re.search(r'nome:\s*(\w+)', contenuto)
    if nome_match:
        tool['nome'] = nome_match.group(1).strip()
    desc_match = re.search(r'descrizione:\s*(.+?)(?:\n|parametri:)', contenuto, re.DOTALL)
    if desc_match:
        tool['descrizione'] = desc_match.group(1).strip()
    codice_match = re.search(r'codice_python:\s*\|\n(.*?)$', contenuto, re.DOTALL)
    if codice_match:
        lines = codice_match.group(1).split('\n')
        codice = '\n'.join(line[4:] if line.startswith('    ') else line for line in lines)
        tool['codice'] = codice.strip()
    return tool if 'nome' in tool and 'codice' in tool else None


def aggiungi_tool_ad_actions(tool: dict) -> bool:
    try:
        with open(ACTIONS_FILE, 'r', encoding='utf-8') as f:
            contenuto_attuale = f.read()
        if f"def {tool['nome']}(" in contenuto_attuale:
            logger.warning(f"Tool {tool['nome']} esiste già")
            return False
        try:
            compile(tool['codice'], '<string>', 'exec')
        except SyntaxError as e:
            logger.error(f"Codice non valido: {e}")
            return False
        BLACKLIST = ['os.remove', 'shutil.rmtree', '__import__', 'eval(', 'exec(']
        for cmd in BLACKLIST:
            if cmd in tool['codice']:
                logger.warning(f"Tool bloccato: contiene '{cmd}'")
                return False
        nuovo_codice = f"\n\n# ===== AUTO-GENERATO DA CLIO =====\n{tool['codice']}\n# ===== FINE =====\n"
        with open(ACTIONS_FILE, 'a', encoding='utf-8') as f:
            f.write(nuovo_codice)
        salva_tool_registro(tool)
        logger.info(f"Nuovo tool aggiunto: {tool['nome']}")
        return True
    except Exception as e:
        logger.error(f"Errore: {e}")
        return False


def salva_tool_registro(tool: dict):
    try:
        os.makedirs(os.path.dirname(CUSTOM_TOOLS_FILE), exist_ok=True)
        registro = []
        if os.path.exists(CUSTOM_TOOLS_FILE):
            with open(CUSTOM_TOOLS_FILE, 'r', encoding='utf-8') as f:
                registro = json.load(f)
        from datetime import datetime
        tool['creato_il'] = datetime.now().isoformat()
        registro.append(tool)
        with open(CUSTOM_TOOLS_FILE, 'w', encoding='utf-8') as f:
            json.dump(registro, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Errore registro: {e}")


def carica_tool_in_runtime(nome_tool: str, azioni_map: dict) -> bool:
    try:
        import actions
        importlib.reload(actions)
        if hasattr(actions, nome_tool):
            azioni_map[nome_tool] = getattr(actions, nome_tool)
            logger.info(f"Tool {nome_tool} caricato in runtime!")
            return True
        return False
    except Exception as e:
        logger.error(f"Errore caricamento runtime: {e}")
        return False


def processa_risposta_clio(risposta: str, azioni_map: dict, origin: str = "telegram") -> tuple:
    """Elabora la risposta dell'AI. Se contiene una richiesta di nuovo tool, la
    creazione (scrittura in actions.py + registrazione runtime) passa dal
    control-plane via policy.guarded_call: "crea_tool" e' un'azione
    IRREVERSIBILE e richiede autorizzazione esplicita (conferma Telegram).
    Senza conferma il codice NON viene mai scritto in actions.py."""
    tool = parse_new_tool_request(risposta)
    if not tool:
        return risposta, False

    pattern = r'\[NUOVO_TOOL_RICHIESTO\].*?\[/NUOVO_TOOL_RICHIESTO\]'
    marker_ok = f"[Ho creato un nuovo comando: {tool['nome']}. Ora posso usarlo!]"

    def _crea_tool(**kwargs):
        """Scrive il nuovo tool in actions.py e lo registra. Solleva se fallisce."""
        dati = {
            "nome": kwargs.get("nome"),
            "descrizione": kwargs.get("descrizione", ""),
            "codice": kwargs.get("codice"),
        }
        if not aggiungi_tool_ad_actions(dati):
            raise RuntimeError("creazione tool rifiutata (blacklist, duplicato o sintassi non valida)")
        if not carica_tool_in_runtime(dati["nome"], azioni_map):
            raise RuntimeError("registrazione runtime del tool fallita")
        return marker_ok

    # UNICO punto di esecuzione: policy + conferma + audit (origin tracciato).
    risultato = policy.guarded_call(
        "crea_tool",
        _crea_tool,
        {"nome": tool.get("nome"), "descrizione": tool.get("descrizione", ""), "codice": tool.get("codice")},
        origin=origin,
    )

    if risultato == marker_ok:
        risposta_pulita = re.sub(pattern, marker_ok, risposta, flags=re.DOTALL)
        return risposta_pulita, True

    # Rifiutata / scaduta / errore: l'utente vede il motivo del control-plane.
    motivo = risultato if isinstance(risultato, str) and risultato[:1] in ("❌", "⌛") else None
    messaggio = motivo or f"[Ho provato a creare il comando {tool.get('nome')} ma c'è stato un problema.]"
    risposta_pulita = re.sub(pattern, messaggio, risposta, flags=re.DOTALL)
    return risposta_pulita, False
