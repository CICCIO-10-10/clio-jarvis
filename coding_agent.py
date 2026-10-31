"""
coding_agent.py — Coding Agent locale di Clio (FASE 4).

Esegue un task di coding in una workspace usando il profilo CODER del router
(Qwen3-Coder-30B servito da llama.cpp, fallback Ollama qwen2.5-coder-64k).

Garanzie di sicurezza:
- PATH CONTAINMENT: si scrive SOLO dentro la workspace (mai fuori).
- Tutte le scritture passano da policy.guarded_call(origin="coding_agent").
- Isolamento: branch git temporaneo (clio-coding-<ts>) se la workspace è un
  repo git pulito; altrimenti copia isolata in una cartella temporanea.
- Test automatici (pytest / npm test) e ROLLBACK se falliscono.
- Timeout complessivo 5 minuti, massimo 20 iterazioni, massimo 5 file per task.

La funzione pubblica `esegui_coding_task(task_descrizione, workspace=None)` è
registrata in registry.AZIONI_MAP ed è classificata IRREVERSIBILE in policy.py.

R3 — CODING ASSISTITO IN SANDBOX (vedi sezione in fondo al file):
`run_coding_task(task_description, workspace=None, agent=None, timeout=300)` è
l'API nuova: lavora SOLO dentro `_clio_sandbox` (env `CLIO_SANDBOX`), delega a
CODER locale (OpenCode solo su opt-in con modello locale), esegue pytest e ritorna
`{"success": bool, "diff": str, "test_output": str}`. Nessun percorso fuori
dalla sandbox è accettato.
"""
import os
import re
import sys
import json
import time
import shutil
import inspect
import logging
import subprocess
import tempfile
import threading
import importlib.util

import requests

import policy
import model_router

logger = logging.getLogger("CodingAgent")

# ── Limiti (dalla specifica FASE 4) ────────────────────────────────────────
TASK_TIMEOUT      = 300     # 5 minuti complessivi
MAX_ITERATIONS    = 20      # iterazioni massime LLM<->test
MAX_FILES         = 5       # file modificabili per task
MAX_CONTEXT_FILES = 5       # file di contesto inviati al modello
MAX_FILE_CHARS    = 4000    # troncamento per file di contesto
LLAMACPP_TIMEOUT  = 60      # tentativo rapido su llama.cpp prima del fallback
OLLAMA_TIMEOUT    = 180     # fallback Ollama


def _has_pytest():
    try:
        return importlib.util.find_spec("pytest") is not None
    except Exception:
        return False

# Estensioni di codice/rilevanti per la raccolta del contesto
_CODE_EXTS = (".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".html",
              ".css", ".vue", ".java", ".c", ".cpp", ".cs", ".go", ".rs",
              ".rb", ".php", ".md", ".txt")
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".idea"}


# ─────────────────────────────────────────────────────────────────────────
#  Utility di filesystem / sicurezza
# ─────────────────────────────────────────────────────────────────────────
def _now_ts():
    return time.strftime("%Y%m%d-%H%M%S")


def _inside(workspace, path):
    """True se `path` è dentro `workspace` (path containment, risolve symlink)."""
    ws = os.path.realpath(workspace)
    p = os.path.realpath(path)
    return p == ws or p.startswith(ws + os.sep)


def _safe_target(workspace, rel):
    """Risolve `rel` dentro la workspace; None se esce dai confini."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    rel = os.path.normpath(rel)
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    target = os.path.join(workspace, rel)
    if not _inside(workspace, target):
        return None
    return target


def _run(cmd, cwd, timeout=120):
    """Esegue un comando e restituisce (returncode, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           shell=isinstance(cmd, str))
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"[timeout dopo {timeout}s]"
    except Exception as e:
        return 1, f"[errore esecuzione: {e}]"


def _is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git"))


def _git_clean(path):
    rc, out = _run(["git", "status", "--porcelain"], path, timeout=30)
    return rc == 0 and out.strip() == ""


# ─────────────────────────────────────────────────────────────────────────
#  Invio della richiesta al Coding Agent (profilo CODER, con fallback)
# ─────────────────────────────────────────────────────────────────────────
def _chat_llamacpp(messages, profile, timeout, max_tokens=None):
    base = (profile.get("base_url") or "http://127.0.0.1:8080/v1").rstrip("/")
    model = profile.get("model") or "local-model"
    key = profile.get("api_key") or "sk-no-key-required"
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages,
              "temperature": profile.get("temperature", 0.2),
              "max_tokens": max_tokens or profile.get("max_tokens", 4096), "stream": False},
        timeout=timeout,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


