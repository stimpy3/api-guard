"""Enforces the architectural rule the whole design rests on.

    Deterministic tools decide. The LLM only explains.

The AI layer runs after the verdict is computed, reads it, and writes prose. It
cannot change pass/fail. If Groq is down, the key is missing, or the model
hallucinates, the build result is identical — which is the point, because a
deployment gate has to be reproducible and an LLM is not.

That rule is worth nothing if it lives only in a docstring. Nobody reviewing a
future pull request will remember it; they will add one convenient import from
the graph code into the verdict path and the guarantee will be gone with no
test failing. So it is checked here, by reading the import statements.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "api_guard"

# Modules that decide the outcome. None may depend on the AI layer.
VERDICT_PATH = [
    "verdict.py",
    "results.py",
    "policy.py",
    "config.py",
    "specs.py",
    "report.py",
    "checks/breaking.py",
    "checks/conformance.py",
    "checks/freshness.py",
]


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("relative", VERDICT_PATH)
def test_verdict_path_does_not_import_the_ai_layer(relative: str) -> None:
    module = SRC / relative
    offenders = {name for name in imports_of(module) if "api_guard.ai" in name}
    assert not offenders, (
        f"{relative} imports {sorted(offenders)}.\n\n"
        "Modules that decide pass/fail must not depend on the AI layer. If the "
        "explanation code needs something from here, move the shared piece into "
        "results.py rather than importing upwards — otherwise an LLM failure can "
        "take the build down, and 'the AI only explains' stops being true."
    )


def test_ai_layer_is_imported_lazily_in_the_cli() -> None:
    """cli.py may use the AI layer, but only inside a function.

    A module-level import would execute langchain at startup, so a missing
    optional extra would break `api-guard check` for everybody — including the
    people who never asked for explanations.
    """
    cli = SRC / "cli.py"
    tree = ast.parse(cli.read_text(encoding="utf-8"), filename=str(cli))

    for node in tree.body:  # top level only
        if isinstance(node, ast.ImportFrom) and node.module and "api_guard.ai" in node.module:
            pytest.fail(
                "cli.py imports the AI layer at module level. Move it inside the "
                "function that needs it so the core gate runs without the extra."
            )


def test_core_package_does_not_require_ai_dependencies() -> None:
    """The gate must run with no LLM libraries installed at all."""
    for relative in VERDICT_PATH:
        found = imports_of(SRC / relative)
        banned = {
            name
            for name in found
            if name.split(".")[0] in {"langchain", "langchain_groq", "langgraph", "mcp", "groq"}
        }
        assert not banned, f"{relative} imports {sorted(banned)} — that belongs in api_guard.ai"
