"""Tests for the live Suite-A cell runner (`evals/runners/suite_a.py`).

No live `claude` calls and no network: the claude subprocess seam is
monkeypatched with a fake JSON payload, and the frozen sibling
interfaces (`evals.fixtures.build`, `evals.runners.acceptance`) are
substituted with interface-faithful fakes injected into `sys.modules`,
so these tests exercise this workstream's wiring regardless of whether
the sibling workstreams have landed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import metaensemble.cli as cli
from evals.runners import suite_a
from evals.runners.api import CellSpec
from evals.runners.suite_a import (
    SuiteATask,
    _claude_isolation_args,
    build_cell_prompt,
    materialize_workspace,
    run_suite_a_live,
)

REPO_ROOT = Path(suite_a.__file__).resolve().parents[2]

# Same author/committer identity the frozen fixture-builder interface pins,
# so fake fixture builds are deterministic the same way real ones are.
FIXED_GIT_ENV = {
    "GIT_AUTHOR_NAME": "MetaEnsemble Fixtures",
    "GIT_AUTHOR_EMAIL": "fixtures@metaensemble.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_NAME": "MetaEnsemble Fixtures",
    "GIT_COMMITTER_EMAIL": "fixtures@metaensemble.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}


def _git(*args: str, cwd: Path, env: dict | None = None) -> str:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=merged,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _make_repo(path: Path, files: dict[str, str]) -> str:
    """Create a one-commit git repo with the pinned fixture identity."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", "fixture: initial state", cwd=path, env=FIXED_GIT_ENV)
    return _git("rev-parse", "HEAD", cwd=path)


def _fixture_files(name: str) -> dict[str, str]:
    return {"pagination.py": f"# fixture source for {name}\n"}


# ---------------------------------------------------------------------------
# Interface-faithful fakes for the frozen sibling workstreams
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_fixture_build(monkeypatch, tmp_path):
    """Inject a fake `evals.fixtures.build` honoring the frozen interface.

    `build_fixture(name, dest) -> sha` materializes a deterministic
    one-commit repo; `FIXTURE_SHAS` maps names to the expected SHAs,
    computed here by building a probe repo with identical content and
    identical pinned author/committer env.
    """
    calls: list[str] = []

    def build_fixture(name: str, dest: Path) -> str:
        calls.append(name)
        return _make_repo(Path(dest), _fixture_files(name))

    shas = {
        name: _make_repo(tmp_path / f"probe-{name}", _fixture_files(name))
        for name in ("oss-fixture-paginator", "oss-fixture-legacy")
    }

    build_mod = types.ModuleType("evals.fixtures.build")
    build_mod.build_fixture = build_fixture
    build_mod.FIXTURE_SHAS = shas
    pkg = types.ModuleType("evals.fixtures")
    pkg.build = build_mod
    monkeypatch.setitem(sys.modules, "evals.fixtures", pkg)
    monkeypatch.setitem(sys.modules, "evals.fixtures.build", build_mod)
    monkeypatch.setattr(suite_a, "_FIXTURE_CACHE", {})
    return build_mod, calls


@dataclass(frozen=True)
class FakeBaselineStats:
    test_count: int
    public_api: dict[str, list[str]] | None


@dataclass(frozen=True)
class FakeAcceptanceReport:
    passed: bool
    score: float
    details: list[str] = field(default_factory=list)