def _chat_ollama(messages, profile, timeout):
    import config as _cfg
    model = profile.get("ollama_model") or "qwen2.5-coder-64k"
    url = getattr(_cfg, "OLLAMA_URL", "http://127.0.0.1:11434")
    # Ollama /api/chat accetta i messaggi in stile OpenAI
    r = requests.post(f"{url}/api/chat",
                      json={"model": model, "messages": messages, "stream": False,
                            "options": {"temperature": profile.get("temperature", 0.2)}},
                      timeout=timeout)
    r.raise_for_status()
    return ((r.json().get("message") or {}).get("content") or "").strip()


# Cache del probe: llama.cpp valutato una sola volta per processo.
_LLAMACPP_USABLE = None


def _llamacpp_probe(profile):
    """Verifica rapidamente se llama.cpp è utilizzabile (non solo 'acceso').

    Su questo hardware (30B CPU-only, pesi > RAM libera) llama-server risponde
    ma a ~0.1 tok/s. Se una generazione minima non ritorna in tempo utile,
    consideriamo il provider non praticabile e usiamo il fallback Ollama.
    """
    global _LLAMACPP_USABLE
    if _LLAMACPP_USABLE is not None:
        return _LLAMACPP_USABLE
    try:
        t0 = time.time()
        txt = _chat_llamacpp([{"role": "user", "content": "ping"}], profile,
                             timeout=20, max_tokens=4)
        dt = time.time() - t0
        _LLAMACPP_USABLE = bool(txt) and dt < 12
        logger.info(f"🧑‍💻 [CODING] probe llama.cpp: {dt:.1f}s usable={_LLAMACPP_USABLE}")
    except Exception as e:
        _LLAMACPP_USABLE = False
        logger.info(f"🧑‍💻 [CODING] probe llama.cpp non disponibile: {type(e).__name__}: {e}")
    return _LLAMACPP_USABLE


def _call_coder(messages, deadline):
    """Prova il profilo CODER (llama.cpp) poi il fallback Ollama. Ritorna (testo, provider)."""
    profile_name = model_router.route_task("coding")      # -> "CODER"
    profile = model_router.get_profile(profile_name)
    chain = model_router.get_provider_chain(profile_name)
    logger.info(f"🧑‍💻 [CODING] profilo {profile_name}; catena provider={chain}")

    # Su CPU-only un 30B con contesto ampio può essere inutilmente lento
    # (pesi > RAM libera -> swap). Un probe rapido decide se vale la pena
    # usare llama.cpp, altrimenti si passa subito al fallback Ollama senza
    # bruciare il budget di 5 minuti.
    if "llamacpp" in chain and not _llamacpp_probe(profile):
        chain = [p for p in chain if p != "llamacpp"]
        logger.info("🧑‍💻 [CODING] llama.cpp troppo lento/non pronto → uso il fallback Ollama.")

    for provider in chain:
        remaining = int(deadline - time.time())
        if remaining <= 5:
            logger.warning("⏱️ [CODING] budget di tempo esaurito prima di un provider.")
            break
        try:
            if provider == "llamacpp":
                txt = _chat_llamacpp(messages, profile, min(LLAMACPP_TIMEOUT, remaining))
            elif provider == "ollama":
                txt = _chat_ollama(messages, profile, min(OLLAMA_TIMEOUT, remaining))
            else:
                continue   # il coding agent usa solo provider locali
            if txt:
                logger.info(f"✅ [CODING] risposta da {provider} ({len(txt)} chars)")
                return txt, provider
        except Exception as e:
            logger.warning(f"⚠️ [CODING] provider {provider} fallito: {type(e).__name__}: {e}")
    return None, None


# ─────────────────────────────────────────────────────────────────────────
#  Raccolta contesto + parsing output modello
# ─────────────────────────────────────────────────────────────────────────
def _list_candidate_files(workspace):
    out = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in files:
            if fn.lower().endswith(_CODE_EXTS):
                full = os.path.join(root, fn)
                try:
                    mtime = os.stat(full).st_mtime
                except OSError:
                    continue
                out.append((mtime, full))
    out.sort(reverse=True)
    return [f for _, f in out]


def _collect_context(workspace, task):
    """Sceglie fino a MAX_CONTEXT_FILES file rilevanti e ne ritorna il testo."""
    cands = _list_candidate_files(workspace)
    mentioned = re.findall(r"[\w.\-/]+\.(?:py|js|mjs|ts|tsx|jsx|json|html|css|java|md|txt)", task)
    mentioned = [m.replace("\\", "/").split("/")[-1].lower() for m in mentioned]

    chosen = []
    for full in cands:
        if os.path.basename(full).lower() in mentioned:
            chosen.append(full)
    for full in cands:
        if len(chosen) >= MAX_CONTEXT_FILES:
            break
        if full not in chosen:
            chosen.append(full)
    chosen = chosen[:MAX_CONTEXT_FILES]

    partes = []
    for full in chosen:
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                contenuto = f.read(MAX_FILE_CHARS)
        except Exception:
            continue
        rel = os.path.relpath(full, workspace).replace("\\", "/")
        partes.append(f"--- FILE: {rel} ---\n{contenuto}")
    return "\n\n".join(partes)


