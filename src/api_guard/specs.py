"""Resolving the two specs being compared.

The revision is whatever is committed in the working tree. The base is
configurable because "what should we compare against?" genuinely differs
between projects: most want the spec on their default branch, some keep a
golden copy on disk, some publish the last released spec at a URL.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path

GIT_PREFIX = "git:"
_TIMEOUT = 30


class SpecError(Exception):
    """A spec that should exist could not be read. Reported as exit code 2."""


class BaseNotFound(Exception):
    """The base spec does not exist yet.

    Deliberately distinct from SpecError. On a repository's first build there is
    no previous contract, so there is nothing to break, and the breaking-change
    check is skipped rather than failed. Failing here would mean api-guard could
    never be adopted without first faking a history.
    """


def read_revision(path: Path) -> bytes:
    """Read the committed spec from the working tree."""
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SpecError(
            f"cannot read spec at {path}: {exc}. "
            "Check `spec.path` in api-guard.yaml is relative to the config file."
        ) from exc


def read_base(base: str, spec_path: Path, root: Path) -> bytes:
    """Resolve `spec.base` to the previous contract's bytes.

    Accepts `git:<ref>`, a filesystem path, or an http(s) URL.
    """
    if base.startswith(GIT_PREFIX):
        return _read_from_git(base[len(GIT_PREFIX) :], spec_path, root)
    if base.startswith(("http://", "https://")):
        return _read_from_url(base)
    return _read_from_file(Path(base), root)


def _read_from_git(ref: str, spec_path: Path, root: Path) -> bytes:
    """Read the spec as it exists at a git ref, e.g. `origin/main`.

    Uses `git show <ref>:<path>` rather than checking anything out, so the
    working tree is never touched — important when this runs mid-pipeline
    alongside a build.
    """
    try:
        relative = spec_path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SpecError(
            f"spec {spec_path} is outside the project root {root}; "
            "cannot read it from git"
        ) from exc

    # git wants forward slashes in pathspecs even on Windows.
    target = f"{ref}:{relative.as_posix()}"

    try:
        completed = subprocess.run(
            ["git", "show", target],
            cwd=root,
            capture_output=True,
            timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise SpecError("git is not on PATH, but `spec.base` uses a git ref") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpecError(f"`git show {target}` timed out after {_TIMEOUT}s") from exc

    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BaseNotFound(
            f"no spec at `{target}`: {message}\n"
            "Usually this means one of:\n"
            "  * this is the first build and the ref does not exist yet\n"
            "  * the CI checkout is shallow — GitHub Actions needs "
            "`fetch-depth: 0` for origin/main to be present\n"
            "  * the spec was added on this branch and is not on the base branch"
        )

    return completed.stdout


def _read_from_file(path: Path, root: Path) -> bytes:
    resolved = path if path.is_absolute() else root / path
    try:
        return resolved.read_bytes()
    except FileNotFoundError as exc:
        raise BaseNotFound(f"base spec not found at {resolved}") from exc
    except OSError as exc:
        raise SpecError(f"cannot read base spec {resolved}: {exc}") from exc


def _read_from_url(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise BaseNotFound(f"base spec not published yet at {url} (404)") from exc
        raise SpecError(f"fetching base spec {url} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SpecError(f"fetching base spec {url} failed: {exc}") from exc
