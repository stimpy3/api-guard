"""Pins the behaviour of the real oasdiff binary.

Everything in this file was discovered by running oasdiff rather than reading
its documentation, and several results contradicted what the docs implied.
These are regression tests: if an oasdiff upgrade changes any of them, the
assumptions api-guard is built on have moved and somebody needs to know before
the gate starts quietly passing things.

Skipped when oasdiff is not on PATH, so the suite still runs on a developer
laptop. They execute inside the Docker image, which is where the release
pipeline runs them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SPECS = Path(__file__).parent / "fixtures" / "specs"

pytestmark = pytest.mark.skipif(
    shutil.which("oasdiff") is None,
    reason="oasdiff not on PATH; run inside the api-guard image",
)


def oasdiff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["oasdiff", *args], capture_output=True, text=True, timeout=120
    )


def breaking(base: str, revision: str, *extra: str) -> subprocess.CompletedProcess:
    return oasdiff("breaking", str(SPECS / base), str(SPECS / revision), *extra)


def changes(base: str, revision: str, *extra: str) -> list[dict]:
    result = breaking(base, revision, "--format", "json", *extra)
    return json.loads(result.stdout) if result.stdout.strip() else []


def ids(base: str, revision: str, *extra: str) -> set[str]:
    return {c["id"] for c in changes(base, revision, *extra)}


# --- the trap -------------------------------------------------------------


def test_breaking_exits_zero_without_fail_on() -> None:
    """THE bug that would silently disable the whole gate.

    `oasdiff breaking` reports breaking changes and then exits 0. A pipeline
    that trusted the bare exit code would go green on every build forever,
    while dutifully printing the breakages in its log. api-guard therefore
    never relies on this exit code — it parses the JSON and decides itself.
    """
    result = breaking("base.yaml", "breaking-property-removed.yaml", "--format", "json")
    assert result.returncode == 0, "if this now fails, oasdiff changed its default"
    assert json.loads(result.stdout), "...but it definitely found a breaking change"


def test_fail_on_err_is_what_produces_a_nonzero_exit() -> None:
    assert breaking(
        "base.yaml", "breaking-property-removed.yaml", "--fail-on", "ERR"
    ).returncode == 1
    assert breaking(
        "base.yaml", "safe-property-added.yaml", "--fail-on", "ERR"
    ).returncode == 0


def test_fail_on_rejects_info() -> None:
    """Documented as valid, actually refused. config.FailOn only allows ERR/WARN."""
    assert breaking(
        "base.yaml", "breaking-property-removed.yaml", "--fail-on", "INFO"
    ).returncode != 0


# --- fingerprints ---------------------------------------------------------


def test_fingerprint_identifies_the_change_not_its_location() -> None:
    """Why waivers key on fingerprint.

    base-shifted.yaml describes an identical API with comments and blank lines
    pushing every definition onto a different line. If the fingerprint tracked
    position, reformatting a spec would silently invalidate every waiver.
    """
    original = changes("base.yaml", "breaking-property-removed.yaml")
    shifted = changes("base-shifted.yaml", "breaking-property-removed.yaml")
    assert [c["fingerprint"] for c in original] == [c["fingerprint"] for c in shifted]
    assert original[0]["fingerprint"], "fingerprint must not be empty"


def test_distinct_changes_get_distinct_fingerprints() -> None:
    """Otherwise one waiver would silence unrelated breakages."""
    entries = changes("deprecated-sunset-future.yaml", "deprecated-removed.yaml")
    prints = [c["fingerprint"] for c in entries]
    assert len(prints) == len(set(prints)) > 1


# --- severity encoding ----------------------------------------------------


def test_severity_levels_are_integers_we_expect() -> None:
    """Severity::from_oasdiff_level maps these; a change here misclassifies."""
    entries = json.loads(
        oasdiff(
            "changelog",
            str(SPECS / "base.yaml"),
            str(SPECS / "breaking-property-removed.yaml"),
            "--format",
            "json",
        ).stdout
    )
    by_id = {c["id"]: c["level"] for c in entries}
    assert by_id["response-required-property-removed"] == 3, "error"
    assert by_id["response-required-property-added"] == 1, "info"


# --- additive changes -----------------------------------------------------


def test_adding_an_optional_property_is_not_breaking() -> None:
    """A gate that blocks additive changes gets switched off within a month."""
    assert changes("base.yaml", "safe-property-added.yaml") == []


# --- deprecation and sunset ----------------------------------------------


def test_endpoint_removal_after_sunset_is_clean() -> None:
    """Tier 1 of the change policy: announce, wait, delete. No waiver needed."""
    assert "api-path-removed-before-sunset" not in ids(
        "deprecated-sunset-past.yaml", "deprecated-removed.yaml"
    )


def test_endpoint_removal_before_sunset_is_breaking() -> None:
    """The promise is enforced, not merely recorded."""
    assert "api-path-removed-before-sunset" in ids(
        "deprecated-sunset-future.yaml", "deprecated-removed.yaml"
    )


def test_x_sunset_on_a_response_property_is_ignored() -> None:
    """The finding that redesigned demo scenarios 6 and 7.

    A field marked deprecated with a sunset date that has already passed is
    STILL reported when removed, because it is required. Sunset semantics cover
    endpoints only — so response fields have no zero-exception retirement path,
    which is exactly why the waiver tier exists.
    """
    assert "response-required-property-removed" in ids(
        "deprecated-sunset-past.yaml", "deprecated-removed.yaml"
    )


def test_removing_an_optional_property_is_clean() -> None:
    assert changes("deprecated-optional-sunset-past.yaml", "deprecated-removed.yaml") == []


def test_deprecation_metadata_changes_nothing_for_properties() -> None:
    """Optionality is the only thing that matters, with or without deprecation."""
    assert changes("optional-no-deprecation.yaml", "deprecated-removed.yaml") == []


def test_demoting_a_required_property_is_the_breaking_moment() -> None:
    """For a field the cost is paid at demotion, not at deletion.

    Consumers relied on the field always being present; making it optional is
    what breaks them. Once optional, the eventual removal is free.
    """
    assert "response-property-became-optional" in ids(
        "base.yaml", "optional-no-deprecation.yaml"
    )