@pytest.fixture
def fake_acceptance(monkeypatch):
    """Inject a fake `evals.runners.acceptance` honoring the frozen interface.

    `state["reports"]` is a FIFO of AcceptanceReports returned by
    `check_acceptance`; `state["log"]` records call ordering so tests can
    assert baseline stats are captured before the claude call.
    """
    state = {"reports": [], "log": [], "baseline": FakeBaselineStats(3, None)}

    def collect_baseline_stats(workspace: Path) -> FakeBaselineStats:
        state["log"].append(("baseline", Path(workspace)))
        return state["baseline"]

    def check_acceptance(workspace, criteria, *, baseline):
        state["log"].append(("acceptance", Path(workspace), list(criteria), baseline))
        if state["reports"]:
            return state["reports"].pop(0)
        return FakeAcceptanceReport(passed=True, score=1.0, details=["PASS stub — ok"])

    mod = types.ModuleType("evals.runners.acceptance")
    mod.BaselineStats = FakeBaselineStats
    mod.AcceptanceReport = FakeAcceptanceReport
    mod.collect_baseline_stats = collect_baseline_stats
    mod.check_acceptance = check_acceptance
    monkeypatch.setitem(sys.modules, "evals.runners.acceptance", mod)
    return state


def _claude_payload(*, cost: float = 0.05, duration: float = 1234.0) -> dict:
    return {
        "type": "result",
        "is_error": False,
        "result": "done",
        "duration_ms": duration,
        "total_cost_usd": cost,
        "modelUsage": {
            "test-model": {
                "inputTokens": 1000,
                "cacheReadInputTokens": 150,
                "cacheCreationInputTokens": 50,
                "outputTokens": 300,
            }
        },
    }


@pytest.fixture
def fake_claude(monkeypatch):
    """Monkeypatch the claude subprocess seam; capture cmd/cwd per call."""
    state = {"calls": [], "payloads": [], "log": None, "mutate_first_call": True}

    def _run(cmd: list[str], *, cwd: Path):
        if state["log"] is not None:
            state["log"].append(("claude", Path(cwd)))
        index = len(state["calls"])
        state["calls"].append({"cmd": list(cmd), "cwd": Path(cwd)})
        if state["mutate_first_call"] and index == 0:
            (Path(cwd) / "agent-artifact.txt").write_text("modified by agent\n")
        payload = (
            state["payloads"][index]
            if index < len(state["payloads"])
            else _claude_payload()
        )
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(suite_a, "_run_claude", _run)
    return state


def _task(**overrides) -> SuiteATask:
    defaults = dict(
        id="a1_bugfix_off_by_one",
        description="Fix the off-by-one bug in the paginator and add a regression test.",
        acceptance=[
            {"kind": "build_passes"},
            {"kind": "test_count_at_least", "value": 5},
            {"kind": "file_modified", "path": "pagination.py"},
        ],
        starting_repo="oss-fixture-paginator",
        starting_sha="__DEFERRED__",
    )
    defaults.update(overrides)
    return SuiteATask(**defaults)


# ---------------------------------------------------------------------------
# materialize_workspace
# ---------------------------------------------------------------------------


def test_materialize_fixture_workspace_checks_out_expected_sha(
    fake_fixture_build, tmp_path,
):
    build_mod, _calls = fake_fixture_build
    workdir = tmp_path / "workdir"
    expected = build_mod.FIXTURE_SHAS["oss-fixture-paginator"]

    workspace = materialize_workspace(_task(), workdir, REPO_ROOT)

    assert workspace.is_dir()
    assert (workspace / ".git").exists()
    assert (workspace / "pagination.py").exists()
    assert _git("rev-parse", "HEAD", cwd=workspace) == expected
    # The fixture cache lives at the prescribed location.
    assert (workdir / "fixtures-cache" / "oss-fixture-paginator").is_dir()


def test_materialize_fixture_second_kind_and_build_cached_once_per_process(
    fake_fixture_build, tmp_path,
):
    build_mod, calls = fake_fixture_build
    workdir = tmp_path / "workdir"
    task = _task(id="a2_refactor_module", starting_repo="oss-fixture-legacy")

    ws1 = materialize_workspace(task, workdir, REPO_ROOT)
    ws2 = materialize_workspace(task, workdir, REPO_ROOT)

    expected = build_mod.FIXTURE_SHAS["oss-fixture-legacy"]
    assert _git("rev-parse", "HEAD", cwd=ws1) == expected
    assert _git("rev-parse", "HEAD", cwd=ws2) == expected
    assert ws1 != ws2  # workspaces are kept, never reused
    assert calls == ["oss-fixture-legacy"]  # built once per process