def _build_prompt(task, workspace, context, test_output=None, iteration=1):
    schema = (
        '{\n'
        '  "files": [{"path": "percorso/relativo.py", "content": "contenuto COMPLETO del file"}],\n'
        '  "test_command": "comando di test opzionale",\n'
        '  "summary": "breve riepilogo di cosa hai fatto"\n'
        '}'
    )
    extra = ""
    if test_output:
        extra = ("\n\nI test sono FALLITI (iterazione %d). Output:\n%s\n"
                 "Correggi i file e restituisci di nuovo il JSON completo." % (iteration, test_output[:3000]))
    return (
        "Sei il Coding Agent locale di Clio. Lavori SOLO dentro questa workspace:\n"
        f"{workspace}\n\n"
        "TASK:\n" + task + "\n\n"
        "FILE ESISTENTI (contesto, al massimo 5):\n" + (context or "(workspace vuota)") + "\n\n"
        "REGOLE:\n"
        f"- Modifica/crea al MASSIMO {MAX_FILES} file.\n"
        "- I percorsi devono essere RELATIVI alla workspace (mai assoluti, mai '..').\n"
        "- Scrivi il contenuto COMPLETO di ogni file (non frammenti/diff).\n"
        "- Se il task richiede test, crea anche il file di test.\n"
        "- Rispondi SOLO con JSON valido, nessun testo fuori dal JSON.\n\n"
        "Formato di risposta:\n" + schema + extra
    )


def _extract_json(text):
    """Estrae il primo oggetto JSON valido dal testo del modello."""
    if not text:
        return None
    text = text.strip().strip("`")
    # rimuovi eventuale fence ```json
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except Exception:
        pass
    # fallback: il blocco {...} più esterno
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Scritture (tutte via guarded_call origin="coding_agent")
# ─────────────────────────────────────────────────────────────────────────
def _coding_confirm(name, args):
    """Conferma interna del coding agent.

    Le scritture avvengono SOLO dentro la sandbox isolata e solo dopo che il
    task irreversibile `esegui_coding_task` è stato confermato dall'utente.
    Quindi i tool di file usati internamente sono auto-approvati; ogni altro
    tool viene negato.
    """
    return "yes" if name in ("scrivi_file_testo", "crea_cartella") else "no"


def _write_file(sandbox, rel, content):
    """Scrive un file nella sandbox passando dal control-plane."""
    from actions import scrivi_file_testo
    target = _safe_target(sandbox, rel)
    if target is None:
        return False, f"percorso non consentito (fuori workspace): {rel}"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    res = policy.guarded_call(
        "scrivi_file_testo", scrivi_file_testo,
        {"percorso": target, "contenuto": content},
        confirm=_coding_confirm, origin="coding_agent")
    ok = isinstance(res, str) and ("✅" in res or "File scritto" in res)
    return ok, res


# ─────────────────────────────────────────────────────────────────────────
#  Test automatici
# ─────────────────────────────────────────────────────────────────────────
def _detect_and_run_tests(sandbox, deadline, hint=None):
    """Esegue pytest / npm test se presenti. Ritorna (eseguito, ok, output)."""
    if hint:
        cmd = hint.strip()
        rc, out = _run(cmd, sandbox, timeout=min(180, max(10, int(deadline - time.time()))))
        return True, rc == 0, f"$ {cmd}\n{out}"

    # Python / pytest
    has_tests = False
    for root, dirs, files in os.walk(sandbox):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if (fn.startswith("test_") and fn.endswith(".py")) or fn.endswith("_test.py"):
                has_tests = True
                break
        if has_tests:
            break
    if has_tests:
        if not _has_pytest():
            logger.warning("⚠️ [CODING] pytest non installato: test Python saltati.")
            return False, True, "(pytest non installato: test Python saltati)"
        rc, out = _run(["python", "-m", "pytest", "-q"], sandbox,
                       timeout=min(180, max(10, int(deadline - time.time()))))
        return True, rc == 0, f"$ pytest -q\n{out}"

    # Node / npm test
    pkg = os.path.join(sandbox, "package.json")
    if os.path.exists(pkg):
        try:
            with open(pkg, "r", encoding="utf-8") as f:
                if "test" in (json.load(f).get("scripts") or {}):
                    rc, out = _run("npm test --silent", sandbox,
                                   timeout=min(180, max(10, int(deadline - time.time()))))
                    return True, rc == 0, f"$ npm test\n{out}"
        except Exception:
            pass

    return False, True, "(nessun test rilevato)"


