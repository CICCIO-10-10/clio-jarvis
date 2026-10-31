"""Skill 3D locale con Blender/bpy: genera mesh OBJ senza codice LLM arbitrario."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("ClioBlenderSkills")

BASE_DIR = Path(__file__).resolve().parent
MODEL_DIR = BASE_DIR / "data" / "3d_models"
SCRIPT_DIR = BASE_DIR / "data" / "blender_scripts"
BLENDER_ENV = "CLIO_BLENDER_EXE"
BLENDER_TIMEOUT = max(15, int(os.getenv("CLIO_BLENDER_TIMEOUT", "180")))
MAX_OBJECT_CHARS = 300
BLENDER_ROOTS = (
    Path(r"C:\Program Files\Blender Foundation"),
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Blender Foundation",
)


class BlenderSkillError(ValueError):
    """Errore prevedibile con messaggio già pronto per l'utente."""


def _version_key(path: Path) -> Tuple[int, int]:
    match = re.search(r"blender[ _-]*(\d+)(?:[._](\d+))?", str(path), re.IGNORECASE)
    return (int(match.group(1)), int(match.group(2) or 0)) if match else (0, 0)


def find_blender() -> Optional[str]:
    r"""Trova Blender tramite override, PATH o Program Files\Blender Foundation."""
    override = os.environ.get(BLENDER_ENV, "").strip().strip('"')
    if override and Path(override).is_file():
        return str(Path(override).resolve())
    if override:
        logger.warning("%s non punta a un eseguibile esistente: %s", BLENDER_ENV, override)

    for nome in ("blender.exe", "blender"):
        trovato = shutil.which(nome)
        if trovato:
            return str(Path(trovato).resolve())

    trovati = []
    for root in dict.fromkeys(BLENDER_ROOTS):
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                dirs[:] = []
                continue
            if depth >= 4:
                dirs[:] = []
            else:
                dirs[:] = [name for name in dirs
                           if name.lower() not in {"windowsapps", "temp", ".git"}]
            if "blender.exe" in {name.lower() for name in files}:
                match = next(name for name in files if name.lower() == "blender.exe")
                trovati.append(current_path / match)

    if not trovati:
        return None
    trovati.sort(key=_version_key, reverse=True)
    return str(trovati[0].resolve())


def _resolve_description(oggetto: str, idea_id: str = ""):
    """Accetta una descrizione diretta o recupera l'obiettivo dall'IdeaSession."""
    sessione = None
    testo = str(oggetto or "").strip()
    if idea_id:
        try:
            import idea_session
            sessione = idea_session.load_idea_session(str(idea_id).strip())
        except Exception as exc:
            raise BlenderSkillError(f"Non riesco a leggere l'IdeaSession: {exc}") from exc
        if sessione is None:
            raise BlenderSkillError(f"IdeaSession '{idea_id}' non trovata.")
        if not testo:
            testo = (sessione.last_observation or sessione.user_goal or "").strip()
    elif not testo:
        try:
            import idea_session
            sessione = idea_session.get_active_idea_session()
            if sessione:
                testo = (sessione.last_observation or sessione.user_goal or "").strip()
        except Exception:
            sessione = None

    if not testo:
        raise BlenderSkillError("Dimmi quale oggetto vuoi modellare in 3D.")
    return testo[:MAX_OBJECT_CHARS], sessione


def _classify_object(description: str) -> Tuple[str, str]:
    """Seleziona un template bpy deterministico e restituisce eventuale nota."""
    testo = description.casefold()
    if re.search(r"\b(sedia|sedie|chair|poltrona)\b", testo):
        return "chair", ""
    if re.search(r"\b(tavolo|tavoli|table|scrivania|desk)\b", testo):
        return "table", ""
    if re.search(r"\b(sgabello|sgabelli|stool)\b", testo):
        return "stool", ""
    if re.search(r"\b(cubo|cuboide|cube|box)\b", testo):
        return "cube", ""
    return "cube", f"Per «{description}» non ho ancora un template dedicato: ho creato un cubo base."


