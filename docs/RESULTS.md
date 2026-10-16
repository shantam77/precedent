# Results (Oct 17, 2026: seven real repositories)

Everything here is reproducible from this repo; raw outputs are in [`eval/out/`](../eval/out) and logs in
[`eval/logs/`](../eval/logs). Real data: seven public repositories, cloned as is, nothing prepared by hand:
NetBox, mitmproxy, paperless-ngx, Wagtail, Outline (TypeScript), Saleor, Sentry (112,758 commits).
Synthetic data (volume and concurrency): [`sim/world.py`](../sim/world.py).

## Summary

| Question | Answer, measured |
|---|---|
| Does cold start work on any repo, with no docs? | **Yes, on all 7.** 0 LLM tokens. NetBox 11 s, Saleor 38 s, Sentry (112k commits, 21.7k files) 8 min. Every decision cites its commit |
| Are the mined decisions real? | **115/120 (96%)** of a random sample (20 enforced decisions per repo, 6 repos) are deliberate removals with the right commit ([labels](../eval/out/precision_labels.json), labelled by Claude: spot-check them) |
| Is the guard noisy on real work? | **0 false alarms on 18,242 real commits** replayed from 6 repositories (final code). The 7 asks were all real reversals (the library came back later). Two false-alarm bugs were found by these replays and fixed tonight (E2) |
| Does it catch real regressions? | Putting back the real code deleted when a library was dropped: **220/221 decisions caught** (1,140/1,428 edits); **98%** of decisions mined at the cut were still true at HEAD (746/765) |
| Does a real agent behave differently with it? | **A/B, 12 tasks × 2 arms, headless Claude Code (Haiku 4.5), 6 repos:** when the prompt names a library the team removed on purpose, the agent reintroduced it in **6/9 without** Precedent and **0/9 with** it (all 6 cited the decision and asked, or used the replacement). Unnamed tasks: 0/3 in both arms. 24 runs, **$1.78**; the with-Precedent runs used 17% fewer input tokens |
| Many agents on one graph? | 8 agents + 2 humans, 1,128 agent ops with injected mistakes: **0 unexpected outcomes, 0 protected changes without a human, 0 deletions, 39 lost races answered "stale"** |
| Tokens? | The Read hook adds context to **9% (NetBox) to 65% (Wagtail, Outline)** of the files people touched, **~50-120 tokens** each time, and each decision only once per session. Edits that break nothing add **0** tokens (the hook says nothing). Repos with many mined JS/tooling decisions get context more often: a place to tune (e.g. hide `observed` ones) |
| Scale? | `check()` **2.5 ms p50 flat** from 5k to 80k files (19k decisions, 216k edges) |

## E1. Cold start on seven repositories

`python -m eval.coldstart ../_data/<repo> ...` (graph per repo, at HEAD).

| repo | files | components | libraries | decisions | enforced | observed only | people | seconds |
|---|---|---|---|---|---|---|---|---|
| netbox | 2,297 | 27 | 152 | 45 | 23 | 21 | 459 | 11.3 |
| mitmproxy | 1,295 | 25 | 221 | 119 | 63 | 47 | 602 | 10.5 |
| paperless-ngx | 1,566 | 30 | 271 | 104 | 46 | 57 | 440 | 14.0 |
| wagtail | 5,243 | 34 | 241 | 103 | 48 | 44 | 1004 | 18.0 |
| outline | 2,860 | 51 | 502 | 249 | 127 | 119 | 250 | 18.3 |
| saleor | 4,724 | 72 | 427 | 326 | 201 | 97 | 297 | 38.2 |
| sentry | 21,739 | 192 | 952 | 560 | 342 | 195 | 956 | 480.3 |

