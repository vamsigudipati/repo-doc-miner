# repo-doc-miner

A CodeBuddy / Claude-Code compatible **Skill** that mines an existing Git repository and produces a coordinated set of three Markdown documents:

- `developer_guide.md` — architecture, sub-package tours, extension SOPs
- `user_guide.md` — installation through advanced user-facing features
- `tutorial.md` — a beginner-to-expert, chapter-by-chapter learning path
- `README.md` — a navigation hub that links the three documents

The skill is grounded in the actual code, configuration, and examples of the target repository, not generic knowledge.

## Contents

```
.
├── SKILL.md                     # Skill manifest + workflow (entry point)
├── assets/
│   └── templates/               # Markdown skeletons used as the literal starting point
│       ├── developer_guide.md
│       ├── user_guide.md
│       ├── tutorial.md
│       └── README.md
├── references/                  # Loaded on demand during writing
│   ├── doc_outlines.md
│   ├── evidence_checklist.md
│   └── writing_style.md
└── scripts/
    └── scaffold_docs.py         # Creates docs/guides/ and materializes the templates
```

## Installation

Drop the whole folder into your agent's skill directory:

- **CodeBuddy (project-scoped)**: `<repo>/.codebuddy/skills/repo-doc-miner/`
- **CodeBuddy (user-scoped)**: `~/.codebuddy/skills/repo-doc-miner/`

The agent auto-discovers the skill via `SKILL.md`.

## Quick Start

Once installed, trigger the skill with a request like:

- "挖掘当前项目，生成开发者文档、用户文档和从入门到精通教程"
- "Mine this repo and produce developer / user / tutorial docs under `docs/guides/`"

The skill will:

1. Scope the output to `docs/guides/` under the repo root.
2. Perform a reconnaissance pass across metadata, public API, sub-packages, examples, tests, CI.
3. Draft per-document outlines from `references/doc_outlines.md`.
4. Scaffold the four files with `scripts/scaffold_docs.py` and fill them with repo-specific content.
5. Validate code-snippet fidelity and surface any `TODO(doc-miner)` gaps.

You can also run the scaffolder manually:

```bash
python scripts/scaffold_docs.py <repo-root> --project-name "<DisplayName>" [--out docs/guides]
```

## Design Principles

- **Ground every claim in code** — file paths and symbol names accompany every feature description.
- **Prefer real snippets** from `examples/` or tests, capped at ~30 lines.
- **Cross-link** the three documents so any one of them is a valid entry point.
- **Mark unresolved gaps** with `TODO(doc-miner)` instead of fabricating behavior.

See `SKILL.md` for the full six-phase workflow and `references/writing_style.md` for prose conventions.

## License

[MIT](./LICENSE)
