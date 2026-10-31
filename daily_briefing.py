"""
CLIO - daily_briefing.py
Generatore del Diario e Briefing Giornaliero per Obsidian.

Raccoglie in modo autonomo:
1. Log delle azioni di Clio nelle ultime 24 ore (Sistema/Clio_Log.md).
2. Note modificate di recente in Inbox e Progetti.
3. Stato social e clip (social_memory.py --status).
4. Sintesi intelligente con Gemini 3.8 Flash (assistente personale).
5. Archiviazione automatica della nota in Inbox/Briefing_YYYY-MM-DD.md.
"""

import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import time
import datetime
import subprocess
import logging
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DailyBriefing] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("DailyBriefing")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import config
    VAULT_DIR = getattr(config, "VAULT_DIR", r"C:\Users\User\OneDrive\OneSyncFiles")
    GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", "")
except Exception:
    VAULT_DIR = r"C:\Users\User\OneDrive\OneSyncFiles"
    GEMINI_API_KEY = ""


def get_clio_logs(hours: int = 24) -> str:
    """Legge i log di Clio_Log.md delle ultime N ore."""
    log_file = os.path.join(VAULT_DIR, "Sistema", "Clio_Log.md")
    if not os.path.exists(log_file):
        return "Nessun registro Clio_Log.md trovato nel Vault."

    cutoff_ts = time.time() - (hours * 3600)
    recent_lines = []

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line_str = line.strip()
                if not line_str.startswith("["):
                    continue
                try:
                    ts_part = line_str.split("]")[0].strip("[")
                    entry_time = time.mktime(time.strptime(ts_part, "%Y-%m-%d %H:%M:%S"))
                    if entry_time >= cutoff_ts:
                        recent_lines.append(line_str)
                except Exception:
                    # Se non riesce a parsare la data, include la riga per prudenza
                    recent_lines.append(line_str)
    except Exception as e:
        return f"Errore lettura log: {e}"

    if not recent_lines:
        return "Nessuna azione eseguita da Clio nelle ultime 24 ore."

    return "\n".join(recent_lines[-40:])  # Ultimi 40 log


def get_vault_recent_files(folders=("Inbox", "Progetti"), hours: int = 24, max_files: int = 8) -> str:
    """Trova le note create o modificate di recente nelle cartelle indicate."""
    if not os.path.exists(VAULT_DIR):
        return "Vault Obsidian non accessibile."

    cutoff_ts = time.time() - (hours * 3600)
    found_files = []

    for folder_name in folders:
        folder_path = os.path.join(VAULT_DIR, folder_name)
        if not os.path.exists(folder_path):
            continue

        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("_trash", ".trash")]
            for f in files:
                if f.lower().endswith(".md"):
                    full_p = os.path.join(root, f)
                    try:
                        st = os.stat(full_p)
                        # Ignora i briefing già creati in passato per evitare autoreferenze
                        if f.startswith("Briefing_"):
                            continue
                        rel_p = os.path.relpath(full_p, VAULT_DIR).replace("\\", "/")
                        found_files.append((st.st_mtime, rel_p, full_p))
                    except Exception:
                        pass

    if not found_files:
        return "Nessuna nota trovata in Inbox o Progetti."

    # Ordina dal più recente
    found_files.sort(key=lambda x: x[0], reverse=True)

    # Prendi file nelle ultime 24 ore (o i primi 5 più recenti in assoluto)
    recent = [item for item in found_files if item[0] >= cutoff_ts]
    if not recent:
        recent = found_files[:max_files]
    else:
        recent = recent[:max_files]

    output_lines = []
    for mtime, rel_p, full_p in recent:
        mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        # Leggi estratto iniziale
        estratto = ""
        try:
            with open(full_p, "r", encoding="utf-8", errors="ignore") as fp:
                righe = [r.strip() for r in fp.readlines() if r.strip() and not r.startswith("---")][:3]
                estratto = " ".join(righe)[:120]
        except Exception:
            pass

        output_lines.append(f"- **{rel_p}** (modificato: {mtime_str})\n  _{estratto}_")

    return "\n".join(output_lines)


