"""Aggregating the checks into one answer.

This module decides whether the build proceeds. It imports nothing from
`api_guard.ai` and must never do so: the AI layer explains results, it does not
produce them. `tests/test_boundaries.py` enforces that mechanically, because a
rule nobody checks is a rule that erodes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from api_guard.policy import WaiverOutcome
from api_guard.results import SCHEMA_VERSION, Change, CheckResult, Status

# Deliberately matching the convention of the tools we wrap, so any CI
# understands the result without an adapter.
EXIT_PASS = 0
EXIT_CONTRACT_VIOLATION = 1
EXIT_TOOL_ERROR = 2


class RunResult(BaseModel):
    """The full outcome of one api-guard run — this is `result.json`."""

    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    verdict: Status
    exit_code: int
    checks: list[CheckResult] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    waivers: WaiverOutcome = Field(default_factory=WaiverOutcome)
    meta: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.exit_code == EXIT_PASS


def decide(
    checks: list[CheckResult],
    changes: list[Change],
    waivers: WaiverOutcome,
    meta: dict[str, str],
) -> RunResult:
    """Fold the check results into a verdict.

    A tool error outranks a contract violation. If oasdiff could not run, we do
    not know whether the contract broke — reporting "breaking change detected"
    would send someone hunting for a change that may not exist.
    """
    if any(c.status is Status.ERROR for c in checks):
        verdict, exit_code = Status.ERROR, EXIT_TOOL_ERROR
    elif any(c.status is Status.FAILED for c in checks):
        verdict, exit_code = Status.FAILED, EXIT_CONTRACT_VIOLATION
    else:
        verdict, exit_code = Status.PASSED, EXIT_PASS

    return RunResult(
        verdict=verdict,
        exit_code=exit_code,
        checks=checks,
        changes=changes,
        waivers=waivers,
        meta=meta,
    )
