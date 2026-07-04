"""Live Suite-A cell runner: sandboxed software-engineering tasks.

Each run materializes a frozen-SHA git workspace, issues exactly one
`claude -p` call inside it, and grades the resulting tree against the
task's acceptance criteria. The workspace is kept (never deleted) under
the eval workdir so a failed run can be inspected post hoc; every run
appends one line to `<workdir>/run-manifest.jsonl` mapping
cell × task × seed to its workspace, starting SHA, model, and exit code.

Workspace materialization never touches the network: OSS fixtures are
built locally by `evals.fixtures.build.build_fixture` (deterministic
single-commit repos) and cloned with `git clone --local`; tasks whose
`starting_repo` is `metaensemble` clone the caller-supplied `repo_root`
the same way and check out the task's frozen `starting_sha`.

Hook isolation (per https://code.claude.com/docs/en/cli-reference):
the CLI documents `--setting-sources` as a "Comma-separated list of
setting sources to load (`user`, `project`, `local`)". MetaEnsemble's
hooks live in user-level settings (`~/.claude/settings.json`, per
https://code.claude.com/docs/en/settings), so baseline cells pass
`--setting-sources project,local` (user source excluded — no
MetaEnsemble hooks) and MM cells pass
`--setting-sources user,project,local`. Both sides pin the sources
explicitly so the comparison never rides an undocumented default. The
mechanism is confined to `_claude_isolation_args` so a live dry-run can
swap it without touching the rest of the runner.
"""
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from evals.runners.api import CellSpec, _tokens_from_claude_payload
from evals.runners.metrics import RunOutcome

# One `claude -p` invocation per run; a software task that has not
# produced a result in 15 minutes is a failed run, not a slow one.
CLAUDE_TIMEOUT_S = 900

BASELINE_CELL_IDS = frozenset({
    "B1_single_agent",
    "B2_single_agent_prompted",
    "B3_subagent_default",
    "B4_best_prompt",
})
MM_CELL_IDS = frozenset({
    "MM_full",
    "MM_minus_manifest",
    "MM_minus_ledger",
    "MM_minus_quality_gate",
})


@runtime_checkable
class TaskSpecLike(Protocol):
    """Duck type for a Suite-A task row from `evals/datasets/suite_a/tasks.yaml`."""

    id: str
    description: str
    acceptance: list[dict]
    starting_repo: str
    starting_sha: str


@dataclass(frozen=True)
class SuiteATask:
    """Concrete TaskSpecLike used by the CLI to load Suite-A rows."""

    id: str
    description: str
    acceptance: list[dict] = field(default_factory=list)
    starting_repo: str = "metaensemble"
    starting_sha: str = "__DEFERRED__"
    title: str = ""


# ---------------------------------------------------------------------------
# Workspace materialization
# ---------------------------------------------------------------------------

# (fixture name, workdir) -> (cache path, deterministic SHA). Built once per
# process per workdir; every run then clones locally from the cache.
_FIXTURE_CACHE: dict[tuple[str, str], tuple[Path, str]] = {}

_WORKSPACE_COUNTER = itertools.count(1)


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run one git command; raise with stderr attached on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _cached_fixture(name: str, workdir: Path) -> tuple[Path, str]:
    """Build fixture `name` under `<workdir>/fixtures-cache/` once per process.

    Asserts the built commit matches the deterministic SHA published in
    `evals.fixtures.build.FIXTURE_SHAS` so a non-deterministic build fails
    loudly instead of silently grading against the wrong starting state.
    """
    # Imported at call time: the fixture builder is only needed for
    # oss-fixture-* tasks, and tests substitute an interface-faithful fake.
    from evals.fixtures.build import FIXTURE_SHAS, build_fixture

    key = (name, str(workdir))
    cached = _FIXTURE_CACHE.get(key)
    if cached is not None and cached[0].exists():
        return cached

    expected = FIXTURE_SHAS.get(name)
    if expected is None:
        raise ValueError(
            f"unknown fixture {name!r}; known fixtures: {sorted(FIXTURE_SHAS)}"
        )
    dest = workdir / "fixtures-cache" / name
    if dest.exists():
        shutil.rmtree(dest)  # stale or partial build from a crashed run
    dest.parent.mkdir(parents=True, exist_ok=True)
    sha = build_fixture(name, dest)
    if sha != expected:
        raise RuntimeError(
            f"fixture {name!r} built commit {sha}, expected {expected}; "
            "the fixture build is not deterministic on this machine"
        )
    _FIXTURE_CACHE[key] = (dest, sha)
    return dest, sha


