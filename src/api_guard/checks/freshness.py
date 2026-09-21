"""Check 1: does the committed spec still match the code?

Without this the other two checks can both pass against a stale contract. A
developer renames a field, forgets to regenerate `openapi.yaml`, and oasdiff
sees no diff because the file never changed — a green build on a broken API.

api-guard does not know how to generate a spec. The project supplies one, which
is what lets a single tool serve FastAPI, Spring Boot and hand-written YAML
alike. There are two ways to hand it over, and which you want depends on where
api-guard is running:

**`--generated-spec PATH` — the pipeline already generated it.** Use this when
api-guard runs from its Docker image. The image deliberately contains no
project dependencies, so it *cannot* execute a project's generator: running
`python scripts/export_openapi.py` inside it fails with ModuleNotFoundError
because FastAPI lives in the application's environment, not the tool's. The
pipeline generates the spec where those dependencies exist — in the app's own
container or build step — and passes the file in.

**`spec.generate_cmd` — api-guard runs it.** Convenient when api-guard is
pip-installed alongside the project and shares its interpreter, which is the
normal local-development case.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from api_guard.results import CheckResult, Status

NAME = "freshness"
_TIMEOUT = 120


def run(
    committed: bytes,
    *,
    generated: bytes | None = None,
    generate_cmd: str | None = None,
    root: Path,
) -> CheckResult:
    """Compare the committed spec against a freshly generated one."""
    if generated is not None:
        return _compare(generated, committed, source="--generated-spec")

    if generate_cmd is None:
        return CheckResult(
            name=NAME,
            status=Status.SKIPPED,
            summary="no generated spec supplied",
            detail=(
                "The spec is assumed to be maintained by hand. To verify it still "
                "matches the code, either pass --generated-spec PATH (recommended "
                "when running from the Docker image) or set `spec.generate_cmd` "
                "(when api-guard shares the project's environment)."
            ),
        )

    try:
        completed = subprocess.run(
            generate_cmd,
            shell=True,  # noqa: S602 - the command comes from the project's own config
            cwd=root,
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=NAME,
            status=Status.ERROR,
            summary=f"spec.generate_cmd timed out after {_TIMEOUT}s",
            detail=f"Command: {generate_cmd}",
        )

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        hint = ""
        if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
            hint = (
                "\n\nThe generator could not import the project's own dependencies. "
                "If api-guard is running from its Docker image that is expected — "
                "the image contains oasdiff and Schemathesis, not your application's "
                "packages. Generate the spec in the project's environment and pass "
                "it in with --generated-spec instead."
            )
        return CheckResult(
            name=NAME,
            status=Status.ERROR,
            summary=f"spec.generate_cmd failed (exit {completed.returncode})",
            detail=f"Command: {generate_cmd}\n\n{stderr}{hint}",
        )

    return _compare(completed.stdout, committed, source=generate_cmd)


def _compare(generated: bytes, committed: bytes, *, source: str) -> CheckResult:
    if generated == committed:
        return CheckResult(
            name=NAME,
            status=Status.PASSED,
            summary="committed spec matches the code",
        )

    return CheckResult(
        name=NAME,
        status=Status.FAILED,
        summary="committed spec is out of date",
        detail=_explain(generated, committed, source),
    )


def _explain(generated: bytes, committed: bytes, generate_cmd: str) -> str:
    """Say what differs, and pre-empt the line-endings red herring.

    CRLF vs LF is the most common cause of this check failing on Windows, and
    it is invisible in a normal diff — worth calling out explicitly rather than
    letting someone lose an afternoon to it.
    """
    lines = [
        "The spec generated from the current code differs from the committed file.",
        "",
        f"  generated : {len(generated)} bytes",
        f"  committed : {len(committed)} bytes",
    ]

    crlf_generated = b"\r\n" in generated
    crlf_committed = b"\r\n" in committed
    if crlf_generated != crlf_committed:
        lines += [
            "",
            "Line endings differ, which is almost certainly the whole problem:",
            f"  generated uses {'CRLF' if crlf_generated else 'LF'}",
            f"  committed uses {'CRLF' if crlf_committed else 'LF'}",
            "",
            "Add `*.yaml text eol=lf` to .gitattributes and make the generator "
            "write bytes rather than text, so the platform cannot rewrite them.",
        ]
    elif generated.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n"):
        lines += ["", "Content is identical apart from line endings."]
    else:
        lines += ["", "Regenerate and commit the spec:", f"  {generate_cmd}"]

    return "\n".join(lines)
