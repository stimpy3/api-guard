"""Command-line entry point.

    api-guard check --config api-guard.yaml

Exit codes follow the tools we wrap, so any CI understands them unaided:

    0  the contract is intact (or every breach is waived)
    1  the contract would break consumers
    2  api-guard could not reach a conclusion - bad config, missing tool

Keeping 1 and 2 distinct matters more than it looks. A typo'd URL reported as
"breaking change detected" sends somebody hunting for a change that does not
exist, and after that happens twice people stop believing the gate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from api_guard import report, specs
from api_guard.checks import breaking, conformance, freshness
from api_guard.config import Config, ConfigError, load
from api_guard.policy import PolicyError, Waiver, load_waivers
from api_guard.results import Change, CheckResult, Status
from api_guard.verdict import EXIT_TOOL_ERROR, RunResult, decide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="api-guard", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Run the contract checks.")
    check.add_argument(
        "--config",
        type=Path,
        default=Path("api-guard.yaml"),
        help="Path to api-guard.yaml (default: ./api-guard.yaml).",
    )
    check.add_argument(
        "--generated-spec",
        type=Path,
        default=None,
        help=(
            "A spec the pipeline already generated, to compare the committed one "
            "against. Use this from the Docker image, which has no access to your "
            "project's dependencies. Takes precedence over spec.generate_cmd."
        ),
    )
    check.add_argument(
        "--url",
        default=None,
        help=(
            "Override runtime.url. The same config is used against the ephemeral "
            "test stack during the build and against staging after deploying, and "
            "those are different addresses."
        ),
    )
    check.add_argument(
        "--explain",
        action="store_true",
        help=(
            "Add an LLM-written impact analysis to the report. Advisory only: "
            "it runs after the verdict and cannot change it."
        ),
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return _run_check(
            args.config,
            generated_spec=args.generated_spec,
            url=args.url,
            explain=args.explain,
        )
    parser.error(f"unknown command {args.command}")
    return EXIT_TOOL_ERROR


def _run_check(
    config_path: Path,
    *,
    generated_spec: Path | None,
    url: str | None,
    explain: bool,
) -> int:
    try:
        config = load(config_path)
    except ConfigError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    if url is not None:
        if config.runtime is None:
            print(
                "api-guard: --url given but api-guard.yaml has no `runtime:` section, "
                "so there is nothing to check against it.",
                file=sys.stderr,
            )
            return EXIT_TOOL_ERROR
        config.runtime.url = url

    generated: bytes | None = None
    if generated_spec is not None:
        try:
            generated = generated_spec.read_bytes()
        except OSError as exc:
            print(f"api-guard: cannot read --generated-spec {generated_spec}: {exc}", file=sys.stderr)
            return EXIT_TOOL_ERROR

    try:
        waivers = _load_waivers(config)
    except PolicyError as exc:
        print(f"api-guard: {exc}", file=sys.stderr)
        return EXIT_TOOL_ERROR

    result = _check(config, waivers, generated=generated)

    if explain:
        _explain(result)

    written = report.write(result, config.resolve(config.report.dir), config.report.formats)

    _print_summary(result, written)
    return result.exit_code


def _load_waivers(config: Config) -> list[Waiver]:
    if config.policy.waivers is None:
        return []
    path = config.resolve(config.policy.waivers)
    if not path.exists():
        # An absent waivers file is the normal state for a healthy project, not
        # a misconfiguration.
        return []
    return load_waivers(path)


def _check(config: Config, waivers: list[Waiver], *, generated: bytes | None = None) -> RunResult:
    checks: list[CheckResult] = []
    changes: list[Change] = []
    waiver_outcome = None

    spec_path = config.resolve(config.spec.path)

    try:
        revision = specs.read_revision(spec_path)
    except specs.SpecError as exc:
        return decide(
            [CheckResult(name="spec", status=Status.ERROR, summary=str(exc))],
            [],
            _empty_waivers(),
            _meta(config),
        )

    checks.append(
        freshness.run(
            revision,
            generated=generated,
            generate_cmd=config.spec.generate_cmd,
            root=config.root,
        )
    )

    try:
        base = specs.read_base(config.spec.base, spec_path, config.root)
    except specs.BaseNotFound as exc:
        checks.append(
            CheckResult(
                name=breaking.NAME,
                status=Status.SKIPPED,
                summary=f"no base spec at {config.spec.base}",
                detail=str(exc),
            )
        )
        base = None
    except specs.SpecError as exc:
        checks.append(
            CheckResult(name=breaking.NAME, status=Status.ERROR, summary=str(exc))
        )
        base = None

    if base is not None:
        result, changes, waiver_outcome = breaking.run(
            base, revision, config.policy, waivers, config.root
        )
        checks.append(result)

    checks.append(conformance.run(config.runtime, spec_path, config.root))

    return decide(checks, changes, waiver_outcome or _empty_waivers(), _meta(config))


def _empty_waivers():
    from api_guard.policy import WaiverOutcome

    return WaiverOutcome()


def _meta(config: Config) -> dict[str, str]:
    """Best-effort provenance. Never fails the run — this is context, not a check."""
    meta = {"spec": str(config.spec.path), "base": config.spec.base}
    for key, command in (
        ("commit", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
    ):
        try:
            done = subprocess.run(command, cwd=config.root, capture_output=True, timeout=10)
            if done.returncode == 0:
                meta[key] = done.stdout.decode("utf-8", errors="replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return meta


def _explain(result: RunResult) -> None:
    """Attach the AI impact analysis, if the extra is installed.

    Imported here rather than at module scope so the core gate has no dependency
    on the AI layer, and so a missing extra degrades to a warning instead of
    taking the build down.
    """
    try:
        from api_guard.ai.explain import explain
    except ImportError:
        print(
            "api-guard: --explain needs the AI extra: pip install 'api-guard[ai]'",
            file=sys.stderr,
        )
        return

    try:
        explain(result)
    except Exception as exc:  # noqa: BLE001 - advisory only, must never fail the build
        print(f"api-guard: explanation unavailable ({exc})", file=sys.stderr)


def _print_summary(result: RunResult, written: dict[str, Path]) -> None:
    print()
    for check in result.checks:
        print(f"  {check.status.value.upper():<8} {check.name:<12} {check.summary}")

    if result.waivers.applied:
        print(f"\n  {len(result.waivers.applied)} waived breaking change(s):")
        for waiver in result.waivers.applied:
            print(f"    - {waiver.describe()} (expires {waiver.expires.isoformat()})")

    if result.waivers.stale:
        print(f"\n  {len(result.waivers.stale)} stale waiver(s) matched nothing:")
        for waiver in result.waivers.stale:
            print(f"    - {waiver.describe()}")

    for check in result.checks:
        if check.detail and check.status.is_blocking:
            print(f"\n{check.name}:\n{check.detail}")

    if written:
        print("\n  reports: " + ", ".join(str(p) for p in written.values()))
    print()


if __name__ == "__main__":
    raise SystemExit(main())
