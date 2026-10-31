# -*- coding: utf-8 -*-
"""test_blender_agent.py — FASE 2 · C5: 2D -> 3D con MOCK.

Nessuna chiamata a Gemini, nessun Blender reale, nessuna GUI: `find_blender`,
`_default_script_llm` e `subprocess.run` sono sostituiti da doppi. Copre:
  * find_blender: override da env, PATH, cartelle standard, versione più nuova;
  * generate_blender_script: pulizia dei fence, epilogo che salva il .blend,
    rifiuto di import comandi/rete/file e di idea_id non sicuri;
  * generate_fallback_3d: OBJ valido davvero (riletto con trimesh), primitive
    dedotte dalla descrizione, writer puro quando trimesh non c'è;
  * generate_3d_from_idea: con Blender (successo/failure/timeout) e senza Blender,
    aggiornamento di file_3d/status_3d, osservazione vuota -> obiettivo;
  * integrazione: registry, metadati LLM, policy SENSITIVE e conferma reale
    attraverso policy.guarded_call (da voce e da API).

Esecuzione:  python -m pytest -q tests/test_blender_agent.py
"""
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import blender_agent as b  # noqa: E402
import idea_session as idea  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402

SCRIPT_BUONO = """import bpy
import math

bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.5))
bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.5, location=(2, 0, 0.75))
"""


# ── Isolamento: nessun file nel repo, nessuna variabile d'ambiente residua ──
@pytest.fixture(autouse=True)
def _isolamento(tmp_path, monkeypatch):
    monkeypatch.setattr(idea, "SESSIONS_FILE", tmp_path / "idea_sessions.json")
    monkeypatch.setattr(b, "MODEL_DIR", tmp_path / "3d_models")
    monkeypatch.setattr(b, "SCRIPT_DIR", tmp_path / "blender_scripts")
    monkeypatch.delenv(b.ENV_BLENDER_EXE, raising=False)
    # Solo il modulo: sostituire b.shutil.which toccherebbe il modulo globale
    # `shutil` (trimesh lo usa per cercare Blender).
    monkeypatch.setattr(b, "shutil", types.SimpleNamespace(which=lambda *a, **k: None))


@pytest.fixture
def sessione():
    """IdeaSession con un'osservazione (il caso normale dopo C2)."""
    sessione = idea.create_idea_session("disegnare una casa", "clio", active_app="mspaint.exe")
    sessione.last_observation = "Un cubo rosso e un cilindro blu su sfondo bianco."
    return idea.update_idea_session(sessione)


# ── find_blender ─────────────────────────────────────────────────────────────
def test_find_blender_assente(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "BLENDER_ROOTS", (str(tmp_path / "niente"),))

    assert b.find_blender() is None


def test_find_blender_override_da_env(tmp_path, monkeypatch):
    eseguibile = tmp_path / "blender.exe"
    eseguibile.write_bytes(b"MZ finto")
    monkeypatch.setenv(b.ENV_BLENDER_EXE, str(eseguibile))
    monkeypatch.setattr(b, "BLENDER_ROOTS", ())

    assert b.find_blender() == str(eseguibile)


def test_find_blender_override_inesistente_viene_ignorato(tmp_path, monkeypatch):
    monkeypatch.setenv(b.ENV_BLENDER_EXE, str(tmp_path / "non_esiste.exe"))
    monkeypatch.setattr(b, "BLENDER_ROOTS", ())

    assert b.find_blender() is None


def test_find_blender_usa_il_path(tmp_path, monkeypatch):
    eseguibile = tmp_path / "blender.exe"
    eseguibile.write_bytes(b"MZ finto")
    monkeypatch.setattr(b.shutil, "which", lambda nome: str(eseguibile))
    monkeypatch.setattr(b, "BLENDER_ROOTS", ())

    assert b.find_blender() == str(eseguibile)