What the scanner reads: `git ls-files` (components), Python AST + JS/TS import strings (dependencies, library uses),
every manifest that ever existed (requirements*, requirements/*.txt|in, pyproject (PEP 621 / PEP 735 / Poetry / uv / PDM),
Pipfile, setup.py/cfg, package.json; lock files skipped), the full first-parent history of those manifests (`-U999999`:
the whole file per commit), `git log --name-only` (people, aliases merged with union-find), and rule lines from
AGENTS.md / CLAUDE.md / CONTRIBUTING / ADR folders.

**Confidence grading (new):** a removal becomes an enforced (`historical`) decision only if the commit names the library,
says remove/drop/replace (and is not about a pin), or adds a replacement. Lock-file churn ("Bump dependencies" dropping a
transitive package) becomes `observed`: context only, never asks or blocks. Format migrations (8+ packages and >50% of a
scope vanish in one commit) never become decisions.

**Precision audit:** [precision_sample.json](../eval/out/precision_sample.json) (20 random enforced decisions per repo,
seed 7) and [precision_labels.json](../eval/out/precision_labels.json): NetBox 18/20, mitmproxy 18/20, Saleor 20/20,
paperless 20/20, Wagtail 19/20, Outline 20/20. The 5 misses: a packaging switch (psycopg2 → psycopg2-binary), a tool moved to
the install script (wheel), two wrong-evidence commits, and one parser bug (a Python version "3.6.0" read as a package:
fixed, names must start with a letter).

## E2. History replay: real commits through the guard

`bash eval/run_replays.sh <repos>` → `python -m eval.replay ../_data/<repo> --cut 2024-01-01`: build the graph as it was on
the cut date (history up to then), run **every real non-merge commit after the cut** through `check()`, then test
"resurrections" (put back the real lines deleted when a library was dropped). New in this round: each flag is labelled
automatically: a **reversal** if the library is back in a manifest at HEAD (asking is the right behaviour), otherwise a
**false alarm**; and "decisions still true at HEAD" measures how durable the mined decisions were.

| repo | cut | commits | false alarms | asks on real reversals | decisions caught (resurrected) | edits caught | decisions still true at HEAD | check p50 | files with context | tokens when shown |
|---|---|---|---|---|---|---|---|---|---|---|
| netbox | 2024-01-01 | 3,579 | 0 | 1 | 13/13 | 43/56 | 31/37 | 8.41 ms | 128/1500 | 60 |
| mitmproxy | 2024-01-01 | 779 | 0 | 4 | 54/55 | 155/202 | 87/90 | 12.52 ms | 477/818 | 52 |
| paperless-ngx | 2024-01-01 | 3,465 | 0 | 0 | 27/27 | 44/86 | 72/72 | 21.65 ms | 454/1420 | 96 |
| wagtail | 2024-01-01 | 4,159 | 0 | 2 | 46/46 | 98/132 | 96/104 | 27.78 ms | 975/1500 | 58 |
| outline | 2024-01-01 | 4,645 | 0 | 0 | 40/40 | 108/199 | 167/169 | 20.66 ms | 978/1500 | 57 |
| saleor | 2024-01-01 | 1,615 | 0 | 0 | 40/40 | 692/753 | 293/293 | 32.87 ms | 899/1500 | 119 |

Total: 18,242 real commits replayed, 0 false-alarm commits.

Outline and Saleor: resurrection tested on a random sample of 40 guarded decisions (`--max-res 40`). Sentry (cut 2025-06-01, 26,998 commits after the cut) is still running; its row is added when it finishes.

Found and fixed during this round:
- paperless-ngx v1: **1 false alarm** in 3,465 commits: a pytest marker line `"tika: Tests requiring Tika service"` in
  pyproject.toml matched the removed `tika`. A manifest match now needs a version, quote or end of line after the name.
  Re-run: see the table ([v1 kept](../eval/out/replay_paperless-ngx_v1_before_marker_fix.json)).
- Reversal asks repeat on later version bumps of the same re-added library (mitmproxy's twine: 1 reversal, 4 commits),
  because the replay never applies the human's approval. In real use, approving the first ask supersedes the decision.
- Line-level misses are mostly docs, comments, CSS/SCSS, gulp/webpack config strings, and lock-like build files.

## E3. Agent A/B (new)

[`eval/agent_ab.py`](../eval/agent_ab.py): 12 tasks on 6 repos, each a normal request touching a library the repo removed on
purpose (9 name it, 3 don't). OFF = Precedent's files moved aside; ON = `precedent install` + cold-start graph. Outcome =
the final working tree (any added line that imports/requires the library or adds it to a manifest), not the agent's words.
Model: Haiku 4.5 via `claude -p` (`--max-budget-usd 0.25` per run).

| task | named | reintroduced off | reintroduced on | on: mentions decision | turns off/on | $ off/on |
|---|---|---|---|---|---|---|
| mitmproxy_blinker | True | False | False | True | 11/8 | 0.098/0.077 |
| netbox_cacheops | True | True | False | True | 6/7 | 0.142/0.078 |
| netbox_drfyasg | True | False | False | False | 3/3 | 0.069/0.064 |
| outline_koarouter | True | False | False | False | 21/16 | 0.182/0.120 |
| outline_lodash | False | False | False | False | 9/8 | 0.104/0.091 |
| paperless_bleach | True | True | False | True | 10/8 | 0.080/0.063 |
| paperless_tqdm | False | False | False | False | 4/4 | 0.061/0.056 |
| saleor_html2text | False | False | False | False | 3/3 | 0.034/0.040 |
| saleor_markdown | True | True | False | True | 7/6 | 0.054/0.043 |
| saleor_psycopg2 | True | True | False | True | 5/8 | 0.058/0.068 |
| wagtail_html5lib | True | True | False | True | 4/3 | 0.046/0.047 |
| wagtail_tablib | True | True | False | True | 4/4 | 0.049/0.052 |

Honest reading: the effect is on requests that go against a past decision (6/9 → 0/9). When the code already shows the
way (drf-yasg in NetBox, koa-router in Outline, blinker in mitmproxy) or the request doesn't name the old library, a good
agent often does the right thing anyway. n = 1 per task.

## E4. Many agents, one graph (unchanged, see eval/out/multi_agent.json)

8 agent threads + 2 humans on a synthetic monorepo (400 components, 20k files, 5k decisions); 30% of supersedes hit the same
25 decisions. 1,128 agent ops: 173 applied, 92 duplicates refused, 73 conflicts refused, 165 supersedes applied + 39 lost
races (stale), 76 stale versions refused, 109 protected overwrites turned into proposals, 43 self-approvals refused,
45 foreign reverts refused, 313 violations asked/denied. **0 unexpected.** Invariants (all 0): protected ended without a
human, double successors, endings without a Change, ended without valid_to. Decisions deleted: 0 (5,001 → 5,448).

## E5. Scale (unchanged, eval/out/bench.json)

| components | files | decisions | edges | check p50 / p95 | context p50 | impact p50 |
|---|---|---|---|---|---|---|
| 100 | 5,000 | 1,200 | 13k | 2.6 / 3.7 ms | 3.7 ms | 1.4 ms |
| 400 | 20,000 | 4,800 | 54k | 2.7 / 3.8 ms | 3.8 ms | 1.6 ms |
| 1,600 | 80,000 | 19,200 | 216k | 2.5 / 3.7 ms | 3.8 ms | 2.1 ms |

Hook end to end: ~0.25 s (daemon) vs ~0.8 s in-process; the graph work is ~3 ms.

## E6. MCP on the wire

[`eval/mcp_wire.py`](../eval/mcp_wire.py) → [`eval/out/mcp_wire.txt`](../eval/out/mcp_wire.txt): raw JSON-RPC over stdio with
the MCP Python SDK 2.3.0, protocol **2026-07-28** (stateless: no `initialize`, `_meta.protocolVersion` +
`clientCapabilities` on every request, `server/discover`, `resultType` on results).

## Earlier single-repo runs (Oct 16)

NetBox-only results, real Claude Code case studies (onboarding → 7 proposals, protected-rule overwrite → proposal D-57,
teammate catch-up in 1 turn), and the git pre-commit gate are in [`eval/out/claude/`](../eval/out/claude) and the git
history of this file.

## Where Precedent does not help (yet)

- Mined decisions are about libraries. Non-literal rules ("don't reach across app boundaries") are context only.
- The A/B is small (12 tasks, n = 1, one model).
- The precision audit was labelled by Claude, not a human (each row has its sha).
- Structure and imports come from HEAD in the replay (only decisions are time-cut).
- Hook latency (~0.25 s) is mostly Python start-up.
