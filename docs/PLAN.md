# Precedent v2: the plan (written Oct 16, 04:30 IST, while you slept)

This replaces the PoC assumptions. Read §1 to §4 first: they answer your questions directly.
§5 onward is the build. The results of what I actually built and measured tonight are in
[`RESULTS.md`](RESULTS.md).

---

## 0. One paragraph

Precedent is a **shared decision graph for coding agents**. It starts from **zero**: on day one it
reads only what every repo already has (code, imports, manifests, git history, CODEOWNERS, AGENTS.md),
costs **0 LLM tokens**, and gives every decision a **trust level**. After that, **the agents
grow the graph themselves** through MCP while they work. Every write is **append-only, versioned,
logged and revertible**. Writes that touch something important become **proposals that a human
confirms** inside their own harness (Claude Code's native "ask" prompt). Agents never have to *read*
the graph on purpose: hooks attach the 3 or 4 relevant lines to what they already do
(`Read`, `Edit`, `git commit`), and stay silent otherwise.

---

## 1. The three assumptions we drop

| PoC assumed | Reality | v2 answer |
|---|---|---|
| A pile of messy docs exists (ADRs, chats, tickets) | Often nothing. Or docs are stale | **Cold start from the repo itself** (§2). Docs are a bonus layer, not a requirement |
| An offline pipeline (LLM extraction) writes the graph | The agent is the one making decisions, all day long | **Agents write the graph through MCP, guarded by policy** (§3, §4). Humans confirm only what matters |
| Agents will read our long answers | Harnesses grep and read files, and nobody pastes documents | **Push tiny context into the existing workflow** (hooks on Read/Edit/commit). Tool answers are ≤ ~400 tokens of plain text with ids |

---

## 2. Cold start: where knowledge comes from on day 0 (no docs, no history needed)

`precedent init <repo>` runs deterministically: no LLM, seconds to a minute.

| Layer | What we read | What it becomes | Trust | Enforcement |
|---|---|---|---|---|
| Structure | `git ls-files`, folders | `Component`, `File-[:IN]->Component` (replaces the hand-written `components.yaml`) | observed | none, context only |
| Imports | Python AST, JS/TS import regex | `Component-[:DEPENDS_ON {imports}]->Component`, `Component-[:USES]->Library` | observed | used by `impact` |
| Manifests now | requirements / pyproject / package.json / go.mod, **plus the comment above each line** | `Library` with its "why" comment (netbox has these) | observed | context |
| Manifest history | `git log -p` on manifests only | "Library X removed on D in commit C (subject)" → **historical decision** that forbids X; "X replaced by Y" | historical | **ask** |
| Commit subjects | strict patterns: "migrate from X to Y", "replace X with Y" | candidate decisions with the commit as evidence | historical | ask, if it names a removed library |
| People | `git log --name-only` (no blobs, cheap), name-based alias merge | `Person-[:KNOWS {commits, last}]->Component` | observed | `who_knows`, reviewers |
| Ownership | CODEOWNERS (if any) | `Team-[:OWNS]->Component` | documented | approvals |
| Agent docs | AGENTS.md / CLAUDE.md / CONTRIBUTING / ADR folders: lines with never / do not / must / instead | `Note` candidates with `doc:line` evidence | documented (as candidates) | none until an agent/human turns them into a decision |
| Code notes | comments with NOTE / IMPORTANT / DO NOT / WHY | `Note` on the file | observed | shown when the file is read |

**A brand-new project with no history** gets only structure and imports. That's fine: the graph
then grows from use (§3). The SessionStart hook says so in one line.

**Turning docs into enforceable rules is a job for the agent, not for us:** `/precedent-onboard` is a
Claude Code command. The agent (already paid for by the user's subscription) reads the candidate
notes, and for each real rule calls `record_decision` with a scope (path glob), forbidden patterns and
evidence (`AGENTS.md:141`). They land as **proposals**. The human approves them in one batch.

---

## 3. Who may write what: the access model

Every write goes through **one policy function** on the server. Hooks add friendlier prompts, but the
server is the real gate, so Cursor or any MCP client gets the same rules.

**Trust ladder** (on every decision): `observed` < `historical` < `agent` < `documented` < `human`, plus a `locked` flag.

| Operation | by an agent | by a human |
|---|---|---|
| read anything, `context`, `check`, `impact`, `why`, read-only `query` | yes | yes |
| `record_decision` (new, no conflict) | **applied**, trust=agent | applied, trust=human |
| `record_decision` that conflicts with or duplicates an active decision | **refused**, told to pass `supersedes=` (shows the duplicate) | same |
| `supersede` / `retire` a decision with trust ≤ agent | **applied** (logged, revertible, owner notified next session) | applied |
| `supersede` / `retire` with trust ≥ documented | **pending proposal**: the old one stays in force until approved | applied |
| anything on a `locked` decision | pending: **an owner-team member** must approve (checked in the graph) | an owner-team member only |
| `amend` (wording, add paths/patterns) | own decisions or ≤ agent; needs `expected_version` | yes |
| `approve` / `reject` a proposal | only through the harness's human prompt (`permissions.ask`) | yes (CLI) |
| `revert(change)` | own session's changes only | any |
| more than N writes per session (default 25) | refused: "ask the user" | n/a |

**Hook verdict on an edit that violates a decision** (from its trust):
`human`/`documented` → **deny** with reason + source · `historical`/`agent` → **ask** the human (native prompt) ·
`observed` → allow + one line of context · `proposed` (not yet approved) → context only.

So the agent is never stuck: "ask" lets the human say "yes, that's intentional", and then the agent
records the new decision, which supersedes the old one.

---

## 4. The safety net: "what if we overwrite something we shouldn't have?"

1. **Nothing is ever deleted or overwritten in place.** Supersede = a new node + `SUPERSEDES` edge +
   `valid_to` on the old one. "As of last Tuesday" still works, forever.
2. **Every write is a `Change` node** (actor, harness, session, op, reason, before/after snapshot),
   linked to what it touched: `git log` for the graph. `precedent log` shows it.
3. **`revert(change_id)`** applies the inverse (re-activates the old decision, retires the new one) and is itself a Change.
4. **Optimistic concurrency.** Every decision has a `version`. Supersede/amend/retire must pass the
   version the agent read. The check-and-set is one Cypher statement (FalkorDB runs writes to a graph
   one at a time, so it is atomic). If someone else changed it first, the agent gets:
   *"D-12 changed since you read it: now v3, superseded by D-40 (alice, claude-code, 2 min ago). Re-read."*
5. **Duplicates are refused**: same component + overlapping title or the same library → "did you mean D-7?".
6. **Evidence is checked cheaply**: `path:line` must exist, `commit:sha` must exist in git. Claims we can't verify are marked so.
7. **Owner approval is a graph query**: approver `MEMBER_OF` the team that `OWNS` the component. The graph decides access, not a config file.
8. **Write budget per session**, so a looping agent can't spam the graph.
9. **Fail-open by default** (hook errors never brick the editor), `PRECEDENT_FAIL_CLOSED=1` for strict teams.

---

## 5. Shared graph: many people, many harnesses

- **One FalkorDB, one graph per repo**, shared by the team. Each MCP server instance knows its actor:
  `git config user.email` + harness name (`claude-code`, `cursor`, `cli`) + session id.
- **Sessions are first-class**: `(:Person)-[:RAN]->(:Session {harness})-[:MADE]->(:Change)`, plus `EDITED` files.
- **"Since your last session"**: SessionStart shows changes by *other* people/agents to the components
  you usually touch (your `KNOWS` edges), and proposals waiting for you (you're in the owner team).
  That's the hand-off between agents and people, in about 5 lines.
- Reads are cheap and concurrent. Writes are serialized per graph by FalkorDB, and CAS handles races.
- **Portable log** (later): `precedent export` writes `.precedent/decisions.jsonl` into the repo, so
  decisions can be reviewed in PRs and the graph rebuilt anywhere. FalkorDB stays the engine for
  impact, as-of, approvals and enforcement, so the decoration test still holds.

---

## 6. Hooks: when they fire (Claude Code; Cursor has the same events)

| Event | Matcher | What Precedent does | Token cost |
|---|---|---|---|
| SessionStart | startup/resume | ≤ 12 lines: what the graph knows, changes since your last session, proposals waiting for you | ~150 |
| PostToolUse | `Read` | if the file is governed by active decisions or has notes: ≤ 5 lines via `additionalContext`. Each decision once per session | 0 to ~120, only when relevant |
| PreToolUse | `Edit\|Write\|MultiEdit` | `check(path, old, new)`: deny / ask / allow + reason | 0 when OK |
| PostToolUse | `Edit\|Write` | record `Session-[:EDITED]->File`; if the edit **introduces a new library**, nudge: "record a decision if this is deliberate" | ~40, rare |
| PreToolUse | `mcp__precedent__(supersede\|retire)` | if the target is protected, `ask` with a one-line summary for the human | 0 |
| git pre-commit | (any harness, humans too) | `precedent check --staged`: deny-level violations block the commit, ask-level ones print warnings | 0 |

Cursor: `.cursor/hooks.json` with `beforeReadFile`, `preToolUse`, `afterFileEdit`, `beforeMCPExecution`
(same JSON fields, snake_case, `permission: allow|deny|ask`).

---

## 7. MCP tools (the contract)

Read (always allowed): `context(paths)` · `check(path, new_text, old_text)` · `impact(target)` ·
`why(id_or_topic, as_of)` · `who_knows(path)` · `history(since)` · `search(text)` · `query(cypher)` (read-only, LIMIT forced)

Write (policy-gated): `record_decision(...)` · `supersede(id, expected_version, ...)` · `amend(id, expected_version, ...)` ·
`retire(id, expected_version, reason)` · `approve(proposal)` / `reject(proposal)` · `revert(change)`

All answers are plain text with ids, versions and sources (`commit:abc123`, `AGENTS.md:141`), not JSON blobs.

---

## 8. Graph model (v2)

```
(:Repo)
(:Component {name, path})       <-[:IN]- (:File {path})       -[:HAS_NOTE]-> (:Note {line, text, kind})
(:Component)-[:DEPENDS_ON {imports, valid_from, valid_to?}]->(:Component)
(:Component)-[:USES {files}]->(:Library {name, why})
(:Person {key, name, emails})-[:KNOWS {commits, last}]->(:Component)
(:Person)-[:MEMBER_OF]->(:Team)-[:OWNS]->(:Component)
(:Decision {id, title, rationale, status, trust, locked, version, scope, forbid[], valid_from, valid_to?, actor})
(:Decision)-[:ABOUT]->(:Component | :Repo)    -[:CHOSE|REJECTED]->(:Library)    -[:SUPERSEDES]->(:Decision)
(:Decision)-[:EVIDENCED_BY]->(:Commit {sha, at, subject, author})
(:Person)-[:RAN]->(:Session {id, harness})-[:MADE]->(:Change {id, op, at, reason, before, after})-[:ON]->(:Decision)
(:Session)-[:EDITED]->(:File)
```
status: `active | proposed | superseded | retired | rejected`. Never null: open intervals are missing properties (FalkorDB).

---

## 9. How we prove it (real data + synthetic volume)

| # | Experiment | Data | Measures |
|---|---|---|---|
| E1 | **Cold start on a real repo** | `netbox-community/netbox` (16k commits, 1.3k .py files), nothing prepared | time, nodes/edges, decisions recovered, 0 tokens |
| E2 | **History replay** | build the graph as of a past date, replay the real commits after it through `check` | **false-alarm rate on real, legitimate commits** + catch rate on **injected regressions** (re-adding removed libraries, manual migrations, pytest in tests) |
| E3 | **Multi-agent shared graph** | 8 concurrent simulated agents + 2 humans, 1000s of ops, with injected bad writes (stale versions, overwriting human decisions, duplicates, spam) | protected decisions overwritten without approval (target: 0), stale writes caught, duplicates caught, every bad change revertible, ops/s, p95 |
| E4 | **Scale** | synthetic monorepo: 400 components, 20k files, 5k decisions, 300 people, 3k sessions | hook latency p50/p95 (must stay < 100 ms), memory |
| E5 | **Real Claude Code on netbox** | headless `claude -p` with MCP + hooks | blocked/asked regressions, tokens with vs without Precedent, agent-written decisions during onboarding |

---

## 10. Brainstorm (everything I considered, with a verdict)

**Build now (in this repo tonight)**
1. Zero-token cold start from code + git (the answer to "no data")
2. Manifest-history decisions: removed libraries become guarded precedents, with the real commit as the source
3. Requirements "why" comments as library rationale (netbox has them)
4. Name-based alias merge for people (one netbox maintainer has 6+ emails)
5. Trust ladder → deny / ask / allow
6. Proposals + native human "ask" for protected writes
7. Optimistic concurrency (version CAS) in one Cypher statement
8. Change log + revert
9. Duplicate/conflict detection on record
10. Evidence verification (file:line, commit sha)
11. Session write budget
12. PostToolUse(Read) context injection, once per decision per session
13. New-library nudge after edits
14. git pre-commit check (harness-agnostic)
15. `query` tool: read-only Cypher for power users and agents
16. Owner-team approval through the graph
17. History-replay eval on real commits (honest false-alarm rate)
18. Multi-agent concurrency simulation with injected mistakes
19. `/precedent-onboard`: the agent turns AGENTS.md rules into proposals, and the human approves in a batch

**Next (Sat)**
20. Cursor `hooks.json` adapter (same script, different field names)
21. `precedent export` → `.precedent/decisions.jsonl` (PR-reviewable)
22. Small web view: graph around a file, proposals queue with approve buttons, change log
23. Decision "health": decisions whose evidence files were deleted → flag as possibly stale
24. Expiring decisions (`review_by` date): "this was decided 2 years ago, still true?"
25. Impact-aware check: a change to a component that 7 others depend on → mention the owners of the dependents
26. PR bot (GitHub Action) that comments with the decisions a PR touches

**Later / pitch only**
27. Learn the policy thresholds from approvals (if humans always approve X, relax it)
28. Decision embeddings for fuzzy duplicate detection (needs the embeddings model)
29. Org-level graph across repos (shared libraries, platform decisions)
30. Personalized PageRank to rank which decisions to show on SessionStart
31. Auto-drift: an agent's commits keep getting "ask" on the same decision → suggest retiring it
32. Signed changes (actor keys) for audit-heavy teams
33. Slack/issue connectors (forbidden data for the hackathon; good roadmap slide)

**Rejected**
- LLM extraction over whole docs at bootstrap (token cost, and you asked to not assume pasted docs)
- Letting agents run write Cypher directly (no policy, no audit)
- Hard locks/mutexes on nodes (CAS is simpler and enough)
- Blocking on every observed convention (too many false alarms, and people would turn it off)

---

## 11. Schedule (submission Sun Oct 18 ~20:00 IST)

| When | What |
|---|---|
| **Tonight (done by me)** | everything in "Build now", E1 to E5, RESULTS.md, README |
| Fri day (you) | watch lessons 6 to 16, read this plan + RESULTS.md, run the demo (README), **rewrite the core Cypher in `precedent/queries/` and the policy function in your own hand** (rules 11-13), fix what you disagree with |
| Sat | Cursor adapter, web view, export, polish the demo on netbox, AI_USAGE.md |
| Sun | deploy (Render + FalkorDB), demo video, blog post, submit Track 03 + 02 (+01) |

## 12. Risks and rules

- **Rules 11-13**: you must be able to explain the model, queries and architecture, and do meaningful
  work yourself. I kept every core query in `precedent/queries/*.cypher` (small, commented) so you can
  rewrite them by hand. Log it honestly in `AI_USAGE.md` (I started it).
- **Public data only:** netbox is Apache-2.0 public. The synthetic data is generated.
- **False alarms kill adoption**: that's why only ≥ documented trust denies, and E2 measures it on real commits.
- **Hooks are per harness**: the server policy is the real gate, and the hooks are UX on top.
- **WSL RAM**: FalkorDB capped at 2 GB, and our graphs are tens of MB.

## 13. Demo script (3 min, on real netbox)

1. Fresh clone of netbox → `precedent init` → 40 s, 0 tokens: components, dependencies, people, 30+ guarded decisions from history.
2. Claude Code: "add a GraphQL type using graphene" → the **ask** prompt: "graphene was replaced by Strawberry in commit … (2024)". Say no → the agent uses Strawberry.
3. Claude Code: "write a migration for this field" → **denied**: "AGENTS.md:141 never write migrations manually" (approved during onboarding).
4. Agent makes a new choice → `record_decision` → a second session (another "person") sees it at SessionStart.
5. That second agent tries to overwrite a human decision → a pending proposal; the human approves in the native prompt; then `revert` → everything is back, nothing lost.
6. Numbers: E2 false-alarm rate on real commits, E3 zero unauthorized overwrites under concurrency, E4 hook p95.
