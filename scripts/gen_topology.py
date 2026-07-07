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
}

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
    kind: str              # "source->copy" | "uses"


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
# Entity detection
# --------------------------------------------------------------------------

def detect_entities(root: Path) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    top_modules = [p for p in root.iterdir() if p.is_dir() and p.name not in IGNORE_DIRS]

    # 1) Top-level directories are modules.
    for mod in top_modules:
        entities.append(Entity(mod.name, "module", mod.name, mod.name))

    # 2) Walk for typed entities.
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
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
            ent = p.stem
            _add(entities, seen, Entity(ent, kind, str(rel), rel.parts[0],
                                        size=p.stat().st_size))

    # 4) Standalone scripts at repo root or under scripts/.
    for p in list(root.glob("*.py")) + list(root.glob("*.sh")) + \
             list(root.glob("scripts/*.py")) + list(root.glob("scripts/*.sh")):
        rel = p.relative_to(root)
        if any(part in IGNORE_DIRS for part in rel.parts):
            continue
        ent = p.stem
        _add(entities, seen, Entity(ent, "script", str(rel), rel.parts[0],
                                    size=p.stat().st_size))

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
            lines.append(f"        {nid}[\"{e.name}{extra}<br/>{e.kind}\"]")
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
        arrow = "-.->" if ed.kind == "source->copy" else "-->"
        label = " synced" if ed.kind == "source->copy" else ""
        lines.append(f"    {s} {arrow} {d}[\"{ed.dst}\"]{label}")
    if not shown:
        lines.append("    %% no dependency edges detected")
    return "\n".join(lines)


def render_markdown(root: Path, entities: list[Entity], edges: list[Edge],
                    project_name: str, max_edges: int) -> str:
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
    inv.append("Solid arrows = `uses`; dotted arrows = `source → copy` "
               "(single source of truth synced into copies).")
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
    inv.append("## 4. Module Deep-Dive Scaffold")
    inv.append("")
    inv.append("> Walk these modules **along the dependency edges above**, highest "
               "complexity first. Replace each `> TODO(doc-miner)` with findings.")
    inv.append("")
    for mod, ents in modules.items():
        inv.append(f"### {mod}")
        inv.append("")
        for e in ents:
            size = f" (~{e.size // 1024} KB)" if e.size and e.kind in ("skill", "script") else ""
            inv.append(f"- **{e.name}** ({e.kind}) — `{e.rel_path}`{size}")
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
    entities = detect_entities(repo_root)
    print(f"  detected {len(entities)} entities")
    edges = detect_edges(repo_root, entities)
    print(f"  detected {len(edges)} dependency edges")

    md = render_markdown(repo_root, entities, edges, project_name, args.max_edges)
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
