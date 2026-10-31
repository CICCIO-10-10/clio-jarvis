"""knowledge_curator.py — R7: Vault Intelligence (sola lettura).

Scansiona un vault Obsidian e produce PROPOSTE di pulizia: duplicati (titolo
uguale CON contenuto quasi identico, oppure contenuto molto simile), note
obsolete (non modificate da N giorni) e link wiki `[[nota]]` rotti. Non modifica,
sposta, rinomina o cancella mai file del vault: l'unica scrittura è il report,
in una cartella esterna al vault (default
`clio/data/vault_proposals_YYYY-MM-DD.md`).

Contratto:
    data = scan_vault("C:/vault")            # dict con note, titoli, link, mtime
    find_duplicates(data["notes"])           # lista di coppie sospette
    find_stale_notes(data["notes"], days=90) # lista di note vecchie
    find_broken_links(data["notes"])         # lista di link inesistenti
    propose_actions(problems)                # lista di frasi leggibili
    generate_report(vault, output_dir)       # Path del report scritto

Azioni CLIO (READ_ONLY in policy.py):
    scansiona_vault(vault_path="", days=90) -> str
    proponi_pulizia_vault(vault_path="", days=90) -> str

Sicurezza:
  * il report non può essere scritto dentro il vault (ValueError);
  * nessun file del vault viene aperto in scrittura;
  * gli errori tornano come testo "ERRORE: ..." nelle azioni, senza eccezioni.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("ClioVault")

try:  # PyYAML e' gia' usato da The Scribe; fallback senza dipendenza
    import yaml
except Exception:  # pragma: no cover - dipende dall'ambiente
    yaml = None  # type: ignore[assignment]

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

DEFAULT_STALE_DAYS = 90
SIMILARITY_THRESHOLD = 0.90     # Jaccard sui token del corpo
TITLE_SIMILARITY_THRESHOLD = 0.85   # R7.1: il match per titolo esige contenuti simili
MIN_CONTENT_TOKENS = 5          # sotto questa soglia non si giudica "simile"
MAX_INSPECT_PROPOSALS = 200     # limite del testo restituito al LLM

# R7.1 — Titoli generici: esistono legittimamente in decine di cartelle diverse
# (_MOC per area, "Agenda", "pensiero_del_giorno", ...). Il solo titolo uguale
# NON basta: senza questa lista il vault reale produceva 539 falsi duplicati.
# Confrontati dopo normalizzazione: minuscolo, senza accenti, `_` -> spazio.
EXCLUDED_TITLES = frozenset({
    "moc", "index", "indice", "leggimi", "readme",
    "agenda", "pensiero del giorno", "curiosita del giorno",
    "note", "note varie", "todo", "da fare", "template", "modello",
    "senza titolo", "untitled", "bozza",
})

EXCLUDED_DIRS = {".obsidian", ".git", ".trash", "node_modules", "__pycache__"}
WIKI_LINK = re.compile(r"!?\[\[([^\]\n]+)\]\]")
H1 = re.compile(r"^\s{0,3}#(?!#)\s+(.+?)\s*#*\s*$", re.MULTILINE)
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
                         re.DOTALL)


# ── Parsing ───────────────────────────────────────────────────────────────────
def _read_text(path: Path) -> str:
    """Legge un file Markdown senza mai sollevare per encoding inatteso."""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Estrae il frontmatter YAML; se assente o invalido non perde il corpo."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    data: Dict[str, Any] = {}
    if yaml is not None:
        try:
            loaded = yaml.safe_load(raw)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    if not data:  # fallback minimale: chiave: valore
        for line in raw.splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition(":")
                data[key.strip()] = value.strip().strip('"').strip("'")
    return data, text[match.end():]


def _extract_title(frontmatter: Dict[str, Any], body: str, path: Path) -> str:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading = H1.search(body)
    if heading:
        return heading.group(1).strip()
    return path.stem


def _extract_links(text: str) -> List[str]:
    """Target grezzi dei wiki-link, nell'ordine di apparizione."""
    return [match.group(1).strip() for match in WIKI_LINK.finditer(text)
            if match.group(1).strip()]


