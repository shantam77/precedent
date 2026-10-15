"""MCP server (stdio): the tools a coding agent (Claude Code, Cursor, ...) uses. Registered by `precedent install`.

Reads are free. Writes go through store.policy(): an agent's writes are applied, refused, or turned into proposals.
approve() counts as a human action only when PRECEDENT_HUMAN_GATE=1, i.e. when the harness is configured to ask
the user before this tool runs (precedent install does that with permissions.ask + a PreToolUse hook).
"""

import os
import uuid

from mcp.server.mcpserver import MCPServer

from . import store, tools

SESSION = f"mcp-{uuid.uuid4().hex[:8]}"
mcp = MCPServer("precedent", instructions=(
    "Precedent is this repo's shared decision graph (FalkorDB): what was decided, why, by whom, what depends on it. "
    "Hooks already check your edits and add context when you read files, so you rarely need to call it. Call `why` "
    "or `impact` before risky changes, and `record_decision` after a real choice (library, pattern, rule) with paths, "
    "a rationale and evidence (file:line, commit:sha, or user: quote). To replace a decision use `supersede` with "
    "the version you read. Never try to get around a deny: tell the user."))


def agent():
    return store.Actor(store.config.actor_email(), "agent", store.config.HARNESS, SESSION)


def text(r):
    return r["text"] if isinstance(r, dict) else r


# ---------------------------------------------------------------- read

@mcp.tool()
def context(paths: list[str]) -> str:
    """Active decisions and code notes for these files (ids, trust, source). Empty means nothing applies."""
    return tools.context(paths)[0] or "No decisions or notes for these files."


@mcp.tool()
def check(path: str, new_text: str, old_text: str = "") -> str:
    """Would adding new_text to path break a decision? verdict ok | ask | deny, with the reason and source."""
    r = tools.check(path, new_text, old_text or None)
    return f"verdict: {r['verdict']}\n{r['text']}".strip()


@mcp.tool()
def why(target: str, as_of: str | None = None) -> str:
    """Explain a decision (D-12), a library (e.g. 'graphene-django') or a topic: rationale, source, lineage, changes.
    as_of='YYYY-MM-DD' lists what was in force on that date."""
    return tools.why(target, as_of)


@mcp.tool()
def impact(target: str) -> str:
    """Components that depend on this component or file (current imports, up to 3 hops) and their decisions."""
    return tools.impact(target)


@mcp.tool()
def who_knows(target: str) -> str:
    """Owner team and the people who know this component or file best (from git history)."""
    return tools.who_knows(target)


@mcp.tool()
def history(hours: int = 24) -> str:
    """Recent changes to decisions by anyone (people and agents), newest first."""
    return tools.history(tools.now_ms() - hours * 3600 * 1000)


@mcp.tool()
def search(text: str) -> str:
    """Full-text search over decision titles and rationales."""
    return tools.search(text)


@mcp.tool()
def query(cypher: str) -> str:
    """Read-only Cypher over the graph (writes are refused, LIMIT 50 added). Labels: Component, File, Library, Person,
    Team, Decision, Commit, Session, Change, Note. Rels: IN, DEPENDS_ON, USES, KNOWS, OWNS, MEMBER_OF, ABOUT, CHOSE,
    REJECTED, SUPERSEDES, EVIDENCED_BY, MADE, ON, EDITED, SAW, FLAGGED, HAS_NOTE, PROPOSES."""
    return tools.query(cypher)


# ---------------------------------------------------------------- write (policy-gated)

@mcp.tool()
def record_decision(title: str, rationale: str, paths: list[str] | None = None, components: list[str] | None = None,
                    chose: list[str] | None = None, rejected: list[str] | None = None, forbid: list[str] | None = None,
                    evidence: list[str] | None = None, trust: str | None = None, force_new: bool = False) -> str:
    """Record a decision you made or found. paths: globs it governs. chose/rejected: library names.
    forbid: strings that must not be added under those paths ('lib:<name>' for libraries, 'any' = no edits at all).
    evidence: 'file:line', 'commit:sha', 'user: <quote>'. trust='documented' (a rule from the repo's docs) makes it a
    proposal a human approves. Refused if it duplicates or contradicts an active decision (use supersede)."""
    return text(store.record_decision(agent(), title, rationale, paths or [], components or [], chose or [],
                                      rejected or [], forbid or [], evidence or [], trust, force_new=force_new))


@mcp.tool()
def supersede(decision_id: str, expected_version: int, title: str, rationale: str, paths: list[str] | None = None,
              chose: list[str] | None = None, rejected: list[str] | None = None, forbid: list[str] | None = None,
              evidence: list[str] | None = None) -> str:
    """Replace a decision with a new one. expected_version = the version you read (e.g. 'D-12 v2' -> 2).
    Decisions with trust documented/human (or locked) become a proposal for a human; the old one stays in force."""
    return text(store.supersede(agent(), decision_id, expected_version, title, rationale, paths or [], [],
                                chose or [], rejected or [], forbid or [], evidence or []))


@mcp.tool()
def amend(decision_id: str, expected_version: int, rationale: str | None = None, add_paths: list[str] | None = None,
          add_forbid: list[str] | None = None, add_evidence: list[str] | None = None) -> str:
    """Add detail to your own (or an agent-level) decision without replacing it."""
    return text(store.amend(agent(), decision_id, expected_version, rationale, add_paths or [], add_forbid or [],
                            add_evidence or []))


@mcp.tool()
def retire(decision_id: str, expected_version: int, reason: str) -> str:
    """End a decision that no longer holds (it stays in history). Protected ones become a proposal."""
    return text(store.retire(agent(), decision_id, expected_version, reason))


@mcp.tool()
def revert(change_id: str, reason: str) -> str:
    """Undo one of YOUR changes from this session (C-123). Other changes need a human."""
    return text(store.revert(agent(), change_id, reason))


@mcp.tool()
def pending() -> str:
    """Proposals waiting for a human."""
    ps = tools.pending()
    return "\n".join(f"- {p['id']} {p['title']} (by {p['actor']}" + (f", replaces {p['replaces']})" if p["replaces"] else ")")
                     for p in ps) or "No pending proposals."


@mcp.tool()
def approve(proposal_id: str) -> str:
    """Approve a proposal. Only works when the harness asks the user first (the user is the one approving)."""
    a = agent()
    if os.getenv("PRECEDENT_HUMAN_GATE") == "1":
        a.kind = "human"
    return text(store.approve(a, proposal_id))


if __name__ == "__main__":
    mcp.run()
