# Writing-Style Conventions

## Language

- All generated documentation is English only, regardless of the source
  repository's own language. If repo-native docs (README, code comments,
  in-repo guides) are in another language, translate the substance into
  English — never mirror the source language, and never produce bilingual
  headings or dual-language text blocks.
- Within a single document, only English may appear outside of: (a) fenced
  code blocks reproducing actual source code verbatim, and (b) a person's
  name or a proper noun with no English form.
- Before handover (Phase 5), scan every generated file for non-ASCII/CJK
  characters outside code blocks; anything found must be translated or
  removed.

## Tone

- Objective, instructional, second-person avoided. Prefer "Use X to do Y" over "You should use X".
- No marketing adjectives ("powerful", "amazing", "cutting-edge").
- Absolute claims ("always", "never") are acceptable only when grounded in source code.

## Code citation format

When referencing a symbol in prose, use the canonical form `path/relative/to/repo.py::Symbol` the first time it appears in a document. Subsequent mentions may drop the path.

Examples:
- First mention: ``The training loop is implemented in `deepxde/model.py::Model.train`.``
- Later mentions: ``During `Model.train` the callbacks fire in this order ...``

## Code snippets

- Use fenced code blocks with the correct language tag (`python`, `bash`, `rust`, …).
- Keep snippets ≤ 30 lines; elide non-essential parts with `# ...` and a comment indicating what was removed.
- Preserve the original repo's import style (e.g. `import deepxde as dde`).
- Never invent function names or parameters. If unsure, verify with `search_content` before committing.

## Tables

- Use GitHub-flavored Markdown tables for:
  - enumerating class hierarchies,
  - mapping environment variables to behaviors,
  - error → cause → fix triplets,
  - comparing backends / adapters.
- Keep rows short; move long explanations into the paragraph above/below.

## ASCII diagrams

- Allowed (encouraged) for architecture overviews.
- Keep width ≤ 72 characters.
- Do not use Unicode box-drawing characters heavier than `┌ ┐ └ ┘ ─ │ ├ ┤ ┬ ┴ ┼ ▶ ◀ ▲ ▼`.

## Mermaid diagrams

- `graph TD` for pipelines / data flow; `sequenceDiagram` for time-ordered
  multi-actor interaction. See doc_outlines.md's "Diagram placement" rule
  for when each is required.
- Node/actor labels match the entity names used in topology.md, so a
  reader can cross-reference between documents.
- Every diagram must be grounded in code actually read — same evidence
  rule as prose; no fabricated steps or actors.
- Prefer Mermaid over ASCII once a diagram would need more than ~5 nodes or
  any branching — ASCII stays acceptable only for a small, purely linear
  2–4-box diagram.

## TODO markers

Use `> TODO(doc-miner): <short description>` as a blockquote when a piece of evidence was not found.

Rules:
- Each TODO lists the missing fact, not the proposed answer.
- Scan with `rg "TODO\(doc-miner\)"` before handover; resolve or list in the summary.

## Cross-document references

- `README.md` → links to all three other files in the same folder.
- `tutorial.md` references concrete sections of `user_guide.md` by anchor ID (Markdown auto-generates from headings).
- `developer_guide.md` references `user_guide.md` for high-level API semantics; avoids duplicating user-level how-tos.

## Section length heuristics

- A top-level section: 100–600 words + optional code block.
- Sub-section (`###`): 50–300 words.
- No section should be empty after Phase 4; if evidence is missing, insert a TODO marker.

## What to cut

- Generic ML or software-engineering tutorials that could apply to any project.
- Advertisements for the project ("the best PINN library").
- Historical anecdotes unless they explain a current design decision.
- Redundant re-statement of content already in the README (link instead).
  This explicitly includes the canonical citation: state it once, in full,
  in README.md's Citation section; every other document links to that
  section (e.g. "see README.md § Citation") instead of repeating the full
  citation block.