def _is_excluded(path: Path, vault: Path) -> bool:
    try:
        parts = path.relative_to(vault).parts
    except ValueError:
        return True
    return any(part.startswith(".") or part in EXCLUDED_DIRS for part in parts[:-1])


def _normalize_title(title: Any) -> str:
    """Titolo confrontabile: minuscolo, senza accenti, spazi compattati."""
    if not isinstance(title, str):
        return ""
    testo = unicodedata.normalize("NFKD", title)
    testo = "".join(ch for ch in testo if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", testo).strip().casefold()


def _title_key(title: Any) -> str:
    """Chiave di confronto titolo: `_MOC` e `MOC` coincidono, accenti ignorati."""
    key = _normalize_title(title).replace("_", " ")
    return re.sub(r"\s+", " ", key).strip()


def _tokens(text: str) -> set:
    return set(re.findall(r"\w+", (text or "").casefold()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rel(path: Path, vault: Path) -> str:
    return path.relative_to(vault).as_posix()


# ── Scansione ─────────────────────────────────────────────────────────────────
def scan_vault(vault_path: str) -> Dict[str, Any]:
    """Legge i .md del vault: frontmatter, titolo, link e data di modifica.

    Non scrive nulla. Le cartelle nascoste/.obsidian/node_modules sono ignorate.
    Ritorna `{"vault", "scanned_at", "count", "notes"}`; ogni nota contiene
    `path` (relativo, posix), `title`, `frontmatter`, `modified` (ISO), `mtime`,
    `size`, `links` e `content` (corpo senza frontmatter).
    """
    if not isinstance(vault_path, (str, os.PathLike)) or not str(vault_path).strip():
        raise ValueError("vault_path mancante")
    vault = Path(vault_path).expanduser()
    if not vault.is_dir():
        raise ValueError(f"vault non trovato o non e' una cartella: {vault}")
    vault = vault.resolve()

    notes: List[Dict[str, Any]] = []
    for path in sorted(vault.rglob("*.md")):
        try:
            if not path.is_file() or _is_excluded(path, vault):
                continue
            text = _read_text(path)
            stat = path.stat()
        except OSError as exc:
            logger.warning("Nota illeggibile %s: %s", path, exc)
            continue
        frontmatter, body = _parse_frontmatter(text)
        notes.append({
            "path": _rel(path, vault),
            "title": _extract_title(frontmatter, body, path),
            "frontmatter": frontmatter,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "mtime": stat.st_mtime,
            "size": stat.st_size,
            "links": _extract_links(text),
            "content": body,
        })

    return {
        "vault": str(vault),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(notes),
        "notes": notes,
    }


# ── Analisi ───────────────────────────────────────────────────────────────────
def find_duplicates(notes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Coppie realmente duplicate: titolo uguale + contenuto quasi identico.

    Regole R7.1 (riducono i falsi positivi sul vault reale):
      * i titoli generici (`_MOC`, `Agenda`, `pensiero_del_giorno`, `Note`,
        `TODO`, ...) sono esclusi dal match per titolo: sono legittimamente
        ripetuti in decine di cartelle;
      * il match per titolo vale solo se anche il contenuto è quasi identico
        (Jaccard >= `TITLE_SIMILARITY_THRESHOLD`, 0.85);
      * resta il match per contenuto (Jaccard >= `SIMILARITY_THRESHOLD`, 0.90),
        che vale anche per i titoli esclusi: se il testo è identico è comunque
        un duplicato.
    Le note con meno di `MIN_CONTENT_TOKENS` parole non vengono giudicate.
    """
    notes = list(notes or [])
    found: List[Dict[str, Any]] = []
    seen: set = set()

    def add(a: Dict[str, Any], b: Dict[str, Any], reason: str, similarity: float) -> None:
        key = tuple(sorted((str(a.get("path")), str(b.get("path")))))
        if key in seen:
            return
        seen.add(key)
        found.append({
            "a": key[0], "b": key[1], "reason": reason,
            "similarity": round(float(similarity), 3),
            "title_a": a.get("title"), "title_b": b.get("title"),
        })

    ordered = sorted(notes, key=lambda note: str(note.get("path")))
    tokens_by_path = {str(note.get("path")): _tokens(note.get("content"))
                      for note in ordered}

    # 1) Titoli uguali, ma SOLO se il contenuto conferma (titoli non generici).
    by_title: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for note in ordered:
        key = _title_key(note.get("title"))
        if key and key not in EXCLUDED_TITLES:
            by_title[key].append(note)
    for group in by_title.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a_tokens = tokens_by_path[str(group[i].get("path"))]
                b_tokens = tokens_by_path[str(group[j].get("path"))]
                if len(a_tokens) < MIN_CONTENT_TOKENS or \
                        len(b_tokens) < MIN_CONTENT_TOKENS:
                    continue
                similarity = _jaccard(a_tokens, b_tokens)
                if similarity >= TITLE_SIMILARITY_THRESHOLD:
                    add(group[i], group[j], "titolo", similarity)

    # 2) Contenuti quasi identici, indipendentemente dal titolo.
    token_sets = [tokens_by_path[str(note.get("path"))] for note in ordered]
    for i in range(len(ordered)):
        a_tokens = token_sets[i]
        if len(a_tokens) < MIN_CONTENT_TOKENS:
            continue
        for j in range(i + 1, len(ordered)):
            b_tokens = token_sets[j]
            if len(b_tokens) < MIN_CONTENT_TOKENS:
                continue
            similarity = _jaccard(a_tokens, b_tokens)
            if similarity >= SIMILARITY_THRESHOLD:
                add(ordered[i], ordered[j], "contenuto", similarity)

    return sorted(found, key=lambda item: (item["a"], item["b"]))


def find_stale_notes(notes: Iterable[Dict[str, Any]],
                     days: int = DEFAULT_STALE_DAYS) -> List[Dict[str, Any]]:
    """Note non modificate da piu' di `days` giorni, dalla piu' vecchia."""
    if not isinstance(days, int) or days <= 0:
        raise ValueError("days deve essere un intero positivo")
    now = datetime.now()
    stale: List[Dict[str, Any]] = []
    for note in notes or []:
        try:
            modified = datetime.fromtimestamp(float(note.get("mtime")))
        except (TypeError, ValueError, OSError):
            continue
        age = (now - modified).days
        if age > days:
            stale.append({
                "path": note.get("path"),
                "title": note.get("title"),
                "modified": modified.isoformat(timespec="seconds"),
                "age_days": age,
                "threshold_days": days,
            })
    return sorted(stale, key=lambda item: (-item["age_days"], str(item["path"])))


def _link_target(raw: str) -> str:
    """Normalizza un target wiki: via alias, heading/block e .md finale."""
    target = (raw or "").split("|", 1)[0]
    target = target.split("#", 1)[0].split("^", 1)[0].strip()
    target = target.replace("\\", "/").lstrip("/").strip()
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target


def find_broken_links(notes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Wiki-link che non corrispondono a nessuna nota del vault.

    Il confronto e' case-insensitive, su percorso relativo senza .md e su nome
    file. Link esterni (http/mailto), ancore pure (`[[#sezione]]`) e allegati
    (estensione non .md) vengono ignorati: qui interessano i link tra note.
    """
    notes = list(notes or [])
    paths = {str(note.get("path") or "").casefold()[:-3]
             for note in notes if str(note.get("path") or "").endswith(".md")}
    names = {Path(str(note.get("path") or "")).stem.casefold() for note in notes}

    broken: List[Dict[str, Any]] = []
    seen: set = set()
    for note in notes:
        note_path = str(note.get("path") or "")
        for raw in note.get("links") or []:
            if not isinstance(raw, str):
                continue
            target = _link_target(raw)
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if "." in Path(target).name and not target.lower().endswith(".md"):
                continue  # allegato: non e' una nota
            key = target.casefold()
            if key in paths or key in names:
                continue
            if (note_path, key) in seen:
                continue
            seen.add((note_path, key))
            broken.append({"path": note_path, "link": raw, "target": target})

    return sorted(broken, key=lambda item: (item["path"], item["target"].casefold()))


# ── Proposte e report ─────────────────────────────────────────────────────────
def propose_actions(problems: Dict[str, Any]) -> List[str]:
    """Trasforma i problemi in frasi operative, senza eseguirle."""
    proposals: List[str] = []
    for duplicate in (problems or {}).get("duplicates", []):
        percent = round(100 * float(duplicate.get("similarity") or 0))
        if duplicate.get("reason") == "titolo":
            proposals.append(
                f"Unisci '{duplicate['a']}' con '{duplicate['b']}' "
                f"(titoli uguali, contenuto simile al {percent}%)")
        else:
            proposals.append(
                f"Unisci '{duplicate['a']}' con '{duplicate['b']}' "
                f"(contenuto simile al {percent}%)")
    for stale in (problems or {}).get("stale", []):
        proposals.append(
            f"Archivia '{stale['path']}' "
            f"(non modificata da {stale['age_days']} giorni)")
    for broken in (problems or {}).get("broken_links", []):
        proposals.append(
            f"Correggi il link '[[{broken['target']}]]' in '{broken['path']}'")
    return proposals


def _collect_problems(notes: List[Dict[str, Any]], days: int) -> Dict[str, Any]:
    return {
        "duplicates": find_duplicates(notes),
        "stale": find_stale_notes(notes, days),
        "broken_links": find_broken_links(notes),
    }


def _summary_text(data: Dict[str, Any], problems: Dict[str, Any],
                  proposals: List[str]) -> str:
    lines = [
        f"Vault: {data['vault']}",
        f"Note scansionate: {data['count']}",
        f"Problemi: {len(problems['duplicates'])} duplicati, "
        f"{len(problems['stale'])} note obsolete, "
        f"{len(problems['broken_links'])} link rotti",
    ]
    if proposals:
        lines.append("")
        lines.append("Proposte (nessuna modifica eseguita):")
        lines.extend(f"- {proposal}" for proposal in proposals[:MAX_INSPECT_PROPOSALS])
        if len(proposals) > MAX_INSPECT_PROPOSALS:
            lines.append(f"... e altre {len(proposals) - MAX_INSPECT_PROPOSALS} proposte.")
    else:
        lines.append("Nessun problema rilevato.")
    return "\n".join(lines)


def _resolve_vault(vault_path: str = "") -> Path:
    """Vault esplicito, poi CLIO_VAULT_PATH, poi la config di The Scribe."""
    raw = (vault_path or "").strip() or (os.getenv("CLIO_VAULT_PATH") or "").strip()
    if not raw:
        try:
            from scribe_control import load_config, scribe_config_path
            config = load_config(scribe_config_path())
            raw = str((config.get("obsidian") or {}).get("vault") or "").strip()
        except Exception:
            raw = ""
    if not raw:
        raise ValueError(
            "vault non specificato: passa vault_path o imposta CLIO_VAULT_PATH")
    vault = Path(raw).expanduser()
    if not vault.is_dir():
        raise ValueError(f"vault non trovato o non e' una cartella: {vault}")
    return vault.resolve()


def _report_text(data: Dict[str, Any], problems: Dict[str, Any],
                 proposals: List[str], days: int) -> str:
    oggi = datetime.now().strftime("%Y-%m-%d")
    righe = [
        f"# Proposte di pulizia del vault — {oggi}",
        "",
        f"- Vault: `{data['vault']}`",
        f"- Note scansionate: {data['count']}",
        f"- Duplicati: {len(problems['duplicates'])}",
        f"- Note obsolete (> {days} giorni): {len(problems['stale'])}",
        f"- Link rotti: {len(problems['broken_links'])}",
        "",
        "> Report generato in sola lettura: nessun file del vault è stato "
        "modificato, spostato o cancellato.",
        "",
    ]
    sections = (
        ("Duplicati", problems["duplicates"]),
        ("Note obsolete", problems["stale"]),
        ("Link rotti", problems["broken_links"]),
    )
    for title, items in sections:
        righe.append(f"## {title}")
        if items:
            righe.extend(f"- {proposal}" for proposal in _proposals_for(title, problems))
        else:
            righe.append("- Nessun problema rilevato.")
        righe.append("")
    righe.append("## Proposte complete")
    if proposals:
        righe.extend(f"{index}. {proposal}" for index, proposal in enumerate(proposals, 1))
    else:
        righe.append("Nessuna proposta: il vault non presenta i problemi cercati.")
    righe.append("")
    return "\n".join(righe)


def _proposals_for(section: str, problems: Dict[str, Any]) -> List[str]:
    """Proposte della sola sezione richiesta (per il report)."""
    if section == "Duplicati":
        return propose_actions({"duplicates": problems["duplicates"]})
    if section == "Note obsolete":
        return propose_actions({"stale": problems["stale"]})
    if section == "Link rotti":
        return propose_actions({"broken_links": problems["broken_links"]})
    return []


def _atomic_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)
    return path


def generate_report(vault_path: str, output_dir: Optional[str] = None,
                    days: int = DEFAULT_STALE_DAYS) -> Path:
    """Scrive il report delle proposte in una cartella FUORI dal vault.

    Ritorna il Path del file `vault_proposals_YYYY-MM-DD.md`. Se `output_dir`
    e' dentro il vault solleva ValueError: il vault non va mai toccato.
    """
    vault = _resolve_vault(vault_path)
    out_dir = Path(output_dir).expanduser().resolve() if output_dir else DATA_DIR
    if out_dir == vault or out_dir.is_relative_to(vault):
        raise ValueError("output_dir non può stare dentro il vault (sola lettura)")

    data = scan_vault(str(vault))
    problems = _collect_problems(data["notes"], days)
    proposals = propose_actions(problems)
    filename = f"vault_proposals_{datetime.now().strftime('%Y-%m-%d')}.md"
    return _atomic_write(out_dir / filename,
                         _report_text(data, problems, proposals, days))


# ── Azioni CLIO (READ_ONLY in policy.py) ──────────────────────────────────────
def scansiona_vault(vault_path: str = "", days: int = DEFAULT_STALE_DAYS) -> str:
    """Scansiona il vault e riporta i problemi, senza scrivere nulla."""
    try:
        vault = _resolve_vault(vault_path)
        data = scan_vault(str(vault))
        problems = _collect_problems(data["notes"], days)
        return _summary_text(data, problems, propose_actions(problems))
    except Exception as exc:
        logger.warning("Scansione vault fallita: %s: %s", type(exc).__name__, exc)
        return f"ERRORE: scansione vault non eseguita: {exc}"


def proponi_pulizia_vault(vault_path: str = "",
                          days: int = DEFAULT_STALE_DAYS) -> str:
    """Genera il report di proposte in `data/` e ne riporta il percorso."""
    try:
        vault = _resolve_vault(vault_path)
        report = generate_report(str(vault), days=days)
        data = scan_vault(str(vault))
        problems = _collect_problems(data["notes"], days)
        proposals = propose_actions(problems)
        return (f"Report proposte: {report}\n"
                f"Nessuna modifica eseguita sul vault.\n\n"
                + _summary_text(data, problems, proposals))
    except Exception as exc:
        logger.warning("Report vault fallito: %s: %s", type(exc).__name__, exc)
        return f"ERRORE: report vault non generato: {exc}"