def test_materialize_fixture_sha_mismatch_raises(fake_fixture_build, tmp_path):
    build_mod, _calls = fake_fixture_build
    build_mod.FIXTURE_SHAS["oss-fixture-paginator"] = "0" * 40
    with pytest.raises(RuntimeError, match="expected"):
        materialize_workspace(_task(), tmp_path / "workdir", REPO_ROOT)


def test_materialize_workspace_with_real_fixture_builder(tmp_path, monkeypatch):
    """Integration against the real frozen fixture interface, both kinds."""
    real_build = pytest.importorskip("evals.fixtures.build")
    monkeypatch.setattr(suite_a, "_FIXTURE_CACHE", {})
    workdir = tmp_path / "workdir"
    for name in ("oss-fixture-paginator", "oss-fixture-legacy"):
        expected = real_build.FIXTURE_SHAS[name]
        task = _task(id=f"real_{name}", starting_repo=name, starting_sha=expected)
        workspace = materialize_workspace(task, workdir, REPO_ROOT)
        assert _git("rev-parse", "HEAD", cwd=workspace) == expected


def test_materialize_metaensemble_checks_out_tagged_sha(tmp_path):
    expected = _git("rev-parse", "v0.2.0^{commit}", cwd=REPO_ROOT)
    task = _task(
        id="a3_doc_update",
        starting_repo="metaensemble",
        starting_sha="v0.2.0",
    )
    workspace = materialize_workspace(task, tmp_path / "workdir", REPO_ROOT)
    assert _git("rev-parse", "HEAD", cwd=workspace) == expected
    assert (workspace / "pyproject.toml").exists()


def test_materialize_metaensemble_deferred_sha_refuses(tmp_path):
    task = _task(id="a3_doc_update", starting_repo="metaensemble")
    with pytest.raises(ValueError, match="deferred"):
        materialize_workspace(task, tmp_path / "workdir", REPO_ROOT)


def test_materialize_unknown_starting_repo_refuses(tmp_path):
    task = _task(starting_repo="github.com/somewhere/else")
    with pytest.raises(ValueError, match="starting_repo"):
        materialize_workspace(task, tmp_path / "workdir", REPO_ROOT)


# ---------------------------------------------------------------------------
# Per-cell prompts
# ---------------------------------------------------------------------------


def test_b1_prompt_is_description_only():
    task = _task()
    prompt = build_cell_prompt("B1_single_agent", task)
    assert prompt == task.description.strip()
    assert "acceptance" not in prompt.lower()
    assert "senior software engineer" not in prompt


def test_b2_prompt_names_criteria_kinds_but_not_thresholds():
    prompt = build_cell_prompt("B2_single_agent_prompted", _task())
    assert "senior software engineer" in prompt
    assert "build_passes" in prompt
    assert "test_count_at_least" in prompt
    assert "file_modified" in prompt
    # The threshold value (5) is deliberately withheld from B2.
    assert "5" not in prompt


def test_b3_prompt_is_b2_plus_subagent_delegation():
    b2 = build_cell_prompt("B2_single_agent_prompted", _task())
    b3 = build_cell_prompt("B3_subagent_default", _task())
    assert b3.startswith(b2)
    assert "Task tool" in b3
    assert "subagent" in b3
    assert "Task tool" not in b2


def test_b4_prompt_has_verbatim_criteria_pointers_and_plan_then_verify():
    prompt = build_cell_prompt("B4_best_prompt", _task())
    assert '"kind": "test_count_at_least"' in prompt
    assert '"value": 5' in prompt  # thresholds shown verbatim
    assert "pagination.py" in prompt
    assert "current working directory" in prompt
    assert "Plan the change" in prompt
    assert "Verify every acceptance criterion" in prompt


