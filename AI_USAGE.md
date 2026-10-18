# AI usage

Rule 11 asks for disclosure, so here it is plainly. I (Shantam) built Precedent during the event with **Claude Code**
as my coding assistant. I set the goals, made the product and design calls, rejected results I did not trust, and
wrote the core graph queries and the write policy myself. Claude Code generated most of the rest of the code.

## What I did

- **The problem and the constraints.** Agents silently undo past team decisions. Precedent had to start from zero on
  a real repo (code and git only, no docs, no LLM tokens), let agents write to a graph shared by people and
  harnesses without overwriting each other, and stay cheap in tokens. No OpenAI.
- **Data and evaluation.** I asked for real public repositories instead of a toy, and later for more of them,
  because one repo was not enough to trust the numbers. That is how the evals grew to 7 repos (NetBox, mitmproxy,
  paperless-ngx, Wagtail, Outline, Saleor, Sentry) and a with/without A/B on real Claude Code runs.
- **The core Cypher, by hand** (Oct 18): all 8 queries in [`precedent/queries/`](precedent/queries): `who_knows`,
  `component_of_file`, `search`, `history`, `as_of`, `lineage`, `impact`, `governing`. Claude explained the graph
  schema and each Cypher feature and reviewed my attempts; I wrote the queries and fixed them until they matched
  the original versions on real graphs (mitmproxy, NetBox, Saleor) and the test suite passed. The one-line
  comments at the top of each file were written by Claude.
- **The write policy** (`store.policy()`, Oct 18): I decided every rule (an agent cannot make a deny-level rule on
  its own, locked decisions go to the owning team, an agent touching a strong decision needs a human, only a human
  approves). Claude typed my answers into the function; it passes 1,120/1,120 combinations against the original.

## What Claude Code generated

- The plan and docs drafts (`docs/PLAN.md`, `docs/RESULTS.md`, this README's first drafts).
- The `precedent/` package around the queries: the cold-start scanner, read tools, write store, hooks, MCP server,
  CLI, and the first versions of all queries (which I then rewrote).
- `tests/`, `sim/` (synthetic monorepo and multi-agent simulation), `eval/` (cold start, history replay, agent A/B,
  scale bench, raw MCP wire capture).
- The precision audit labels (`eval/out/precision_labels.json`): labelled by Claude, each with its commit sha, so
  anyone can re-check them.
- Commits made by Claude Code carry a `Co-Authored-By: Claude` line.

## How it was verified

- 13 end-to-end tests against a live FalkorDB (`pytest tests`), on a git repo built on the fly.
- Cold start on 7 real public repositories; replay of 45,240 real later commits through the guard; 24 real Claude
  Code runs for the A/B; a multi-agent simulation with invariants checked on the graph. Results and raw outputs are
  in [`docs/RESULTS.md`](docs/RESULTS.md) and `eval/out/`.
- My hand-written queries were checked against the original versions on real graphs before replacing them.

## Before the event

A small proof of concept (`poc/`, not in this repo) existed before Oct 15. Nothing was copied from it: this is a new
design written during the event.