def _script_source(model_kind: str, output_path: Path) -> str:
    """Compone uno script bpy da template statici; nessuna stringa utente è codice."""
    output_literal = json.dumps(str(output_path.resolve()), ensure_ascii=True)
    header = '''# Generato da CLIO: template bpy statico e validato.\nimport bpy\n\n'''
    helpers = r'''
def material(name, color, metallic=0.0, roughness=0.42):
    mat = bpy.data.materials.new(name=name)
    mat.diffuse_color = color
    mat.use_nodes = True
    shader = mat.node_tree.nodes.get("Principled BSDF")
    if shader:
        shader.inputs["Base Color"].default_value = color
        shader.inputs["Metallic"].default_value = metallic
        shader.inputs["Roughness"].default_value = roughness
    return mat

def box(name, location, dimensions, mat, bevel=0.025):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if mat:
        obj.data.materials.append(mat)
    if bevel > 0:
        modifier = obj.modifiers.new(name="Edge bevel", type="BEVEL")
        modifier.width = bevel
        modifier.segments = 3
        obj.modifiers.new(name="Weighted normals", type="WEIGHTED_NORMAL")
    return obj

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
wood = material("Warm wood", (0.36, 0.16, 0.055, 1.0), roughness=0.34)
wood_light = material("Seat wood", (0.52, 0.28, 0.10, 1.0), roughness=0.38)
metal = material("Dark metal", (0.055, 0.065, 0.075, 1.0), metallic=0.62, roughness=0.28)
'''

    if model_kind == "chair":
        builder = r'''
# Sedia: sedile, quattro gambe (le due posteriori sostengono lo schienale) e schienale.
box("Sedile", (0.0, 0.0, 1.05), (1.60, 1.60, 0.18), wood_light, 0.055)
for x in (-0.65, 0.65):
    box("Gamba anteriore", (x, -0.65, 0.48), (0.16, 0.16, 0.96), wood, 0.025)
    box("Gamba posteriore", (x, 0.65, 1.175), (0.16, 0.16, 2.35), wood, 0.025)
box("Schienale", (0.0, 0.72, 1.77), (1.60, 0.15, 1.26), wood_light, 0.055)
'''
    elif model_kind == "table":
        builder = r'''
# Tavolo: piano e quattro gambe.
box("Piano", (0.0, 0.0, 1.44), (1.90, 1.25, 0.18), wood_light, 0.055)
for x in (-0.78, 0.78):
    for y in (-0.48, 0.48):
        box("Gamba", (x, y, 0.675), (0.16, 0.16, 1.35), wood, 0.025)
'''
    elif model_kind == "stool":
        builder = r'''
# Sgabello: seduta e quattro gambe.
box("Seduta", (0.0, 0.0, 0.85), (1.05, 1.05, 0.16), wood_light, 0.05)
for x in (-0.40, 0.40):
    for y in (-0.40, 0.40):
        box("Gamba", (x, y, 0.425), (0.12, 0.12, 0.85), wood, 0.02)
'''
    else:
        builder = r'''
# Oggetto base per richieste senza un template specifico.
box("Cubo", (0.0, 0.0, 0.5), (1.0, 1.0, 1.0), wood_light, 0.035)
'''

    exporter = r'''
output_path = OUTPUT_PATH
bpy.ops.object.select_all(action="DESELECT")
for obj in bpy.context.scene.objects:
    if obj.type == "MESH":
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

if hasattr(bpy.ops.wm, "obj_export"):
    result = bpy.ops.wm.obj_export(
        filepath=output_path,
        export_selected_objects=True,
        apply_modifiers=True,
        export_materials=True,
        forward_axis="NEGATIVE_Y",
        up_axis="Z",
    )
else:
    result = bpy.ops.export_scene.obj(
        filepath=output_path,
        use_selection=True,
        use_mesh_modifiers=True,
        use_materials=True,
        axis_forward="-Y",
        axis_up="Z",
    )
if "FINISHED" not in result:
    raise RuntimeError("Esportazione OBJ non completata: " + str(result))
print("CLIO_OBJ_EXPORTED=" + output_path)
'''.replace("OUTPUT_PATH", output_literal)
    return header + helpers + builder + exporter