def _claim_workspace_dir(workdir: Path, run_id: str) -> Path:
    """Reserve a fresh workspace path; never clobber a kept prior workspace."""
    base = workdir / "workspaces" / run_id
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


def materialize_workspace(
    task: TaskSpecLike,
    workdir: Path,
    repo_root: Path,
    *,
    run_id: str | None = None,
) -> Path:
    """Materialize the task's frozen starting state as a fresh git workspace.

    `oss-fixture-*` repos are built deterministically into
    `<workdir>/fixtures-cache/<name>` (once per process), then cloned with
    `git clone --local` and hard-reset to the fixture SHA. `metaensemble`
    tasks clone `repo_root` locally and check out `task.starting_sha`.
    No network access on any path. Returns the workspace path.
    """
    workdir = Path(workdir).resolve()
    starting_repo = str(task.starting_repo)
    workspace = _claim_workspace_dir(
        workdir, run_id or f"{task.id}-{next(_WORKSPACE_COUNTER):04d}"
    )

    if starting_repo.startswith("oss-fixture-"):
        cache_path, sha = _cached_fixture(starting_repo, workdir)
        declared = str(getattr(task, "starting_sha", "") or "")
        if declared and not declared.startswith("__DEFERRED__") and declared != sha:
            raise RuntimeError(
                f"task {task.id}: starting_sha {declared} does not match the "
                f"deterministic fixture SHA {sha} for {starting_repo!r}"
            )
        _git("clone", "--local", str(cache_path), str(workspace))
        _git("reset", "--hard", sha, cwd=workspace)
    elif starting_repo == "metaensemble":
        sha = str(task.starting_sha)
        if not sha or sha.startswith("__DEFERRED__"):
            raise ValueError(
                f"task {task.id}: starting_sha is deferred; resolve it to a "
                "real commit before a live Suite-A run"
            )
        _git("clone", "--local", str(Path(repo_root).resolve()), str(workspace))
        _git("checkout", sha, cwd=workspace)
    else:
        raise ValueError(
            f"task {task.id}: unknown starting_repo {starting_repo!r} "
            "(expected 'oss-fixture-*' or 'metaensemble')"
        )
    return workspace


def _workspace_modified(workspace: Path, start_sha: str) -> bool:
    """True when the agent changed anything: dirty tree, new files, or new commits."""
    if _git("status", "--porcelain", cwd=workspace):
        return True
    return _git("rev-parse", "HEAD", cwd=workspace) != start_sha


# ---------------------------------------------------------------------------
# Per-cell prompts
# ---------------------------------------------------------------------------

_ROLE_PREAMBLE = (
    "You are a senior software engineer working directly in this repository "
    "(the current working directory). Be careful and methodical: read the "
    "relevant files before editing, keep the change minimal and focused, and "
    "re-check your work before you finish."
)

_B3_DELEGATION = (
    "Delegate the substeps of this task to subagents via the Task tool: "
    "dispatch one subagent per substep (investigation, implementation, "
    "verification) and integrate their results yourself before finishing."
)