def test_mm_full_prompt_is_b4_brief_plus_protocol():
    b4 = build_cell_prompt("B4_best_prompt", _task())
    mm = build_cell_prompt("MM_full", _task())
    assert mm.startswith(b4)
    assert "metaensemble-protocol" in mm
    assert "dispatch an Executor" in mm
    assert "Compose a Manifest" in mm
    assert "Record the Run in the Ledger" in mm
    assert "Quality gate: verify the Deliverable" in mm


@pytest.mark.parametrize(
    "cell_id,dropped,kept,ablation_marker",
    [
        (
            "MM_minus_manifest",
            "Compose a Manifest",
            ["Record the Run in the Ledger", "Quality gate: verify the Deliverable"],
            "WITHOUT composing a Manifest",
        ),
        (
            "MM_minus_ledger",
            "Record the Run in the Ledger",
            ["Compose a Manifest", "Quality gate: verify the Deliverable"],
            "WITHOUT recording Run rows in the Ledger",
        ),
        (
            "MM_minus_quality_gate",
            "Quality gate: verify the Deliverable",
            ["Compose a Manifest", "Record the Run in the Ledger"],
            "WITHOUT the post-Deliverable quality gate",
        ),
    ],
)
def test_ablation_prompts_drop_named_element_and_state_it(
    cell_id, dropped, kept, ablation_marker,
):
    prompt = build_cell_prompt(cell_id, _task())
    assert dropped not in prompt
    assert ablation_marker in prompt
    for element in kept:
        assert element in prompt


def test_unknown_cell_prompt_raises():
    with pytest.raises(ValueError, match="unknown Suite-A cell"):
        build_cell_prompt("B9_mystery", _task())


# ---------------------------------------------------------------------------
# Hook isolation args
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell_id", sorted(suite_a.BASELINE_CELL_IDS))
def test_isolation_args_baselines_exclude_user_settings(cell_id):
    assert _claude_isolation_args(cell_id) == ["--setting-sources", "project,local"]


@pytest.mark.parametrize("cell_id", sorted(suite_a.MM_CELL_IDS))
def test_isolation_args_mm_cells_include_user_settings(cell_id):
    assert _claude_isolation_args(cell_id) == [
        "--setting-sources", "user,project,local",
    ]


def test_isolation_args_accepts_cellspec():
    cell = CellSpec(id="MM_full", kind="full_system", dispatch_fn="MM_full")
    assert _claude_isolation_args(cell) == ["--setting-sources", "user,project,local"]


# ---------------------------------------------------------------------------
# run_suite_a_live end-to-end (fake claude, real trivial workspace)
# ---------------------------------------------------------------------------


