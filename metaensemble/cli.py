"""MetaEnsemble CLI.

Installed by `pyproject.toml` as the `metaensemble` console script. The
canonical entry point for bootstrapping a project (`metaensemble init`)
and for invoking the small read-only tools (`metaensemble limits`,
`metaensemble standup`, etc.) from outside an agent runtime.

See ARCHITECTURE.md §4 (Portability) for the bootstrap semantics this CLI
implements.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parent
EXPERIMENTAL_NOTICE = (
    "MetaEnsemble v0.3.0 is feedback-first software. Its first full-tier "
    "calibration measured quality parity with strong single-agent baselines "
    "at a 1.55x token premium on single-context tasks; no quality-per-token "
    "superiority is claimed for that class. See docs/SYSTEM-CARD.md."
)
LAYOUT_CHOICES = ("namespaced", "top-level")


def _normalize_layout(value: str) -> str:
    return value.strip().lower()


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize `.metaensemble/` in the current working directory."""
    target = Path.cwd() / ".metaensemble"
    if target.exists() and not args.force:
        print(f"{target} already exists; pass --force to reinitialize.", file=sys.stderr)
        return 1

    # Delegate to the shared idempotent initializer that `apply_install` also
    # uses. It creates every subdirectory we need, the Ledger DB, the
    # JSONL mirror, the starter budgets.yaml, and the project-local
    # `.gitignore` that keeps transient state out of the user's git
    # history.
    from metaensemble.lib.installer import _ensure_project_state
    _ensure_project_state(Path.cwd())

    state = target / "state"
    budgets_target = target / "budgets.yaml"
    print(f"Initialized MetaEnsemble project state at {target}/")
    print(f"  - Ledger database:  {state / 'department.db'}")
    print(f"  - Append-only mirror: {state / 'runs.jsonl'}")
    print(f"  - Budget config:    {budgets_target}")
    print(f"  - Git ignore:       {target / '.gitignore'} (transient state excluded; declarations committable)")
    print(f"\n{EXPERIMENTAL_NOTICE}")

    if args.pack:
        # Starter packs are planned for a future release; surface what is available now.
        print(f"\nNote: starter pack '{args.pack}' is reserved for a future release. Core ships seven base Roles.")
        print(f"  Curated Roles available at: {CORE_DIR / 'roles'}/")
    return 0


def cmd_limits(_: argparse.Namespace) -> int:
    from metaensemble.tools import limits
    return limits.main()


def cmd_standup(_: argparse.Namespace) -> int:
    from metaensemble.tools import standup
    return standup.main()


def cmd_executors(_: argparse.Namespace) -> int:
    from metaensemble.tools import executors
    return executors.main()


def cmd_perf(_: argparse.Namespace) -> int:
    from metaensemble.tools import perf
    return perf.main()


def cmd_stats(_: argparse.Namespace) -> int:
    from metaensemble.tools import stats
    return stats.main()


def cmd_ledger(args: argparse.Namespace) -> int:
    from metaensemble.tools import ledger
    return ledger.main(args.subargs)


def cmd_hook(args: argparse.Namespace) -> int:
    """Invoke a hook script by filename. Used by the resilient launcher.

    settings.json hook commands installed by `_hook_command` route through
    this entry point as `me-run hook <filename>`. The indirection makes
    the hook commands path-portable: the launcher resolves the Python
    interpreter and PYTHONPATH at execution time, so moving the project
    or recreating the venv does not require a settings.json rewrite.
    """
    import runpy

    hook_path = CORE_DIR / "hooks" / args.name
    if not hook_path.exists() or not hook_path.is_file():
        print(f"hook script not found: {hook_path}", file=sys.stderr)
        return 1
    # `run_name="__main__"` triggers the script's `if __name__ == "__main__"`
    # guard so the hook's `sys.exit(run())` path fires as if it had been
    # invoked directly. SystemExit propagates; the caller's exit code matches.
    runpy.run_path(str(hook_path), run_name="__main__")
    return 0


def cmd_statusline(_: argparse.Namespace) -> int:
    """Invoke the statusline script through the resilient launcher."""
    import runpy

    statusline_path = CORE_DIR / "statusline" / "me_status.py"
    if not statusline_path.exists() or not statusline_path.is_file():
        print(f"statusline script not found: {statusline_path}", file=sys.stderr)
        return 1
    runpy.run_path(str(statusline_path), run_name="__main__")
    return 0


