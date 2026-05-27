"""Command-injection and unsafe-deserialization audit.

This test reads every Python file under `metaensemble/hooks/` and asserts that
none of them use the dangerous constructs `SECURITY.md` names as
forbidden. The audit catches regressions even before they trigger an
actual exploit: a `shell=True` or a `yaml.load` would slip into an
unrelated PR otherwise.

The audit is intentionally pattern-based (substring + AST walk) rather
than runtime-based. Static checks are reproducible, fast, and visible
to reviewers; they do not depend on test data or fixtures.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"


def _hook_files() -> list[Path]:
    return sorted(
        p for p in HOOKS_DIR.iterdir()
        if p.suffix == ".py" and not p.name.startswith("_")
    )


@pytest.fixture(scope="module")
def hook_sources() -> dict[Path, str]:
    return {p: p.read_text() for p in _hook_files()}


# --- String-level audit (cheap, catches obvious patterns) -----------------


def test_hooks_never_use_shell_true(hook_sources):
    """`subprocess.run(..., shell=True)` exposes the call to shell parsing.

    MetaEnsemble never invokes shells; argv lists are the contract.
    """
    for path, src in hook_sources.items():
        assert "shell=True" not in src, (
            f"{path.name} uses shell=True; pass argv list instead"
        )


def test_hooks_never_use_unsafe_yaml_load(hook_sources):
    """`yaml.load` can instantiate arbitrary Python objects.

    Hooks must call `yaml.safe_load` for any YAML they parse.
    """
    for path, src in hook_sources.items():
        # Allow `yaml.safe_load` (the safe variant).
        # Match `yaml.load(` and require it is NOT the safe one.
        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if "yaml.load(" in stripped and "yaml.safe_load" not in stripped:
                pytest.fail(
                    f"{path.name}:{i} uses yaml.load; use yaml.safe_load"
                )


def test_hooks_never_use_os_system(hook_sources):
    for path, src in hook_sources.items():
        assert "os.system(" not in src, (
            f"{path.name} uses os.system; use subprocess.run with argv list"
        )


# --- AST-level audit (catches eval/exec even when imported indirectly) ---


def _walk_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def test_hooks_never_use_eval_or_exec(hook_sources):
    """`eval` and `exec` turn text into code. Hooks must never call them.

    The check uses the AST so a bare `eval()` call is caught even if
    the source uses `from builtins import eval` or similar redirection.
    """
    for path, src in hook_sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"{path.name} has a syntax error: {exc}")
        for call in _walk_calls(tree):
            target = None
            if isinstance(call.func, ast.Name):
                target = call.func.id
            elif isinstance(call.func, ast.Attribute):
                target = call.func.attr
            if target in {"eval", "exec"}:
                pytest.fail(f"{path.name} calls {target}() — not allowed in hooks")


def test_hooks_handle_json_decode_errors(hook_sources):
    """Every `json.loads`/`json.load` should be inside a `try/except` block
    that names JSONDecodeError, or use a wrapper helper from `_common.py`."""
    for path, src in hook_sources.items():
        # If the file doesn't call json.loads/json.load directly, nothing to check.
        if "json.loads(" not in src and "json.load(" not in src:
            continue
        # Heuristic: the file should mention JSONDecodeError somewhere, OR
        # delegate to a helper (read_input) that catches it centrally.
        # `_common.py`'s read_input handles JSONDecodeError so hooks that
        # only consume runtime payload via that helper are safe.
        if "JSONDecodeError" in src:
            continue
        if "read_input()" in src:
            # Indirectly safe via the common helper.
            continue
        pytest.fail(
            f"{path.name} uses json.loads/json.load without "
            "JSONDecodeError handling and without delegating to read_input()"
        )
