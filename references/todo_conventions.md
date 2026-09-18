# TODO Hygiene Conventions (apply during Phase 5 validation)

Before handover, re-classify every `TODO(doc-miner)` marker in the four
delivered docs against these five forms. Only category 1 should survive as
an actual `TODO(doc-miner)` tag.

1. **Genuine gap** — a specific unread file/symbol. Keep the tag, but name
   the exact file/lines/symbol — never ship a bare "what does X do"
   placeholder in a delivered doc (topology.md's own Phase-1 scaffold is the
   only place that's acceptable, and only until Phase 2 walks that node, or
   an auto-summary is present per gen_topology.py §8a).
2. **Permanent fact about the upstream repo** (no pinned deps, no
   CITATION.cff, no requirements.txt) — state once, in the single most
   relevant section (developer_guide.md's metadata snapshot is the default
   home), and cross-reference everywhere else instead of repeating it.
   Never tag with TODO — nothing further will be "found" by reading more.
3. **Already-resolved explanation** — plain prose, no TODO tag. If Phase 2
   found the answer, don't wrap it in a marker implying it's still open.
4. **Inherent scope limitation** (repo never executed, no GPU/data to
   runtime-test) — fixed wording: "Not runtime-tested — this pass is a
   static code read; <specific missing resource>." Never phrase as a future
   TODO implying a later pass will resolve it.
5. **Maintainer-facing recommendation** — tag as `> Recommendation:`, never
   `TODO(doc-miner)`. This is advice to the repo's own maintainers, not a
   mining gap.
6. **Externally-resolved fact** (e.g. a citation not present in-repo, found
   via web search) — state the fact plainly with a one-line note that it
   was resolved externally, so a reader knows to trust it despite the
   absence in-repo. Not a TODO.