# ─────────────────────────────────────────────────────────────────────────
#  API pubblica
# ─────────────────────────────────────────────────────────────────────────
def _chiedi_workspace():
    """Nessuna workspace indicata: chiedila (Telegram / Web UI)."""
    default = os.getenv("CLIO_CODING_DEFAULT_WORKSPACE", "").strip()
    if default and os.path.isdir(default):
        return default
    return None


# ─────────────────────────────────────────────────────────────────────────
#  R3 — CODING ASSISTITO IN SANDBOX
#
#  Tutto avviene in UNA sola cartella: `_clio_sandbox` (fuori dai progetti
#  reali). `workspace` è validata contro la radice della sandbox: qualunque
#  percorso esterno viene rifiutato con un dict di errore, mai eseguito.
#
#  API: run_coding_task(task_description) -> {"success","diff","test_output"}
# ─────────────────────────────────────────────────────────────────────────
SANDBOX_ENV = "CLIO_SANDBOX"
SANDBOX_DEFAULT_NAME = "_clio_sandbox"

# R4: ogni task deve produrre patch + TEST. Se l'utente non lo chiede, lo aggiungiamo.
TEST_REQUIREMENT = ("\n\nAggiungi anche il test corrispondente in test_main.py "
                    "che copra il nuovo codice: il test deve passare con pytest. "
                    "È obbligatorio: senza test aggiornati il task non è valido.")
_TEST_HINT_RE = re.compile(r"(?i)\b(tests?|pytest|unit\s*tests?)\b")


def with_test_requirement(task_description: str) -> str:
    """Rende esplicita la richiesta del test se il task non la contiene già."""
    task = (task_description or "").strip()
    if not task or _TEST_HINT_RE.search(task):
        return task
    return task + TEST_REQUIREMENT


def _tests_touched(diff):
    """True se il diff modifica almeno un file di test."""
    for line in (diff or "").splitlines():
        if not line.startswith("diff --git "):
            continue
        if " b/" not in line:
            continue
        path = line.rsplit(" b/", 1)[1].strip().replace("\\", "/").lower()
        nome = path.rsplit("/", 1)[-1]
        if (nome.startswith("test_") or nome.endswith("_test.py")
                or path.startswith(("tests/", "test/"))
                or "/tests/" in path or "/test/" in path):
            return True
    return False


def get_sandbox_root() -> str:
    """Radice della sandbox di coding (env CLIO_SANDBOX o `<clio>/../_clio_sandbox`)."""
    env = (os.getenv(SANDBOX_ENV) or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir, SANDBOX_DEFAULT_NAME))


def _resolve_sandbox_workspace(workspace=None):
    """Ritorna (path, errore). Accetta solo la sandbox o una sua sottocartella."""
    root = get_sandbox_root()
    if not workspace:
        return root, None
    target = os.path.abspath(os.path.expanduser(str(workspace)))
    if not _inside(root, target):
        return None, (f"workspace rifiutata: {target} è fuori dalla sandbox {root}. "
                      "Il coding agent può lavorare solo dentro la sandbox.")
    return target, None


def _git_diff(workspace, timeout=60):
    """Diff del lavoro non committato (i file nuovi compaiono via `git add -N`)."""
    if not _is_git_repo(workspace):
        return f"(diff non disponibile: {workspace} non è un repo git)"
    _run(["git", "add", "-N", "--", "."], workspace, timeout=timeout)
    rc, out = _run(["git", "diff", "--no-color", "--", "."], workspace, timeout=timeout)
    if rc != 0:
        return f"(git diff non riuscito: {out})"
    return out


def _run_pytest(workspace, timeout=120):
    """Esegue la suite della sandbox. Ritorna (ok, output formattato)."""
    cmd = [sys.executable or "python", "-m", "pytest", "-q"]
    rc, out = _run(cmd, workspace, timeout=timeout)
    return rc == 0, f"$ pytest -q\n{out}"


