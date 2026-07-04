"""End-to-end offline rehearsal of the live Suite-A pipeline for task a1.

Unlike `test_eval_suite_a.py` (which fakes the sibling workstreams),
this file wires the REAL pieces together with no fakes between them:

- the real task row loaded from `evals/datasets/suite_a/tasks.yaml`,
- the real deterministic fixture builder (`evals.fixtures.build`),
- the real acceptance grader (`evals.runners.acceptance`),
- the real cell runner (`evals.runners.suite_a`).

The ONLY monkeypatched seam is the claude subprocess itself
(`suite_a._run_claude`): a scripted "competent agent" that edits the
workspace exactly the way a real agent should for a1 (fix the
off-by-one at pagination.py:42, append a regression test pinning the
final page) and exits with a realistic `claude -p --output-format json`
payload. Zero live `claude` calls, zero network, zero API spend.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from evals.fixtures.build import FIXTURE_SHAS
from evals.runners import suite_a
from evals.runners.api import CellSpec
from evals.runners.suite_a import SuiteATask, build_cell_prompt, run_suite_a_live

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_YAML = REPO_ROOT / "evals" / "datasets" / "suite_a" / "tasks.yaml"

# a1's frozen starting state: the deterministic paginator fixture commit.
A1_PINNED_SHA = FIXTURE_SHAS["oss-fixture-paginator"]

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="Suite-A workspaces require git"
)


def _load_a1() -> SuiteATask:
    """Load task a1 from the real dataset so the rehearsal grades the
    exact criteria a live run would."""
    data = yaml.safe_load(TASKS_YAML.read_text(encoding="utf-8"))
    row = next(t for t in data["tasks"] if t["id"] == "a1_bugfix_off_by_one")
    return SuiteATask(
        id=row["id"],
        description=row["description"],
        acceptance=list(row["acceptance"]),
        starting_repo=row["starting_repo"],
        starting_sha=row["starting_sha"],
        title=row.get("title", ""),
    )


def _git_head(workspace: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# The scripted competent agent
# ---------------------------------------------------------------------------

# The seeded bug at pagination.py:42 and its minimal correct fix.
_BUGGY_LINE = "    stop = min(start + page_size, len(items) - 1)\n"
_FIXED_LINE = "    stop = min(start + page_size, len(items))\n"

# Regression test pinning the final-page boundary the fixture's own four
# tests deliberately never cover; raises the collected count to 5.
_REGRESSION_TEST = (
    "\n\n"
    "def test_final_page_carries_the_remainder():\n"
    "    assert paginate(list(range(6)), 1, 3) == [3, 4, 5]\n"
    "    assert paginate(list(range(10)), 3, 3) == [9]\n"
)


def _apply_competent_patch(workspace: Path) -> None:
    """Edit the workspace the way a competent agent would for task a1."""
    src = workspace / "pagination.py"
    text = src.read_text(encoding="utf-8")
    assert _BUGGY_LINE in text, "workspace does not contain the seeded a1 bug"
    src.write_text(text.replace(_BUGGY_LINE, _FIXED_LINE), encoding="utf-8")
    tests = workspace / "test_pagination.py"
    tests.write_text(
        tests.read_text(encoding="utf-8") + _REGRESSION_TEST, encoding="utf-8"
    )


def _claude_result_payload() -> dict:
    """A realistic `claude -p --output-format json` result payload."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 48123.0,
        "duration_api_ms": 44210.0,
        "num_turns": 12,
        "result": (
            "Fixed the off-by-one slice bound in pagination.py and added a "
            "regression test for the final page."
        ),
        "session_id": "0f0e0d0c-e2e0-4e2e-8e2e-rehearsal0001",
        "total_cost_usd": 0.1842,
        "usage": {
            "input_tokens": 5200,
            "cache_read_input_tokens": 2100,
            "cache_creation_input_tokens": 800,
            "output_tokens": 940,
        },
        "modelUsage": {
            "claude-sonnet-4-5": {
                "inputTokens": 5200,
                "cacheReadInputTokens": 2100,
                "cacheCreationInputTokens": 800,
                "outputTokens": 940,
            }
        },
    }


# tokens_in = inputTokens + cacheRead + cacheCreation; tokens_out = outputTokens
_EXPECTED_TOKENS_IN = 5200 + 2100 + 800
_EXPECTED_TOKENS_OUT = 940


@pytest.fixture
def scripted_claude(monkeypatch):
    """Replace the claude subprocess seam with the scripted agent.

    `state["edit"]` toggles whether the agent actually patches the
    workspace; either way it exits 0 with the realistic JSON payload,
    so the no-op arm isolates acceptance grading from transport errors.
    """
    state = {"calls": [], "edit": True}

    def _run(cmd: list[str], *, cwd: Path):
        workspace = Path(cwd)
        state["calls"].append({"cmd": list(cmd), "cwd": workspace})
        if state["edit"]:
            _apply_competent_patch(workspace)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(_claude_result_payload()), stderr=""
        )

    monkeypatch.setattr(suite_a, "_run_claude", _run)
    # Each test gets its own workdir; never reuse another test's cache.
    monkeypatch.setattr(suite_a, "_FIXTURE_CACHE", {})
    return state


