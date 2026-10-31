"""blender_agent.py — FASE 2 · C5: da schizzo 2D a modello 3D, in modo RESILIENTE.

Il vincolo di progetto è che il risultato ci sia SEMPRE: se Blender è installato
si usa Blender (script `bpy` generato dal LLM, eseguito in `--background`), se
non c'è si genera comunque un `.obj` valido con trimesh (libreria pura Python +
numpy, nessuna compilazione) e, come ultima rete di sicurezza, con un writer OBJ
scritto a mano senza dipendenze.

Contratto pubblico (registrato in AZIONI_MAP come `genera_modello_3d`):
    find_blender() -> str | None
    generate_blender_script(description, idea_id) -> str      (percorso .py)
    generate_fallback_3d(description, idea_id) -> str         (percorso .obj)
    generate_3d_from_idea(idea_id) -> dict

Sicurezza:
  * il tool è SENSITIVE in policy.py: richiede conferma (da voce o da API) prima
    di scrivere file ed eseguire codice generato;
  * lo script generato dal LLM viene VALIDATO: solo import bpy/math/mathutils/random,
    nessun os/subprocess/open/eval/exec/rete. Il salvataggio del .blend è un
    epilogo aggiunto da questo modulo, non dal modello;
  * Blender è lanciato con `--background --factory-startup`: nessuna GUI, nessuna
    preferenza/addon dell'utente, timeout esplicito;
  * nessuna eccezione raggiunge CLIO: ogni passo ha un fallback e tutto torna in
    un dict con `ok`/`error`.

Nota sui nomi: la IdeaSession espone `file_3d` e `status_3d` (in Python un campo
non può iniziare con una cifra; corrispondono a `3d_file` / `3d_status`).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import idea_session

logger = logging.getLogger("ClioBlender")

ACTION_NAME = "genera_modello_3d"          # SENSITIVE in policy.py
ENV_BLENDER_EXE = "CLIO_BLENDER_EXE"
BLENDER_EXE = "blender.exe"

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = BASE_DIR / "data" / "blender_scripts"
MODEL_DIR = BASE_DIR / "data" / "3d_models"

# Cartelle indicate dalla specifica + fallback versionati.
BLENDER_ROOTS = (
    r"C:\Program Files\Blender Foundation",
    os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Users\Public"), "Programs"),
    r"C:\Program Files (x86)\Blender Foundation",
)
MAX_SEARCH_DEPTH = 4
SKIP_DIRS = {"windowsapps", "node_modules", ".git", "$recycle.bin", "temp"}
BLENDER_TIMEOUT = 180          # secondi: generazione + salvataggio .blend

MAX_DESCRIPTION_CHARS = 1500
MAX_SCRIPT_CHARS = 20000
MAX_PRIMITIVES = 4

# Import ammessi nello script bpy generato dal LLM.
ALLOWED_IMPORT_ROOTS = {"bpy", "math", "mathutils", "random"}
# Token che rendono lo script inaccettabile (esecuzione di comandi, rete, file).
FORBIDDEN_TOKENS = (
    "subprocess", "os.system", "os.popen", "os.remove", "os.rmdir", "os.environ",
    "shutil", "socket", "requests", "urllib", "http", "ctypes", "importlib",
    "__import__", "eval(", "exec(", "open(", "pathlib", "pickle", "yaml",
)

# Primitive riconosciute nella descrizione (parole chiave in italiano e inglese).
# L'ordine conta solo per i pareggi: si usa la posizione della parola nel testo.
PRIMITIVE_KEYWORDS = {
    "box": ("cubo", "cube", "box", "scatola", "quadrato", "rettangolo", "casa",
            "edificio", "palazzo", "muro", "tavolo", "libro", "mattoncino"),
    "cylinder": ("cilindro", "cylinder", "colonna", "torre", "tubo", "bicchiere",
                 "tazza", "lattina", "albero", "tronco"),
    "sphere": ("sfera", "sphere", "palla", "sole", "pianeta", "testa", "mela",
               "arancia", "palloncino"),
    "cone": ("cono", "cone", "pigna", "gelato", "cappello", "tetto", "montagna"),
    "torus": ("toro", "torus", "ciambella", "anello", "ruota", "cerchio"),
}
DEFAULT_PRIMITIVES = ("box", "cylinder")


class BlenderAgentError(RuntimeError):
    """Errore prevedibile del 3D agent (messaggio già pronto per l'utente)."""


# ── Ricerca di Blender ───────────────────────────────────────────────────────
def _version_key(percorso: Path) -> Tuple[int, int]:
    """Estrae la versione dal percorso ("Blender 4.2" -> (4, 2)) per scegliere la più nuova."""
    match = re.search(r"blender[ _-]*(\d+)(?:[._](\d+))?", str(percorso), re.IGNORECASE)
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


def _candidates_in(radice: str, max_depth: int = MAX_SEARCH_DEPTH) -> List[Path]:
    """Tutti i blender.exe sotto `radice`, senza scendere troppo in profondità."""
    base = Path(radice)
    if not base.is_dir():
        return []
    trovati: List[Path] = []
    for corrente, cartelle, files in os.walk(base):
        attuale = Path(corrente)
        try:
            profondita = len(attuale.relative_to(base).parts)
        except ValueError:
            profondita = max_depth
        if profondita >= max_depth:
            cartelle[:] = []
        else:
            cartelle[:] = [c for c in cartelle if c.lower() not in SKIP_DIRS]
        for nome in files:
            if nome.lower() == BLENDER_EXE:
                trovati.append(attuale / nome)
    return trovati


def find_blender() -> Optional[str]:
    """Percorso di blender.exe, o None se non è installato.

    Ordine: CLIO_BLENDER_EXE (override esplicito) -> PATH -> cartelle standard
    (Program Files\\Blender Foundation, AppData\\Local\\Programs,
    Program Files (x86)\\Blender Foundation). A parità, vince la versione più nuova.
    """
    override = (os.environ.get(ENV_BLENDER_EXE) or "").strip().strip('"')
    if override:
        percorso = Path(override)
        if percorso.is_file():
            return str(percorso)
        logger.warning(f"[3D] {ENV_BLENDER_EXE} punta a un file inesistente: {override}")

    nel_path = shutil.which("blender")
    if nel_path:
        return str(Path(nel_path))

    candidati: List[Path] = []
    for radice in BLENDER_ROOTS:
        candidati.extend(_candidates_in(radice))
    if not candidati:
        logger.info("[3D] Blender non trovato: si userà il fallback trimesh")
        return None

    candidati.sort(key=_version_key, reverse=True)
    scelto = candidati[0]
    logger.info(f"[3D] Blender trovato: {scelto}")
    return str(scelto)


# ── Script bpy generato dal LLM ──────────────────────────────────────────────
PROMPT_TEMPLATE = """Sei un tecnico Blender. Scrivi SOLO codice Python per Blender 4.x (modulo bpy),
senza spiegazioni e senza blocchi markdown.

DESCRIZIONE DELLA SCENA: {descrizione}

VINCOLI OBBLIGATORI:
- parti da una scena vuota: rimuovi tutti gli oggetti esistenti con bpy.ops.object.select_all(action='SELECT') e bpy.ops.object.delete()
- crea SOLO le forme descritte (massimo 4), usando bpy.ops.mesh.primitive_*_add
- disponi le forme senza sovrapporle, appoggiate sul piano z=0
- usa solo: bpy, math, mathutils, random
- NON salvare il file, NON aprire finestre, NON usare os/subprocess/open/eval/exec, NON scaricare asset
- massimo 120 righe, codice deterministico
"""


def _clean_description(description: str) -> str:
    testo = (description or "").strip()
    if not testo:
        raise BlenderAgentError("descrizione mancante: non c'è nulla da modellare")
    return testo[:MAX_DESCRIPTION_CHARS]


def _safe_idea_id(idea_id: str) -> str:
    """id usato come nome file: niente path traversal, niente caratteri strani."""
    valore = (idea_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", valore):
        raise BlenderAgentError(f"idea_id non valido per un nome file: {idea_id!r}")
    return valore


def _default_script_llm(prompt: str) -> str:
    """Genera lo script con Gemini (un solo client in CLIO: quello del Vision Agent)."""
    from config import MODEL_PRIMARY
    from vision_agent import _gemini_client

    risposta = _gemini_client().models.generate_content(model=MODEL_PRIMARY, contents=[prompt])
    return getattr(risposta, "text", "") or ""


def _clean_code(testo: str) -> str:
    """Ripulisce un blocco markdown e taglia le righe di prosa prima del codice."""
    codice = (testo or "").strip()
    fence = re.search(r"```(?:python)?\s*(.+?)```", codice, re.DOTALL)
    if fence:
        codice = fence.group(1).strip()
    if len(codice) > MAX_SCRIPT_CHARS:
        raise BlenderAgentError(f"script troppo lungo ({len(codice)} caratteri)")
    righe = codice.splitlines()
    for indice, riga in enumerate(righe):
        spoglia = riga.strip()
        if spoglia.startswith(("import bpy", "from bpy", "import math", "from mathutils")):
            righe = righe[indice:]
            break
    else:
        raise BlenderAgentError("lo script generato non usa bpy")
    return "\n".join(righe).strip() + "\n"


def _validate_script(codice: str) -> None:
    """Accetta solo script bpy innocui: niente comandi, rete o accesso a file."""
    basso = codice.lower()
    for vietato in FORBIDDEN_TOKENS:
        if vietato in basso:
            raise BlenderAgentError(f"script rifiutato: usa '{vietato}'")

    for riga in codice.splitlines():
        spoglia = riga.strip()
        modulo = None
        match_from = re.match(r"from\s+([\w.]+)\s+import\b", spoglia)
        if match_from:
            modulo = match_from.group(1)
        else:
            match_import = re.match(r"import\s+(.+)", spoglia)
            if match_import:
                for pezzo in match_import.group(1).split(","):
                    radice = pezzo.strip().split(" as ")[0].split(".")[0]
                    if radice and radice not in ALLOWED_IMPORT_ROOTS:
                        raise BlenderAgentError(f"script rifiutato: import '{radice}'")
                continue
        if modulo and modulo.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
            raise BlenderAgentError(f"script rifiutato: import '{modulo}'")


def generate_blender_script(description: str, idea_id: str,
                            blend_path: Optional[Path] = None) -> str:
    """Genera lo script bpy che costruisce la scena e lo salva in data/blender_scripts/."""
    descrizione = _clean_description(description)
    identificativo = _safe_idea_id(idea_id)
    destinazione = Path(blend_path) if blend_path is not None else (MODEL_DIR / f"{identificativo}.blend")

    prompt = PROMPT_TEMPLATE.format(descrizione=descrizione)
    codice = _clean_code(_default_script_llm(prompt))
    _validate_script(codice)

    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    percorso = SCRIPT_DIR / f"{identificativo}.py"
    epilogo = (
        "\n# ── CLIO: salvataggio deterministico (aggiunto da blender_agent, non dal LLM) ──\n"
        "import bpy as _clio_bpy\n"
        f"_clio_bpy.ops.wm.save_as_mainfile(filepath={str(destinazione)!r})\n"
    )
    percorso.write_text(codice + epilogo, encoding="utf-8")
    logger.info(f"[3D] script bpy salvato: {percorso}")
    return str(percorso)


def _run_blender(blender_exe: str, script_path: str, blend_path: Path) -> Tuple[bool, str]:
    """Esegue Blender in background. True se il .blend esiste davvero."""
    comando = [
        blender_exe,
        "--background",
        "--factory-startup",
        "--python", str(script_path),
        "--", "--clio-out", str(blend_path),
    ]
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    inizio = time.time()
    try:
        esito = subprocess.run(
            comando,
            capture_output=True,
            text=True,
            timeout=BLENDER_TIMEOUT,
            cwd=str(MODEL_DIR),
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout di {BLENDER_TIMEOUT}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    durata = time.time() - inizio
    output = ((esito.stdout or "") + (esito.stderr or "")).strip()
    prodotto = blend_path.is_file() and blend_path.stat().st_size > 0
    logger.info(f"[3D] Blender exit={esito.returncode} in {durata:.1f}s, blend={'sì' if prodotto else 'no'}")
    return prodotto, output[-500:]


# ── Fallback: OBJ con trimesh (o con un writer puro) ─────────────────────────
def _primitives_from_description(description: str) -> List[str]:
    """Traduce la descrizione in primitive, nell'ordine in cui compaiono."""
    testo = (description or "").lower()
    posizioni: List[Tuple[int, str]] = []
    for tipo, parole in PRIMITIVE_KEYWORDS.items():
        for parola in parole:
            match = re.search(r"\b" + re.escape(parola) + r"\b", testo)
            if match:
                posizioni.append((match.start(), tipo))
                break
    posizioni.sort()
    tipi: List[str] = []
    for _, tipo in posizioni:
        if tipo not in tipi:
            tipi.append(tipo)
    if not tipi:
        tipi = list(DEFAULT_PRIMITIVES)
    return tipi[:MAX_PRIMITIVES]


def _trimesh_primitive(tipo: str, indice: int):
    """Mesh trimesh di una primitiva, appoggiata su z=0 e distanziata lungo X."""
    import trimesh

    if tipo == "sphere":
        mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.6)
        mesh.apply_translation((0, 0, 0.6))
        larghezza = 1.2
    elif tipo == "cylinder":
        mesh = trimesh.creation.cylinder(radius=0.5, height=1.5, sections=32)
        mesh.apply_translation((0, 0, 0.75))
        larghezza = 1.0
    elif tipo == "cone":
        mesh = trimesh.creation.cone(radius=0.6, height=1.2, sections=32)
        mesh.apply_translation((0, 0, 0.6))
        larghezza = 1.2
    elif tipo == "torus":
        mesh = trimesh.creation.torus(major_radius=0.6, minor_radius=0.2,
                                      major_segments=32, minor_segments=16)
        mesh.apply_translation((0, 0, 0.2))
        larghezza = 1.6
    else:  # box
        mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
        mesh.apply_translation((0, 0, 0.5))
        larghezza = 1.0

    mesh.apply_translation((indice * (larghezza + 0.6) + larghezza / 2, 0, 0))
    return mesh, larghezza


def _write_obj_trimesh(tipi: List[str], percorso: Path) -> None:
    """Scrive un unico OBJ con tutte le primitive, usando trimesh."""
    import trimesh

    pezzi = []
    for indice, tipo in enumerate(tipi):
        mesh, _ = _trimesh_primitive(tipo, indice)
        pezzi.append(mesh)
    scena = trimesh.util.concatenate(pezzi)
    scena.export(str(percorso))


def _write_obj_puro(tipi: List[str], percorso: Path) -> None:
    """Ultima rete di sicurezza: OBJ scritto a mano (nessuna dipendenza).

    Scrive un cubo per ogni primitiva, con dimensioni diverse: il file è valido
    anche se trimesh non è installabile.
    """
    vertici: List[Tuple[float, float, float]] = []
    facce: List[Tuple[int, int, int, int]] = []
    misure = {"box": 1.0, "cylinder": 0.9, "sphere": 1.1, "cone": 1.0, "torus": 1.3}

    for indice, tipo in enumerate(tipi):
        lato = misure.get(tipo, 1.0)
        base = indice * (lato + 1.0)
        offset = len(vertici) + 1
        for dz in (0.0, lato):
            for dx, dy in ((0, 0), (lato, 0), (lato, lato), (0, lato)):
                vertici.append((base + dx, dy, dz))
        facce.append((offset, offset + 1, offset + 2, offset + 3))
        facce.append((offset + 4, offset + 7, offset + 6, offset + 5))
        for coppia in ((0, 1), (1, 2), (2, 3), (3, 0)):
            facce.append((offset + coppia[0], offset + coppia[1],
                          offset + 4 + coppia[1], offset + 4 + coppia[0]))

    righe = [f"# CLIO fallback 3D — {len(tipi)} primitive: {', '.join(tipi)}"]
    righe += [f"v {x:.4f} {y:.4f} {z:.4f}" for x, y, z in vertici]
    righe += ["f " + " ".join(str(i) for i in faccia) for faccia in facce]
    percorso.write_text("\n".join(righe) + "\n", encoding="utf-8")


def _obj_valido(percorso: Path) -> bool:
    """Controllo minimo: file non vuoto con almeno un vertice e una faccia."""
    try:
        testo = percorso.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    ha_vertici = any(riga.startswith("v ") for riga in testo.splitlines())
    ha_facce = any(riga.startswith("f ") for riga in testo.splitlines())
    return ha_vertici and ha_facce


def generate_fallback_3d(description: str, idea_id: str) -> str:
    """Genera comunque un .obj con primitive dedotte dalla descrizione."""
    descrizione = _clean_description(description)
    identificativo = _safe_idea_id(idea_id)
    tipi = _primitives_from_description(descrizione)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    percorso = MODEL_DIR / f"{identificativo}.obj"

    try:
        _write_obj_trimesh(tipi, percorso)
    except Exception as e:
        logger.warning(f"[3D] trimesh non disponibile ({type(e).__name__}: {e}): uso il writer puro")
        _write_obj_puro(tipi, percorso)

    if not _obj_valido(percorso):
        raise BlenderAgentError(f"il file 3D non è valido: {percorso}")

    logger.info(f"[3D] fallback {'+'.join(tipi)} -> {percorso.name} "
                f"({percorso.stat().st_size // 1024} KB)")
    return str(percorso)


# ── Ciclo completo ───────────────────────────────────────────────────────────
def _salva_esito(sessione, percorso: Path, stato: str, descrizione: str,
                 fonte: str, blender: Optional[str], motivo: str) -> Dict[str, Any]:
    sessione.file_3d = str(percorso)
    sessione.status_3d = stato
    sessione.touch()
    try:
        idea_session.update_idea_session(sessione)
    except Exception as e:
        logger.warning(f"[3D] impossibile salvare l'idea: {type(e).__name__}: {e}")
        return {"ok": False, "idea_id": sessione.idea_id, "file": str(percorso),
                "status": stato,
                "error": f"file creato ({percorso}) ma stato non salvato: {e}"}

    logger.info(f"[3D] idea {sessione.idea_id} -> {stato}: {percorso}")
    etichetta = "Blender" if stato == "blender" else "fallback trimesh"
    messaggio = (f"🧊 Modello 3D generato con {etichetta}: {percorso} "
                 f"(idea {sessione.idea_id[:8]})")
    if motivo:
        messaggio += f" — nota: {motivo}"
    return {
        "ok": True,
        "idea_id": sessione.idea_id,
        "file": str(percorso),
        "status": stato,
        "blender": blender,
        "descrizione": descrizione,
        "fonte_descrizione": fonte,
        "motivo": motivo,
        "messaggio": messaggio,
    }


def _resolve_session(idea_id: str):
    """Sessione per id, oppure l'ultima ancora aperta. (None, motivo) se non c'è."""
    if (idea_id or "").strip():
        sessione = idea_session.load_idea_session(idea_id)
        if sessione is None:
            return None, f"Idea «{idea_id}» non trovata: non genero nulla."
        return sessione, ""
    sessione = idea_session.get_active_idea_session()
    if sessione is None:
        return None, ("Nessuna IdeaSession aperta: avvia prima un'idea con "
                      "apri_app_e_inizia_idea (o passa un idea_id).")
    return sessione, ""


def generate_3d_from_idea(idea_id: str = "") -> Dict[str, Any]:
    """IdeaSession -> modello 3D. Blender se c'è, altrimenti OBJ di fallback.

    `idea_id` vuoto = ultima IdeaSession ancora aperta (come per il Vision Agent).
    """
    sessione, motivo_id = _resolve_session(idea_id)
    if sessione is None:
        return {"ok": False, "idea_id": idea_id, "error": motivo_id}

    descrizione = (sessione.last_observation or "").strip()
    fonte = "osservazione"
    if not descrizione:
        descrizione = sessione.user_goal
        fonte = "obiettivo"
    if not descrizione:
        return {"ok": False, "idea_id": sessione.idea_id,
                "error": "L'idea non ha né osservazione né obiettivo: niente da modellare."}

    blender = find_blender()
    motivo = ""

    if blender:
        script = None
        try:
            script = generate_blender_script(descrizione, sessione.idea_id)
        except Exception as e:
            motivo = f"script Blender non generato ({type(e).__name__}: {e})"
        if script:
            blend = MODEL_DIR / f"{sessione.idea_id}.blend"
            prodotto, output = _run_blender(blender, script, blend)
            if prodotto:
                return _salva_esito(sessione, blend, "blender", descrizione, fonte, blender, "")
            motivo = f"Blender non ha prodotto il .blend ({output[-200:] or 'nessun output'})"
    else:
        motivo = "Blender non installato"

    try:
        percorso = Path(generate_fallback_3d(descrizione, sessione.idea_id))
    except Exception as e:
        logger.error(f"[3D] fallback fallito: {type(e).__name__}: {e}")
        return {"ok": False, "idea_id": sessione.idea_id,
                "error": f"generazione 3D fallita: {e}", "motivo": motivo}
    return _salva_esito(sessione, percorso, "fallback", descrizione, fonte, blender, motivo)


def describe_blender_agent() -> str:
    """Riepilogo leggibile dell'agente (usato a voce e nei report)."""
    blender = find_blender()
    stato = f"Blender: {blender}" if blender else "Blender: non installato (fallback OBJ/trimesh)"
    return (f"Blender Agent (C5): trasforma la descrizione di uno schizzo in un modello 3D. "
            f"{stato}. Output in data/3d_models/.")
