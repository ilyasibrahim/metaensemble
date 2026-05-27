"""Tests for the evaluation harness (W8).

These tests cover the deterministic pieces: metric math (Wilson CI,
pass@budget, overhead ratio), report rendering, replay cassettes, and
live-run payload parsing. They do not call Claude.
"""
from __future__ import annotations

import json
from pathlib import Path


from evals.runners.api import (
    CellSpec,
    HarnessReport,
    TaskSpec,
    Tier,
    assemble_report,
    evaluate_release_gates,
    _parse_suite_b_predictions,
    _tokens_from_claude_payload,
    render_report,
    run_cell_replay,
)
from evals.runners.metrics import (
    RunOutcome,
    compute_cell_metrics,
    wilson_95,
)


def test_wilson_95_basic():
    """Sanity-check the Wilson interval against known values."""
    wi = wilson_95(7, 10)
    assert wi.n == 10
    assert wi.point == 0.7
    # Wilson 95% CI for 7/10 ≈ (0.397, 0.892).
    assert 0.35 < wi.lo < 0.45
    assert 0.85 < wi.hi < 0.93


def test_wilson_95_empty():
    """n=0 returns a defined interval rather than NaN."""
    wi = wilson_95(0, 0)
    assert wi.n == 0
    assert wi.point == 0.0
    assert wi.lo == 0.0
    assert wi.hi == 1.0


def _outcome(passed: bool, tokens: int = 1000, **kw) -> RunOutcome:
    defaults = dict(
        task_id="t",
        seed=0,
        passed=passed,
        quality_score=1.0 if passed else 0.0,
        minimum_useful_answer_score=0.8 if passed else 0.0,
        tokens_in=tokens // 2,
        tokens_out=tokens // 2,
        budget_exceeded=False,
        duration_ms=2000.0,
    )
    defaults.update(kw)
    return RunOutcome(**defaults)


def test_compute_cell_metrics_pass_at_budget_excludes_budget_violators():
    """A run that passed acceptance but exceeded budget does not count
    toward pass@budget."""
    runs = [
        _outcome(passed=True),
        _outcome(passed=True),
        _outcome(passed=True, budget_exceeded=True),  # excluded from pass@budget
        _outcome(passed=False),
    ]
    m = compute_cell_metrics(cell_id="MM", runs=runs)
    # 2 of 4 count: 2 pure passes; the budget-violator and the fail do not.
    assert m.pass_at_budget.n == 4
    assert m.pass_at_budget.point == 0.5


def test_compute_cell_metrics_overhead_ratio():
    """The orchestration_overhead_ratio is MM total tokens / baseline tokens."""
    runs = [_outcome(passed=True, tokens=4000) for _ in range(5)]
    m = compute_cell_metrics(
        cell_id="MM",
        runs=runs,
        baseline_total_tokens=10000,
    )
    # 5 × 4000 = 20000; 20000 / 10000 = 2.0
    assert m.orchestration_overhead_ratio == 2.0


def test_compute_cell_metrics_failed_run_token_waste():
    """Failed and budget-exceeded runs count toward failed_run_token_waste."""
    runs = [
        _outcome(passed=True, tokens=1000),
        _outcome(passed=False, tokens=2000),
        _outcome(passed=True, budget_exceeded=True, tokens=3000),
    ]
    m = compute_cell_metrics(cell_id="MM", runs=runs)
    # Both the failed and the budget-violator contribute their tokens.
    assert m.failed_run_token_waste == 5000


def test_replay_runner_reads_cassettes(tmp_path: Path):
    """Replay tier reads cassettes from the prescribed path."""
    cell = CellSpec(id="B1", kind="baseline", dispatch_fn="single_agent")
    task = TaskSpec(id="t1", suite="suite_a", description="x", acceptance=[])
    cassette_dir = tmp_path / "cassettes"
    cassette_path = cassette_dir / cell.id / task.id / "0.json"
    cassette_path.parent.mkdir(parents=True)
    cassette_path.write_text(json.dumps({
        "task_id": task.id,
        "seed": 0,
        "passed": True,
        "quality_score": 0.9,
        "minimum_useful_answer_score": 0.8,
        "tokens_in": 100,
        "tokens_out": 200,
        "budget_exceeded": False,
        "duration_ms": 1500.0,
    }))
    outcomes = run_cell_replay(cell, [task], cassette_dir, seeds=1)
    assert len(outcomes) == 1
    assert outcomes[0].passed is True
    assert outcomes[0].tokens_out == 200


def test_replay_runner_reads_packed_cassettes(tmp_path: Path):
    """Replay also accepts compact JSONL packs for shipped fixture data."""
    cell = CellSpec(id="B1", kind="baseline", dispatch_fn="single_agent")
    task = TaskSpec(id="t1", suite="suite_a", description="x", acceptance=[])
    cassette_dir = tmp_path / "cassettes"
    cassette_dir.mkdir()
    (cassette_dir / "bootstrap.jsonl").write_text(json.dumps({
        "source": "bootstrap_fixture_not_empirical",
        "cell_id": cell.id,
        "task_id": task.id,
        "seed": 0,
        "passed": True,
        "quality_score": 0.9,
        "minimum_useful_answer_score": 0.8,
        "tokens_in": 100,
        "tokens_out": 200,
        "budget_exceeded": False,
        "duration_ms": 1500.0,
    }) + "\n")
    outcomes = run_cell_replay(cell, [task], cassette_dir, seeds=1)
    assert len(outcomes) == 1
    assert outcomes[0].task_id == "t1"