def _trivial_project(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "proj"
    sha = _make_repo(repo, {"app.py": "VALUE = 1\n"})
    return repo, sha


def _live_task(sha: str) -> SuiteATask:
    return SuiteATask(
        id="t_live",
        description="Do the thing.",
        acceptance=[{"kind": "build_passes"}],
        starting_repo="metaensemble",
        starting_sha=sha,
    )


def test_run_suite_a_live_end_to_end_wiring(fake_acceptance, fake_claude, tmp_path):
    repo, sha = _trivial_project(tmp_path)
    workdir = tmp_path / "workdir"
    fake_claude["log"] = fake_acceptance["log"]
    fake_acceptance["reports"] = [
        FakeAcceptanceReport(passed=True, score=1.0, details=["PASS build_passes — ok"]),
        FakeAcceptanceReport(
            passed=False, score=0.0,
            details=["FAIL build_passes — pytest exited 1"],
        ),
    ]

    cell = CellSpec(id="MM_full", kind="full_system", dispatch_fn="MM_full")
    outcomes = run_suite_a_live(
        cell,
        [_live_task(sha)],
        seeds=2,
        budget_usd=0.30,
        workdir=workdir,
        repo_root=repo,
        model="test-model",
        claude_extra_args=["--verbose"],
    )

    assert [o.seed for o in outcomes] == [0, 1]
    first, second = outcomes

    # Seed 0: acceptance passed; fake claude modified a file in the workspace.
    assert first.task_id == "t_live"
    assert first.passed is True
    assert first.quality_score == 1.0
    assert first.minimum_useful_answer_score == 1.0
    assert first.tokens_in == 1200  # 1000 + 150 cacheRead + 50 cacheCreation
    assert first.tokens_out == 300
    assert first.budget_exceeded is False
    assert first.duration_ms == 1234.0
    assert first.failure_reason is None

    # Seed 1: acceptance failed; untouched workspace; first FAIL detail wins.
    assert second.passed is False
    assert second.quality_score == 0.0
    assert second.minimum_useful_answer_score == 0.0
    assert second.failure_reason == "FAIL build_passes — pytest exited 1"

    # Baseline stats are captured BEFORE the claude call, acceptance after,
    # in the same workspace, for every run.
    kinds = [entry[0] for entry in fake_acceptance["log"]]
    assert kinds == ["baseline", "claude", "acceptance"] * 2
    per_run = [fake_acceptance["log"][i:i + 3] for i in (0, 3)]
    for run_entries in per_run:
        workspaces = {entry[1] for entry in run_entries}
        assert len(workspaces) == 1
    # check_acceptance received the criteria and the captured baseline.
    acceptance_call = fake_acceptance["log"][2]
    assert acceptance_call[2] == [{"kind": "build_passes"}]
    assert acceptance_call[3] == fake_acceptance["baseline"]

    # The claude command is the documented single invocation.
    cmd = fake_claude["calls"][0]["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert ["--output-format", "json"] == cmd[2:4]
    budget_index = cmd.index("--max-budget-usd")
    assert cmd[budget_index + 1] == "0.3000"
    model_index = cmd.index("--model")
    assert cmd[model_index + 1] == "test-model"
    assert "--no-session-persistence" in cmd
    sources_index = cmd.index("--setting-sources")
    assert cmd[sources_index + 1] == "user,project,local"  # MM cell keeps hooks
    assert "--verbose" in cmd  # claude_extra_args pass-through
    assert cmd[-1] == build_cell_prompt("MM_full", _live_task(sha))

    # Workspaces are kept and indexed by run-manifest.jsonl.
    manifest_lines = [
        json.loads(line)
        for line in (workdir / "run-manifest.jsonl").read_text().splitlines()
    ]
    assert len(manifest_lines) == 2
    for seed, row in enumerate(manifest_lines):
        assert row["cell"] == "MM_full"
        assert row["task"] == "t_live"
        assert row["seed"] == seed
        assert row["sha"] == sha
        assert row["model"] == "test-model"
        assert row["exit"] == 0
        assert Path(row["workspace"]).is_dir()
    assert manifest_lines[0]["workspace"] != manifest_lines[1]["workspace"]
    # The fake claude ran inside the seed-0 workspace.
    assert str(fake_claude["calls"][0]["cwd"]) == manifest_lines[0]["workspace"]


def test_run_suite_a_live_baseline_cell_uses_isolation_and_bare_prompt(
    fake_acceptance, fake_claude, tmp_path,
):
    repo, sha = _trivial_project(tmp_path)
    task = _live_task(sha)
    cell = CellSpec(id="B1_single_agent", kind="baseline", dispatch_fn="B1_single_agent")

    run_suite_a_live(
        cell, [task], seeds=1, budget_usd=0.30,
        workdir=tmp_path / "workdir", repo_root=repo, model="test-model",
    )

    cmd = fake_claude["calls"][0]["cmd"]
    sources_index = cmd.index("--setting-sources")
    assert cmd[sources_index + 1] == "project,local"  # user-level hooks excluded
    assert cmd[-1] == task.description.strip()


def test_run_suite_a_live_flags_budget_exceeded(fake_acceptance, fake_claude, tmp_path):
    repo, sha = _trivial_project(tmp_path)
    fake_claude["payloads"] = [_claude_payload(cost=0.31)]
    cell = CellSpec(id="B4_best_prompt", kind="baseline", dispatch_fn="B4_best_prompt")

    outcomes = run_suite_a_live(
        cell, [_live_task(sha)], seeds=1, budget_usd=0.30,
        workdir=tmp_path / "workdir", repo_root=repo, model="test-model",
    )

    assert outcomes[0].budget_exceeded is True


def test_run_suite_a_live_reports_parse_failure(fake_acceptance, fake_claude, tmp_path):
    repo, sha = _trivial_project(tmp_path)
    fake_claude["payloads"] = ["this is not json"]
    fake_claude["mutate_first_call"] = False
    fake_acceptance["reports"] = [FakeAcceptanceReport(passed=False, score=0.0)]
    cell = CellSpec(id="B1_single_agent", kind="baseline", dispatch_fn="B1_single_agent")

    outcomes = run_suite_a_live(
        cell, [_live_task(sha)], seeds=1, budget_usd=0.30,
        workdir=tmp_path / "workdir", repo_root=repo, model="test-model",
    )

    outcome = outcomes[0]
    assert outcome.passed is False
    assert outcome.failure_reason.startswith("claude_output_parse_failed")
    assert outcome.tokens_in == 0
    assert outcome.tokens_out == 0
    assert outcome.minimum_useful_answer_score == 0.0


def test_run_suite_a_live_handles_timeout(fake_acceptance, monkeypatch, tmp_path):
    repo, sha = _trivial_project(tmp_path)
    workdir = tmp_path / "workdir"

    def _timeout(cmd, *, cwd):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=suite_a.CLAUDE_TIMEOUT_S)

    monkeypatch.setattr(suite_a, "_run_claude", _timeout)
    fake_acceptance["reports"] = [FakeAcceptanceReport(passed=False, score=0.0)]
    cell = CellSpec(id="MM_full", kind="full_system", dispatch_fn="MM_full")

    outcomes = run_suite_a_live(
        cell, [_live_task(sha)], seeds=1, budget_usd=0.30,
        workdir=workdir, repo_root=repo, model="test-model",
    )

    assert outcomes[0].passed is False
    assert "claude_timeout" in outcomes[0].failure_reason
    row = json.loads((workdir / "run-manifest.jsonl").read_text().splitlines()[0])
    assert row["exit"] is None


# ---------------------------------------------------------------------------
# CLI --suite flag
# ---------------------------------------------------------------------------


@pytest.fixture
def _hermetic_main(monkeypatch):
    """Keep `cli.main` from touching real on-disk user state in these tests."""
    import metaensemble.lib.installer as installer

    monkeypatch.setattr(installer, "migrate_vocabulary_state", lambda home=None: [])


def test_cli_eval_suite_flag_parses(_hermetic_main, monkeypatch):
    captured = {}

    def _fake_cmd_eval(args):
        captured["suite"] = args.suite
        return 0

    monkeypatch.setattr(cli, "cmd_eval", _fake_cmd_eval)
    assert cli.main(["eval", "--suite", "a"]) == 0
    assert captured["suite"] == "a"
    assert cli.main(["eval", "--suite", "b"]) == 0
    assert captured["suite"] == "b"
    assert cli.main(["eval"]) == 0
    assert captured["suite"] == "all"


def test_cli_eval_suite_flag_rejects_unknown_value(_hermetic_main, monkeypatch):
    monkeypatch.setattr(cli, "cmd_eval", lambda args: 0)
    with pytest.raises(SystemExit):
        cli.main(["eval", "--suite", "c"])
