---
description: Turn this repo's written rules (AGENTS.md, CLAUDE.md, CONTRIBUTING, ADRs) into Precedent decisions a human approves
---
Onboard this repository into Precedent (the decision graph behind the `precedent` MCP tools).

1. List the candidate rules Precedent already found in the repo's docs (it read them at `init`, no need to open the files):
   call `query` with `MATCH (:Repo)-[:HAS_NOTE]->(n:Note {kind: 'rule'}) RETURN n.id, n.text`.
2. Keep only rules about the CODE that a reviewer could check on a single edit (where things go, what must not be
   written, which library to use). Skip process rules (PR etiquette, issue workflow, release notes).
3. For each kept rule (at most 8), call `record_decision` with:
   - `title`: a short imperative ("Never hand-write Django migrations")
   - `rationale`: the rule's reason, quoting the doc
   - `paths`: the globs it governs (e.g. `netbox/*/migrations/*.py`)
   - `forbid`: what must not be added there. `lib:<name>` for a library, `any` if files there must never be edited
     by hand, or a short literal code fragment (e.g. `import pytest`). Leave it empty if nothing can be checked literally.
   - `evidence`: the note id, e.g. `AGENTS.md:141`
   - `trust`: `documented`
   They become proposals; a human approves them.
4. Finish with a 5-line summary: proposals created, rules skipped and why. Do not edit any file.
