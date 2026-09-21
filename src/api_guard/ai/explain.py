"""Turns a verdict into something a human can act on.

Three outputs: what breaks for consumers, the migration that avoids breaking
them, and — only if the team decides to break anyway — a draft waiver entry.

Two constraints shape all of this.

**It cannot affect the build.** By the time anything here runs, the exit code
is already decided. Every failure path returns quietly. A gate that goes red
because an LLM provider had a bad minute is a gate people route around.

**The model is small.** Groq serves open models, so this sends a filtered,
pre-parsed list of changes rather than raw OpenAPI documents, asks for
structured output, and validates what comes back. It is not trusted to parse a
spec or to know the policy rules — those are computed here and handed over.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from api_guard.results import Status

if TYPE_CHECKING:
    from api_guard.verdict import RunResult

DEFAULT_MODEL = "openai/gpt-oss-120b"

# Small models lose the thread on long lists, and a report nobody reads is
# worth nothing anyway. The worst breakages are enough to act on.
_MAX_CHANGES = 8
_TIMEOUT = 45


class Explanation(BaseModel):
    """The structured answer requested from the model."""

    impact: str = Field(description="What breaks for existing consumers, in plain English.")
    migration: str = Field(description="How to make this change without breaking them.")
    severity_note: str = Field(
        default="",
        description="Anything a reviewer should notice that the rules cannot express.",
    )


def explain(result: RunResult) -> str | None:
    """Append an impact analysis to the report. Returns the markdown, or None.

    Never raises. Every failure — missing key, missing extra, provider down,
    malformed response — degrades to returning None, because the alternative is
    an advisory feature taking down a deployment gate.
    """
    try:
        return _explain(result)
    except Exception:  # noqa: BLE001 - advisory only; see the module docstring
        return None


def _explain(result: RunResult) -> str | None:
    if result.verdict is Status.PASSED and not result.changes:
        return None  # nothing to explain

    api_key = _api_key()
    if not api_key:
        return None

    changes = sorted(
        result.changes, key=lambda c: c.severity.rank, reverse=True
    )[:_MAX_CHANGES]
    if not changes:
        return None

    explanation = _ask(api_key, changes)
    if explanation is None:
        return None

    return _render(explanation, changes)


def _api_key() -> str | None:
    """Read the key, loading .env if python-dotenv happens to be installed.

    In CI the key arrives as a real environment variable from the secret store;
    .env is a local-development convenience, not the mechanism.
    """
    key = os.environ.get("GROQ_API_KEY")
    if key:
        return key
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        return None
    return os.environ.get("GROQ_API_KEY") or None


def _ask(api_key: str, changes: list) -> Explanation | None:
    from langchain_groq import ChatGroq

    model = ChatGroq(
        api_key=api_key,
        model=os.environ.get("GROQ_MODEL", DEFAULT_MODEL),
        temperature=0,
        timeout=_TIMEOUT,
        max_retries=1,
    )

    # The facts are computed here and handed over. The model is asked to
    # explain them, not to work them out: it never sees a spec file, so it
    # cannot decide what counts as breaking.
    facts = "\n".join(
        f"- [{c.severity}] {c.operation or ''} {c.path or ''}: {c.text} (rule: {c.id})"
        for c in changes
    )

    prompt = (
        "You are reviewing breaking changes to an HTTP API, detected by a "
        "deterministic diff tool. The analysis below is already correct; do not "
        "dispute it or re-classify anything.\n\n"
        f"Detected changes:\n{facts}\n\n"
        "Write, for the developer who is now blocked:\n"
        "1. impact - what breaks for existing consumers, concretely. Name the "
        "fields and endpoints. No preamble.\n"
        "2. migration - how to ship this without breaking them. Two rules of "
        "this system that you must respect:\n"
        "   * To retire an ENDPOINT: set `deprecated: true` on the operation "
        "(that exact key - there is no `x-deprecated`) plus an `x-sunset` date, "
        "ship that, and delete it after the date passes. No exception needed.\n"
        "   * A RESPONSE FIELD has no sunset mechanism. The breaking moment is "
        "demoting it from required to optional; once optional, deleting it is "
        "free. Suggest adding the replacement field first, then demoting.\n"
        "3. severity_note - optional, only if a reviewer would otherwise miss "
        "something.\n\n"
        "Be concise and specific. Prefer the deprecation route over an exception."
    )

    try:
        structured = model.with_structured_output(Explanation)
        answer = structured.invoke(prompt)
    except Exception:  # noqa: BLE001
        # Small models sometimes cannot produce valid structured output. Fall
        # back to prose rather than losing the explanation entirely.
        try:
            raw = model.invoke(prompt)
            text = getattr(raw, "content", "") or ""
            return Explanation(impact=text.strip(), migration="") if text.strip() else None
        except Exception:  # noqa: BLE001
            return None

    return answer if isinstance(answer, Explanation) else None


def _render(explanation: Explanation, changes: list) -> str:
    lines = [
        "## What this means",
        "",
        explanation.impact.strip(),
        "",
    ]

    if explanation.migration.strip():
        lines += ["### Suggested migration", "", explanation.migration.strip(), ""]

    if explanation.severity_note.strip():
        lines += ["> " + explanation.severity_note.strip(), ""]

    # The fingerprints are computed, not generated. Getting one wrong would
    # produce a waiver that silently matches nothing, so the model is never
    # asked to reproduce them.
    lines += [
        "### If you are breaking this deliberately",
        "",
        "Prefer the migration above. If the team decides to break it now, add "
        "this to `waivers.yaml` and fill in the reason — a waiver is reviewed "
        "in the pull request and kept in git history, so it needs to say "
        "something a reader can evaluate:",
        "",
        "```yaml",
    ]
    for change in changes:
        if change.fingerprint:
            lines += [
                f'- fingerprint: "{change.fingerprint}"',
                f"  id: {change.id}",
                f"  path: {change.path or ''}",
                '  reason: "TICKET-000 - why this is acceptable, and who confirmed consumers are migrated"',
                "  approved_by: your-name",
                "  expires: 2027-01-01",
            ]
    lines += ["```", ""]

    lines += [
        "<sub>Written by a language model from the diff above. Advisory only - "
        "it ran after the verdict and did not influence it.</sub>",
        "",
    ]
    return "\n".join(lines)