def _agent_positional_args(agent, task, workspace, timeout):
    """Quanti argomenti posizionali passare all'agente (iniettato o default)."""
    try:
        params = inspect.signature(agent).parameters
    except (TypeError, ValueError):
        params = {}
    if not params:
        return (task, workspace, timeout)
    positional = [p for p in params.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    n = 3 if any(p.kind is p.VAR_POSITIONAL for p in params.values()) else len(positional)
    values = (task, workspace, timeout)
    return values[:max(0, min(3, n))]


def _normalize_agent_result(value):
    """(ok, output, timed_out) da qualunque cosa ritorni l'agente."""
    if isinstance(value, dict):
        ok = bool(value.get("ok", True))
        out = value.get("output")
        if out in (None, ""):
            out = value.get("text") or value.get("error") or ""
        return ok, str(out), False
    if value is None:
        return True, "", False
    if isinstance(value, bool):
        return value, "", False
    return True, str(value), False


def _run_agent(agent, task, workspace, timeout):
    """Lancia l'agente con una deadline reale. Non solleva MAI eccezioni.

    Se l'agente supera il timeout la richiesta viene abbandonata (thread daemon)
    e il chiamante riceve timed_out=True: CLIO non resta bloccato.
    """
    box = {}

    def _worker():
        try:
            box["value"] = agent(*_agent_positional_args(agent, task, workspace, timeout))
        except BaseException as e:      # noqa: BLE001 - l'agente non deve abbattere CLIO
            box["error"] = f"{type(e).__name__}: {e}"

    th = threading.Thread(target=_worker, name="clio-coding-agent", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, f"⏱️ agente ancora in esecuzione dopo {timeout}s: richiesta abbandonata.", True
    if "error" in box:
        return False, f"❌ agente fallito: {box['error']}", False
    return _normalize_agent_result(box.get("value"))


_OPENCODE_LOCAL_PROVIDERS = {"ollama", "lmstudio", "vllm"}


def _is_local_opencode_model(model):
    """Accetta solo provider che identificano esplicitamente un runtime locale."""
    provider, separator, model_id = str(model or "").strip().partition("/")
    return bool(separator and model_id and provider.lower() in _OPENCODE_LOCAL_PROVIDERS)


def _opencode_agent(task, workspace=None, timeout=TASK_TIMEOUT):
    """OpenCode opzionale, solo con provider/modello locale esplicito."""
    model = (os.getenv("CLIO_CODING_MODEL") or "").strip()
    if not _is_local_opencode_model(model):
        return {"ok": False, "provider": None,
                "output": ("OpenCode richiede un modello locale esplicito in "
                           "CLIO_CODING_MODEL (ollama/, lmstudio/ o vllm/).")}
    exe = shutil.which("opencode")
    if not exe:
        return {"ok": False, "provider": None,
                "output": "opencode non trovato nel PATH; nessun fallback cloud eseguito."}
    cmd = [exe, "run", "-m", model]
    cmd += ["--auto", task]
    rc, out = _run(cmd, workspace or get_sandbox_root(), timeout=timeout)
    return {"ok": rc == 0, "provider": "opencode", "output": out}


def _local_agent(task, workspace=None, timeout=TASK_TIMEOUT):
    """Profilo CODER locale (llama.cpp / Ollama), senza fallback cloud."""
    workspace = workspace or get_sandbox_root()
    deadline = time.time() + timeout
    context = _collect_context(workspace, task)
    prompt = _build_prompt(task, workspace, context)
    raw, provider = _call_coder([{"role": "user", "content": prompt}], deadline)
    if not raw:
        return {"ok": False, "provider": provider, "output":
                "nessun provider locale ha risposto (llama.cpp/Ollama irraggiungibili)."}
    data = _extract_json(raw)
    if not data or not isinstance(data.get("files"), list) or not data["files"]:
        return {"ok": False, "provider": provider,
                "output": "risposta del modello senza JSON valido:\n" + raw[:1500]}

    written, errors = [], []
    for item in data["files"][:MAX_FILES]:
        item = item or {}
        rel = str(item.get("path") or "").strip()
        content = item.get("content")
        if not rel or content is None:
            errors.append(f"voce non valida: {item!r}")
            continue
        ok, res = _write_file(workspace, rel, str(content))
        if ok:
            written.append(os.path.normpath(rel).replace("\\", "/"))
        else:
            errors.append(str(res))
    output = "file scritti: " + (", ".join(written) or "(nessuno)")
    if errors:
        output += "\nerrori: " + "; ".join(errors[:5])
    return {"ok": bool(written), "provider": provider, "output": output}


def _default_agent(task, workspace=None, timeout=TASK_TIMEOUT):
    """Usa il CODER locale per default; OpenCode richiede opt-in locale esplicito.

    La semplice presenza del binario OpenCode non è sufficiente: il suo modello
    predefinito potrebbe usare un servizio remoto o a pagamento.
    """
    backend = (os.getenv("CLIO_CODING_AGENT") or "local").strip().lower()
    if backend in {"", "local", "ollama", "llamacpp"}:
        return _local_agent(task, workspace, timeout)
    if backend != "opencode":
        return {"ok": False, "provider": None,
                "output": f"backend CODER non riconosciuto: {backend}"}
    model = (os.getenv("CLIO_CODING_MODEL") or "").strip()
    if not _is_local_opencode_model(model):
        return {"ok": False, "provider": None,
                "output": ("OpenCode non avviato: seleziona un modello locale "
                           "esplicito (ollama/, lmstudio/ o vllm/).")}
    return _opencode_agent(task, workspace, timeout)


def run_coding_task(task_description: str, workspace: str = None, agent=None,
                    timeout: int = TASK_TIMEOUT) -> dict:
    """Esegue un task di coding DENTRO la sandbox e lo verifica con pytest.

    Args:
        task_description: cosa deve fare l'agente (linguaggio naturale).
        workspace: solo la sandbox (o una sua sottocartella). Default: sandbox.
        agent: callable opzionale (task, workspace, timeout) -> dict/str/bool;
               serve ai test e per iniettare un agente diverso da OpenCode.
        timeout: secondi complessivi di sicurezza (default 300).

    Returns:
        {"success": bool, "diff": str, "test_output": str,
         "agent_output": str, "workspace": str, "timed_out": bool, "error": str}
        `success` è True solo se l'agente ha finito E pytest passa.

    Non solleva mai: ogni problema diventa `success=False` + `error`.
    """
    result = {
        "success": False,
        "diff": "",
        "test_output": "",
        "agent_output": "",
        "workspace": "",
        "task_effective": "",
        "tests_touched": False,
        "timed_out": False,
        "error": None,
    }

    task = (task_description or "").strip()
    if not task:
        result["error"] = "task_description vuota"
        return result
    task = with_test_requirement(task)          # R4: patch + test, sempre
    result["task_effective"] = task

    try:
        timeout = max(5, int(timeout))
    except (TypeError, ValueError):
        timeout = TASK_TIMEOUT
    deadline = time.time() + timeout

    target, err = _resolve_sandbox_workspace(workspace)
    if err:
        logger.warning(f"🧑‍💻 [CODING] {err}")
        result["error"] = err
        return result
    if not os.path.isdir(target):
        result["error"] = (f"sandbox non trovata: {target}. "
                           f"Crea la cartella o imposta {SANDBOX_ENV}.")
        return result
    result["workspace"] = target

    run_agent = agent or _default_agent
    logger.info(f"🧑‍💻 [CODING] sandbox workspace={target} task={task!r} timeout={timeout}s")
    ok_agent, agent_out, timed_out = _run_agent(run_agent, task, target, timeout)
    result["agent_output"] = agent_out
    result["timed_out"] = timed_out
    logger.info(f"🧑‍💻 [CODING] agente ok={ok_agent} timed_out={timed_out}")

    # Il diff si calcola sempre: mostra cosa ha toccato l'agente.
    result["diff"] = _git_diff(target)
    result["tests_touched"] = _tests_touched(result["diff"])

    # pytest sempre eseguito (verifica reale, indipendente dall'agente).
    remaining = 60 if timed_out else max(10, int(deadline - time.time()))
    tests_ok, test_out = _run_pytest(target, timeout=remaining)
    result["test_output"] = test_out

    # Contratto R4: patch + test. Successo solo se l'agente ha anche aggiornato i test.
    result["success"] = bool(ok_agent and tests_ok and not timed_out
                             and result["tests_touched"])
    if not result["success"]:
        if timed_out:
            result["error"] = f"timeout: l'agente non ha finito entro {timeout}s"
        elif not ok_agent:
            result["error"] = "l'agente di coding non ha completato il task"
        elif not tests_ok:
            result["error"] = "i test della sandbox sono falliti"
        else:
            result["error"] = ("l'agente non ha aggiunto/aggiornato alcun test "
                               "(nessun file test_*.py nel diff)")
    logger.info(f"🧑‍💻 [CODING] esito success={result['success']} error={result['error']}")
    return result


def _format_coding_result(esito: dict) -> str:
    """Rende il dict di run_coding_task una risposta leggibile per Jarvis."""
    sandbox = esito.get("workspace") or get_sandbox_root()
    diff = (esito.get("diff") or "").strip() or "(nessuna modifica)"
    test_out = (esito.get("test_output") or "").strip() or "(pytest non eseguito)"
    if esito.get("success"):
        return ("✅ Coding Agent (sandbox): task completato, test PASSATI.\n"
                f"Sandbox: {sandbox}\n"
                f"File di test aggiornati: {'sì' if esito.get('tests_touched') else 'no'}\n\n"
                f"Test:\n{test_out[:800]}\n\n"
                f"Diff:\n{diff[:2000]}")
    agente = (esito.get("agent_output") or "").strip()
    return ("❌ Coding Agent (sandbox): task NON completato.\n"
            f"Motivo: {esito.get('error') or 'sconosciuto'}\n"
            f"Sandbox: {sandbox}\n\n"
            f"Test:\n{test_out[:800]}"
            + (f"\n\nAgente:\n{agente[:800]}" if agente else ""))


def esegui_coding_task(task_descrizione: str, workspace: str = None) -> str:
    """Azione di Clio: task di coding DELEGATO, sempre e solo nella sandbox.

    Registrata in registry.AZIONI_MAP e classificata IRREVERSIBILE in policy.py:
    passa da policy.guarded_call e richiede sempre la conferma dell'utente.
    Ritorna una stringa (contratto di registry/jarvis_brain).
    """
    return _format_coding_result(run_coding_task(task_descrizione, workspace=workspace))



def _esegui_coding_task_legacy(task_descrizione: str, workspace: str = None) -> str:
    """Coding Agent locale: esegue `task_descrizione` dentro `workspace`.

    Crea/modifica fino a 5 file, esegue i test (pytest/npm) e fa rollback se
    falliscono. Tutte le scritture passano da guarded_call(origin="coding_agent").
    """
    logger.info(f"🧑‍💻 [CODING] Task: {task_descrizione!r} workspace={workspace!r}")
    deadline = time.time() + TASK_TIMEOUT

    # (a) workspace obbligatoria
    if not workspace:
        workspace = _chiedi_workspace()
    if not workspace:
        return ("🧑‍💻 Coding Agent: mi serve una workspace. Rispondi con "
                "'coding task: <descrizione> in C:\\percorso\\progetto' oppure "
                "imposta CLIO_CODING_DEFAULT_WORKSPACE.")

    workspace = os.path.abspath(os.path.expanduser(workspace))

    # (b) verifica esistenza e scrivibilità
    if not os.path.isdir(workspace):
        return f"❌ Coding Agent: la workspace non esiste: {workspace}"
    if not os.access(workspace, os.W_OK):
        return f"❌ Coding Agent: la workspace non è scrivibile: {workspace}"

    # (c) isolamento: branch git temporaneo oppure copia isolata
    git_mode = _is_git_repo(workspace) and _git_clean(workspace)
    sandbox = workspace
    original_branch = None
    temp_dir = None
    branch = f"clio-coding-{_now_ts()}"

    if git_mode:
        rc, orig = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], workspace, timeout=30)
        original_branch = orig.strip() if rc == 0 else "HEAD"
        rc, out = _run(["git", "checkout", "-b", branch], workspace, timeout=30)
        if rc != 0:
            return f"❌ Coding Agent: impossibile creare il branch git '{branch}': {out}"
        logger.info(f"🧑‍💻 [CODING] branch git temporaneo: {branch}")
    else:
        temp_dir = tempfile.mkdtemp(prefix="clio_coding_")
        try:
            shutil.copytree(workspace, temp_dir, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*_SKIP_DIRS))
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return f"❌ Coding Agent: impossibile creare la copia isolata: {e}"
        sandbox = temp_dir
        logger.info(f"🧑‍💻 [CODING] copia isolata: {sandbox}")

    touched = []            # percorsi relativi scritti
    applied_files = 0
    test_hint = None
    last_test_out = ""
    summary_text = ""

    try:
        for iteration in range(1, MAX_ITERATIONS + 1):
            if time.time() > deadline:
                return _rollback_and_report(sandbox, workspace, git_mode, original_branch,
                                            branch, temp_dir, touched,
                                            "⏱️ Coding Agent: timeout di 5 minuti raggiunto.")

            context = _collect_context(sandbox, task_descrizione)
            prompt = _build_prompt(task_descrizione, sandbox, context,
                                   test_output=last_test_out, iteration=iteration)
            messages = [{"role": "user", "content": prompt}]

            raw, provider = _call_coder(messages, deadline)
            if not raw:
                return _rollback_and_report(sandbox, workspace, git_mode, original_branch,
                                            branch, temp_dir, touched,
                                            "❌ Coding Agent: nessun provider locale ha risposto "
                                            "(llama.cpp/Ollama irraggiungibili o budget di 5 minuti "
                                            "esaurito).")

            data = _extract_json(raw)
            if not data or not isinstance(data.get("files"), list) or not data["files"]:
                last_test_out = f"Risposta non valida (JSON mancante). Raw:\n{raw[:1500]}"
                logger.warning("🧑‍💻 [CODING] risposta senza JSON valido, ritento.")
                continue

            summary_text = str(data.get("summary") or "").strip()
            if data.get("test_command"):
                test_hint = str(data["test_command"]).strip()

            # (e) applica le modifiche (max MAX_FILES)
            newly = []
            errors = []
            for item in data["files"][:MAX_FILES]:
                rel = str(item.get("path") or "").strip()
                content = item.get("content")
                if not rel or content is None:
                    errors.append(f"voce senza path/content: {item!r}")
                    continue
                ok, res = _write_file(sandbox, rel, str(content))
                if ok:
                    rel_norm = os.path.normpath(rel).replace("\\", "/")
                    if rel_norm not in touched:
                        touched.append(rel_norm)
                    newly.append(rel_norm)
                    applied_files += 1
                    logger.info(f"🧑‍💻 [CODING] scritto: {rel_norm}")
                else:
                    errors.append(str(res))

            if not newly:
                last_test_out = "Nessun file scritto. Errori: " + "; ".join(errors[:5])
                logger.warning("🧑‍💻 [CODING] nessun file applicato, ritento.")
                continue

            # (f) test automatici
            ran, ok, out = _detect_and_run_tests(sandbox, deadline, hint=test_hint)
            last_test_out = out
            logger.info(f"🧑‍💻 [CODING] test eseguiti={ran} ok={ok}")

            if ok:
                # (h) mantieni
                return _finalize_success(sandbox, workspace, git_mode, branch, temp_dir,
                                         touched, applied_files, provider, summary_text,
                                         ran, out)

            # (g) test falliti -> se possibile ritenta, altrimenti rollback
            if iteration >= MAX_ITERATIONS:
                break

        return _rollback_and_report(sandbox, workspace, git_mode, original_branch, branch,
                                    temp_dir, touched,
                                    "❌ Coding Agent: test falliti dopo le iterazioni disponibili.",
                                    test_output=last_test_out)

    except Exception as e:
        logger.error(f"🧑‍💻 [CODING] errore: {e}")
        return _rollback_and_report(sandbox, workspace, git_mode, original_branch, branch,
                                    temp_dir, touched, f"❌ Coding Agent: errore imprevisto: {e}")