def test_find_blender_cartella_standard_sceglie_la_versione_piu_nuova(tmp_path, monkeypatch):
    radice = tmp_path / "Blender Foundation"
    for versione in ("Blender 3.6", "Blender 4.2", "Blender 4.10"):
        cartella = radice / versione
        cartella.mkdir(parents=True)
        (cartella / "blender.exe").write_bytes(b"MZ finto")
    monkeypatch.setattr(b, "BLENDER_ROOTS", (str(radice),))

    trovato = b.find_blender()

    assert trovato is not None
    assert "Blender 4.10" in trovato


def test_find_blender_non_scende_troppo_in_profondo(tmp_path, monkeypatch):
    radice = tmp_path / "radice"
    profondo = radice / "a" / "b" / "c" / "d" / "e"
    profondo.mkdir(parents=True)
    (profondo / "blender.exe").write_bytes(b"MZ finto")
    monkeypatch.setattr(b, "BLENDER_ROOTS", (str(radice),))

    assert b.find_blender() is None


# ── generate_blender_script ──────────────────────────────────────────────────
def test_script_pulito_dai_fence_e_salvato(tmp_path):
    risposta = f"Ecco lo script:\n```python\n{SCRIPT_BUONO}```\nSpero vada bene."
    b._default_script_llm = lambda prompt: risposta

    percorso = b.generate_blender_script("un cubo e un cilindro", "idea-1")

    testo = Path(percorso).read_text(encoding="utf-8")
    assert Path(percorso) == b.SCRIPT_DIR / "idea-1.py"
    assert testo.startswith("import bpy")
    assert "```" not in testo and "Spero vada bene" not in testo


def test_script_contiene_l_epilogo_che_salva_il_blend(tmp_path):
    b._default_script_llm = lambda prompt: SCRIPT_BUONO
    destinazione = tmp_path / "scena.blend"

    percorso = Path(b.generate_blender_script("un cubo", "idea-2", blend_path=destinazione))
    testo = percorso.read_text(encoding="utf-8")

    assert "save_as_mainfile" in testo
    assert repr(str(destinazione)) in testo          # percorso letterale, con gli escape
    assert testo.index("primitive_cube_add") < testo.index("save_as_mainfile")


def test_script_senza_blend_path_usa_data_3d_models(tmp_path):
    b._default_script_llm = lambda prompt: SCRIPT_BUONO

    percorso = Path(b.generate_blender_script("un cubo", "idea-3"))

    assert repr(str(b.MODEL_DIR / "idea-3.blend")) in percorso.read_text(encoding="utf-8")


@pytest.mark.parametrize("vietato", [
    "import os\nos.system('calc')",
    "import subprocess",
    "import requests",
    "percorso = open('/etc/passwd').read()",
    "eval('1+1')",
    "exec('print(1)')",
    "from pathlib import Path",
    "import bpy\nimport socket",
])
def test_script_rifiuta_comandi_rete_e_file(vietato):
    b._default_script_llm = lambda prompt: vietato

    with pytest.raises(b.BlenderAgentError):
        b.generate_blender_script("un cubo", "idea-4")


def test_script_che_non_usa_bpy_viene_rifiutato(tmp_path):
    b._default_script_llm = lambda prompt: "print('ciao')"

    with pytest.raises(b.BlenderAgentError, match="bpy"):
        b.generate_blender_script("un cubo", "idea-5")
    assert list(b.SCRIPT_DIR.glob("*.py")) == []      # niente file lasciati in giro


def test_script_troppo_lungo_viene_rifiutato():
    b._default_script_llm = lambda prompt: "import bpy\n" + "x = 1\n" * 20000

    with pytest.raises(b.BlenderAgentError, match="troppo lungo"):
        b.generate_blender_script("un cubo", "idea-6")


def test_descrizione_mancante():
    with pytest.raises(b.BlenderAgentError, match="descrizione mancante"):
        b.generate_blender_script("   ", "idea-7")


@pytest.mark.parametrize("idea_id", ["", "../evil", "a/b", "idea con spazi", "x" * 81, None])
def test_idea_id_non_sicuro(idea_id):
    with pytest.raises(b.BlenderAgentError, match="idea_id non valido"):
        b.generate_blender_script("un cubo", idea_id)


