#!/usr/bin/env python3
"""Generate a repository topology map (Mermaid) + entity inventory.

Part of the repo-doc-miner skill (v2, topology-first). Given a repository
root, this script:

  1. Detects entity types present in the repo (modules, skills, agents,
     commands, MCP connectors, managed agents, language packages, scripts…).
  2. Extracts dependency edges between entities by scanning each entity's
     primary file for references (``system.file:``, ``from_plugin:``,
     ``skills.path:``, ``references:``, relative-path tokens, ``import``/
     ``require`` statements).
  3. Writes a Markdown file with two Mermaid diagrams (structural graph +
     dependency-edge graph), an entity inventory table, and a per-module
     deep-dive scaffold.

The output is the *first deliverable* of the skill; a human should refine
the auto-detected edges by reading the most important manifests.

Usage
-----
    python scripts/gen_topology.py <repo-root> \
        [--out docs/guides/topology.md] \
        [--project-name "MyProject"] \
        [--max-edges 200]

Exit codes
----------
    0  success
    1  repo root does not exist
    2  could not write output file
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", "site-packages", "*.egg-info",
}

TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".py", ".pyi", ".js", ".jsx", ".ts",
    ".tsx", ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".sh",
    ".bash", ".rs", ".go", ".java", ".rb", ".lua", ".cpp", ".c", ".h",
    ".m",
}

# SciML entity-kind hints, scanned over each script entity's own content.
# A script may match zero or one kind; the result is a secondary label
# (Entity.extra["sciml_kind"]) layered on top of the primary kind="script".
SCIML_PATTERNS = {
    "pinn": [
        r"torch\.autograd\.grad",
        r"\b(pde_loss|physics_loss|residual_loss|f_pred)\b",
        r"\bcollocation_points?\b",
    ],
    "rom": [
        r"\b(POD|SVD)\b",
        r"\b(beta_vae|BetaVAE|VariationalAutoencoder)\b",
        r"\blatent_dim\b",
    ],
    "rl-agent": [
        r"\bclass\s+\w*Agent\b",
        r"\btensorforce\b|\bstable_baselines3?\b",
        r"\bPPO\b|\bDDPG\b",
    ],
    "rl-env": [
        r"class\s+\w+\(.*gym\.Env.*\)",
        r"def\s+step\(self,\s*action\)",
        r"def\s+reset\(self",
    ],
    "xai": [
        r"\bimport\s+shap\b",
        r"\bcaptum\b",
        r"\bGradCAM\b|\bintegrated_gradients\b",
    ],
    "physics-sim": [
        r"\bode45\b|\bode15s\b|\bodefun\b",
        r"\bpdepe\b",
        r"function\s+\w+\s*=\s*\w+_odefun\(",
    ],
}


def detect_sciml_kind(content: str) -> str | None:
    for kind, patterns in SCIML_PATTERNS.items():
        if any(re.search(p, content) for p in patterns):
            return kind
    return None


_DOCSTRING_PATTERN = re.compile(r'^\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', re.S)
_SHEBANG_PATTERN = re.compile(r'^#!')
_CODING_DECL_PATTERN = re.compile(r'^#.*coding[:=]')


def extract_summary(content: str, max_len: int = 160) -> str | None:
    """Cheap, unverified one-liner from a leading docstring/comment block,
    so an entity Phase 2 hasn't walked yet still ships a source-grounded
    description instead of a content-free TODO placeholder."""
    lines = content.lstrip().splitlines()
    # Skip a leading run of single-line comments (shebang, `# -*- coding:
    # ... -*-`) that precede the real docstring — otherwise the docstring
    # match below never fires and the comment fallback grabs the encoding
    # declaration instead of any actual description.
    idx = 0
    while idx < len(lines) and lines[idx].strip().startswith(("#", "%", "//")):
        idx += 1
    m = _DOCSTRING_PATTERN.match("\n".join(lines[idx:]).lstrip())
    if m:
        text = " ".join(m.group(1).split())
        return text[:max_len] + ("…" if len(text) > max_len else "")
    # Fallback for .m / .sh: leading comment block. Skip a leading shebang
    # (`#!...`) or PEP 263 encoding declaration (`# -*- coding: ... -*-`)
    # first, same boilerplate the docstring branch above already skips —
    # otherwise a Python file with only those two lines and no docstring
    # or real comment ships them as a "summary" instead of returning None.
    fb_idx = 0
    while fb_idx < len(lines):
        s = lines[fb_idx].strip()
        if _SHEBANG_PATTERN.match(s) or _CODING_DECL_PATTERN.match(s):
            fb_idx += 1
            continue
        break
    # Bounded to a genuinely *leading* comment block: the first line after
    # the shebang/encoding skip that is not itself a comment line (blank
    # lines included) ends the scan immediately. This replaces the old
    # control flow, which kept scanning past arbitrary non-comment code
    # looking for any later comment line — picking up commented-out dead
    # code or mid-file remarks as if they were a file-level description.
    comment_lines = []
    for line in lines[fb_idx:]:
        s = line.strip()
        if s.startswith(("#", "%", "//")):
            comment_lines.append(s.lstrip("#%/ "))
        else:
            break
    if comment_lines:
        text = " ".join(comment_lines)
        return text[:max_len] + ("…" if len(text) > max_len else "")
    return None


# Loss/optimizer call signatures across the common SciML frameworks (Keras
# .compile(loss=...), tf.keras.losses.*, torch.nn.*Loss, and a bare
# criterion=... assignment). Not exhaustive, but targets the exact "loss
# function unread" TODO type that keeps coming up during Phase 2.
_TRAINING_CONFIG_PATTERN = re.compile(
    r"(?:\.compile\(\s*loss\s*=\s*['\"]?([A-Za-z_.]+)"
    r"|tf\.keras\.losses\.(\w+)"
    r"|nn\.(\w*Loss)\("
    r"|criterion\s*=\s*(\w+)\()"
)


def detect_training_config(content: str) -> list[str]:
    found = set()
    for m in _TRAINING_CONFIG_PATTERN.finditer(content):
        found.update(g for g in m.groups() if g)
    return sorted(found)


# Keywords whose presence marks a "source -> copy" (single-source-of-truth)
# relationship rather than an ordinary "uses" relationship.
COPY_KEYWORDS = ("system.file", "from_plugin", "skills.path", "skills:", "copied", "synced")

MAX_FILE_READ = 200_000  # bytes


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Entity:
    name: str
    kind: str
    rel_path: str          # repo-relative path of the primary file/dir
    module: str            # top-level module (first path segment)
    size: int = 0
    extra: dict = field(default_factory=dict)


@dataclass
class Edge:
    src: str               # entity id
    dst: str               # entity id
    kind: str              # "source->copy" | "uses" | "data-contract"
    label: str = ""        # optional annotation (e.g. matched filename prefix)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def sanitize_id(text: str) -> str:
    """Make a Mermaid-safe node id (no spaces / punctuation)."""
    s = re.sub(r"[^0-9A-Za-z_]+", "_", text)
    return s.strip("_") or "node"


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES


def iter_text_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.relative_to(root).parts):
            continue
        if not is_text_file(p):
            continue
        yield p


def safe_read(path: Path) -> str:
    try:
        data = path.read_bytes()[:MAX_FILE_READ]
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Vendored-subtree detection
# --------------------------------------------------------------------------

# A dropped-in copy of a third-party library (e.g. libs/shap/, or
# code/py_bin/py_packages/{shap,slicer,cloudpickle}/) is not the repo's own
# code. Left unfiltered, it gets scanned by the §3a script walk and its
# generic save()/load() methods flood §3c's data-contract detector with
# false positives (32/32 on XAI_turbulentchannel_3d_simplified, all noise).
_VENDOR_DIR_NAMES = {
    "vendor", "vendored", "third_party", "thirdparty",
    "external", "_vendor", "site-packages",
}
_PACKAGE_MARKERS = ("PKG-INFO", "setup.py", "pyproject.toml",
                     "LICENSE", "LICENSE.txt")


def is_vendored_subtree(d: Path) -> bool:
    if d.name.lower() in _VENDOR_DIR_NAMES:
        return True
    if (d / "__init__.py").exists() and any((d / m).exists() for m in _PACKAGE_MARKERS):
        return True
    if any(d.glob("*.dist-info")) or any(d.glob("*.egg-info")):
        return True
    # Full upstream checkout dropped in whole: the package marker sits at
    # the checkout root (d) while the importable package lives one level
    # down, under a same-named subdirectory (Diff-SPORT/libs/shap/ has
    # setup.py + LICENSE at its own root and the real "shap" package at
    # libs/shap/shap/__init__.py — neither directory alone satisfies the
    # "both at the same level" rule above).
    if any((d / m).exists() for m in _PACKAGE_MARKERS) and (d / d.name / "__init__.py").exists():
        return True
    return False


def find_vendored_dirs(root: Path) -> set[Path]:
    vendored: set[Path] = set()
    for d in root.rglob("*"):
        if not d.is_dir():
            continue
        if any(part in IGNORE_DIRS for part in d.relative_to(root).parts):
            continue
        if any(v in d.parents for v in vendored):
            continue  # already covered by an ancestor match
        if is_vendored_subtree(d):
            vendored.add(d)
    return vendored


# --------------------------------------------------------------------------
# Entity detection
# --------------------------------------------------------------------------

def detect_entities(root: Path) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    vendored_dirs = find_vendored_dirs(root)

    def is_vendored(p: Path) -> bool:
        return p in vendored_dirs or any(v in p.parents for v in vendored_dirs)

    top_modules = [p for p in root.iterdir()
                   if p.is_dir() and p.name not in IGNORE_DIRS and not is_vendored(p)]

    # 1) Top-level directories are modules.
    for mod in top_modules:
        entities.append(Entity(mod.name, "module", mod.name, mod.name))

    # 2) Walk for typed entities.
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if is_vendored(p):
            continue
        name = p.name

        if name == "SKILL.md":
            kind, ent = "skill", p.parent.name if len(rel.parts) > 1 else "repo-doc-miner"
            _add(entities, seen, Entity(ent, kind, str(rel),
                                        rel.parts[0] if len(rel.parts) > 1 else "(root)",
                                        size=p.stat().st_size))
        elif name == "agent.yaml" or name == "agent.yml":
            kind, ent = "managed-agent", p.parent.name
            _add(entities, seen, Entity(ent, kind, str(rel), rel.parts[0]))
        elif name == ".mcp.json":
            servers = _count_mcp_servers(p)
            ent = p.parent.name
            _add(entities, seen, Entity(f"{ent} (mcp)", "connectors", str(rel),
                                        rel.parts[0], extra={"servers": servers}))
        elif name == "pyproject.toml" or name == "setup.py" or name == "setup.cfg":
            if p.parent != root:  # package, not repo root metadata
                ent = p.parent.name
                _add(entities, seen, Entity(ent, "py-package", str(rel), rel.parts[0]))
        elif name == "package.json":
            if p.parent != root:
                ent = p.parent.name
                _add(entities, seen, Entity(ent, "node-package", str(rel), rel.parts[0]))
        elif name == "Cargo.toml" and p.parent != root:
            ent = p.parent.name
            _add(entities, seen, Entity(ent, "rust-crate", str(rel), rel.parts[0]))
        elif name == "go.mod" and p.parent != root:
            ent = p.parent.name
            _add(entities, seen, Entity(ent, "go-module", str(rel), rel.parts[0]))

    # 3) agents/*.md and commands/*.md (CodeBuddy/Claude plugin convention).
    for pattern, kind in (("agents/*.md", "agent"), ("commands/*.md", "command")):
        for p in root.glob(pattern):
            rel = p.relative_to(root)
            if any(part in IGNORE_DIRS for part in rel.parts):
                continue
            if is_vendored(p):
                continue
            ent = p.stem
            _add(entities, seen, Entity(ent, kind, str(rel), rel.parts[0],
                                        size=p.stat().st_size))

    # 4) Scripts anywhere in the tree, including topic-named folders
    #    (VinuesaLAB-AI repos favor "Neural networks models/"-style folders
    #    over a src/ or scripts/ convention, so a root/scripts/-only glob
    #    misses almost everything).
    for p in root.rglob("*"):
        if p.suffix.lower() not in (".py", ".sh", ".m"):
            continue
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        if is_vendored(p):
            continue
        ent = p.stem
        module = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        content = safe_read(p)
        sciml_kind = detect_sciml_kind(content)
        extra = {}
        if sciml_kind:
            extra["sciml_kind"] = sciml_kind
        auto_summary = extract_summary(content)
        if auto_summary:
            extra["auto_summary"] = auto_summary
        training_config = detect_training_config(content)
        if training_config:
            extra["training_config"] = training_config
        _add(entities, seen, Entity(ent, "script", str(rel), module,
                                    size=p.stat().st_size, extra=extra))

    return entities


def _add(entities: list[Entity], seen: set[str], e: Entity) -> None:
    key = (e.kind, e.rel_path)
    if key in seen:
        return
    seen.add(key)
    entities.append(e)


def _count_mcp_servers(path: Path) -> int:
    try:
        data = json.loads(safe_read(path))
    except Exception:
        return 0
    # Accept both {"mcpServers": {...}} and {"servers": {...}}.
    servers = data.get("mcpServers") or data.get("servers") or {}
    return len(servers) if isinstance(servers, dict) else 0


# --------------------------------------------------------------------------
# Edge detection
# --------------------------------------------------------------------------

# Capture a path-like token after a keyword or as a standalone relative path.
_REF_PATTERN = re.compile(
    r"(?:system\.file|from_plugin|skills\.path|skills|references|src|include|path)"
    r"\s*[:=]\s*['\"]?([^'\"\n]+?)['\"]?"
    r"|(\.{1,2}/[\w./\-]+)"                      # relative path token
    r"|([\w./\-]+\.(?:md|yaml|yml|json|py|ts|js))"  # file reference token
)


def detect_edges(root: Path, entities: list[Entity]) -> list[Edge]:
    # Map: normalized repo-relative path -> entity id.
    by_path: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for e in entities:
        by_path[e.rel_path.replace("\\", "/")] = e.name
        by_name[e.name.lower()] = e.name

    # id lookup by sanitized name for Mermaid matching later.
    id_of = {e.name: e.name for e in entities}

    edges: list[Edge] = []
    seen_pairs: set[tuple[str, str]] = set()

    # Build a quick resolver: given a referenced raw path + the file it came
    # from, resolve to a repo-relative path and match an entity.
    def resolve(raw: str, base_file: Path) -> str | None:
        raw = raw.strip().strip("'\"`").strip()
        if not raw:
            return None
        candidate = (base_file.parent / raw).resolve()
        try:
            rel = candidate.relative_to(root.resolve())
            return rel.as_posix()
        except Exception:
            return None

    for e in entities:
        fpath = root / e.rel_path
        if not fpath.is_file():
            continue
        content = safe_read(fpath)
        if not content:
            continue

        for m in _REF_PATTERN.finditer(content):
            token = m.group(1) or m.group(2) or m.group(3)
            if not token:
                continue
            # Keyword-based copy detection from the matched prefix.
            is_copy = any(kw in (m.group(0).split(token)[0] or "") for kw in COPY_KEYWORDS)

            resolved = resolve(token, fpath)
            target_name = None
            if resolved and resolved in by_path:
                target_name = by_path[resolved]
            else:
                # fallback: only exact name match, or a path token whose
                # last segment equals a known entity name (avoids noisy edges
                # from docstring mentions of words like "repo-doc-miner").
                low = token.lower()
                for nm, ent in by_name.items():
                    if low == nm or (("/" in low or "." in low) and nm in low):
                        target_name = ent
                        break

            if not target_name or target_name == e.name:
                continue
            pair = (e.name, target_name)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            kind = "source->copy" if is_copy else "uses"
            edges.append(Edge(e.name, target_name, kind))

    return edges


# Detects the "simulate -> save -> train on saved data" pattern common in
# SciML repos, where a MATLAB/Python writer and a Python reader never
# import/require each other but share a file-based data contract.
#
# A literal-only, exact-call-name regex (scipy.io.savemat(...), \bload\(...)
# misses two patterns that turned out to be the norm rather than the
# exception on the real pilot repo:
#   - Python code aliases the module and/or assigns the filename to a
#     variable first: `import scipy.io as sio; dataFilename = "x.mat";
#     sio.loadmat(dataFilename)`. Neither "scipy.io.loadmat" nor a bare
#     "load(" matches "sio.loadmat(dataFilename)".
#   - MATLAB builds the filename by concatenating literals with a function
#     call: `save(['./moehlis_data_' num2str(nTS) '.mat'], 'data')` — no
#     literal ever sits directly inside the call's parentheses.
# So detection here is call-name-substring based (any `...load...(` /
# `...save...(` / `to_csv(` / `read_csv(`) rather than exact-name based, and
# resolves both a direct literal argument and a same-file variable
# assignment; the MATLAB bracket-concat case is handled by a dedicated
# pattern that joins the literal fragments and ignores the interpolated part.
_DATA_EXTENSIONS = ("mat", "csv", "h5", "hdf5", "npy", "npz", "pkl")

_IO_CALL_PATTERN = re.compile(
    r"(\b\w*save\w*\(|\.to_csv\(|\b\w*load\w*\(|\bread_csv\()"
    r"\s*(?:['\"]([^'\"]+)['\"]|(\w+))"
)

_VAR_ASSIGN_PATTERN = re.compile(
    r"(\w+)\s*=\s*['\"]([^'\"]+\.(?:" + "|".join(_DATA_EXTENSIONS) + r"))['\"]"
)

_MATLAB_SAVE_CONCAT_PATTERN = re.compile(
    r"\bsave\(\s*\[\s*((?:['\"][^'\"]*['\"]\s*,?\s*)+)"
)

_STRING_LITERAL_PATTERN = re.compile(r"['\"]([^'\"]*)['\"]")


def _data_token_prefix(token: str) -> str:
    # Strip an extension and any trailing variable-interpolation suffix
    # (e.g. "moehlis_data_<nTS>.mat" -> "moehlis_data") so a writer and a
    # reader using different concrete filenames still match. Also strip a
    # trailing literal digit run (e.g. "moehlis_data_100.mat" -> also
    # "moehlis_data") since a hardcoded sample count is the literal-code
    # equivalent of the same interpolation.
    base = re.split(r"[_\-]?\{|\%|\$", token.rsplit(".", 1)[0])[0]
    base = re.sub(r"[_\-]?\d+$", "", base)
    return base.strip("_- ").lower()


def _data_tokens_in_content(content: str) -> tuple[set[str], set[str]]:
    """Return (write_tokens, read_tokens): filename-prefix tokens found by
    scanning a single script's content for direct-literal, variable-
    indirected, and MATLAB-bracket-concatenated save/load calls."""
    writes: set[str] = set()
    reads: set[str] = set()
    var_literals = dict(_VAR_ASSIGN_PATTERN.findall(content))

    for m in _IO_CALL_PATTERN.finditer(content):
        call, literal, ident = m.group(1), m.group(2), m.group(3)
        token = literal if literal is not None else var_literals.get(ident)
        if not token:
            continue
        prefix = _data_token_prefix(token)
        if not prefix:
            continue
        bucket = writes if ("save" in call or "to_csv" in call) else reads
        bucket.add(prefix)

    for m in _MATLAB_SAVE_CONCAT_PATTERN.finditer(content):
        joined = "".join(_STRING_LITERAL_PATTERN.findall(m.group(1)))
        prefix = _data_token_prefix(joined.lstrip("./\\"))
        if prefix:
            writes.add(prefix)

    return writes, reads


def detect_data_contract_edges(root: Path, entities: list[Entity]) -> list[Edge]:
    writers: dict[str, list[str]] = {}
    readers: dict[str, list[str]] = {}
    for e in entities:
        if e.kind != "script":
            continue
        content = safe_read(root / e.rel_path)
        write_tokens, read_tokens = _data_tokens_in_content(content)
        for prefix in write_tokens:
            writers.setdefault(prefix, []).append(e.name)
        for prefix in read_tokens:
            readers.setdefault(prefix, []).append(e.name)

    edges = []
    for prefix, w_list in writers.items():
        for w in w_list:
            for r in readers.get(prefix, []):
                if w != r:
                    edges.append(Edge(w, r, "data-contract", label=prefix))
    return edges


def chain_data_contract_edges(edges: list[Edge]) -> list[list[Edge]]:
    """Group data-contract edges sharing an entity into linear chains
    (A->B, B->C => one chain A->B->C). A chain of 2+ edges (3+ entities)
    is worth its own diagram; 0-1 edge chains stay in the existing
    dependency-edges graph only."""
    by_source = {e.src: e for e in edges}
    starts = {e.src for e in edges} - {e.dst for e in edges}
    chains = []
    for start in starts:
        chain, cur, seen = [], start, {start}
        while cur in by_source:
            e = by_source[cur]
            if e.dst in seen:
                break  # guard against a data-contract cycle
            chain.append(e)
            seen.add(e.dst)
            cur = e.dst
        if len(chain) >= 2:
            chains.append(chain)
    return chains


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

KIND_COLOR = {
    "module": "fill:#e1f0ff,stroke:#3b82f6",
    "skill": "fill:#e8f5e9,stroke:#2e7d32",
    "agent": "fill:#fff3e0,stroke:#ef6c00",
    "command": "fill:#f3e5f5,stroke:#8e24aa",
    "managed-agent": "fill:#ffebee,stroke:#c62828",
    "connectors": "fill:#e0f7fa,stroke:#00838f",
    "py-package": "fill:#ede7f6,stroke:#5e35b1",
    "node-package": "fill:#fce4ec,stroke:#d81b60",
    "rust-crate": "fill:#fff8e1,stroke:#ff8f00",
    "go-module": "fill:#e8eaf6,stroke:#3949ab",
    "script": "fill:#f1f8e9,stroke:#7cb342",
}


def build_structural_graph(entities: list[Entity]) -> str:
    lines = ["graph TD"]
    modules = {}
    for e in entities:
        modules.setdefault(e.module, []).append(e)

    for mod, ents in modules.items():
        sg_id = sanitize_id(f"sg_{mod}")
        lines.append(f"    subgraph {sg_id}[\"{mod}\"]")
        for e in ents:
            nid = sanitize_id(e.name)
            style = KIND_COLOR.get(e.kind, "")
            extra = ""
            if e.kind == "connectors":
                extra = f" ({e.extra.get('servers', '?')} servers)"
            sciml_kind = e.extra.get("sciml_kind")
            sciml_suffix = f" [{sciml_kind}]" if sciml_kind else ""
            lines.append(f"        {nid}[\"{e.name}{extra}<br/>{e.kind}{sciml_suffix}\"]")
            if style:
                lines.append(f"        style {nid} {style}")
        lines.append("    end")
    return "\n".join(lines)


def build_edge_graph(entities: list[Entity], edges: list[Edge], max_edges: int) -> str:
    lines = ["graph LR"]
    id_of = {e.name: sanitize_id(e.name) for e in entities}
    shown = set()
    capped = list(edges)
    if len(capped) > max_edges:
        capped = capped[:max_edges]
    for ed in capped:
        s = id_of.get(ed.src, sanitize_id(ed.src))
        d = id_of.get(ed.dst, sanitize_id(ed.dst))
        shown.add(s)
        shown.add(d)
        if ed.kind == "data-contract":
            # Edge label (not a trailing suffix) keeps this visually
            # distinct from the "synced" source->copy dotted arrows.
            data_label = ed.label or "data"
            lines.append(f'    {s} -.->|"data: {data_label}"| {d}["{ed.dst}"]')
        else:
            arrow = "-.->" if ed.kind == "source->copy" else "-->"
            label = " synced" if ed.kind == "source->copy" else ""
            lines.append(f"    {s} {arrow} {d}[\"{ed.dst}\"]{label}")
    if not shown:
        lines.append("    %% no dependency edges detected")
    return "\n".join(lines)


def build_pipeline_graph(chain: list[Edge], entities: list[Entity]) -> str:
    """Render one chained data-contract pipeline as its own small
    graph TD, using the same node styling as the structural graph."""
    lines = ["graph TD"]
    kind_of = {e.name: e.kind for e in entities}
    node_names = [chain[0].src] + [e.dst for e in chain]
    for name in node_names:
        nid = sanitize_id(name)
        style = KIND_COLOR.get(kind_of.get(name, "script"), "")
        lines.append(f'    {nid}["{name}"]')
        if style:
            lines.append(f"    style {nid} {style}")
    for e in chain:
        s, d = sanitize_id(e.src), sanitize_id(e.dst)
        lines.append(f'    {s} -->|"data: {e.label or "data"}"| {d}')
    return "\n".join(lines)


def render_markdown(root: Path, entities: list[Entity], edges: list[Edge],
                    project_name: str, max_edges: int,
                    vendored_dirs: set[Path] | None = None) -> str:
    modules = {}
    for e in entities:
        modules.setdefault(e.module, []).append(e)

    inv = []
    inv.append(f"# {project_name} — Repository Topology Map")
    inv.append("")
    inv.append("> Auto-generated by `repo-doc-miner/scripts/gen_topology.py` (v2, "
               "topology-first). This is the **first deliverable** of the mining "
               "process: a structural map of the repository. Refine the detected "
               "edges by reading the most important manifests, then deep-dive each "
               "module along these edges.")
    inv.append("")
    inv.append("---")
    inv.append("")
    inv.append("## 1. Structural Topology")
    inv.append("")
    inv.append("```mermaid")
    inv.append(build_structural_graph(entities))
    inv.append("```")
    inv.append("")
    inv.append("## 2. Dependency Edges")
    inv.append("")
    inv.append("Solid arrows = `uses`; dotted `... synced` arrows = "
               "`source → copy` (single source of truth synced into copies); "
               "dotted `\"data: <prefix>\"`-labeled arrows = `data-contract` "
               "(one entity writes a file another loads — no direct "
               "import/require between them).")
    inv.append("")
    inv.append("```mermaid")
    inv.append(build_edge_graph(entities, edges, max_edges))
    inv.append("```")
    inv.append("")
    inv.append("## 3. Entity Inventory")
    inv.append("")
    inv.append("| Kind | Count | Example locations |")
    inv.append("| --- | --- | --- |")
    by_kind: dict[str, list[Entity]] = {}
    for e in entities:
        by_kind.setdefault(e.kind, []).append(e)
    for kind, ents in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        locs = ", ".join(sorted({e.rel_path for e in ents})[:3])
        inv.append(f"| {kind} | {len(ents)} | `{locs}` |")
    inv.append("")
    inv.append("## 4. Excluded as Vendored")
    inv.append("")
    inv.append("Dropped-in third-party library copies (matched by directory name "
               "or by an embedded package marker such as `setup.py`/`PKG-INFO` "
               "next to an `__init__.py`, or a sibling `*.dist-info`/`*.egg-info`) "
               "are excluded from every section above — they are not the repo's "
               "own code, and their generic `save()`/`load()` methods would "
               "otherwise flood the data-contract detector with false positives.")
    inv.append("")
    if vendored_dirs:
        for rel in sorted(str(d.relative_to(root)) for d in vendored_dirs):
            inv.append(f"- `{rel}/`")
    else:
        inv.append("- none detected")
    inv.append("")
    inv.append("## 5. Detected Pipelines")
    inv.append("")
    dc_edges = [e for e in edges if e.kind == "data-contract"]
    chains = chain_data_contract_edges(dc_edges)
    if chains:
        inv.append("Linear data-contract chains (writer → reader → reader → "
                   "...) of 3+ entities, found by following each `data-contract` "
                   "edge to the end of its chain. Chains of 1-2 edges stay in "
                   "the Dependency Edges graph above only — not worth a "
                   "separate diagram.")
        inv.append("")
        for i, chain in enumerate(chains, start=1):
            names = " → ".join([chain[0].src] + [e.dst for e in chain])
            inv.append(f"### Pipeline {i}: {names}")
            inv.append("")
            inv.append("```mermaid")
            inv.append(build_pipeline_graph(chain, entities))
            inv.append("```")
            inv.append("")
    else:
        inv.append("No data-contract chain of 3+ entities detected.")
        inv.append("")
    inv.append("## 6. Module Deep-Dive Scaffold")
    inv.append("")
    inv.append("> Walk these modules **along the dependency edges above**, highest "
               "complexity first. Replace each `> TODO(doc-miner)` with findings.")
    inv.append("")
    for mod, ents in modules.items():
        inv.append(f"### {mod}")
        inv.append("")
        for e in ents:
            size = f" (~{e.size // 1024} KB)" if e.size and e.kind in ("skill", "script") else ""
            training_config = e.extra.get("training_config")
            note = f" — detected loss: {', '.join(training_config)}" if training_config else ""
            inv.append(f"- **{e.name}** ({e.kind}) — `{e.rel_path}`{size}{note}")
            auto_summary = e.extra.get("auto_summary")
            if auto_summary:
                inv.append(f'  - > Auto-summary (unverified, from source docstring): "{auto_summary}"')
                inv.append("  - > TODO(doc-miner): confirm against source and note which entities it references.")
            else:
                inv.append(f"  - > TODO(doc-miner): what does {e.name} do and which entities does it reference?")
        inv.append("")
    inv.append("---")
    inv.append("")
    inv.append("> Refine this map, then use it as the backbone for "
               "`developer_guide.md`, `user_guide.md`, and `tutorial.md`.")
    return "\n".join(inv)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a repo topology map (Mermaid) + entity inventory."
    )
    parser.add_argument("repo_root", type=Path, help="Path to the target repository root.")
    parser.add_argument("--out", default=None, help="Output Markdown path (default: <repo_root>/docs/guides/topology.md).")
    parser.add_argument("--project-name", default=None, help="Display name (default: repo folder name).")
    parser.add_argument("--max-edges", type=int, default=200, help="Cap on rendered dependency edges.")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    if not repo_root.is_dir():
        print(f"error: repo root does not exist: {repo_root}", file=sys.stderr)
        return 1

    project_name = args.project_name or repo_root.name
    out_path = Path(args.out) if args.out else (repo_root / "docs" / "guides" / "topology.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Mining topology for: {repo_root}")
    vendored_dirs = find_vendored_dirs(repo_root)
    if vendored_dirs:
        print(f"  excluded {len(vendored_dirs)} vendored subtree(s)")
    entities = detect_entities(repo_root)
    print(f"  detected {len(entities)} entities")
    edges = detect_edges(repo_root, entities)
    data_edges = detect_data_contract_edges(repo_root, entities)
    edges = edges + data_edges
    print(f"  detected {len(edges)} dependency edges "
          f"({len(data_edges)} data-contract)")

    md = render_markdown(repo_root, entities, edges, project_name, args.max_edges,
                          vendored_dirs=vendored_dirs)
    try:
        out_path.write_text(md, encoding="utf-8")
    except Exception as exc:
        print(f"error: cannot write {out_path}: {exc}", file=sys.stderr)
        return 2

    print(f"  wrote {out_path}")
    print("Done. Next: refine the edges, then run Phase 2 (module deep-dive).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
