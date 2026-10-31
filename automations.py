"""
automations.py — Ponte tra Clio e le automazioni di Francesco (Cortex).

Cosa fa:
1. Legge il registro `CLIO_AUTOMAZIONI.md` dal vault Obsidian.
2. Espone un tool per ogni automazione principale (es. leggi_dashboard_cortex,
   esegui_report_giornaliero, stato_pipeline_notturna).
3. Ogni tool lancia lo script corrispondente via subprocess e restituisce
   l'output (troncato se lunghissimo; timeout di default 60s, sovrascrivibile
   con la variabile d'ambiente CLIO_AUTOMATIONS_TIMEOUT).
4. Espone `riepilogo_automazioni()` che elenca tutte le automazioni trovate.

Principio: Clio è solo l'ESECUTORE. Gli script in AUTOMAZIONI non vengono
modificati. Il controllo resta in policy.py (classificazione + conferma + audit):
i nomi dei tool qui sotto vengono registrati nelle classi di policy.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("Automations")

# ── Percorsi ────────────────────────────────────────────────────────────────
VAULT = Path(r"C:\Users\User\OneDrive\OneSyncFiles")
DOC_PATH = VAULT / "CLIO_AUTOMAZIONI.md"
AUTOM = Path(r"C:\Users\User\Desktop\AUTOMAZIONI")
LOCAL = AUTOM / "local_automations"

DEFAULT_TIMEOUT = int(os.getenv("CLIO_AUTOMATIONS_TIMEOUT", "60"))


# ─────────────────────────────────────────────────────────────────────────
#  Engine di esecuzione (subprocess + timeout + kill dell'albero processi)
# ─────────────────────────────────────────────────────────────────────────
def _step(script, args=None, cwd=None, timeout=None):
    return {"script": Path(script), "args": list(args or []),
            "cwd": Path(cwd) if cwd else None, "timeout": timeout}


def _build_command(script: Path, args):
    if script.suffix.lower() in (".bat", ".cmd"):
        return ["cmd", "/c", str(script), *args]
    # -u: output non bufferizzato, così su timeout si cattura comunque il parziale
    return [sys.executable, "-u", str(script), *args]


def _kill_tree(pid):
    """Chiude il processo e tutti i suoi figli (evita .bat orfani su Windows)."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    except Exception:
        pass