_MM_ELEMENTS: dict[str, str] = {
    "manifest": (
        "Compose a Manifest for the dispatch: a typed contract naming the "
        "objective, the inputs, the acceptance criteria, and the Deliverable path."
    ),
    "ledger": (
        "Record the Run in the Ledger so the dispatch is observable after the fact."
    ),
    "quality_gate": (
        "Quality gate: verify the Deliverable against every acceptance "
        "criterion before finishing; do not finish while any criterion fails."
    ),
}

_MM_ABLATIONS: dict[str, tuple[str, str]] = {
    # cell id -> (dropped element key, explicit ablation statement)
    "MM_minus_manifest": (
        "manifest",
        "Ablation: run the protocol WITHOUT composing a Manifest — dispatch "
        "the Executor without a typed contract.",
    ),
    "MM_minus_ledger": (
        "ledger",
        "Ablation: run the protocol WITHOUT recording Run rows in the Ledger.",
    ),
    "MM_minus_quality_gate": (
        "quality_gate",
        "Ablation: run the protocol WITHOUT the post-Deliverable quality "
        "gate — finish without a final re-verification pass.",
    ),
}


def _criteria_kinds(task: TaskSpecLike) -> list[str]:
    return sorted({
        str(c.get("kind"))
        for c in task.acceptance
        if isinstance(c, dict) and c.get("kind")
    })


def _file_pointers(task: TaskSpecLike) -> list[str]:
    pointers: list[str] = []
    for criterion in task.acceptance:
        if not isinstance(criterion, dict):
            continue
        for key in ("path", "glob"):
            value = criterion.get(key)
            if value:
                pointers.append(str(value))
    return pointers


def _b2_prompt(task: TaskSpecLike) -> str:
    kinds = _criteria_kinds(task)
    graded = (
        "Your result is graded against acceptance criteria of these kinds: "
        + ", ".join(kinds)
        + ". The exact thresholds are not shown; do the task thoroughly."
        if kinds
        else "Your result is graded against fixed acceptance criteria."
    )
    return f"{_ROLE_PREAMBLE} {graded}\n\nTask:\n{task.description.strip()}"


def _b4_brief(task: TaskSpecLike) -> str:
    criteria_json = json.dumps(list(task.acceptance), indent=2, ensure_ascii=False)
    pointers = _file_pointers(task)
    pointer_line = (
        "Files named by the acceptance criteria: " + ", ".join(pointers)
        if pointers
        else "No specific files are named by the acceptance criteria."
    )
    return (
        f"{_ROLE_PREAMBLE}\n\n"
        f"Task:\n{task.description.strip()}\n\n"
        "Acceptance criteria (graded exactly as written):\n"
        f"{criteria_json}\n\n"
        "Workspace pointers:\n"
        "- The workspace root is the current working directory; it is a git "
        "checkout of the project under test.\n"
        f"- {pointer_line}\n\n"
        "Method — plan, then verify:\n"
        "1. Plan the change: list the concrete steps before touching any file.\n"
        "2. Execute the plan.\n"
        "3. Verify every acceptance criterion above against the workspace "
        "before you finish; fix anything that fails."
    )


def _mm_prompt(cell_id: str, task: TaskSpecLike) -> str:
    dropped, ablation_note = _MM_ABLATIONS.get(cell_id, (None, None))
    lines = [
        "MetaEnsemble Coordinator protocol:",
        "- Act as the MetaEnsemble Coordinator: dispatch an Executor for this "
        "task per the metaensemble-protocol skill.",
    ]
    for key, text in _MM_ELEMENTS.items():
        if key != dropped:
            lines.append(f"- {text}")
    if ablation_note:
        lines.append(f"- {ablation_note}")
    return _b4_brief(task) + "\n\n" + "\n".join(lines)


