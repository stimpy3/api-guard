"""Waivers: recording that a breaking change was noticed and accepted anyway.

The gate's job is not to prevent breaking changes — requirements change, and a
tool that blocks every change gets switched off within a month. Its job is to
prevent *unacknowledged* ones. A waiver is that acknowledgement, and because
`waivers.yaml` is committed it gets reviewed in the pull request and preserved
in git history.

Two deliberate differences from oasdiff's built-in `--err-ignore` file:

**Matching is on `fingerprint`, not output text.** oasdiff's ignore file matches
against the human-readable wording of its own output, which changes between
versions. Its `fingerprint` field identifies the change itself — verified
stable when a spec is reformatted so every definition moves line (see
tests/fixtures/specs/README.md). So api-guard asks oasdiff for JSON with no
ignore file and does the filtering here, where it can also report what it did.

**Waivers expire.** An ignore file accumulates forever and silently swallows
future breakages on the same endpoint. An expired waiver fails the build,
forcing somebody to re-justify it or delete it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PolicyError(Exception):
    """A waiver file that is missing, malformed, or contains an expired entry.

    Reported as exit code 2 (tool/policy error) rather than 1 (contract
    violation): the API may be perfectly fine, it is the paperwork that is
    wrong, and sending someone to hunt for a breaking change that does not
    exist wastes their afternoon.
    """


class Waiver(BaseModel):
    """One acknowledged breaking change."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str = Field(
        min_length=4,
        description="oasdiff's fingerprint for the change. This is what is matched on.",
    )
    reason: str = Field(
        min_length=10,
        description=(
            "Why this break is acceptable. A ticket reference and who confirmed "
            "consumers are migrated. 'temp fix' is not a reason."
        ),
    )
    approved_by: str = Field(min_length=1, description="Who accepted the consequences.")
    expires: date = Field(description="After this date the waiver stops working.")

    # Context for human reviewers. Never used for matching: duplicating
    # oasdiff's identifiers here would just be a second thing to keep in sync.
    id: str | None = Field(default=None, description="oasdiff check id, for readers.")
    path: str | None = Field(default=None, description="Endpoint, for readers.")

    def is_expired(self, today: date) -> bool:
        return self.expires < today

    def describe(self) -> str:
        where = f" on {self.path}" if self.path else ""
        what = self.id or self.fingerprint
        return f"{what}{where}"


class WaiverOutcome(BaseModel):
    """What the waivers did to this run, for the report."""

    applied: list[Waiver] = Field(default_factory=list)
    stale: list[Waiver] = Field(default_factory=list)

    @property
    def any_applied(self) -> bool:
        return bool(self.applied)


def load_waivers(path: Path, *, today: date | None = None) -> list[Waiver]:
    """Read and validate a waivers file.

    Raises PolicyError on anything wrong, including expiry: validation happens
    once, here, so the checks downstream can assume every waiver is usable.
    """
    today = today or date.today()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read waivers file {path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path} is not valid YAML: {exc}") from exc

    if data is None:
        return []
    if not isinstance(data, list):
        raise PolicyError(
            f"{path} must contain a list of waivers, got {type(data).__name__}. "
            "An empty file is fine if nothing is currently waived."
        )

    waivers: list[Waiver] = []
    problems: list[str] = []

    for index, entry in enumerate(data):
        if not isinstance(entry, dict):
            problems.append(f"  entry {index + 1}: expected a mapping, got {type(entry).__name__}")
            continue
        try:
            waivers.append(Waiver(**entry))
        except ValidationError as exc:
            label = entry.get("fingerprint", f"entry {index + 1}")
            for error in exc.errors():
                field = ".".join(str(part) for part in error["loc"]) or "(root)"
                problems.append(f"  {label}: {field}: {error['msg']}")

    if problems:
        raise PolicyError(f"invalid waivers in {path}:\n" + "\n".join(problems))

    expired = [w for w in waivers if w.is_expired(today)]
    if expired:
        listed = "\n".join(
            f"  {w.describe()} — expired {w.expires.isoformat()}, approved by {w.approved_by}"
            for w in expired
        )
        raise PolicyError(
            f"{len(expired)} waiver(s) in {path} have expired:\n{listed}\n\n"
            "A waiver is a decision with a shelf life. Either the change is still "
            "acceptable — extend the date and say why — or it never was, and the "
            "waiver should go."
        )

    seen: dict[str, int] = {}
    for w in waivers:
        seen[w.fingerprint] = seen.get(w.fingerprint, 0) + 1
    duplicates = [fp for fp, count in seen.items() if count > 1]
    if duplicates:
        raise PolicyError(
            f"duplicate waiver fingerprint(s) in {path}: {', '.join(sorted(duplicates))}"
        )

    return waivers


def apply_waivers(
    changes: list[dict],
    waivers: list[Waiver],
) -> tuple[list[dict], WaiverOutcome]:
    """Filter waived changes out of oasdiff's results.

    Returns the changes that still count, plus a record of which waivers were
    used and which matched nothing. Stale waivers are reported but do not fail
    the build: the usual reason a waiver stops matching is that somebody fixed
    the underlying problem properly, and punishing that would be perverse.
    """
    waived_by_fingerprint = {w.fingerprint: w for w in waivers}
    matched: set[str] = set()
    remaining: list[dict] = []

    for change in changes:
        fingerprint = change.get("fingerprint")
        if fingerprint and fingerprint in waived_by_fingerprint:
            matched.add(fingerprint)
        else:
            remaining.append(change)

    outcome = WaiverOutcome(
        applied=[w for w in waivers if w.fingerprint in matched],
        stale=[w for w in waivers if w.fingerprint not in matched],
    )
    return remaining, outcome