# ---------------------------------------------------------------------------
# Rehearsal: B1 and MM_full pass a1 after the competent patch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cell_id,kind,expected_sources",
    [
        ("B1_single_agent", "baseline", "project,local"),
        ("MM_full", "full_system", "user,project,local"),
    ],
)
def test_a1_end_to_end_pass_after_competent_patch(
    scripted_claude, tmp_path, cell_id, kind, expected_sources,
):
    task = _load_a1()
    workdir = tmp_path / "workdir"
    cell = CellSpec(id=cell_id, kind=kind, dispatch_fn=cell_id)

    outcomes = run_suite_a_live(
        cell,
        [task],
        seeds=1,
        budget_usd=0.30,
        workdir=workdir,
        repo_root=REPO_ROOT,
        model="claude-sonnet-4-5",
    )

    # Every RunOutcome field is populated with the graded/parsed values.
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.task_id == "a1_bugfix_off_by_one"
    assert outcome.seed == 0
    assert outcome.passed is True          # all 5 real a1 criteria graded PASS
    assert outcome.quality_score == 1.0
    assert outcome.minimum_useful_answer_score == 1.0  # tree was modified
    assert outcome.tokens_in == _EXPECTED_TOKENS_IN
    assert outcome.tokens_out == _EXPECTED_TOKENS_OUT
    assert outcome.budget_exceeded is False
    assert outcome.duration_ms == 48123.0
    assert outcome.failure_reason is None

    # run-manifest.jsonl indexes the kept workspace.
    rows = [
        json.loads(line)
        for line in (workdir / "run-manifest.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["cell"] == cell_id
    assert row["task"] == "a1_bugfix_off_by_one"
    assert row["seed"] == 0
    assert row["model"] == "claude-sonnet-4-5"
    assert row["exit"] == 0

    # The workspace was materialized at the pinned deterministic SHA
    # (the patch is a working-tree edit; HEAD stays at the frozen state).
    workspace = Path(row["workspace"])
    assert workspace.is_dir()
    assert row["sha"] == A1_PINNED_SHA
    assert _git_head(workspace) == A1_PINNED_SHA
    # The competent patch landed in the graded tree.
    assert _FIXED_LINE in (workspace / "pagination.py").read_text()
    assert "test_final_page_carries_the_remainder" in (
        workspace / "test_pagination.py"
    ).read_text()

    # Exactly one claude invocation, inside the workspace, with the
    # documented per-cell flags and the cell's prompt as the final arg.
    assert len(scripted_claude["calls"]) == 1
    call = scripted_claude["calls"][0]
    assert call["cwd"] == workspace
    cmd = call["cmd"]
    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.3000"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-5"
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--setting-sources") + 1] == expected_sources
    assert cmd[-1] == build_cell_prompt(cell_id, task)


# ---------------------------------------------------------------------------
# Rehearsal: an unpatched no-op payload fails a1's acceptance
# ---------------------------------------------------------------------------


def test_a1_end_to_end_noop_payload_fails_acceptance(scripted_claude, tmp_path):
    scripted_claude["edit"] = False  # agent "succeeds" but touches nothing
    task = _load_a1()
    workdir = tmp_path / "workdir"
    cell = CellSpec(id="B1_single_agent", kind="baseline", dispatch_fn="B1_single_agent")

    outcomes = run_suite_a_live(
        cell,
        [task],
        seeds=1,
        budget_usd=0.30,
        workdir=workdir,
        repo_root=REPO_ROOT,
        model="claude-sonnet-4-5",
    )

    outcome = outcomes[0]
    assert outcome.passed is False
    # Unpatched tree: build_passes and lint_clean still PASS (the seeded
    # bug is invisible to the fixture's own 4 tests), but
    # test_count_at_least (4 < 5) and both file_modified criteria FAIL.
    assert outcome.quality_score == pytest.approx(2 / 5)
    assert outcome.minimum_useful_answer_score == 0.0  # tree untouched
    assert outcome.failure_reason is not None
    assert outcome.failure_reason.startswith("FAIL test_count_at_least")
    # Transport succeeded, so token accounting is still populated.
    assert outcome.tokens_in == _EXPECTED_TOKENS_IN
    assert outcome.tokens_out == _EXPECTED_TOKENS_OUT
    assert outcome.budget_exceeded is False

    # The failed run is still indexed for post-hoc inspection.
    row = json.loads((workdir / "run-manifest.jsonl").read_text().splitlines()[0])
    assert row["sha"] == A1_PINNED_SHA
    assert row["exit"] == 0
    assert _git_head(Path(row["workspace"])) == A1_PINNED_SHA
