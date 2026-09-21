"""Check 2: would this change break existing consumers?

Wraps oasdiff, with one deliberate departure: **`--fail-on` is not passed.**

oasdiff's exit code cannot express what we need, because waivers are applied
after the fact — a run with three breaking changes, all waived, must come out
green. So oasdiff is asked for JSON and nothing else, api-guard filters the
waived entries, and the verdict is computed here.

That also sidesteps a trap. `oasdiff breaking` exits **0** when it finds
breaking changes unless `--fail-on` is given: it prints them and reports
success. A pipeline that trusted the bare exit code would pass every build
forever while dutifully listing the breakages in its log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from api_guard.config import PolicyConfig
from api_guard.policy import Waiver, WaiverOutcome, apply_waivers
from api_guard.results import Change, CheckResult, Status

NAME = "breaking"
_TIMEOUT = 120
_ENV_BIN = "API_GUARD_OASDIFF"


class OasdiffNotFound(Exception):
    """oasdiff is not installed. Exit code 2 — a tool problem, not a contract one."""


def _binary() -> str:
    """Locate oasdiff.

    The published image installs it at /usr/local/bin/oasdiff. The env var is an
    escape hatch for running against a different build locally.
    """
    override = os.environ.get(_ENV_BIN)
    if override:
        return override
    found = shutil.which("oasdiff")
    if not found:
        raise OasdiffNotFound(
            "oasdiff is not on PATH. Run api-guard from its Docker image, or set "
            f"{_ENV_BIN} to the binary's location."
        )
    return found


def run(
    base: bytes,
    revision: bytes,
    policy: PolicyConfig,
    waivers: list[Waiver],
    root: Path,
) -> tuple[CheckResult, list[Change], WaiverOutcome]:
    """Compare two specs and decide whether the difference blocks the build."""
    try:
        raw = _invoke_oasdiff(base, revision, policy, root)
    except OasdiffNotFound as exc:
        return (
            CheckResult(name=NAME, status=Status.ERROR, summary=str(exc)),
            [],
            WaiverOutcome(),
        )
    except _OasdiffFailed as exc:
        return (
            CheckResult(
                name=NAME,
                status=Status.ERROR,
                summary="oasdiff could not compare the specs",
                detail=str(exc),
            ),
            [],
            WaiverOutcome(),
        )

    remaining_raw, waiver_outcome = apply_waivers(raw, waivers)

    try:
        changes = [Change.from_oasdiff(entry) for entry in remaining_raw]
    except ValueError as exc:
        # An unrecognised severity level. Fail rather than guess: silently
        # downgrading something we do not understand is how gates rot.
        return (
            CheckResult(
                name=NAME,
                status=Status.ERROR,
                summary="oasdiff returned a severity level api-guard does not recognise",
                detail=f"{exc}. This usually means oasdiff was upgraded; pin its version.",
            ),
            [],
            waiver_outcome,
        )

    threshold = policy.fail_on
    blocking = [c for c in changes if c.severity.rank >= _rank(threshold)]

    return (
        _verdict(blocking, changes, waiver_outcome, threshold),
        changes,
        waiver_outcome,
    )


def _rank(fail_on: str) -> int:
    return {"WARN": 2, "ERR": 3}[str(fail_on)]


def _verdict(
    blocking: list[Change],
    all_changes: list[Change],
    waivers: WaiverOutcome,
    threshold: str,
) -> CheckResult:
    waived_note = (
        f" ({len(waivers.applied)} waived)" if waivers.any_applied else ""
    )

    if blocking:
        listed = "\n".join(f"  [{c.severity}] {c.describe()}" for c in blocking)
        return CheckResult(
            name=NAME,
            status=Status.FAILED,
            summary=f"{len(blocking)} breaking change(s) at or above {threshold}{waived_note}",
            detail=(
                f"{listed}\n\n"
                "If this change is intentional, it needs to be acknowledged rather "
                "than forced through:\n"
                "  * retiring an endpoint? deprecate it with an x-sunset date and "
                "remove it after that date\n"
                "  * otherwise add a waiver to waivers.yaml with a reason, an "
                "approver and an expiry date\n\n"
                "Fingerprints for waivers:\n"
                + "\n".join(f"  {c.fingerprint}  # {c.id}" for c in blocking)
            ),
        )

    if all_changes:
        return CheckResult(
            name=NAME,
            status=Status.PASSED,
            summary=(
                f"no changes at or above {threshold}; "
                f"{len(all_changes)} below threshold{waived_note}"
            ),
            detail="\n".join(f"  [{c.severity}] {c.describe()}" for c in all_changes),
        )

    return CheckResult(
        name=NAME,
        status=Status.PASSED,
        summary=f"no breaking changes{waived_note}",
    )


class _OasdiffFailed(Exception):
    pass


def _invoke_oasdiff(base: bytes, revision: bytes, policy: PolicyConfig, root: Path) -> list[dict]:
    """Write both specs to temp files and run oasdiff over them.

    Temp files rather than piping: oasdiff takes two paths, and the base spec
    comes from `git show`, so it has no path on disk of its own.
    """
    with tempfile.TemporaryDirectory(prefix="api-guard-") as tmp:
        tmp_dir = Path(tmp)
        base_path = tmp_dir / "base.yaml"
        revision_path = tmp_dir / "revision.yaml"
        base_path.write_bytes(base)
        revision_path.write_bytes(revision)

        command = [
            _binary(),
            "breaking",
            str(base_path),
            str(revision_path),
            "--format",
            "json",
            # Deliberately NO --fail-on: see the module docstring.
            "--deprecation-days-stable",
            str(policy.deprecation_days_stable),
            "--deprecation-days-beta",
            str(policy.deprecation_days_beta),
        ]
        if policy.severity_levels is not None:
            levels = policy.severity_levels
            command += ["--severity-levels", str(levels if levels.is_absolute() else root / levels)]

        try:
            completed = subprocess.run(command, capture_output=True, timeout=_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise _OasdiffFailed(f"oasdiff timed out after {_TIMEOUT}s") from exc

        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()

        # Without --fail-on, a non-zero exit means oasdiff itself failed —
        # an unreadable or invalid spec, not a breaking change.
        if completed.returncode != 0:
            raise _OasdiffFailed(
                f"oasdiff exited {completed.returncode}.\n{stderr or stdout}"
            )

        if not stdout:
            return []

        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise _OasdiffFailed(f"could not parse oasdiff output as JSON: {exc}\n{stdout}") from exc

        if not isinstance(parsed, list):
            raise _OasdiffFailed(f"expected a JSON list from oasdiff, got {type(parsed).__name__}")

        return parsed
