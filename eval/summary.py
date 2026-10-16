"""One table from every result file in eval/out (cold starts, replays, agent A/B), as markdown.

    python -m eval.summary > eval/out/SUMMARY.md
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "out"
REPOS = ["netbox", "mitmproxy", "paperless-ngx", "wagtail", "outline", "saleor", "sentry"]


def load(name):
    f = OUT / name
    return json.loads(f.read_text()) if f.exists() else None


def main():
    print("## Cold start (HEAD, 0 LLM tokens)\n")
    print("| repo | files | components | libraries | decisions | enforced | observed only | people | seconds |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in REPOS:
        c = load(f"coldstart_{r}.json")
        if c:
            s = c["stats"]
            print(f"| {r} | {s['files']:,} | {s['components']} | {s['libraries']} | {s['decisions']} | {s['guarded_decisions']} | "
                  f"{s.get('observed_only', '')} | {s['people']} | {s['seconds']} |")
    print("\n## History replay (graph as of the cut, every later real commit checked)\n")
    print("| repo | cut | commits | false alarms | asks on real reversals | decisions caught (resurrected) | edits caught | "
          "decisions still true at HEAD | check p50 | files with context | tokens when shown |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    tot = {"commits": 0, "fa": 0}
    for r in REPOS:
        d = load(f"replay_{r}.json")
        if not d:
            continue
        tot["commits"] += d["replayed_commits"]
        tot["fa"] += d.get("flagged_commits_false_alarm", 0)
        print(f"| {r} | {d['cut']} | {d['replayed_commits']:,} | {d.get('flagged_commits_false_alarm', '?')} | "
              f"{d.get('flagged_commits_reversal', '?')} | {d['decisions_caught']}/{d['decisions_with_resurrection_tested']} | "
              f"{d['resurrection_caught']}/{d['resurrection_edits']} | {d.get('decisions_still_true_at_head', '?')}/{d.get('decisions_at_cut', '?')} | "
              f"{d['check_ms_p50']} ms | {d['read_context_nonempty']}/{d['read_context_files']} | {d['read_context_avg_tokens_when_shown']} |")
    print(f"\nTotal: {tot['commits']:,} real commits replayed, {tot['fa']} false-alarm commits.\n")
    print("## Agent A/B (headless Claude Code, Haiku 4.5)\n")
    print("| task | named | reintroduced off | reintroduced on | on: mentions decision | turns off/on | $ off/on |")
    print("|---|---|---|---|---|---|---|")
    ab = {}
    for f in sorted((OUT / "agent_ab").glob("*.json")):
        d = json.loads(f.read_text())
        ab.setdefault(d["task"], {})[d["arm"]] = d
    for t, arms in ab.items():
        o, n = arms.get("off"), arms.get("on")
        if o and n:
            print(f"| {t} | {o['named_in_prompt']} | {o['reintroduced']} | {n['reintroduced']} | {n['mentions_decision']} | "
                  f"{o['turns']}/{n['turns']} | {o['cost_usd']:.3f}/{n['cost_usd']:.3f} |")


if __name__ == "__main__":
    main()