def build_cell_prompt(cell: CellSpec | str, task: TaskSpecLike) -> str:
    """Build the single-invocation prompt for one cell × task.

    Cell contract (mirrors `evals/configs/default.yaml`):
    - B1_single_agent: the task description only — no extra framing.
    - B2_single_agent_prompted: strong role + carefulness preamble naming the
      acceptance-criteria kinds but not their thresholds.
    - B3_subagent_default: B2 plus an explicit subagent-delegation instruction.
    - B4_best_prompt: full criteria verbatim + workspace pointers +
      plan-then-verify method — the strongest honest single-agent competitor.
    - MM_full: the B4 brief plus the MetaEnsemble Coordinator protocol
      (dispatch per the metaensemble-protocol skill, Manifest, Ledger,
      quality gate).
    - MM_minus_*: MM_full minus the named element, with the removal stated
      explicitly so the model does not reinvent it.
    """
    cell_id = getattr(cell, "id", cell)
    if cell_id == "B1_single_agent":
        return task.description.strip()
    if cell_id == "B2_single_agent_prompted":
        return _b2_prompt(task)
    if cell_id == "B3_subagent_default":
        return _b2_prompt(task) + "\n\n" + _B3_DELEGATION
    if cell_id == "B4_best_prompt":
        return _b4_brief(task)
    if cell_id in MM_CELL_IDS:
        return _mm_prompt(cell_id, task)
    raise ValueError(f"unknown Suite-A cell: {cell_id!r}")


def _claude_isolation_args(cell: CellSpec | str) -> list[str]:
    """Per-cell settings-isolation flags for one `claude -p` invocation.

    Documented mechanism: `--setting-sources` — "Comma-separated list of
    setting sources to load (`user`, `project`, `local`)"
    (https://code.claude.com/docs/en/cli-reference). MetaEnsemble installs
    its hooks in user-level settings (`~/.claude/settings.json`; hook scopes
    per https://code.claude.com/docs/en/settings), so baseline cells exclude
    the `user` source and MM cells include it. Sources are pinned explicitly
    on both sides so neither depends on the CLI's undocumented default.

    This is the single replaceable seam for hook isolation: if a live
    dry-run falsifies the mechanism, change only this function.
    """
    cell_id = getattr(cell, "id", cell)
    if cell_id in MM_CELL_IDS:
        return ["--setting-sources", "user,project,local"]
    return ["--setting-sources", "project,local"]


# ---------------------------------------------------------------------------
# Live cell runner
# ---------------------------------------------------------------------------