def _run_step(step):
    script = Path(step["script"])
    timeout = step.get("timeout") or DEFAULT_TIMEOUT
    if not script.exists():
        logger.warning("Automazione: script non trovato %s", script)
        return {"ok": False, "timed_out": False, "exit": -1,
                "output": f"[Clio] Script non trovato: {script}", "script": str(script)}
    cwd = step.get("cwd") or script.parent
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("CORTEX_DASHBOARD_NO_OPEN", "1")  # non aprire finestre dall'esecuzione Clio
    cmd = _build_command(script, step.get("args", []))
    logger.info("🛠️ [AUTOMAZIONI] %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
    except Exception as e:
        logger.error("Automazione non avviabile (%s): %s", script.name, e)
        return {"ok": False, "timed_out": False, "exit": -1,
                "output": f"[Clio] Avvio fallito: {type(e).__name__}: {e}", "script": str(script)}
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            out = ""
    return {"ok": (not timed_out and proc.returncode == 0), "timed_out": timed_out,
            "exit": proc.returncode, "output": out or "", "script": str(script)}


def _tail(text, n=3000):
    text = text or ""
    text = text.strip()
    return text if len(text) <= n else "…(inizio troncato)…\n" + text[-n:]


def _format_step(res, step):
    name = Path(res["script"]).name
    if res["timed_out"]:
        note = f"⏱ timeout dopo {step.get('timeout') or DEFAULT_TIMEOUT}s — processo terminato"
    elif res["exit"] == 0:
        note = "✓ exit 0"
    else:
        note = f"✗ exit {res['exit']}"
    out = _tail(res["output"]) or "(nessun output)"
    return f"[{name}] {note}\n{out}"


# ─────────────────────────────────────────────────────────────────────────
#  Catalogo eseguibile (allineato a CLIO_AUTOMAZIONI.md)
# ─────────────────────────────────────────────────────────────────────────
CATALOG = {
    "esegui_pipeline_notturna": {
        "nome": "Pipeline notturna (RUN_GIORNALIERO)", "tipo": "irreversibile",
        "desc": "Esegue l'intera pipeline notturna: briefing, organizer, Stripe, stats social, report, dashboard, git sync.",
        "steps": [_step(LOCAL / "RUN_GIORNALIERO.bat", cwd=LOCAL)],
    },
    "leggi_dashboard_cortex": {
        "nome": "Dashboard Cortex (locale)", "tipo": "read-only",
        "desc": "Rigenera e legge la dashboard Cortex locale dai dati live/offline, senza aprire il browser.",
        "steps": [_step(AUTOM / "cortex_dashboard_local.py", ["--no-open", "--skip-social"], cwd=AUTOM)],
    },
    "esegui_dashboard_refresh": {
        "nome": "Dashboard Cortex refresh (Drive + Obsidian)", "tipo": "irreversibile",
        "desc": "Aggiorna cortex_dashboard_data.json su Drive e cortex_live.json nel vault (Stripe + Firestore + analytics + GA4).",
        "steps": [_step(LOCAL / "cortex_dashboard_refresh.py", cwd=LOCAL)],
    },
    "esegui_report_giornaliero": {
        "nome": "Report giornaliero Cortex (Bitly + Stripe)", "tipo": "reversibile",
        "desc": "Genera il report giornaliero Bitly + Stripe.",
        "steps": [_step(LOCAL / "cortex_stats_daily.py", cwd=LOCAL)],
    },
    "esegui_report_social": {
        "nome": "Report social unico", "tipo": "reversibile",
        "desc": "Unisce TikTok + YouTube + Instagram + funnel e genera le raccomandazioni contenuti.",
        "steps": [
            _step(AUTOM / "report_social_unico.py", cwd=AUTOM),
            _step(AUTOM / "analizza_performance_social.py", cwd=AUTOM),
        ],
    },
    "esegui_briefing_mattutino": {
        "nome": "Briefing email mattutino", "tipo": "reversibile",
        "desc": "Legge Gmail + Calendar (read-only) e genera la nota di briefing.",
        "steps": [_step(LOCAL / "briefing_email_locale.py", cwd=LOCAL)],
    },
    "esegui_organizer_vault": {
        "nome": "Obsidian Organizer (vault)", "tipo": "reversibile",
        "desc": "Archivia le note del vault, trascrive gli audio, aggiorna le stats e fa il backup git.",
        "steps": [_step(LOCAL / "vault_engine" / "obsidian_organizer.py", cwd=LOCAL / "vault_engine")],
    },
    "esegui_social_stats_sync": {
        "nome": "Social stats sync", "tipo": "reversibile",
        "desc": "Sincronizza le statistiche TikTok / Instagram / YouTube / Stripe.",
        "steps": [_step(LOCAL / "social_stats_sync.py", cwd=LOCAL)],
    },
    "esegui_stripe_check": {
        "nome": "Stripe payment check", "tipo": "reversibile",
        "desc": "Controlla nuovi pagamenti Stripe e scrive la notifica nel vault.",
        "steps": [_step(LOCAL / "stripe_payment_check.py", cwd=LOCAL)],
    },
    "esegui_analytics_scraper": {
        "nome": "Analytics scraper TikTok", "tipo": "reversibile",
        "desc": "Aggiorna le statistiche TikTok Studio → analytics_data.json.",
        "steps": [_step(AUTOM / "analytics_scraper.py", cwd=AUTOM)],
    },
    "esegui_archivia_social": {
        "nome": "Archivia social post (YouTube/Instagram)", "tipo": "reversibile",
        "desc": "Aggiorna l'archivio storico per-post di YouTube e Instagram.",
        "steps": [_step(AUTOM / "archivia_social_post.py", cwd=AUTOM)],
    },
    "stato_health_automazioni": {
        "nome": "Health check automazioni", "tipo": "read-only",
        "desc": "Controlli di salute read-only delle automazioni → .health_status.json.",
        "steps": [_step(LOCAL / "health_check.py", cwd=LOCAL)],
    },
    "esegui_smoke_test": {
        "nome": "Smoke test automazioni", "tipo": "reversibile",
        "desc": "Lancia ogni generatore chiave in modalità leggera e segnala i guasti.",
        "steps": [_step(LOCAL / "smoke_test.py", cwd=LOCAL)],
    },
    "esegui_weekly_review": {
        "nome": "Weekly review", "tipo": "reversibile",
        "desc": "Genera la review settimanale delle note del vault (Groq).",
        "steps": [_step(LOCAL / "weekly_review_locale.py", cwd=LOCAL)],
    },
    "invia_welcome_new_users": {
        "nome": "Welcome nuovi utenti", "tipo": "irreversibile",
        "desc": "Legge i nuovi iscritti da Firestore e invia la mail di benvenuto Cortex.",
        "steps": [_step(LOCAL / "welcome_new_users.py", cwd=LOCAL)],
    },
    "leggi_play_store_report": {
        "nome": "Play Store reports (read-only)", "tipo": "read-only",
        "desc": "Legge i report Google Play (installazioni, store performance) dal bucket.",
        "steps": [_step(LOCAL / "play_store_reports.py", cwd=LOCAL)],
    },
    "esegui_ig_token_refresh": {
        "nome": "IG token refresh", "tipo": "irreversibile",
        "desc": "Rinnova il token Instagram (Graph API) prima della scadenza.",
        "steps": [_step(LOCAL / "ig_token_refresh.py", cwd=LOCAL)],
    },
}

# Tool con logica dedicata (non semplice esecuzione di uno script)
SPECIAL_TOOLS = {"stato_pipeline_notturna", "riepilogo_automazioni",
                 "esegui_automazione", "leggi_automazione"}

READ_ONLY_TOOLS = {
    "riepilogo_automazioni", "stato_pipeline_notturna", "leggi_dashboard_cortex",
    "stato_health_automazioni", "leggi_play_store_report", "leggi_automazione",
}
REVERSIBLE_TOOLS = {
    "esegui_report_giornaliero", "esegui_report_social", "esegui_briefing_mattutino",
    "esegui_organizer_vault", "esegui_social_stats_sync", "esegui_stripe_check",
    "esegui_analytics_scraper", "esegui_archivia_social", "esegui_smoke_test",
    "esegui_weekly_review",
}
IRREVERSIBLE_TOOLS = {
    "esegui_pipeline_notturna", "esegui_dashboard_refresh", "invia_welcome_new_users",
    "esegui_ig_token_refresh", "esegui_automazione",  # runner generico: fail-safe
}


# ─────────────────────────────────────────────────────────────────────────
#  Lettura del registro dal vault
# ─────────────────────────────────────────────────────────────────────────
def _parse_documentazione():
    """Legge CLIO_AUTOMAZIONI.md e restituisce la lista delle automazioni."""
    if not DOC_PATH.exists():
        logger.warning("Registro automazioni non trovato: %s", DOC_PATH)
        return []
    entries, cur = [], None
    for raw in DOC_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if cur:
                entries.append(cur)
            cur = {"nome": line[3:].strip(), "percorso": "", "cosa_fa": "",
                   "come_si_lancia": "", "frequenza": "", "tipo": "", "tool": "", "timeout": ""}
        elif cur and line.strip().startswith("- "):
            key, _, val = line.strip()[2:].partition(":")
            key = key.strip().lower().replace(" ", "_")
            if key in cur:
                cur[key] = val.strip()
    if cur:
        entries.append(cur)
    for e in entries:
        if not e.get("percorso"):
            logger.warning("Automazione '%s' senza percorso: salto.", e.get("nome", "?"))
    return entries


# ─────────────────────────────────────────────────────────────────────────
#  Esecuzione
# ─────────────────────────────────────────────────────────────────────────
def _run_automation(tool):
    spec = CATALOG.get(tool)
    if not spec:
        return f"[Clio] Automazione '{tool}' non nel catalogo."
    parts = [f"▶ {spec['nome']}  [{spec['tipo']}]"]
    for step in spec["steps"]:
        res = _run_step(step)
        parts.append(_format_step(res, step))
    return "\n".join(parts)


def _dashboard_stato():
    files = [
        ("HTML dashboard", AUTOM / "cortex_dashboard_local.html"),
        ("dati notturni", VAULT / "ARCHIVIO" / "Vita" / "Business" / "cortex_live.json"),
        ("storico dashboard", AUTOM / "dashboard_storico.json"),
    ]
    righe = ["Stato file dashboard:"]
    for nome, p in files:
        if p.exists():
            ore = (time.time() - p.stat().st_mtime) / 3600
            righe.append(f"- {nome}: aggiornato {ore:.1f} ore fa")
        else:
            righe.append(f"- {nome}: assente")
    return "\n".join(righe)


def _dashboard_kpi():
    """Legge i numeri chiave della dashboard da cortex_live.json (read-only)."""
    live = VAULT / "ARCHIVIO" / "Vita" / "Business" / "cortex_live.json"
    if not live.exists():
        return ""
    try:
        d = json.loads(live.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return f"KPI dashboard non leggibili: {e}"
    fs = d.get("firestore", {}) or {}
    st = d.get("stripe", {}) or {}
    an = d.get("analytics", {}) or {}
    ga = (d.get("ga4", {}) or {}).get("last7d", {}) or {}
    g7 = ga.get("sessions", "?")
    righe = [
        f"KPI dashboard (aggiornato {d.get('aggiornato', '?')}):",
        f"- Utenti totali: {fs.get('utenti_totali', '?')} (free {fs.get('users_free', '?')}, attivati {fs.get('users_activated', '?')})",
        f"- Abbonamenti attivi: {st.get('abbonamenti_attivi', '?')} · MRR: {st.get('mrr', '?')}",
        f"- Visite oggi (landing/app): {an.get('visite_oggi_landing', '?')}/{an.get('visite_oggi_app', '?')} · verificate tot: {an.get('visite_subtotale_verificato', '?')}",
        f"- GA4 sessioni 7gg: {g7}",
    ]
    return "\n".join(righe)


def _tail_file(path, n=1200):
    try:
        p = Path(path)
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
        return text[-n:].strip()
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────
#  TOOL ESPOSTI A CLIO
# ─────────────────────────────────────────────────────────────────────────
def get_registered_automations():
    return [
        {"name": t["tool"], "label": t["label"], "class": t["class"]}
        for t in TOOLS
    ]

def riepilogo_automazioni() -> str:
    """Elenca tutte le automazioni documentate (nome, percorso, frequenza, stato) e lo stato della pipeline notturna."""
    docs = _parse_documentazione()
    righe = [f"Automazioni documentate: {len(docs)}"]
    for d in docs:
        p = Path(d["percorso"]) if d.get("percorso") else None
        stato = "ok" if (p and p.exists()) else "PERCORSO MANCANTE"
        righe.append(f"- {d['nome']} [{d.get('tipo') or '?'}] — {stato}")
        righe.append(f"    percorso: {d.get('percorso') or '?'}")
        righe.append(f"    frequenza: {d.get('frequenza') or '?'}")
        tool = (d.get("tool") or "").strip()
        if tool:
            righe.append(f"    tool Clio: {tool}")
    righe.append("")
    righe.append(stato_pipeline_notturna())
    return "\n".join(righe)


def stato_pipeline_notturna() -> str:
    """Stato dell'ultima pipeline notturna: battito, esito degli step e eventuali fallimenti (read-only)."""
    righe = []
    hb = VAULT / ".pipeline_heartbeat"
    if hb.exists():
        txt = hb.read_text(encoding="utf-8", errors="replace").strip()
        try:
            ts = datetime.strptime(txt, "%Y-%m-%d %H:%M:%S")
            ore = (datetime.now() - ts).total_seconds() / 3600
            righe.append(f"Ultimo battito pipeline: {txt} ({ore:.1f} ore fa)")
        except Exception:
            righe.append(f"Ultimo battito pipeline: {txt} (formato non riconosciuto)")
    else:
        righe.append("Nessun battito pipeline (.pipeline_heartbeat assente).")

    st = VAULT / ".pipeline_status.json"
    if st.exists():
        try:
            data = json.loads(st.read_text(encoding="utf-8-sig"))
            righe.append(f"Step: {data.get('totali', '?')} · falliti: {data.get('falliti', '?')} · aggiornato: {data.get('aggiornato', '?')}")
            bad = [s.get("step") for s in data.get("step", []) if s.get("stato") != "ok"]
            righe.append("Step NON ok: " + ", ".join(bad) if bad else "Tutti gli step risultano ok.")
        except Exception as e:
            righe.append(f"Status pipeline illeggibile: {e}")
    else:
        righe.append("Status pipeline assente (.pipeline_status.json).")
    return "\n".join(righe)


def leggi_dashboard_cortex() -> str:
    """Rigenera e legge la dashboard Cortex locale (dati live/offline), senza aprire il browser."""
    res = _run_automation("leggi_dashboard_cortex")
    extra = _dashboard_kpi()
    log = _tail_file(LOCAL / "logs" / "dashboard_source_refresh.log")
    blocchi = [res]
    if extra:
        blocchi.append(extra)
    blocchi.append(_dashboard_stato())
    if log:
        blocchi.append("Ultimo log refresh fonti:\n" + log)
    return "\n\n".join(blocchi)


def esegui_pipeline_notturna() -> str:
    """Esegue l'intera pipeline notturna di Cortex (azione pesante, irreversibile)."""
    return _run_automation("esegui_pipeline_notturna")


def esegui_dashboard_refresh() -> str:
    """Aggiorna la dashboard Cortex su Drive e nel vault (irreversibile: scrive su Drive)."""
    return _run_automation("esegui_dashboard_refresh")


def esegui_report_giornaliero() -> str:
    """Genera il report giornaliero Cortex (Bitly + Stripe)."""
    return _run_automation("esegui_report_giornaliero")


def esegui_report_social() -> str:
    """Genera il report social unico (TikTok+YouTube+Instagram) e le raccomandazioni contenuti."""
    return _run_automation("esegui_report_social")


def esegui_briefing_mattutino() -> str:
    """Genera il briefing email + calendario del mattino."""
    return _run_automation("esegui_briefing_mattutino")


def esegui_organizer_vault() -> str:
    """Archivia le note del vault e trascrive gli audio (Obsidian Organizer)."""
    return _run_automation("esegui_organizer_vault")


def esegui_social_stats_sync() -> str:
    """Sincronizza le statistiche social (TikTok/Instagram/YouTube/Stripe)."""
    return _run_automation("esegui_social_stats_sync")


def esegui_stripe_check() -> str:
    """Controlla nuovi pagamenti Stripe."""
    return _run_automation("esegui_stripe_check")


def esegui_analytics_scraper() -> str:
    """Aggiorna le statistiche TikTok (scraper TikTok Studio)."""
    return _run_automation("esegui_analytics_scraper")


def esegui_archivia_social() -> str:
    """Aggiorna l'archivio storico per-post di YouTube e Instagram."""
    return _run_automation("esegui_archivia_social")


def stato_health_automazioni() -> str:
    """Esegue i controlli di salute delle automazioni (read-only) e restituisce l'esito."""
    return _run_automation("stato_health_automazioni")


def esegui_smoke_test() -> str:
    """Esegue lo smoke test leggero delle automazioni."""
    return _run_automation("esegui_smoke_test")


def esegui_weekly_review() -> str:
    """Genera la review settimanale delle note del vault."""
    return _run_automation("esegui_weekly_review")


def invia_welcome_new_users() -> str:
    """Invia la mail di benvenuto ai nuovi iscritti Cortex (irreversibile: invia email)."""
    return _run_automation("invia_welcome_new_users")


def leggi_play_store_report() -> str:
    """Legge i report Google Play (read-only)."""
    return _run_automation("leggi_play_store_report")


def esegui_ig_token_refresh() -> str:
    """Rinnova il token Instagram (irreversibile: modifica credenziali)."""
    return _run_automation("esegui_ig_token_refresh")


# ── Runner generici (coprono le automazioni senza tool dedicato) ───────────
def _resolve_tool(nome):
    n = str(nome or "").strip().lower()
    if not n:
        return None
    for t in set(CATALOG) | SPECIAL_TOOLS:
        if t == n:
            return t
    for t, spec in CATALOG.items():
        if n in spec["nome"].lower() or spec["nome"].lower() in n:
            return t
    for d in _parse_documentazione():
        nm = d.get("nome", "").lower()
        if nm and (n in nm or nm in n):
            t = (d.get("tool") or "").strip()
            if t and t in (set(CATALOG) | SPECIAL_TOOLS):
                return t
    return None


def esegui_automazione(nome: str = "") -> str:
    """Esegue un'automazione scelta per nome (anche senza tool dedicato). Richiede conferma in policy."""
    tool = _resolve_tool(nome)
    if not tool:
        return f"Non trovo un'automazione corrispondente a '{nome}'. Usa riepilogo_automazioni() per l'elenco."
    if tool in CATALOG:
        return _run_automation(tool)
    if tool == "stato_pipeline_notturna":
        return stato_pipeline_notturna()
    return f"Tool '{tool}' non eseguibile genericamente."


def leggi_automazione(nome: str = "") -> str:
    """Legge l'output di un'automazione read-only scelta per nome (non esegue le azioni a rischio)."""
    tool = _resolve_tool(nome)
    if not tool:
        return f"Non trovo l'automazione '{nome}'. Usa riepilogo_automazioni() per l'elenco."
    if tool == "stato_pipeline_notturna":
        return stato_pipeline_notturna()
    if tool == "riepilogo_automazioni":
        return riepilogo_automazioni()
    spec = CATALOG.get(tool)
    if spec and spec["tipo"] == "read-only":
        return _run_automation(tool)
    return f"'{tool}' non è read-only: usa esegui_automazione() (richiede conferma)."


# ─────────────────────────────────────────────────────────────────────────
#  Registro per Clio (registry.AZIONI_MAP + jarvis_brain.TOOLS_METADATA)
# ─────────────────────────────────────────────────────────────────────────
AUTOMATION_TOOLS = {
    "riepilogo_automazioni": riepilogo_automazioni,
    "stato_pipeline_notturna": stato_pipeline_notturna,
    "leggi_dashboard_cortex": leggi_dashboard_cortex,
    "esegui_pipeline_notturna": esegui_pipeline_notturna,
    "esegui_dashboard_refresh": esegui_dashboard_refresh,
    "esegui_report_giornaliero": esegui_report_giornaliero,
    "esegui_report_social": esegui_report_social,
    "esegui_briefing_mattutino": esegui_briefing_mattutino,
    "esegui_organizer_vault": esegui_organizer_vault,
    "esegui_social_stats_sync": esegui_social_stats_sync,
    "esegui_stripe_check": esegui_stripe_check,
    "esegui_analytics_scraper": esegui_analytics_scraper,
    "esegui_archivia_social": esegui_archivia_social,
    "stato_health_automazioni": stato_health_automazioni,
    "esegui_smoke_test": esegui_smoke_test,
    "esegui_weekly_review": esegui_weekly_review,
    "invia_welcome_new_users": invia_welcome_new_users,
    "leggi_play_store_report": leggi_play_store_report,
    "esegui_ig_token_refresh": esegui_ig_token_refresh,
    "esegui_automazione": esegui_automazione,
    "leggi_automazione": leggi_automazione,
}

TOOLS_METADATA = {
    "riepilogo_automazioni": {"desc": "Elenca tutte le automazioni di Cortex: cosa fanno, percorso, frequenza e stato.", "params": []},
    "stato_pipeline_notturna": {"desc": "Stato dell'ultima pipeline notturna: battito, esito degli step, eventuali fallimenti.", "params": []},
    "leggi_dashboard_cortex": {"desc": "Rigenera e legge la dashboard Cortex locale (dati live/offline), senza aprire il browser.", "params": []},
    "esegui_pipeline_notturna": {"desc": "Esegue l'intera pipeline notturna di Cortex (azione pesante).", "params": []},
    "esegui_dashboard_refresh": {"desc": "Aggiorna la dashboard Cortex su Drive e nel vault.", "params": []},
    "esegui_report_giornaliero": {"desc": "Genera il report giornaliero Cortex (Bitly + Stripe).", "params": []},
    "esegui_report_social": {"desc": "Genera il report social unico (TikTok+YouTube+Instagram) e le raccomandazioni contenuti.", "params": []},
    "esegui_briefing_mattutino": {"desc": "Genera il briefing email + calendario del mattino.", "params": []},
    "esegui_organizer_vault": {"desc": "Archivia le note del vault e trascrive gli audio (Obsidian Organizer).", "params": []},
    "esegui_social_stats_sync": {"desc": "Sincronizza le statistiche social (TikTok/Instagram/YouTube/Stripe).", "params": []},
    "esegui_stripe_check": {"desc": "Controlla nuovi pagamenti Stripe.", "params": []},
    "esegui_analytics_scraper": {"desc": "Aggiorna le statistiche TikTok (scraper TikTok Studio).", "params": []},
    "esegui_archivia_social": {"desc": "Aggiorna l'archivio storico per-post di YouTube e Instagram.", "params": []},
    "stato_health_automazioni": {"desc": "Esegue i controlli di salute delle automazioni (read-only).", "params": []},
    "esegui_smoke_test": {"desc": "Esegue lo smoke test leggero delle automazioni.", "params": []},
    "esegui_weekly_review": {"desc": "Genera la review settimanale delle note del vault.", "params": []},
    "invia_welcome_new_users": {"desc": "Invia la mail di benvenuto ai nuovi iscritti Cortex.", "params": []},
    "leggi_play_store_report": {"desc": "Legge i report Google Play (read-only).", "params": []},
    "esegui_ig_token_refresh": {"desc": "Rinnova il token Instagram.", "params": []},
    "esegui_automazione": {"desc": "Esegue un'automazione di Cortex scelta per nome.", "params": ["nome"]},
    "leggi_automazione": {"desc": "Legge l'output di un'automazione read-only scelta per nome.", "params": ["nome"]},
}


# ─────────────────────────────────────────────────────────────────────────
#  Classificazione in policy (read-only / reversibile / irreversibile)
# ─────────────────────────────────────────────────────────────────────────
try:
    import policy as _policy
    _policy.READ_ONLY.update(READ_ONLY_TOOLS)
    _policy.REVERSIBLE.update(REVERSIBLE_TOOLS)
    _policy.IRREVERSIBLE.update(IRREVERSIBLE_TOOLS)
    logger.info("🧩 [AUTOMAZIONI] %d tool registrati in policy.", len(AUTOMATION_TOOLS))
except Exception as _e:  # pragma: no cover
    logger.warning("policy non aggiornata con i tool automazioni: %s", _e)


__all__ = [
    "AUTOMATION_TOOLS", "TOOLS_METADATA", "CATALOG",
    "riepilogo_automazioni", "stato_pipeline_notturna", "leggi_dashboard_cortex",
    "esegui_pipeline_notturna", "esegui_dashboard_refresh", "esegui_report_giornaliero",
    "esegui_report_social", "esegui_briefing_mattutino", "esegui_organizer_vault",
    "esegui_social_stats_sync", "esegui_stripe_check", "esegui_analytics_scraper",
    "esegui_archivia_social", "stato_health_automazioni", "esegui_smoke_test",
    "esegui_weekly_review", "invia_welcome_new_users", "leggi_play_store_report",
    "esegui_ig_token_refresh", "esegui_automazione", "leggi_automazione",
]