def cmd_setup(args: argparse.Namespace, input_fn=input) -> int:
    """Interactive setup wizard.

    Lists every project Claude Code knows about, prompts the user to
    choose one to adopt, runs `user-setup` if needed (asking for layout
    only when user-level integration hasn't been installed yet), then
    runs `adopt` on the chosen project. Replaces the previous
    cwd-implicit flow with an explicit project picker.

    `input_fn` is parameterized for tests; production callers use the
    builtin `input`.
    """
    from metaensemble.lib.installer import detect_user_layout, discover_projects

    print("MetaEnsemble setup wizard\n")

    # No explicit bootstrap step here: `cmd_user_setup` (invoked below
    # when user-level integration isn't installed) emits the launcher
    # via its `render-launcher` plan action, which is idempotent. If
    # the launcher is already there, that action becomes a no-op.

    # Step 1 — show the project picker.
    projects = discover_projects()
    if not projects:
        print(
            "No Claude Code projects found on this machine. Open a "
            "Claude Code session in a project directory first, then "
            "re-run `metaensemble setup`.",
            file=sys.stderr,
        )
        return 1

    print("Known projects (from Claude Code's `~/.claude/projects/`):\n")
    options: list = []
    for i, p in enumerate(projects, start=1):
        available = p.path.exists()
        mark = "[installable]" if available else "[unavailable]"
        if p.has_metaensemble_dir:
            status = "installed"
        elif available:
            status = "not installed"
        else:
            status = "directory missing"
        run_summary = (
            f"{p.run_count} run(s)"
            + (f", last {p.last_run_ts}" if p.last_run_ts else "")
            if p.run_count else "no runs"
        )
        print(f"  {mark:<14}  {i}. {p.path}")
        print(f"                     {status} · {run_summary}")
        options.append(p)
    print()

    # Step 3 — prompt for project choice.
    prompt = f"Which project to adopt? [1-{len(options)}, q to quit]: "
    try:
        choice = input_fn(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        return 1
    if choice.lower() in ("q", "quit", ""):
        print("Aborted.")
        return 0
    try:
        idx = int(choice) - 1
        chosen = options[idx]
        if idx < 0 or idx >= len(options):
            raise IndexError
    except (ValueError, IndexError):
        print(f"setup: invalid choice {choice!r}.", file=sys.stderr)
        return 1

    if not chosen.path.exists():
        print(
            f"setup: {chosen.path} no longer exists on disk; cannot adopt.\n"
            "(Claude Code's records can outlive the directories it saw.)",
            file=sys.stderr,
        )
        return 1

    # Step 4 — run user-setup if it hasn't been run yet.
    current_layout = detect_user_layout()
    if current_layout is None:
        if args.layout:
            chosen_layout = _normalize_layout(args.layout)
            print(f"User-level integration not installed; using --layout={chosen_layout}.")
        else:
            try:
                layout_input = input_fn(
                    "User-level integration not installed.\n"
                    "  namespaced — slash commands install namespaced "
                    "(/metaensemble:dispatch)\n"
                    "  top-level  — slash commands install top-level (/dispatch)\n"
                    "Layout [namespaced/top-level, default=namespaced]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=sys.stderr)
                return 1
            chosen_layout = _normalize_layout(layout_input or "namespaced")
            if chosen_layout not in LAYOUT_CHOICES:
                print(f"setup: invalid layout {chosen_layout!r}.", file=sys.stderr)
                return 1
        print(f"\nRunning user-setup (layout={chosen_layout}) ...")
        rc = cmd_user_setup(argparse.Namespace(layout=chosen_layout, dry_run=False))
        if rc != 0:
            return rc
    else:
        print(f"User-level integration already installed (layout={current_layout.value}).")

    # Step 5 — adopt the chosen project.
    print(f"\nAdopting {chosen.path} …")
    return cmd_adopt(argparse.Namespace(path=str(chosen.path), dry_run=False))


def cmd_manifest(args: argparse.Namespace) -> int:
    """`metaensemble manifest <subcommand>` — Manifest authoring helpers.

    Subcommands:
        validate <path>      — load + schema-validate the YAML file at <path>
                               and report errors with line:column when possible.
        new-id               — print a fresh `hm-<UUIDv7>` manifest id to stdout.
        scaffold <task>      — write a starter Manifest YAML to stdout (or to
                               `-o <path>`) pre-filled with TODO markers in
                               every required-but-author-supplied field. The
                               output deliberately fails schema validation
                               until the author replaces the `TODO:` markers.
    """
    from metaensemble.lib.manifest import load_manifest

    if args.subcmd == "validate":
        path = Path(args.path)
        if not path.exists():
            print(f"manifest validate: file not found: {path}", file=sys.stderr)
            return 1
        try:
            data = load_manifest(path)
        except Exception as exc:
            # Surface YAML parse errors with line:column and schema
            # violations with the field path so the author can fix the
            # specific line rather than guessing.
            mark = getattr(exc, "problem_mark", None)
            if mark is not None and getattr(exc, "problem", None):
                loc = (
                    f" at line {mark.line + 1}, column {mark.column + 1}"
                    if mark.line is not None and mark.column is not None
                    else ""
                )
                print(f"{path}: YAML error{loc}: {exc.problem}", file=sys.stderr)
                return 1
            message = getattr(exc, "message", None)
            abs_path = getattr(exc, "absolute_path", None)
            if message:
                field_loc = ""
                if abs_path is not None:
                    try:
                        parts = list(abs_path)
                        if parts:
                            field_loc = f" at field `{'.'.join(str(p) for p in parts)}`"
                    except TypeError:
                        pass
                print(f"{path}: schema error{field_loc}: {message}", file=sys.stderr)
                return 1
            print(f"{path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"{path}: valid ({data.get('manifest_id')}, task={data.get('task')!r}, "
              f"deliverables={len(data.get('expected_deliverables', []))})")
        return 0

    if args.subcmd == "new-id":
        from metaensemble.lib.ids import uuid7
        print(f"hm-{uuid7()}")
        return 0

    if args.subcmd == "scaffold":
        from metaensemble.lib.manifest import scaffold_manifest

        # The library renderer owns the starter-YAML shape (TODO markers,
        # task escaping) and pre-fills `context.files` with the project's
        # detected memory surfaces ({path, role: memory}) so the receiving
        # Executor is handed the runtime's memory files instead of
        # re-discovering them.
        body = scaffold_manifest(args.task, project=Path.cwd())
        out_path = getattr(args, "output", None)
        if out_path:
            # SKILL.md advertises writing into `.metaensemble/manifests/`,
            # which the author may not have created yet. Create the parent
            # chain so scaffold succeeds the first time rather than
            # surfacing a FileNotFoundError traceback.
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(body)
            print(f"wrote scaffold to {out_path}")
        else:
            sys.stdout.write(body)
        return 0

    print(f"manifest: unknown subcommand {args.subcmd!r}", file=sys.stderr)
    return 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Walk the pending-sidecar directory and reconcile stranded entries.

    Used after a runtime crash, `kill -9`, or budget-exhaustion exit, where
    the PreToolUse hook stamped a sidecar but PostToolUse never fired. Each
    stranded sidecar is written to the Ledger as a failed Run with a
    `failure_reason` naming the cause, then deleted.
    """
    from datetime import timedelta

    from metaensemble.hooks._common import db_path, jsonl_path, migration_sql, state_dir
    from metaensemble.lib.ledger import Ledger
    from metaensemble.lib.reconcile import reconcile_stale_pending

    if args.dry_run:
        # Dry-run path: count sidecars that would be reconciled without
        # writing anything. Useful for `metaensemble reconcile --dry-run`
        # to confirm cleanup before mutating the Ledger.
        from metaensemble.lib.reconcile import _iter_pending
        max_age = timedelta(minutes=max(0, args.older_than_minutes))
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc) - max_age
        eligible: list[str] = []
        for entry, pending in _iter_pending(state_dir()):
            try:
                ts = datetime.fromisoformat(pending.started_ts)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                ts = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
            if ts <= cutoff:
                eligible.append(pending.run_id)
        print(f"Would reconcile {len(eligible)} pending sidecar(s) older than "
              f"{args.older_than_minutes} minute(s).")
        for run_id in eligible:
            print(f"  - {run_id}")
        return 0

    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())
    reconciled = reconcile_stale_pending(
        ledger,
        state_dir(),
        max_age=timedelta(minutes=max(0, args.older_than_minutes)),
    )
    ledger.close()
    print(f"Reconciled {len(reconciled)} pending sidecar(s).")
    for r in reconciled:
        print(f"  - {r.run_id}  task={r.task_id}  reason={r.reason}")
    return 0


def cmd_relaunch(args: argparse.Namespace) -> int:
    """Print the relaunch context for an Executor, without dispatching a new Run."""
    from metaensemble.hooks._common import db_path, jsonl_path, migration_sql
    from metaensemble.lib.ledger import Ledger
    from metaensemble.lib.relaunch import prepare_relaunch, render_relaunch_context

    ledger = Ledger(db_path=db_path(), jsonl_path=jsonl_path())
    ledger.initialize(migration_sql())
    ctx = prepare_relaunch(ledger, args.alias, full=args.full)
    ledger.close()
    if ctx is None:
        print(f"No Executor with alias `{args.alias}`.", file=sys.stderr)
        return 1
    print(render_relaunch_context(ctx))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Read-only inventory of the user's and project's existing setup."""
    from metaensemble.lib.installer import survey
    result = survey(write_report=True)
    if result.report_path:
        print(f"Inspection complete. Report written to:\n  {result.report_path}")
    print(f"\nFound {len(result.discovered)} existing artifact(s).")
    print(f"Found {len(result.collisions)} collision(s) with MetaEnsemble-shipped items.")
    print(
        "\nReview the report and `.metaensemble/install-decisions.yaml`, then run `metaensemble adopt` "
        "(or `metaensemble setup` for the wizard)."
    )
    return 0


def cmd_user_setup(args: argparse.Namespace) -> int:
    """Install MetaEnsemble's user-level runtime integration.

    User-level integration is the layout-shaped layer that lives under
    `~/.claude/` (slash commands, output styles, the metaensemble-protocol
    skill, lifecycle hooks, statusline) and `~/.metaensemble/runtime/`
    (the vendored runtime, including `bin/me-run`). It is global across
    every project on this machine — `--layout={namespaced|top-level}`
    decides whether commands install namespaced or top-level, and that
    choice applies to all adopted projects.

    Does NOT touch any project's `.metaensemble/`. Use `metaensemble
    adopt` to register a specific project after this runs.
    """
    from metaensemble.lib.installer import (
        Layout, apply_install, plan_install, remap_user_scope_backup_paths,
        remediate_stale_managed_symlinks, render_plan, survey,
    )
    from metaensemble.lib.topology import (
        detect_editable_install, editable_install_notice,
    )
    from dataclasses import replace

    layout = Layout(_normalize_layout(args.layout))

    # Surface install topology before doing anything else. An editable
    # install (`pip install -e .`) means the runner we are about to pin
    # will resolve `import metaensemble` back to the dev source tree —
    # the install becomes load-bearing on that tree, which contradicts
    # the documented "install once, dev tree dispensable" promise. We
    # do NOT block; users iterating on the source legitimately want
    # this. We just make the state legible.
    topology = detect_editable_install()
    if topology.editable:
        print(editable_install_notice(topology, layout.value))
        print()

    # The inspection reads `~/.claude/` (user-level inventory) for agent
    # collisions and reads the cwd for project signals. Project signals
    # don't affect user-scope actions, so the cwd binding is harmless
    # here — we filter the plan to user-scope only below.
    survey_result = survey(write_report=False)
    plan = plan_install(survey_result, layout)
    user_plan = replace(plan, actions=plan.user_actions())
    user_plan = remap_user_scope_backup_paths(user_plan)

    if args.dry_run:
        print(render_plan(user_plan))
        return 0

    # Upgrade-path remediation: a previous install may have left symlinks
    # for runtime artifacts whose source files have since been renamed or
    # removed (e.g. legacy `window.md` after the limits rename). Clean
    # those up before applying so the new layout lands without colliding.
    stale = remediate_stale_managed_symlinks()
    if stale:
        print(f"Cleaned up {len(stale)} stale managed symlink(s) from a prior install:")
        for path in stale:
            print(f"  - {path}")

    report = apply_install(user_plan, dry_run=False, user_scope_only=True)
    if not report.applied and not report.errors:
        print(
            f"Unchanged. 0 action(s) applied, {len(report.noop)} no-op(s) "
            f"in `{layout.value}` layout."
        )
    else:
        print(
            f"User-level integration applied: {len(report.applied)} action(s) "
            f"in `{layout.value}` layout."
            + (f" {len(report.noop)} no-op(s) skipped." if report.noop else "")
        )
    _print_action_report(
        title="User-level action status",
        applied=report.applied,
        noop=report.noop,
        errors=report.errors,
    )
    if report.errors:
        print(f"\n{len(report.errors)} error(s):", file=sys.stderr)
        for action, msg in report.errors:
            print(f"  - {action.description}: {msg}", file=sys.stderr)
        return 1
    print(
        "\nNext: `metaensemble adopt [<project-path>]` registers a project "
        "as a MetaEnsemble consumer. Run from the project root, or pass "
        "the path."
    )
    return 0


def _render_adopt_dry_run(
    *,
    project,
    plan,
    decisions_path,
    decisions_existed: bool,
    project_state_existed: bool,
) -> str:
    """Render the project-adoption preview without touching the project.

    Project adoption has implicit side effects (`.metaensemble/` state,
    `.gitignore`, `active-roles.yaml`) in addition to per-agent install
    actions. The generic install-plan renderer only knows the per-agent
    actions, so using it directly made dry-runs say `Actions: 0` while
    real adopt still initialized project state.
    """
    report_path = project / ".metaensemble" / f"inspection-{plan.timestamp}.md"
    defaults_path = (
        project / ".metaensemble" / f"install-decisions.{plan.timestamp}.yaml"
        if decisions_existed else decisions_path
    )
    state_verb = "refresh/verify" if project_state_existed else "initialize"

    lines = [
        f"# MetaEnsemble adopt plan — layout `{plan.layout.value}`",
        "",
        f"Timestamp: {plan.timestamp}",
        f"Active Roles: {', '.join(plan.active_roles) or '(none)'}",
    ]
    if plan.inactive_roles:
        lines.append(f"Inactive Roles: {', '.join(plan.inactive_roles)}")
    lines.extend([
        "",
        "## Project setup actions",
        "",
        f"- Would write inspection report: `{report_path}`",
    ])
    if decisions_existed:
        lines.append(f"- Would keep existing decisions file: `{decisions_path}`")
        lines.append(f"- Would write current default decisions for diff: `{defaults_path}`")
    else:
        lines.append(f"- Would write fresh default decisions: `{decisions_path}`")
    lines.extend([
        f"- Would {state_verb} project state under: `{project / '.metaensemble'}`",
        f"- Would ensure Ledger DB exists: `{project / '.metaensemble' / 'state' / 'department.db'}`",
        "- Would ensure root `.gitignore` ignores: `.metaensemble/`",
        f"- Would write active roles: `{project / '.metaensemble' / 'active-roles.yaml'}`",
        "",
        "## Per-agent install actions",
        "",
        f"Actions: {len(plan.actions)}",
    ])
    if plan.actions:
        for i, action in enumerate(plan.actions, 1):
            lines.append("")
            lines.append(f"### {i}. {action.kind}")
            lines.append("")
            lines.append(f"- Description: {action.description}")
            if action.source:
                lines.append(f"- Source: `{action.source}`")
            if action.target:
                lines.append(f"- Target: `{action.target}`")
            if action.backup_path:
                lines.append(f"- Backup: `{action.backup_path}`")
    else:
        lines.append("- None — every per-agent decision is a no-op on disk.")
    return "\n".join(lines)


def _print_action_report(
    *,
    title: str,
    applied,
    noop=None,
    errors=None,
) -> None:
    """Verbose-by-default per-action status for install/teardown operations."""
    noop = list(noop or [])
    errors = list(errors or [])
    if not applied and not noop and not errors:
        print(f"{title}: no filesystem actions.")
        return
    print(f"{title}:")
    for action in applied:
        target = f" -> {action.target}" if action.target else ""
        print(f"  - OK   {action.kind}{target}")
        print(f"        {action.description}")
    for action in noop:
        target = f" -> {action.target}" if action.target else ""
        print(f"  - SKIP {action.kind}{target}")
        print("        already in desired state")
    for action, msg in errors:
        target = f" -> {action.target}" if action.target else ""
        print(f"  - FAIL {action.kind}{target}", file=sys.stderr)
        print(f"        {action.description}: {msg}", file=sys.stderr)


def cmd_adopt(args: argparse.Namespace) -> int:
    """Register a project as a MetaEnsemble consumer.

    Runs the inspection (read-only) on the project, then applies the
    project-scope subset of the install plan: per-project state
    (`<project>/.metaensemble/`), agent conversions and Role
    installations dictated by `install-decisions.yaml`, and the
    managed `.gitignore` block.

    Requires `metaensemble user-setup` to have run first. The layout
    used by user-setup is detected from `~/.claude/` and reused;
    `adopt` does NOT take a `--layout` flag because layout is a
    user-level decision that applies to every adopted project.

    The project path defaults to the current working directory; pass
    a path positionally to adopt a different project.
    """
    from dataclasses import replace

    from metaensemble.lib.installer import (
        apply_install, detect_user_layout, load_decisions,
        plan_install, survey,
    )

    layout = detect_user_layout()
    if layout is None:
        print(
            "adopt: MetaEnsemble user-level integration is not installed. "
            "Run `metaensemble user-setup --layout={namespaced,top-level}` "
            "first; then re-run adopt.",
            file=sys.stderr,
        )
        return 1

    project = Path(args.path).resolve() if args.path else Path.cwd()
    if not project.is_dir():
        print(f"adopt: project path is not a directory: {project}", file=sys.stderr)
        return 1

    # Snapshot pre-adoption state so we can name the side effects honestly.
    # The previous copy ("Unchanged. 0 action(s) applied") was misleading
    # when the per-agent decisions happen to be all-no-op on disk but the
    # project still gets initialized and the inspection re-runs.
    project_dir = project / ".metaensemble"
    decisions_path = project_dir / "install-decisions.yaml"
    project_state_existed = (project_dir / "state").exists()
    decisions_existed = decisions_path.exists()

    print(f"Adopting project: {project}")
    print(f"User-level layout (inherited): {layout.value}")

    survey_result = survey(project=project, write_report=not args.dry_run)
    if survey_result.report_path:
        print(f"Inspection report: {survey_result.report_path}")

    # Honor the Principal's edited decisions when the file is on disk;
    # otherwise use the defaults the inspection just wrote.
    decisions = survey_result.decisions
    if decisions_existed:
        try:
            decisions = load_decisions(decisions_path)
            print(f"Decisions: using existing {decisions_path}")
            # survey() writes install-decisions.<timestamp>.yaml when the
            # main file already exists, so the Principal can diff the
            # current defaults against their edits.
            from metaensemble.lib.installer import _project_metaensemble_dir
            timestamp = survey_result.report_path.stem.replace("inspection-", "") if survey_result.report_path else None
            if timestamp:
                companion = _project_metaensemble_dir(project) / f"install-decisions.{timestamp}.yaml"
                if companion.exists():
                    print(f"           current defaults written to {companion} for diff")
        except Exception as exc:
            print(
                f"warning: could not read {decisions_path}: {exc}; using defaults",
                file=sys.stderr,
            )
    else:
        if args.dry_run:
            print(f"Decisions: fresh defaults would be written to {decisions_path}")
        else:
            print(f"Decisions: fresh defaults written to {decisions_path}")

    plan = plan_install(survey_result, layout, project=project, decisions=decisions)
    project_plan = replace(plan, actions=plan.project_actions())

    if args.dry_run:
        print(_render_adopt_dry_run(
            project=project,
            plan=project_plan,
            decisions_path=decisions_path,
            decisions_existed=decisions_existed,
            project_state_existed=project_state_existed,
        ))
        return 0

    report = apply_install(project_plan, project=project, dry_run=False)
    state_verb = "initialized" if not project_state_existed else "refreshed"
    print(f"Project state {state_verb}: {project_dir}/")
    if not report.applied and not report.errors:
        print(
            "Install actions: 0 applied "
            "(every per-agent decision is a no-op on disk — nothing to convert)"
        )
    else:
        print(
            f"Install actions: {len(report.applied)} applied"
            + (f", {len(report.noop)} no-op(s) skipped" if report.noop else "")
        )
    _print_action_report(
        title="Per-agent action status",
        applied=report.applied,
        noop=report.noop,
        errors=report.errors,
    )
    if report.errors:
        print(f"\n{len(report.errors)} error(s):", file=sys.stderr)
        for action, msg in report.errors:
            print(f"  - {action.description}: {msg}", file=sys.stderr)
        return 1
    if report.backup_root:
        print(f"Backups written to: {report.backup_root}")
    if plan.active_roles:
        print(f"Active Roles ({len(plan.active_roles)}): {', '.join(plan.active_roles)}")
    return 0


def cmd_unadopt(args: argparse.Namespace) -> int:
    """Reverse a project's MetaEnsemble adoption.

    Walks `<project>/.metaensemble/backups/` and reverses the project-
    scope actions (agent restorations, curated-Role uninstalls,
    `.gitignore` block strip). Leaves user-level integration in
    place — `~/.claude/` symlinks, hooks, statusline, and the launcher
    all survive. Use `metaensemble user-teardown` to remove those.

    The project path defaults to cwd. Pass `--purge-state` to also
    delete `<project>/.metaensemble/` entirely.
    """
    from metaensemble.lib.installer import uninstall

    project = Path(args.path).resolve() if args.path else Path.cwd()
    if not project.is_dir():
        print(f"unadopt: project path is not a directory: {project}", file=sys.stderr)
        return 1

    report = uninstall(
        project=project,
        restore=True,
        purge_project_state_flag=args.purge_state,
        scope="project",
        dry_run=args.dry_run,
    )
    verb = "Would reverse" if args.dry_run else "Reversed"
    print(f"{verb} {len(report.applied)} action(s) at project scope.")
    _print_action_report(
        title="Project teardown action status",
        applied=report.applied,
        noop=report.noop,
        errors=report.errors,
    )
    if report.errors:
        print(f"\n{len(report.errors)} error(s):", file=sys.stderr)
        for action, msg in report.errors:
            print(f"  - {action.description}: {msg}", file=sys.stderr)
        return 1
    return 0


def cmd_user_teardown(args: argparse.Namespace) -> int:
    """Reverse user-setup.

    Walks the user-level backup roots
    (`~/.metaensemble/installs/<timestamp>/plan.json`) and reverses
    every user-scope action — slash command symlinks, output styles,
    the metaensemble-protocol skill, the lifecycle hook entries in
    `settings.json`, and the statusline. The vendored runtime at
    `~/.metaensemble/runtime/` (including its `bin/me-run`) survives
    unless `--purge-state` is set (it is the recovery anchor for the
    next user-setup).

    Leaves every adopted project's `.metaensemble/` in place. Use
    `metaensemble unadopt` per project to clear those.
    """
    from metaensemble.lib.installer import uninstall

    report = uninstall(
        restore=True,
        purge_user_state_flag=args.purge_state,
        scope="user",
        dry_run=args.dry_run,
    )
    verb = "Would reverse" if args.dry_run else "Reversed"
    print(f"{verb} {len(report.applied)} action(s) at user scope.")
    _print_action_report(
        title="User teardown action status",
        applied=report.applied,
        noop=report.noop,
        errors=report.errors,
    )
    if report.errors:
        print(f"\n{len(report.errors)} error(s):", file=sys.stderr)
        for action, msg in report.errors:
            print(f"  - {action.description}: {msg}", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run diagnostic checks; exit non-zero if any check fails."""
    from metaensemble.lib.doctor import render_report, run_doctor
    report = run_doctor(fix=args.fix)
    print(render_report(report))
    return 1 if report.has_failures else 0


def cmd_projects(args: argparse.Namespace) -> int:
    """List every Claude Code project on this machine + MetaEnsemble install status.

    Useful when you have many projects and want to know which ones already
    have MetaEnsemble configured. Run `metaensemble setup` for the wizard,
    or `cd <path>` and run `metaensemble adopt` to register a specific
    project.
    """
    from metaensemble.lib.installer import (
        discover_projects, prune_missing_projects, render_projects,
    )
    if getattr(args, "prune", False):
        removed = prune_missing_projects()
        print(f"Pruned {len(removed)} stale project registration(s).")
        for path in removed:
            print(f"  - {path}")
    projects = discover_projects()
    print(render_projects(projects))
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run an evaluation cycle in one of three tiers (replay/smoke/full).

    The harness lives under `evals/`; this CLI command is the thin
    wrapper that ties tier selection to the runner dispatch and writes
    the report to the caller's `evals/reports/<UTC-date>-<tier>.md`.
    The smoke tier runs a live, side-effect-free classification call by default.
    The full tier requires `--allow-live` before it spends money.
    """
    from datetime import datetime, timezone
    from pathlib import Path
    import yaml
    from evals.runners.api import (
        CellSpec,
        TaskSpec,
        Tier,
        assemble_report,
        evaluate_release_gates,
        render_report,
        run_cell_replay,
        run_suite_b_live_claude,
    )

    package_root = CORE_DIR.parent
    evals_root = package_root / "evals"
    config_path = Path(args.config) if args.config else evals_root / "configs" / "default.yaml"
    config = yaml.safe_load(config_path.read_text())

    tier = Tier(args.tier)
    suite_a = yaml.safe_load((evals_root / "datasets" / "suite_a" / "tasks.yaml").read_text())
    suite_b = yaml.safe_load((evals_root / "datasets" / "suite_b" / "items.yaml").read_text())
    suite_a_tasks = [
        TaskSpec(id=t["id"], suite="suite_a",
                 description=t["description"], acceptance=t.get("acceptance", []))
        for t in suite_a["tasks"]
    ]
    suite_b_tasks = [
        TaskSpec(id=it["id"], suite="suite_b",
                 description=it["text"], acceptance=[],
                 acceptable_labels=it.get("acceptable_labels", []))
        for it in suite_b["items"]
    ]

    suite_sel = getattr(args, "suite", "all") or "all"
    if tier == Tier.SMOKE and suite_sel == "a":
        print(
            "--suite a is not available on the smoke tier; the smoke suite is "
            "the Suite-B classification set. Use --tier full for live Suite-A runs.",
            file=sys.stderr,
        )
        return 2

    seeds = args.seeds if args.seeds is not None else (
        1 if tier == Tier.SMOKE else int(config["cycle"]["seeds"])
    )
    budget_usd = (
        args.budget_usd
        if args.budget_usd is not None
        else float(config["cycle"]["budget_usd"])
    )
    if seeds < 1:
        print("--seeds must be >= 1", file=sys.stderr)
        return 2
    if budget_usd <= 0:
        print("--budget-usd must be > 0", file=sys.stderr)
        return 2

    if tier == Tier.FULL and not args.allow_live:
        print(
            "Refusing to run the full live tier: pass --allow-live to confirm.\n"
            "The full tier may spend real Claude budget; rerun with explicit "
            "cell/seed/budget values if you want a constrained release check.",
            file=sys.stderr,
        )
        return 2

    cells = [CellSpec(id=c["id"], kind=c["kind"], dispatch_fn=c["id"])
             for c in config["cells"]]
    cell_filter = args.cells
    if cell_filter is None and tier == Tier.SMOKE:
        cell_filter = "MM_full"
    if cell_filter and cell_filter != "all":
        wanted = {c.strip() for c in cell_filter.split(",") if c.strip()}
        cells = [c for c in cells if c.id in wanted]
        missing = wanted - {c.id for c in cells}
        if missing:
            print(f"Unknown eval cell(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2

    replay_tasks = []
    if suite_sel in ("a", "all"):
        replay_tasks.extend(suite_a_tasks)
    if suite_sel in ("b", "all"):
        replay_tasks.extend(suite_b_tasks)

    if tier == Tier.REPLAY:
        cassette_dir = evals_root / "cassettes"
        cells_with_outcomes = []
        for cell in cells:
            try:
                outcomes = run_cell_replay(
                    cell,
                    replay_tasks,
                    cassette_dir,
                    seeds=seeds,
                )
            except FileNotFoundError as exc:
                print(f"replay tier missing cassette: {exc}", file=sys.stderr)
                return 1
            cells_with_outcomes.append((cell, outcomes))
        b4_tokens = next(
            (
                sum(o.tokens_in + o.tokens_out for o in outcomes)
                for cell, outcomes in cells_with_outcomes
                if cell.id == "B4_best_prompt"
            ),
            None,
        )
        baseline_lookup = (
            {
                cell.id: b4_tokens
                for cell, _outcomes in cells_with_outcomes
                if cell.id != "B4_best_prompt" and cell.kind != "baseline"
            }
            if b4_tokens
            else None
        )
        report = assemble_report(
            tier=tier,
            cells_with_outcomes=cells_with_outcomes,
            baseline_total_tokens_lookup=baseline_lookup,
        )
        if (cassette_dir / "bootstrap.jsonl").exists():
            report.notes.append(
                "The bootstrap cassettes verify replay mechanics only; "
                "they are not empirical benchmark evidence."
            )
    else:
        # Smoke and full tiers — live Claude Code calls. Smoke defaults to the
        # MM_full cell and the smoke suite so it exercises MetaEnsemble's dispatch path
        # without mutating the project. Full is allowed only with explicit
        # confirmation; Suite A tasks with deferred fixture SHAs are skipped and
        # named in the report rather than fabricated.
        # Suite A runs live only on the full tier, only for tasks whose
        # starting_sha is resolved (deferred rows stay skipped-and-named).
        suite_a_live_tasks = []
        run_suite_a = tier == Tier.FULL and suite_sel in ("a", "all")
        if run_suite_a:
            from evals.runners.suite_a import SuiteATask, run_suite_a_live
            suite_a_live_tasks = [
                SuiteATask(
                    id=t["id"],
                    description=t["description"],
                    acceptance=t.get("acceptance", []),
                    starting_repo=t["starting_repo"],
                    starting_sha=str(t.get("starting_sha", "")),
                    title=t.get("title", ""),
                )
                for t in suite_a["tasks"]
                if not str(t.get("starting_sha", "")).startswith("__DEFERRED__")
            ]
        suite_a_workdir = Path.cwd() / "evals" / "workdir"
        executor_model = str(
            (config["cycle"].get("model_routing") or {}).get("executor")
            or "sonnet"
        )

        print(f"=== Eval pre-flight ({tier.value}) ===")
        print(f"Cells   : {len(cells)}")
        print(f"Suite A : {len(suite_a_live_tasks)} tasks (live)")
        print(f"Suite B : {len(suite_b_tasks) if suite_sel in ('b', 'all') else 0} items (smoke set)")
        print(f"Seeds   : {seeds}")
        print(f"Budget  : USD {budget_usd:.2f} per run")
        print()
        cells_with_outcomes = []
        for cell in cells:
            outcomes = []
            if suite_sel in ("b", "all"):
                outcomes.extend(run_suite_b_live_claude(
                    cell,
                    suite_b_tasks,
                    seeds=seeds,
                    budget_usd=budget_usd,
                    cwd=Path.cwd(),
                ))
            if suite_a_live_tasks:
                outcomes.extend(run_suite_a_live(
                    cell,
                    suite_a_live_tasks,
                    seeds=seeds,
                    budget_usd=budget_usd,
                    workdir=suite_a_workdir,
                    repo_root=package_root,
                    model=executor_model,
                ))
            cells_with_outcomes.append((cell, outcomes))
        b4_tokens = next(
            (
                sum(o.tokens_in + o.tokens_out for o in outcomes)
                for cell, outcomes in cells_with_outcomes
                if cell.id == "B4_best_prompt"
            ),
            None,
        )
        baseline_lookup = (
            {
                cell.id: b4_tokens
                for cell, _outcomes in cells_with_outcomes
                if cell.id != "B4_best_prompt" and cell.kind != "baseline"
            }
            if b4_tokens
            else None
        )
        report = assemble_report(
            tier=tier,
            cells_with_outcomes=cells_with_outcomes,
            baseline_total_tokens_lookup=baseline_lookup,
        )
        failure_counts: dict[str, int] = {}
        for cell, outcomes in cells_with_outcomes:
            for outcome in outcomes:
                if outcome.passed:
                    continue
                reason = outcome.failure_reason or "unclassified failure"
                key = f"{cell.id}: {reason}"
                failure_counts[key] = failure_counts.get(key, 0) + 1
        report.notes.append(
            "Live smoke run: labels were scored against the shipped smoke set. "
            "This side-effect-free run proves the live eval path and reports "
            "pass@budget; dispatch/install behavior is tested separately, and "
            "the smoke set is not an independently labeled calibration set."
        )
        if failure_counts:
            rendered = "; ".join(
                f"{reason} ({count})"
                for reason, count in sorted(failure_counts.items())[:5]
            )
            report.notes.append(f"Failure reasons: {rendered}.")
        if tier == Tier.FULL:
            deferred_suite_a = [
                t["id"] for t in suite_a["tasks"]
                if str(t.get("starting_sha", "")).startswith("__DEFERRED__")
            ]
            if deferred_suite_a:
                report.notes.append(
                    "Suite A live tasks skipped because their fixture SHAs are "
                    f"deferred: {', '.join(deferred_suite_a)}."
                )
            if suite_a_live_tasks:
                report.notes.append(
                    f"Suite A live runs: {len(suite_a_live_tasks)} tasks × "
                    f"{seeds} seeds per cell; workspaces kept under "
                    f"{suite_a_workdir} (see run-manifest.jsonl)."
                )
            elif suite_sel == "b":
                report.notes.append("Suite A excluded by --suite b.")

    gate_failed = False
    reporting = config.get("reporting") or {}
    if tier == Tier.FULL:
        gate_failed, gate_notes = evaluate_release_gates(
            report,
            failed_run_waste_threshold=reporting.get("failed_run_waste_threshold"),
            overhead_ratio_ceiling=reporting.get("overhead_ratio_ceiling"),
        )
        report.notes.extend(gate_notes)

    report_path = Path.cwd() / "evals" / "reports" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{tier.value}.md"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(report))
    print(f"Eval report written to {report_path}")
    if gate_failed:
        return 1
    return 0


def cmd_export_agents(args: argparse.Namespace) -> int:
    """Reverse-convert MetaEnsemble Roles back into Claude Code agent files.

    Recovery escape hatch. Useful when:
      - A user wants to leave MetaEnsemble but keep the agents they had before.
      - Project adoption backups are unavailable, so `unadopt` cannot
        replay converted-agent restores.
      - A user wants to copy the converted Roles to a *different* machine
        where they will run as plain Claude Code agents.
    """
    from metaensemble.lib.installer import export_agents

    target_dir = Path(args.target_dir).resolve() if args.target_dir else None
    written = export_agents(
        target_dir=target_dir,
        include_user=not args.project_only,
        include_project=not args.user_only,
        overwrite=args.overwrite,
    )
    if not written:
        print("No Role files reverse-converted (target may already have these files; pass --overwrite to replace).")
        return 0
    print(f"Reverse-converted {len(written)} Role file(s) into Claude Code agents:")
    for p in written:
        print(f"  {p}")
    return 0


def main(argv: list[str] | None = None) -> int:
    from metaensemble import __version__

    parser = argparse.ArgumentParser(
        prog="metaensemble",
        description=EXPERIMENTAL_NOTICE,
    )
    parser.add_argument("--version", action="version", version=f"metaensemble {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize MetaEnsemble in the current project")
    p_init.add_argument("--pack", choices=["ml", "web", "data"], help="Optional starter pack (future release)")
    p_init.add_argument("--force", action="store_true", help="Reinitialize an existing .metaensemble/")
    p_init.set_defaults(func=cmd_init)

    p_limits = sub.add_parser("limits", help="Current 5-hour token-limit status")
    p_limits.set_defaults(func=cmd_limits)

    p_standup = sub.add_parser("standup", help="Daily standup digest")
    p_standup.set_defaults(func=cmd_standup)

    p_executors = sub.add_parser("executors", help="List active Executors")
    p_executors.set_defaults(func=cmd_executors)

    p_perf = sub.add_parser("perf", help="Rolling performance metrics")
    p_perf.set_defaults(func=cmd_perf)

    p_stats = sub.add_parser("stats", help="One-screen Ledger growth and run-mix summary")
    p_stats.set_defaults(func=cmd_stats)

    p_ledger = sub.add_parser("ledger", help="Query the Ledger (see `ledger --help`)")
    p_ledger.add_argument("subargs", nargs=argparse.REMAINDER)
    p_ledger.set_defaults(func=cmd_ledger)

    p_hook = sub.add_parser(
        "hook",
        help="Invoke a lifecycle hook script by filename (runtime integration; "
             "installed in settings.json by `metaensemble user-setup`)",
    )
    p_hook.add_argument(
        "name",
        help="Hook script filename, e.g. `pre_task.py`",
    )
    p_hook.set_defaults(func=cmd_hook)

    p_statusline = sub.add_parser(
        "statusline",
        help="Invoke the MetaEnsemble statusline script (runtime integration)",
    )
    p_statusline.set_defaults(func=cmd_statusline)

    p_setup = sub.add_parser(
        "setup",
        help="Interactive wizard: pick a project to adopt, then run "
             "user-setup (if needed) and adopt.",
    )
    p_setup.add_argument(
        "--layout", choices=LAYOUT_CHOICES, default=None,
        help="Skip the layout prompt when user-setup hasn't run yet. "
             "Has no effect if user-setup is already installed. "
             "Choices control only slash-command placement.",
    )
    p_setup.set_defaults(func=cmd_setup)

    p_manifest = sub.add_parser(
        "manifest",
        help="Manifest authoring helpers (subcommands: validate, new-id, scaffold).",
    )
    p_manifest_sub = p_manifest.add_subparsers(dest="subcmd", required=True)
    p_manifest_validate = p_manifest_sub.add_parser(
        "validate",
        help="Load + schema-validate a Manifest YAML file.",
    )
    p_manifest_validate.add_argument("path", help="Path to the Manifest YAML file.")
    p_manifest_sub.add_parser(
        "new-id",
        help="Print a fresh `hm-<UUIDv7>` Manifest id to stdout.",
    )
    p_manifest_scaffold = p_manifest_sub.add_parser(
        "scaffold",
        help="Print a starter Manifest YAML to stdout (or write to `-o <path>`).",
    )
    p_manifest_scaffold.add_argument(
        "task", help="kebab-case task identifier for the new Manifest."
    )
    p_manifest_scaffold.add_argument(
        "-o", "--output", default=None,
        help="Write the scaffold to this path instead of stdout.",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="Reconcile stranded pending-Run sidecars into the Ledger",
    )
    p_reconcile.add_argument(
        "--older-than-minutes", type=int, default=0,
        help="Only reconcile sidecars older than N minutes (default: 0 = all).",
    )
    p_reconcile.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be reconciled without writing to the Ledger.",
    )
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_relaunch = sub.add_parser(
        "relaunch",
        help="Print the relaunch context for an Executor alias (does not dispatch)",
    )
    p_relaunch.add_argument("alias", help="Executor alias, e.g. `arch-7b3`")
    p_relaunch.add_argument(
        "--full", action="store_true",
        help="Load the entire prior Deliverable and every prior Brief",
    )
    p_relaunch.set_defaults(func=cmd_relaunch)

    p_inspect = sub.add_parser(
        "inspect",
        help="Read-only inventory of existing Claude Code setup; no changes made",
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_user_setup = sub.add_parser(
        "user-setup",
        help="Install MetaEnsemble's user-level runtime integration "
             "(once per machine; sets the layout for every adopted project)",
    )
    p_user_setup.add_argument(
        "--layout", choices=LAYOUT_CHOICES, default="namespaced",
        help=(
            "namespaced: slash commands install under /metaensemble:* and "
            "output styles are prefixed (metaensemble-wire, metaensemble-deliverable). "
            "top-level: slash commands install directly, e.g. /dispatch, "
            "and output styles are unprefixed; collisions with user-authored "
            "files are refused. The choice applies to all adopted projects; "
            "re-run with a different layout to switch."
        ),
    )
    p_user_setup.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned user-level actions without applying any",
    )
    p_user_setup.set_defaults(func=cmd_user_setup)

    p_adopt = sub.add_parser(
        "adopt",
        help="Register a project as a MetaEnsemble consumer "
             "(requires user-setup to have run first)",
    )
    p_adopt.add_argument(
        "path", nargs="?", default=None,
        help="Path to the project to adopt (default: current directory).",
    )
    p_adopt.add_argument(
        "--dry-run", action="store_true",
        help="Print the planned project-scope actions without applying any",
    )
    p_adopt.set_defaults(func=cmd_adopt)

    p_unadopt = sub.add_parser(
        "unadopt",
        help="Reverse a project's MetaEnsemble adoption "
             "(leaves user-level integration intact)",
    )
    p_unadopt.add_argument(
        "path", nargs="?", default=None,
        help="Path to the project to unadopt (default: current directory).",
    )
    p_unadopt.add_argument(
        "--purge-state", action="store_true",
        help="Also delete <project>/.metaensemble/ entirely",
    )
    p_unadopt.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be reversed without making any changes.",
    )
    p_unadopt.set_defaults(func=cmd_unadopt)

    p_user_teardown = sub.add_parser(
        "user-teardown",
        help="Reverse user-setup (removes commands, hooks, statusline, skill, "
             "output styles from ~/.claude/)",
    )
    p_user_teardown.add_argument(
        "--purge-state", action="store_true",
        help="Also delete ~/.metaensemble/ entirely (vendored runtime under runtime/ and runtime-versions/, roles, installs, rate-limit cache)",
    )
    p_user_teardown.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be reversed without making any changes.",
    )
    p_user_teardown.set_defaults(func=cmd_user_teardown)


    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose the install and report problems",
    )
    p_doctor.add_argument(
        "--fix", action="store_true",
        help="Apply safe remediations for fixable check failures. Legacy C1/C6 checks are inert and not affected.",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_projects = sub.add_parser(
        "projects",
        help="List every Claude Code project on this machine + MetaEnsemble install status",
    )
    p_projects.add_argument(
        "--prune", action="store_true",
        help="Remove Claude Code project registrations whose cwd no longer exists before listing.",
    )
    p_projects.set_defaults(func=cmd_projects)

    p_eval = sub.add_parser(
        "eval",
        help="Run an evaluation cycle (replay / smoke / full)",
    )
    p_eval.add_argument(
        "--tier", choices=["replay", "smoke", "full"], default="replay",
        help="Evaluation tier. PR replay is the default; full requires --allow-live.",
    )
    p_eval.add_argument(
        "--config", type=str, default=None,
        help="Path to a config YAML; default `evals/configs/default.yaml`.",
    )
    p_eval.add_argument(
        "--cells", type=str, default=None,
        help="Comma-separated cell ids or `all`. Defaults: replay/full all, smoke MM_full.",
    )
    p_eval.add_argument(
        "--suite", choices=["a", "b", "all"], default="all",
        help="Task suite selection. `a` = software-engineering tasks (live on "
             "the full tier only), `b` = classification smoke set, `all` = both.",
    )
    p_eval.add_argument(
        "--seeds", type=int, default=None,
        help="Override seed count. Defaults: replay/full config value, smoke 1.",
    )
    p_eval.add_argument(
        "--budget-usd", type=float, default=None,
        help="Override per-run live budget for smoke/full preflight.",
    )
    p_eval.add_argument(
        "--allow-live", action="store_true",
        help="Required to run the full live tier; otherwise full prints pre-flight only.",
    )
    p_eval.set_defaults(func=cmd_eval)

    p_export = sub.add_parser(
        "export-agents",
        help="Reverse-convert MetaEnsemble Roles into Claude Code agent files",
    )
    p_export.add_argument(
        "--target-dir", type=str, default=None,
        help="Directory to write the agents into (default: ~/.claude/agents/).",
    )
    p_export.add_argument(
        "--user-only", action="store_true",
        help="Only export user-layer Roles from ~/.metaensemble/roles/.",
    )
    p_export.add_argument(
        "--project-only", action="store_true",
        help="Only export project-layer Roles from <project>/.metaensemble/roles/.",
    )
    p_export.add_argument(
        "--overwrite", action="store_true",
        help="Replace existing files at the target path. Default: skip existing.",
    )
    p_export.set_defaults(func=cmd_export_agents)

    args = parser.parse_args(argv)
    # One-shot migration of legacy `parallel`/`incorporate` vocabulary in
    # on-disk state. Idempotent + process-cached, so commands that don't
    # touch state pay only a registry-walk once per session. See addendum
    # Addition 1 for the policy and the rewrite scope.
    if args.cmd not in ("hook",):
        try:
            from metaensemble.lib.installer import migrate_vocabulary_state
            migrate_vocabulary_state()
        except Exception:  # nosec B110 — migration must not break the CLI
            pass
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