def test_replay_missing_cassette_raises(tmp_path: Path):
    cell = CellSpec(id="B1", kind="baseline", dispatch_fn="x")
    task = TaskSpec(id="t1", suite="suite_a", description="x", acceptance=[])
    import pytest
    with pytest.raises(FileNotFoundError):
        run_cell_replay(cell, [task], tmp_path / "absent", seeds=1)


def test_live_suite_b_prediction_parser_accepts_json_in_result_text():
    predictions, quality = _parse_suite_b_predictions("""
    Here is the JSON:
    {"predictions":[
      {"id":"b1","label":"northern_standard","confidence":0.87},
      {"id":"b2","label":"maay","confidence":1.5}
    ]}
    """)
    assert predictions == {"b1": "northern_standard", "b2": "maay"}
    assert quality["b1"] == 0.87
    assert quality["b2"] == 1.0


def test_live_suite_b_token_parser_prefers_model_usage():
    tokens_in, tokens_out = _tokens_from_claude_payload({
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "modelUsage": {
            "claude-haiku": {
                "inputTokens": 10,
                "cacheReadInputTokens": 20,
                "cacheCreationInputTokens": 30,
                "outputTokens": 40,
            }
        },
    })
    assert tokens_in == 60
    assert tokens_out == 40


def test_render_report_markdown_shape():
    cell = CellSpec(id="MM_full", kind="full_system", dispatch_fn="mm")
    runs = [_outcome(passed=True, tokens=1000) for _ in range(5)]
    report = assemble_report(
        tier=Tier.SMOKE,
        cells_with_outcomes=[(cell, runs)],
        baseline_total_tokens_lookup={"MM_full": 4000},
    )
    out = render_report(report)
    assert "Evaluation report (smoke)" in out
    assert "MM_full" in out
    assert "pass@budget" in out


def test_evaluate_release_gates_flags_threshold_violations():
    report = HarnessReport(
        tier=Tier.FULL,
        cells=[
            compute_cell_metrics(
                cell_id="MM_full",
                runs=[
                    _outcome(passed=True, tokens=1000),
                    _outcome(passed=False, tokens=2000),
                ],
                baseline_total_tokens=1000,
            )
        ],
        notes=[],
    )
    failed, notes = evaluate_release_gates(
        report,
        failed_run_waste_threshold=0.10,
        overhead_ratio_ceiling=2.0,
    )
    assert failed is True
    assert any("D-8" in note and "FAIL" in note for note in notes)
    assert any("D-9" in note and "FAIL" in note for note in notes)


def test_yaml_datasets_load():
    """Sanity-check that the shipped YAML datasets parse and have ids."""
    import yaml
    root = Path(__file__).resolve().parent.parent.parent
    suite_a = root / "evals" / "datasets" / "suite_a" / "tasks.yaml"
    suite_b = root / "evals" / "datasets" / "suite_b" / "items.yaml"
    a = yaml.safe_load(suite_a.read_text())
    b = yaml.safe_load(suite_b.read_text())
    assert len(a["tasks"]) == 8, "Suite A must have 8 tasks per the plan"
    assert len(b["items"]) == 12, "classification smoke set must have 12 items"
    # Every task / item carries a stable id.
    assert all(t["id"] for t in a["tasks"])
    assert all(it["id"] for it in b["items"])


def test_shipped_bootstrap_replay_pack_covers_default_cycle():
    """`metaensemble eval --tier replay` should work in a clean checkout."""
    import yaml
    root = Path(__file__).resolve().parent.parent.parent
    config = yaml.safe_load((root / "evals" / "configs" / "default.yaml").read_text())
    suite_a = yaml.safe_load((root / "evals" / "datasets" / "suite_a" / "tasks.yaml").read_text())
    suite_b = yaml.safe_load((root / "evals" / "datasets" / "suite_b" / "items.yaml").read_text())
    tasks = [
        TaskSpec(id=t["id"], suite="suite_a", description=t["description"], acceptance=[])
        for t in suite_a["tasks"]
    ] + [
        TaskSpec(id=it["id"], suite="suite_b", description=it["text"], acceptance=[])
        for it in suite_b["items"]
    ]
    cassette_dir = root / "evals" / "cassettes"

    for c in config["cells"]:
        cell = CellSpec(id=c["id"], kind=c["kind"], dispatch_fn=c["id"])
        outcomes = run_cell_replay(
            cell,
            tasks,
            cassette_dir,
            seeds=config["cycle"]["seeds"],
        )
        assert len(outcomes) == len(tasks) * config["cycle"]["seeds"]