def _finalize_success(sandbox, workspace, git_mode, branch, temp_dir, touched,
                      applied_files, provider, summary, ran_tests, test_output):
    if git_mode:
        _run(["git", "add", "-A"], workspace, timeout=30)
        _run(["git", "commit", "-m", f"clio coding agent: {summary[:60] or 'task'}"],
             workspace, timeout=30)
    else:
        # copia i file applicati dalla sandbox alla workspace reale
        for rel in touched:
            src = os.path.join(sandbox, rel)
            dst = os.path.join(workspace, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            except Exception as e:
                logger.warning(f"⚠️ [CODING] copia-back fallita per {rel}: {e}")
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    test_line = ""
    if ran_tests:
        esito = "PASSATI" if "fail" not in test_output.lower() and "error" not in test_output.lower() else "eseguiti"
        test_line = f"\nTest: {esito}\n{test_output[:800]}"
    branch_line = f"\nBranch git: {branch}" if git_mode else ""
    logger.info(f"🧑‍💻 [CODING] completato: {applied_files} file, provider={provider}")
    return (f"✅ Coding Agent: task completato.\n"
            f"File: {', '.join(touched) or '(nessuno)'}\n"
            f"Provider: {provider}{branch_line}{test_line}\n\n{summary}".strip())


def _rollback_and_report(sandbox, workspace, git_mode, original_branch, branch, temp_dir,
                         touched, motivo, test_output=""):
    if git_mode:
        for rel in touched:
            _run(["git", "checkout", "--", rel], workspace, timeout=30)
            full = os.path.join(workspace, rel)
            if os.path.exists(full):
                # file nuovo creato dall'agente: rimuovilo (non era tracciato)
                rc, tracked = _run(["git", "ls-files", "--error-unmatch", rel], workspace, timeout=30)
                if rc != 0:
                    try:
                        os.remove(full)
                    except OSError:
                        pass
        if original_branch:
            _run(["git", "checkout", original_branch], workspace, timeout=30)
        _run(["git", "branch", "-D", branch], workspace, timeout=30)
    else:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
    out = f"\n\n{test_output[:600]}" if test_output else ""
    logger.warning(f"🧑‍💻 [CODING] rollback: {motivo}")
    return f"{motivo}\nRollback completato (nessuna modifica mantenuta).{out}"


__all__ = ["esegui_coding_task", "run_coding_task", "get_sandbox_root"]
