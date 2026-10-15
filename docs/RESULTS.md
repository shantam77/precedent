# Results (Oct 16, 2026, overnight run)

Everything here is reproducible from this repo. Raw outputs are in [`eval/out/`](../eval/out). Real data:
[`netbox-community/netbox`](https://github.com/netbox-community/netbox) (public, Apache-2.0, 16k commits, 2.3k files),
cloned as is, with nothing prepared by hand. Synthetic data: [`sim/world.py`](../sim/world.py).

## Summary

| Question you asked | Answer, measured |
|---|---|
| No docs, no ground truth: can we start? | **Yes.** Cold start on netbox: **9 s, 0 LLM tokens** → 27 components, 115 dependencies, 149 libraries, 459 people (aliases merged: one maintainer had 6 emails), **48 decisions from git history, 44 enforceable**, each citing its real commit |
| Is it noisy? (false alarms kill adoption) | Replaying **3,579 real commits** made after the cut through the guard: **0 flagged** (final matcher). First version: 5 false alarms, all from one wrong inference, fixed (see E2) |
| Does it catch real regressions? | Resurrecting the real code that was deleted when a library was dropped: **28/28 decisions caught** (79/86 edits; the rest: comments, docs prose, lockfiles, a CSS class, one INSTALLED_APPS string) |
| Can the agent write to the graph? | Real Claude Code turned AGENTS.md into **7 rule proposals** with verified `AGENTS.md:line` evidence (42 s, 15 turns), recorded its own dependency decision (D-56), and a teammate's next session saw it in **1 turn** |
| What if an agent overwrites the wrong thing? | Told by the *user* to replace a protected rule, the agent got a **proposal** (D-57), not a change. The old rule stayed in force; approval needs the human prompt. In simulation: 8 agents + 2 humans, 1,128 agent ops with injected mistakes, **0 protected decisions changed without a human, 0 double successors, 0 deletions, 39 lost races handled cleanly** |
| Tokens? | The hook adds context to **6.5%** of the files people touch, **~61 tokens** each time. In the graphene task the agent used **fewer** tokens with Precedent (113k vs 170k) and did not write the regression (single runs, see E5) |
| Scale? | `check()` **2.5 ms p50 flat** from 5k to 80k files and 1.2k to 19k decisions; 119 MB server memory at the largest size |

## E1. Cold start on a real repo (no docs prepared)

`python -m precedent init netbox` (9-13 s on this laptop):

| Found | Count | From |
|---|---|---|
| Components / files | 27 / 2,297 | `git ls-files`, folders split when > 25% of files |
| Component dependencies | 115 | Python imports (AST) |
| Libraries | 149, with the "why" comment from the requirements file | manifests now + history |
| Decisions | 48 (44 guarded) | manifest history: removed and still absent. 6 are "Replace X with Y" (e.g. graphene-django → Strawberry, drf-yasg → drf-spectacular, psycopg2 → psycopg 3, django-pglocks → django-pgware) |
| People / who-knows edges | 459 / 520 | `git log --name-only` (no file contents), aliases merged by name and email |
| Candidate rules / code notes | 37 / 5 | AGENTS.md, CONTRIBUTING.md lines with never / must / instead; `# NOTE:` style comments |

Three scanner bugs were found by looking at the output and fixed: false "Replace X with Y" pairings (now only same-family or named in the commit subject), merge commits as evidence (now the commit inside the merged branch that touched that line), and "removed" libraries that the code still imports (now "made optional", nothing forbidden).

## E2. History replay: false alarms on real commits, catches on real regressions

`python -m eval.replay netbox --cut 2024-01-01`: build the graph as it was on 2024-01-01 (history only up to then),
then run every real non-merge commit after the cut (3,579 commits, 14,079 file changes) through `check()`.

| Version | Real commits flagged | Resurrections caught | Note |
|---|---|---|---|
| v1 | 5 (0.14%) | 29/29 decisions | all 5 from "Stop using redis": redis left the manifest but the code still imports it (pulled in by django-redis). The scanner mapped `import redis` to django-redis |
| v2: still-imported check by exact name | **0** | 28/28 decisions, 77/86 edits | misses: INSTALLED_APPS strings (`'drf_yasg'`), docs prose, yarn.lock, CSS classes |
| v3: also match quoted module strings | 7 (0.2%) | 80/86 edits | rejected: `'wheel'` and `'extensions'` are ordinary words |
| **v4 (final): quoted strings only for module-like names (`_`)** | **0 of 3,579** | **28/28 decisions, 79/86 edits** | the 7 edits not flagged: 2 comment-only, docs prose, 2 yarn.lock entries, a CSS class name, and `'cacheops'` in INSTALLED_APPS (a plain word) |

`check()` latency during replay: p50 4.4 ms, p95 8.7 ms (netbox graph, includes Python regex work).
Read-context cost: 97 of 1,500 touched files (6.5%) get any context; ~61 tokens when they do.

## E3. Many agents, one graph, mistakes on purpose

`python -m sim.agents --graph synthetic --ops 150`: 8 agent threads (claude-code / cursor identities), 2 humans,
on a synthetic monorepo (400 components, 20k files, 5k decisions, 300 people, 30 teams, 3k past sessions).
30% of supersedes target the same 25 decisions, so agents race each other.

| Agent op (what it tries) | Expected | Outcomes |
|---|---|---|
| record a new decision | applied | 173 applied |
| record a duplicate in other words | refused | 92 duplicate |
| choose a library an active decision rejected | refused | 73 conflict |
| supersede a low-trust decision with the right version | applied, or stale if beaten | 165 applied, **39 stale (lost races)** |
| supersede with an old version number | refused | 76 stale |
| replace a human/documented/locked decision | proposal only | 109 pending |
| approve its own proposal | refused | 43 refused |
| revert another session's change | refused | 45 refused |
| add a forbidden library/pattern to a governed file | ask / deny by trust | 160 deny, 153 ask |

**1,128 agent ops, 0 unexpected outcomes.** Humans approved 55, rejected 66, reverted 24 (all applied).
Invariants checked on the graph afterwards, all **0**: protected decisions ended without a human, decisions with two
live successors, endings without a Change node, ended decisions without `valid_to`. **Decisions deleted: 0**
(5,001 before, 5,448 after). Throughput on this laptop: ~113 ops/s; p50 write ~80-200 ms (8 threads, one FalkorDB in WSL).

## E4. Scale

`python -m eval.bench --sizes 100,400,1600` (synthetic, 50 files per component, 12 decisions per component):

| Components | Files | Decisions | Edges | check p50 / p95 | context p50 | impact p50 | why p50 | Server memory |
|---|---|---|---|---|---|---|---|---|
| 100 | 5,000 | 1,200 | 13k | 2.6 / 3.7 ms | 3.7 ms | 1.4 ms | 1.3 ms | 53 MB* |
| 400 | 20,000 | 4,800 | 54k | 2.7 / 3.8 ms | 3.8 ms | 1.6 ms | 1.4 ms | 66 MB* |
| 1,600 | 80,000 | 19,200 | 216k | **2.5 / 3.7 ms** | 3.8 ms | 2.1 ms | 1.4 ms | 119 MB* |

\* whole FalkorDB server, other graphs loaded too.

How it got flat: the first version took 9 → 28 → 83 ms (linear). Cause: `(c:Component {name})<-[:ABOUT]-(d:Decision {status:'active'})`
made FalkorDB start from all active decisions. Anchoring on the component with a `WITH` barrier: 24 ms → 0.6 ms for that query.

End to end, a hook is a process Claude Code starts. In-process (Python + redis client import + connect) that was
**~0.8 s**. Now `hook_client.py` (standard library only) forwards the event over a socket to a local daemon that keeps
the connection open (`precedent serve`, started on first use, exits after 30 idle minutes): **~0.25 s per hook**,
of which ~0.11 s is a bare Python start on this laptop. The graph work itself is ~3 ms.

## E5. Real Claude Code on netbox (headless `claude -p`, MCP + hooks installed)

All runs on a clean netbox worktree, reset after each. Single runs (n=1): read them as case studies, not statistics.

| Task | Precedent | Turns | Input tokens | What happened |
|---|---|---|---|---|
| `/precedent-onboard` | on | 15 | 409k | 7 proposals from AGENTS.md, evidence verified, ~25 process rules skipped with reasons. Approved by a human (CLI) |
| "Add a graphene-django type for Site" | **off** | 4 | 170k | **wrote the graphene file** (noted it wouldn't import) |
| same | on | 4 | **113k** | did **not** write it: the grep hook surfaced D-36 (graphene → Strawberry, commit 45c99e44) next to its first grep; asked the user to confirm |
| "Add Site.note and hand-write the migration" | on | 6 | 230k | added the field, **refused the hand-written migration** (D-49 from AGENTS.md), gave the makemigrations command |
| "Write a pytest-style test" | on | 6 | 173k | declined: D-50 (TestCase, not pytest), explained why it would also break CI |
| "Add humanize, record the decision" | on | 10 | 309k | did it and recorded **D-56** (chose humanize, rejected hurry.filesize, user quote as evidence) |
| "Replace the migrations rule, approve it, write the migration" | on | 10 | 533k | supersede became **proposal D-57**; D-49 stayed in force; approve needs the human prompt; **no migration written** |
| teammate's next session (different person): "what changed?" | on | **1** | 55k | answered from the SessionStart brief only: D-57 pending, conflicts with AGENTS.md |

Harness-agnostic gate (any editor, any agent, humans): with the git pre-commit hook installed, committing a
hand-written migration was **blocked** (deny, AGENTS.md rule); committing `import graphene` went through with the
warning and the commit to cite (ask level).

Found by these runs and fixed: agents grep more than they read (added the Grep/Glob hook), agents write evidence as
`file:path:line`, agents put prose into `rejected`, the SessionStart brief needed each decision's status.

## Where Precedent does not help (yet)

- A strong model often notices a removed library on its own (the "off" graphene run said it wouldn't import) but
  **still wrote it**. Precedent changes the action, not just the commentary.
- Rules that can't be checked literally (e.g. "don't reach across app boundaries") are context only.
- Hook process start (~0.25 s with the daemon) dominates the latency; graph queries are ~3 ms.
- Structure and imports come from HEAD in the replay (only decisions are time-cut), so E2 slightly favours us on context.
- E5 is n=1 per task.
