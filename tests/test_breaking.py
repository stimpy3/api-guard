"""Tests for the verdict logic in the breaking check.

oasdiff is stubbed here so these run anywhere, fast, with no Docker. What is
under test is everything api-guard does *around* oasdiff: severity
thresholding, waiver interaction, and refusing to guess when the input looks
wrong.

The behaviour of oasdiff itself is pinned separately, in
test_oasdiff_behaviour.py, against the real binary.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from api_guard.checks import breaking
from api_guard.config import FailOn, PolicyConfig
from api_guard.policy import Waiver
from api_guard.results import Status

ERR, WARN, INFO = 3, 2, 1


def entry(fingerprint: str = "aaa1", level: int = ERR, **extra) -> dict:
    base = {
        "fingerprint": fingerprint,
        "id": "response-required-property-removed",
        "text": "removed the required property `email`",
        "level": level,
        "operation": "GET",
        "path": "/users",
    }
    base.update(extra)
    return base


@pytest.fixture
def stub(monkeypatch):
    """Replace the oasdiff subprocess with canned output."""

    def _stub(entries: list[dict]):
        monkeypatch.setattr(
            breaking, "_invoke_oasdiff", lambda base, rev, policy, root: entries
        )

    return _stub


def check(waivers: list[Waiver] | None = None, **policy_kwargs):
    policy = PolicyConfig(**policy_kwargs)
    return breaking.run(b"base", b"revision", policy, waivers or [], Path("."))


def waiver(fingerprint: str) -> Waiver:
    return Waiver(
        fingerprint=fingerprint,
        reason="an adequate explanation of the decision",
        approved_by="sohan",
        expires=date(2099, 1, 1),
    )


# --- severity thresholding ------------------------------------------------


def test_error_level_change_fails(stub) -> None:
    stub([entry(level=ERR)])
    result, changes, _ = check()
    assert result.status is Status.FAILED
    assert len(changes) == 1


def test_no_changes_passes(stub) -> None:
    stub([])
    result, _, _ = check()
    assert result.status is Status.PASSED


def test_warn_does_not_fail_at_default_threshold(stub) -> None:
    """Default fail_on is ERR, so a warning is reported but does not block."""
    stub([entry(level=WARN)])
    result, changes, _ = check()
    assert result.status is Status.PASSED
    assert len(changes) == 1, "the change should still be reported, just not blocking"


def test_warn_fails_when_threshold_lowered(stub) -> None:
    stub([entry(level=WARN)])
    result, _, _ = check(fail_on=FailOn.WARN)
    assert result.status is Status.FAILED


def test_info_never_blocks_even_at_warn_threshold(stub) -> None:
    stub([entry(level=INFO)])
    result, _, _ = check(fail_on=FailOn.WARN)
    assert result.status is Status.PASSED


def test_unknown_severity_is_an_error_not_a_guess(stub) -> None:
    """Fail loudly on something we do not understand.

    An unrecognised level means oasdiff changed underneath us. Silently
    treating it as harmless is how a gate quietly stops being one.
    """
    stub([entry(level=99)])
    result, _, _ = check()
    assert result.status is Status.ERROR
    assert "pin its version" in (result.detail or "")


# --- waiver interaction ---------------------------------------------------


def test_waived_change_does_not_block(stub) -> None:
    stub([entry(fingerprint="aaa1")])
    result, _, outcome = check(waivers=[waiver("aaa1")])
    assert result.status is Status.PASSED
    assert len(outcome.applied) == 1


def test_waiver_does_not_swallow_a_different_change(stub) -> None:
    """The failure that would matter most: one waiver silencing everything."""
    stub([entry(fingerprint="aaa1"), entry(fingerprint="bbb2")])
    result, changes, outcome = check(waivers=[waiver("aaa1")])
    assert result.status is Status.FAILED
    assert [c.fingerprint for c in changes] == ["bbb2"]
    assert len(outcome.applied) == 1


def test_waived_run_still_reports_what_was_waived(stub) -> None:
    """A silent waiver is no better than no gate — it must stay visible."""
    stub([entry(fingerprint="aaa1")])
    result, _, _ = check(waivers=[waiver("aaa1")])
    assert "waived" in result.summary


def test_unused_waiver_is_stale_not_fatal(stub) -> None:
    stub([])
    result, _, outcome = check(waivers=[waiver("unused")])
    assert result.status is Status.PASSED
    assert [w.fingerprint for w in outcome.stale] == ["unused"]


# --- failure output -------------------------------------------------------


def test_failure_lists_fingerprints_for_waiving(stub) -> None:
    """Whoever is blocked needs the fingerprint to write the waiver.

    Without it they must go and run oasdiff by hand to find it, and at that
    point they will reach for --err-ignore instead.
    """
    stub([entry(fingerprint="deadbeef")])
    result, _, _ = check()
    assert "deadbeef" in (result.detail or "")


def test_failure_mentions_the_deprecation_route(stub) -> None:
    """Point at the zero-exception path before offering the waiver."""
    stub([entry()])
    result, _, _ = check()
    assert "sunset" in (result.detail or "").lower()


# --- tool failures --------------------------------------------------------


def test_missing_oasdiff_is_an_error_not_a_failure(monkeypatch) -> None:
    """A missing tool must not look like a broken contract."""

    def boom(*_args, **_kwargs):
        raise breaking.OasdiffNotFound("oasdiff is not on PATH")

    monkeypatch.setattr(breaking, "_invoke_oasdiff", boom)
    result, _, _ = check()
    assert result.status is Status.ERROR


def test_oasdiff_crash_is_an_error_not_a_failure(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise breaking._OasdiffFailed("invalid spec")

    monkeypatch.setattr(breaking, "_invoke_oasdiff", boom)
    result, changes, _ = check()
    assert result.status is Status.ERROR
    assert changes == []