def _run_claude(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Subprocess seam for the single `claude -p` call. Tests monkeypatch this."""
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_S,
    )


def run_suite_a_live(
    cell: CellSpec,
    tasks: Sequence[TaskSpecLike],
    *,
    seeds: int,
    budget_usd: float,
    workdir: Path,
    repo_root: Path,
    model: str,
    claude_extra_args: Sequence[str] | None = None,
    seed_start: int = 0,
) -> list[RunOutcome]:
    """Run one Suite-A cell live: seeds × tasks, one `claude -p` call per run.

    Per run: materialize the frozen workspace, capture baseline stats,
    invoke Claude Code inside the workspace (JSON output, budget-capped,
    no session persistence, per-cell settings isolation), grade the tree
    with `check_acceptance`, and emit a RunOutcome. Workspaces are kept
    under `<workdir>/workspaces/` and indexed by
    `<workdir>/run-manifest.jsonl`.
    """
    # Imported at call time so the module loads without the acceptance
    # workstream present; tests substitute an interface-faithful fake.
    from evals.runners.acceptance import check_acceptance, collect_baseline_stats

    cell_id = getattr(cell, "id", cell)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    manifest_path = workdir / "run-manifest.jsonl"

    outcomes: list[RunOutcome] = []
    for task in tasks:
        # seed_start lets a multi-session cycle resume where a prior batch
        # stopped (e.g. a 1-seed pilot becomes seed 0 of the full run)
        # without re-buying completed seeds. The seed is a repetition
        # index only — it never enters the prompt — so batches are
        # statistically interchangeable.
        for seed in range(seed_start, seed_start + seeds):
            workspace = materialize_workspace(
                task, workdir, repo_root,
                run_id=f"{cell_id}/{task.id}/seed{seed}",
            )
            start_sha = _git("rev-parse", "HEAD", cwd=workspace)
            baseline = collect_baseline_stats(workspace)

            prompt = build_cell_prompt(cell_id, task)
            cmd = [
                "claude", "-p",
                "--output-format", "json",
                "--max-budget-usd", f"{budget_usd:.4f}",
                "--model", str(model),
                "--no-session-persistence",
                # Print mode cannot answer permission prompts, so without an
                # explicit mode every file edit is silently denied and each
                # cell degrades to prose-only (verified live, 2026-07-04:
                # a Write in a bare `claude -p` fails; with bypass it lands).
                # The workspace is a throwaway sandbox clone, which is what
                # makes bypass an acceptable permission posture here.
                "--permission-mode", "bypassPermissions",
                *_claude_isolation_args(cell),
                *(list(claude_extra_args) if claude_extra_args else []),
                prompt,
            ]

            exit_code: int | None
            claude_error: str | None = None
            stdout = ""
            stderr = ""
            try:
                proc = _run_claude(cmd, cwd=workspace)
                stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
            except subprocess.TimeoutExpired:
                exit_code = None
                claude_error = f"claude_timeout: no result within {CLAUDE_TIMEOUT_S}s"

            # Parse the payload exactly like api.py's suite-B live path.
            duration_ms = 0.0
            cost_usd = 0.0
            tokens_in = 0
            tokens_out = 0
            exact_models: list[str] = []
            if claude_error is None:
                if not stdout.strip() and exit_code != 0:
                    claude_error = (stderr or "claude_failed").strip()[:500]
                else:
                    try:
                        payload = json.loads(stdout)
                        duration_ms = float(payload.get("duration_ms") or 0.0)
                        cost_usd = float(payload.get("total_cost_usd") or 0.0)
                        tokens_in, tokens_out = _tokens_from_claude_payload(payload)
                        # The report must record exact model IDs; the CLI
                        # accepts aliases, so the payload's modelUsage keys
                        # are the only authoritative source.
                        if isinstance(payload.get("modelUsage"), dict):
                            exact_models = sorted(payload["modelUsage"].keys())
                        if exit_code != 0 or payload.get("is_error"):
                            errors = payload.get("errors") or []
                            claude_error = (
                                "; ".join(str(e) for e in errors)
                                or str(payload.get("result") or "")
                                or str(payload.get("subtype") or "")
                                or "claude_failed"
                            )
                    except Exception as exc:
                        claude_error = f"claude_output_parse_failed: {exc}"
                        duration_ms = 0.0
                        tokens_in = 0
                        tokens_out = 0
            budget_exceeded = (
                cost_usd > budget_usd
                or "maximum budget" in (claude_error or "").lower()
            )

            # Grade the workspace even after a Claude error or timeout: the
            # tree may still (rarely) satisfy the criteria, and grading is
            # what decides `passed`, not the transport.
            report = check_acceptance(
                workspace, list(task.acceptance), baseline=baseline,
            )
            modified = _workspace_modified(workspace, start_sha)
            first_fail = next(
                (d for d in report.details if str(d).startswith("FAIL")), None,
            )
            failure_reason = (
                None
                if report.passed
                else (first_fail or claude_error or "acceptance_failed")
            )

            outcomes.append(RunOutcome(
                task_id=task.id,
                seed=seed,
                passed=report.passed,
                quality_score=float(report.score),
                minimum_useful_answer_score=1.0 if modified else 0.0,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                budget_exceeded=budget_exceeded,
                duration_ms=duration_ms,
                failure_reason=failure_reason,
            ))

            with manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "cell": cell_id,
                    "task": task.id,
                    "seed": seed,
                    "workspace": str(workspace),
                    "sha": start_sha,
                    "model": str(model),
                    "exact_models": exact_models,
                    "cost_usd": cost_usd,
                    "exit": exit_code,
                }) + "\n")
    return outcomes