def test_la_descrizione_viene_troncata_nel_prompt():
    prompts = []
    b._default_script_llm = lambda prompt: prompts.append(prompt) or SCRIPT_BUONO

    b.generate_blender_script("cubo " * 1000, "idea-8")

    assert len(prompts[0]) < 3000                       # MAX_DESCRIPTION_CHARS + istruzioni


# ── generate_fallback_3d ─────────────────────────────────────────────────────
def test_fallback_crea_un_obj_valido(tmp_path):
    percorso = Path(b.generate_fallback_3d("un cubo e un cilindro", "idea-9"))

    assert percorso == b.MODEL_DIR / "idea-9.obj"
    assert percorso.is_file() and percorso.stat().st_size > 0
    testo = percorso.read_text(encoding="utf-8", errors="replace")
    assert any(r.startswith("v ") for r in testo.splitlines())
    assert any(r.startswith("f ") for r in testo.splitlines())
    assert b._obj_valido(percorso)


def test_fallback_obj_rileggibile_da_trimesh():
    trimesh = pytest.importorskip("trimesh")

    percorso = Path(b.generate_fallback_3d("un cubo e una sfera", "idea-10"))
    mesh = trimesh.load(str(percorso), force="mesh")

    assert len(mesh.vertices) > 0 and len(mesh.faces) > 0
    assert mesh.volume > 0                             # solido chiuso, non un guscio


@pytest.mark.parametrize("descrizione,attese", [
    ("una sfera e un cubo", ["sphere", "box"]),
    ("un cilindro e un cono", ["cylinder", "cone"]),
    ("una ciambella", ["torus"]),
    ("un bel paesaggio vuoto", list(b.DEFAULT_PRIMITIVES)),
    ("", list(b.DEFAULT_PRIMITIVES)),
])
def test_primitive_dedotte_dalla_descrizione(descrizione, attese):
    assert b._primitives_from_description(descrizione) == attese


def test_primitive_limitate_a_quattro():
    tipi = b._primitives_from_description("cubo cilindro sfera cono ciambella")

    assert tipi == ["box", "cylinder", "sphere", "cone"]


def test_parole_dentro_altre_parole_non_contano():
    assert b._primitives_from_description("una casalinga qualunque") == list(b.DEFAULT_PRIMITIVES)


def test_fallback_usa_il_writer_puro_se_trimesh_non_ce(monkeypatch):
    def esplode(tipi, percorso):
        raise ImportError("No module named 'trimesh'")

    monkeypatch.setattr(b, "_write_obj_trimesh", esplode)

    percorso = Path(b.generate_fallback_3d("un cubo", "idea-11"))
    testo = percorso.read_text(encoding="utf-8")

    assert "CLIO fallback 3D" in testo
    assert testo.count("v ") == 8 and testo.count("f ") == 6      # un cubo


def test_fallback_non_valido_diventa_un_errore_chiaro(monkeypatch):
    monkeypatch.setattr(b, "_write_obj_trimesh", lambda tipi, percorso: Path(percorso).write_text(""))
    monkeypatch.setattr(b, "_write_obj_puro", lambda tipi, percorso: Path(percorso).write_text(""))

    with pytest.raises(b.BlenderAgentError, match="non è valido"):
        b.generate_fallback_3d("un cubo", "idea-12")