def _validate_obj(path: Path) -> Tuple[bool, int, int]:
    vertices = faces = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if line.startswith("v "):
                    vertices += 1
                elif line.startswith("f "):
                    faces += 1
    except OSError:
        return False, 0, 0
    return path.is_file() and path.stat().st_size > 0 and vertices > 0 and faces > 0, vertices, faces


def _update_idea(sessione, obj_path: Path) -> str:
    if sessione is None:
        return ""
    try:
        import idea_session
        sessione.file_3d = str(obj_path)
        sessione.status_3d = "blender"
        sessione.touch()
        idea_session.update_idea_session(sessione)
    except Exception as exc:
        logger.warning("Modello creato ma IdeaSession non aggiornata: %s", exc)
        return " (IdeaSession non aggiornata)"
    return ""


def genera_modello_3d_blender(oggetto: str = "", idea_id: str = "") -> str:
    """Crea un OBJ reale con Blender/bpy. Se Blender manca, non genera un falso modello."""
    try:
        descrizione, sessione = _resolve_description(oggetto, idea_id)
    except BlenderSkillError as exc:
        return f"Capo, {exc}"

    blender_exe = find_blender()
    if not blender_exe:
        return (
            "Capo, per generare un modello 3D reale mi serve Blender. "
            "Scaricalo da blender.org e installalo; non ho creato un modello fittizio."
        )

    model_kind, nota = _classify_object(descrizione)
    slug = unicodedata.normalize("NFKD", descrizione.casefold())
    slug = slug.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")[:40] or "modello_3d"
    obj_path = MODEL_DIR / f"{slug}_{uuid.uuid4().hex[:8]}.obj"
    script_path = None

    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", suffix=".py",
            prefix="clio_blender_", dir=SCRIPT_DIR, delete=False,
        ) as script_file:
            script_file.write(_script_source(model_kind, obj_path))
            script_path = Path(script_file.name)

        result = subprocess.run(
            [blender_exe, "-b", "--factory-startup", "-P", str(script_path)],
            cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=BLENDER_TIMEOUT,
            check=False,
        )
        if result.returncode != 0:
            tail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[-900:]
            raise BlenderSkillError(f"Blender è terminato con errore: {tail or result.returncode}")

        valido, vertices, faces = _validate_obj(obj_path)
        if not valido:
            raise BlenderSkillError("Blender non ha esportato un OBJ valido con vertici e facce.")

        session_note = _update_idea(sessione, obj_path)
        messaggio = (
            f"✅ Modello 3D creato con Blender: {obj_path} "
            f"({vertices} vertici, {faces} facce).{session_note}"
        )
        if nota:
            messaggio += f" Nota: {nota}"
        logger.info("[BLENDER SKILL] %s (%s vertici, %s facce)", obj_path, vertices, faces)
        return messaggio
    except subprocess.TimeoutExpired:
        return f"Capo, Blender ha superato il limite di {BLENDER_TIMEOUT} secondi; il modello non è stato completato."
    except BlenderSkillError as exc:
        logger.warning("[BLENDER SKILL] %s", exc)
        return f"❌ Capo, {exc}"
    except Exception as exc:
        logger.exception("[BLENDER SKILL] Errore durante la generazione")
        return f"❌ Capo, generazione Blender non riuscita: {type(exc).__name__}: {exc}"
    finally:
        if script_path is not None:
            try:
                script_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Script temporaneo Blender non rimosso (%s): %s", script_path, exc)


__all__ = ["find_blender", "genera_modello_3d_blender"]