def get_social_status() -> str:
    """Esegue social_memory.py --status per leggere lo stato social."""
    social_script = r"C:\Users\User\Desktop\AUTOMAZIONI\social_memory.py"
    if not os.path.exists(social_script):
        return "Script social_memory.py non trovato."

    try:
        res = subprocess.run(
            [sys.executable, social_script, "--status"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="ignore"
        )
        out = (res.stdout or "").strip()
        if out:
            return out
        return res.stderr or "Nessun dato social disponibile."
    except Exception as e:
        return f"Errore recupero stato social: {e}"


def call_llm_for_briefing(context_clio: str, context_vault: str, context_social: str) -> str:
    """Invia il contesto a Gemini 3.8 Flash per generare il Briefing in stile Clio."""
    today_str = datetime.date.today().strftime("%d/%m/%Y")
    
    prompt = f"""Sei Clio, l'assistente AI personale e PC Agent del Capo.
Oggi è il {today_str}. Il tuo compito è scrivere il "Briefing Giornaliero" per il Capo.

Tono:
- Italiano naturale, professionale ma diretto e complice (chiamalo 'Capo').
- Sii sintetica, brillante, ben organizzata con titoli chiari ed emoji.
- Evita convenevoli prolissi, vai dritto ai fatti e alle priorità operative.

Ecco i dati reali raccolti dal sistema nelle ultime 24 ore:

═══ 1. AZIONI RECENTI DI CLIO (Clio_Log) ═══
{context_clio}

═══ 2. MODIFICHE NEL VAULT OBSIDIAN (Inbox & Progetti) ═══
{context_vault}

═══ 3. STATO REGISTRO SOCIAL (Tyrion Clips / TikTok / YouTube) ═══
{context_social}

═══ ISTRUZIONI DI FORMATTAZIONE ═══
Struttura il Briefing nelle seguenti sezioni Markdown:
# ☀️ Daily Briefing - {today_str}
1. **🚀 Panoramica & Attività Clio**: sintesi veloce di cosa ha fatto Clio e del sistema.
2. **📱 Aggiornamento Social (Tyrion Clips)**: quante clip sono pronte, cosa c'è in bozza o da pubblicare.
3. **📂 Secondo Cervello (Obsidian)**: note recenti su cui si è lavorato tra Inbox e Progetti.
4. **🎯 Focus & Priorità del Giorno**: 2 o 3 obiettivi consigliati per massimizzare la produttività oggi.

Concludi con una frase motivante o operativa in stile Clio.
"""

    # 1. Prova con Gemini 3.8 Flash
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt
        )
        if response and response.text:
            return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini API non disponibile per il briefing ({e}), tento con Groq...")

    # 2. Fallback Groq (Qwen / Llama)
    try:
        from groq import Groq
        groq_key = getattr(config, "GROQ_API_KEY", "")
        if groq_key:
            gclient = Groq(api_key=groq_key)
            completion = gclient.chat.completions.create(
                model=getattr(config, "GROQ_FAST_MODEL", "qwen/qwen3.8-27b"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=1500
            )
            return completion.choices[0].message.content.strip()
    except Exception as eg:
        logger.warning(f"Groq fallback non riuscito: {eg}")

    # 3. Fallback locale deterministico
    return f"""# ☀️ Daily Briefing - {today_str}

Buongiorno Capo! Ecco il riepilogo generato in modalità di sicurezza locale:

### 🚀 Azioni Recenti di Clio
{context_clio}

### 📱 Stato Social
{context_social}

### 📂 File Recenti nel Vault
{context_vault}

### 🎯 Priorità
Controllare le bozze social e revisionare le note recenti in Inbox.
"""


def generate_daily_briefing() -> str:
    """Flusso completo di creazione e salvataggio del Daily Briefing."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    now_time = datetime.datetime.now().strftime("%H:%M:%S")
    logger.info(f"Avvio creazione Daily Briefing per la data {today}...")

    # 1. Raccogli dati
    logger.info("Lettura log di Clio...")
    clio_logs = get_clio_logs(hours=24)

    logger.info("Lettura note recenti in Obsidian...")
    vault_files = get_vault_recent_files(hours=24)

    logger.info("Lettura registro social...")
    social_status = get_social_status()

    # 2. Genera con LLM
    logger.info("Generazione briefing intelligente con LLM...")
    briefing_body = call_llm_for_briefing(clio_logs, vault_files, social_status)

    # 3. Formatta nota con frontmatter
    note_content = f"""---
data: {today}
orario: {now_time}
tipo: briefing_giornaliero
autore: Clio AI
sistema: Jarvis Brain PC Agent
---

{briefing_body}
"""

    # 4. Salva in Inbox\Briefing_YYYY-MM-DD.md
    target_dir = os.path.join(VAULT_DIR, "Inbox")
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, f"Briefing_{today}.md")

    with open(target_path, "w", encoding="utf-8") as f:
        f.write(note_content)

    logger.info(f"✅ Daily Briefing salvato con successo in: {target_path}")

    # Registra l'azione in Clio_Log.md
    try:
        import jarvis_brain
        jarvis_brain.log_clio_action("generate_daily_briefing", {"data": today, "file": target_path}, True)
    except Exception:
        pass

    return target_path


def main():
    print("=" * 65)
    print("☀️ CLIO - GENERAZIONE DAILY BRIEFING (Obsidian Vault)")
    print("=" * 65)
    path = generate_daily_briefing()
    print()
    print(f"✅ Briefing pronto nel Vault:")
    print(f"👉 {path}")
    print("=" * 65)


if __name__ == "__main__":
    main()