# ── _run_blender: solo background, mai GUI ───────────────────────────────────
def _finto_run(crea_blend=True, returncode=0, stdout="ok", errore=None):
    chiamate = []

    def runner(comando, **kwargs):
        chiamate.append((comando, kwargs))
        if errore:
            raise errore
        if crea_blend:
            Path(comando[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(comando[-1]).write_bytes(b"BLENDER-v300")
        return subprocess.CompletedProcess(comando, returncode, stdout, "")

    return runner, chiamate


def test_run_blender_usa_solo_background(tmp_path, monkeypatch):
    runner, chiamate = _finto_run()
    monkeypatch.setattr(b.subprocess, "run", runner)

    esito, _ = b._run_blender("C:\\finto\\blender.exe", "script.py", tmp_path / "out.blend")

    assert esito is True
    comando, kwargs = chiamate[0]
    assert comando[0] == "C:\\finto\\blender.exe"
    assert "--background" in comando
    assert "--factory-startup" in comando
    assert "--python" in comando and "script.py" in comando
    assert kwargs["timeout"] == b.BLENDER_TIMEOUT


def test_run_blender_senza_file_non_e_successo(tmp_path, monkeypatch):
    runner, _ = _finto_run(crea_blend=False, returncode=1, stdout="errore bpy")
    monkeypatch.setattr(b.subprocess, "run", runner)

    esito, output = b._run_blender("C:\\finto\\blender.exe", "script.py", tmp_path / "out.blend")

    assert esito is False and "errore bpy" in output


def test_run_blender_timeout_non_blocca(tmp_path, monkeypatch):
    runner, _ = _finto_run(errore=subprocess.TimeoutExpired("blender", b.BLENDER_TIMEOUT))
    monkeypatch.setattr(b.subprocess, "run", runner)

    esito, output = b._run_blender("C:\\finto\\blender.exe", "script.py", tmp_path / "out.blend")

    assert esito is False and "timeout" in output


# ── generate_3d_from_idea ────────────────────────────────────────────────────
def test_idea_inesistente_non_genera_nulla(monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: pytest.fail("non deve cercare Blender"))

    esito = b.generate_3d_from_idea("idea-fantasma")

    assert esito["ok"] is False and "non trovata" in esito["error"]


def test_senza_idee_aperte_non_genera_nulla(monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: pytest.fail("non deve cercare Blender"))

    esito = b.generate_3d_from_idea("")

    assert esito["ok"] is False and "Nessuna IdeaSession" in esito["error"]


def test_senza_blender_usa_il_fallback(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: None)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    riletta = idea.load_idea_session(sessione.idea_id)
    assert esito["ok"] is True
    assert esito["status"] == "fallback" and esito["blender"] is None
    assert esito["motivo"] == "Blender non installato"
    assert Path(esito["file"]).suffix == ".obj" and Path(esito["file"]).is_file()
    assert riletta.file_3d == esito["file"] and riletta.status_3d == "fallback"
    assert riletta.last_verified_at                        # touch()
    assert "🧊" in esito["messaggio"]


def test_senza_idea_id_usa_l_ultima_aperta(monkeypatch):
    prima = idea.create_idea_session("prima idea", "clio")
    seconda = idea.create_idea_session("seconda idea con un cubo", "clio")
    monkeypatch.setattr(b, "find_blender", lambda: None)

    esito = b.generate_3d_from_idea()

    assert esito["ok"] is True and esito["idea_id"] == seconda.idea_id
    assert idea.load_idea_session(prima.idea_id).file_3d == ""
    assert idea.load_idea_session(seconda.idea_id).file_3d != ""


def test_usa_l_osservazione_quando_c_e(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: None)
    usate = []
    monkeypatch.setattr(b, "generate_fallback_3d",
                        lambda descrizione, idea_id: usate.append(descrizione) or
                        str(b.MODEL_DIR / "x.obj"))

    b.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    Path(b.MODEL_DIR / "x.obj").write_text("v 0 0 0\nf 1 1 1\n")

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is True and esito["fonte_descrizione"] == "osservazione"
    assert usate == [sessione.last_observation]


def test_senza_osservazione_usa_l_obiettivo(monkeypatch):
    sessione = idea.create_idea_session("una torre e una palla", "clio")
    monkeypatch.setattr(b, "find_blender", lambda: None)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is True and esito["fonte_descrizione"] == "obiettivo"
    assert esito["descrizione"] == "una torre e una palla"


def test_con_blender_genera_il_blend(sessione, monkeypatch):
    runner, chiamate = _finto_run()
    monkeypatch.setattr(b, "find_blender", lambda: "C:\\finto\\blender.exe")
    monkeypatch.setattr(b.subprocess, "run", runner)
    monkeypatch.setattr(b, "_default_script_llm", lambda prompt: SCRIPT_BUONO)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    riletta = idea.load_idea_session(sessione.idea_id)
    assert esito["ok"] is True and esito["status"] == "blender"
    assert Path(esito["file"]).suffix == ".blend" and Path(esito["file"]).is_file()
    assert riletta.file_3d == esito["file"] and riletta.status_3d == "blender"
    assert "Blender" in esito["messaggio"]

    comando = chiamate[0][0]
    assert comando[0] == "C:\\finto\\blender.exe" and "--background" in comando
    assert Path(comando[comando.index("--python") + 1]).is_file()     # script su disco
    assert not list(b.MODEL_DIR.glob("*.obj"))                        # nessun fallback


def test_blender_che_fallisce_ripiega_sul_fallback(sessione, monkeypatch):
    runner, _ = _finto_run(crea_blend=False, returncode=1, stdout="Traceback bpy")
    monkeypatch.setattr(b, "find_blender", lambda: "C:\\finto\\blender.exe")
    monkeypatch.setattr(b.subprocess, "run", runner)
    monkeypatch.setattr(b, "_default_script_llm", lambda prompt: SCRIPT_BUONO)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is True and esito["status"] == "fallback"
    assert "non ha prodotto il .blend" in esito["motivo"]
    assert idea.load_idea_session(sessione.idea_id).status_3d == "fallback"


def test_blender_in_timeout_ripiega_sul_fallback(sessione, monkeypatch):
    runner, _ = _finto_run(errore=subprocess.TimeoutExpired("blender", 1))
    monkeypatch.setattr(b, "find_blender", lambda: "C:\\finto\\blender.exe")
    monkeypatch.setattr(b.subprocess, "run", runner)
    monkeypatch.setattr(b, "_default_script_llm", lambda prompt: SCRIPT_BUONO)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["status"] == "fallback" and "timeout" in esito["motivo"]


def test_script_rifiutato_ripiega_sul_fallback(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: "C:\\finto\\blender.exe")
    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: pytest.fail("non deve eseguire"))
    monkeypatch.setattr(b, "_default_script_llm", lambda prompt: "import os\nos.system('calc')")

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is True and esito["status"] == "fallback"
    assert "script Blender non generato" in esito["motivo"]


def test_gemini_che_esplode_ripiega_sul_fallback(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: "C:\\finto\\blender.exe")

    def esplode(prompt):
        raise RuntimeError("quota Gemini esaurita")

    monkeypatch.setattr(b, "_default_script_llm", esplode)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is True and esito["status"] == "fallback"
    assert "quota Gemini" in esito["motivo"]


def test_fallback_che_esplode_e_un_errore_chiaro(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: None)

    def esplode(descrizione, idea_id):
        raise b.BlenderAgentError("disco pieno")

    monkeypatch.setattr(b, "generate_fallback_3d", esplode)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is False and "generazione 3D fallita" in esito["error"]
    assert idea.load_idea_session(sessione.idea_id).file_3d == ""


def test_stato_non_salvabile_e_un_errore_chiaro(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: None)

    def esplode(*argomenti, **kwargs):
        raise idea.IdeaSessionError("store in sola lettura")

    monkeypatch.setattr(b.idea_session, "update_idea_session", esplode)

    esito = b.generate_3d_from_idea(sessione.idea_id)

    assert esito["ok"] is False and "stato non salvato" in esito["error"]
    assert Path(esito["file"]).is_file()               # il modello esiste comunque


# ── idea_session: nuovi campi retro-compatibili ─────────────────────────────
def test_idea_session_vecchia_senza_campi_3d():
    vecchia = b.idea_session.IdeaSession.from_dict(
        {"idea_id": "abc-123", "project_id": "clio", "user_goal": "vecchia idea",
         "last_observation": "un cubo"})

    assert vecchia.file_3d == "" and vecchia.status_3d == ""


def test_idea_session_accetta_i_campi_3d():
    sessione = idea.IdeaSession(idea_id="abc-124", project_id="clio", user_goal="idea",
                                file_3d="C:\\x\\m.obj", status_3d="FALLBACK")

    assert sessione.status_3d == "fallback"
    assert "modello 3D [fallback]" in sessione.describe()


def test_idea_session_status_3d_sconosciuto_viene_azzerato():
    sessione = idea.IdeaSession(idea_id="abc-125", project_id="clio", user_goal="idea",
                                status_3d="boh")

    assert sessione.status_3d == ""


# ── Integrazione: registro, metadati, policy, conferma ─────────────────────
def test_azione_registrata_nel_registro_unico():
    assert registry.AZIONI_MAP[b.ACTION_NAME] is b.generate_3d_from_idea


def test_metadati_llm_presenti():
    from jarvis_brain import TOOLS_METADATA

    assert TOOLS_METADATA[b.ACTION_NAME]["params"] == ["idea_id"]
    assert "3D" in TOOLS_METADATA[b.ACTION_NAME]["desc"]


def test_policy_e_sensitive_e_chiede_conferma():
    assert policy.classify(b.ACTION_NAME) == "sensitive"
    assert b.ACTION_NAME not in policy.READ_ONLY
    assert b.ACTION_NAME not in policy.REVERSIBLE


@pytest.mark.parametrize("origine", ["voice", "web", "api", "telegram"])
def test_guarded_call_con_conferma_esegue(sessione, monkeypatch, origine):
    monkeypatch.setattr(b, "find_blender", lambda: None)
    richieste = []

    def conferma(nome, args):
        richieste.append((nome, args))
        return "yes"

    esito = policy.guarded_call(
        b.ACTION_NAME, registry.AZIONI_MAP[b.ACTION_NAME],
        {"idea_id": sessione.idea_id}, confirm=conferma, origin=origine)

    assert richieste and richieste[0][0] == b.ACTION_NAME
    assert esito["ok"] is True
    assert idea.load_idea_session(sessione.idea_id).status_3d == "fallback"


def test_guarded_call_senza_conferma_non_scrive_nulla(sessione, monkeypatch):
    monkeypatch.setattr(b, "find_blender", lambda: pytest.fail("non deve generare"))

    esito = policy.guarded_call(
        b.ACTION_NAME, registry.AZIONI_MAP[b.ACTION_NAME],
        {"idea_id": sessione.idea_id}, confirm=lambda nome, args: "no", origin="web")

    assert "annullata" in esito.lower()
    assert idea.load_idea_session(sessione.idea_id).file_3d == ""
    assert not b.MODEL_DIR.exists()


def test_la_conferma_non_si_puo_forzare_dagli_argomenti(sessione, monkeypatch):
    """`confirmed`/`force` sono chiavi vietate: il tool non le riceve mai."""
    monkeypatch.setattr(b, "find_blender", lambda: None)

    esito = policy.guarded_call(
        b.ACTION_NAME, registry.AZIONI_MAP[b.ACTION_NAME],
        {"idea_id": sessione.idea_id, "confirmed": True, "force": True},
        confirm=lambda nome, args: "no", origin="web")

    assert "annullata" in esito.lower()
    assert idea.load_idea_session(sessione.idea_id).file_3d == ""


def test_il_modulo_non_apre_mai_la_gui():
    """Blender gira solo in background: nessun flag che apra una finestra."""
    import inspect

    sorgente = inspect.getsource(b)

    assert "--background" in sorgente
    assert "--factory-startup" in sorgente
    for vietato in ("os.startfile", "blender-launcher", "wm.open_mainfile(", "bpy.ops.wm.save_mainfile"):
        assert vietato not in sorgente


def test_describe_blender_agent():
    testo = b.describe_blender_agent()

    assert "Blender Agent" in testo and "data/3d_models" in testo
