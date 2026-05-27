# MetaEnsemble Security Posture

MetaEnsemble runs entirely on the Principal's machine — there is no
hosted service, no shared backend, no third-party data store. The
security surface is therefore the local one: hooks that read and mutate
files under the Principal's `~/.metaensemble/` and `<project>/.metaensemble/`
trees, and integration with the agent runtime's settings.json.

This document names the trust boundaries, the assets each hook can touch,
and the engineering invariants the test suite enforces against the hook
source code.

## Trust boundaries

| Boundary | Trusted | Not Trusted |
|---|---|---|
| MetaEnsemble core (`metaensemble/`) | Yes, code review + tests. | — |
| The Principal's `~/.metaensemble/` (Roles, vendored runtime at `runtime/` plus history under `runtime-versions/`, budgets, rate-limit cache). | Yes. The Principal owns this directory by construction. | — |
| `<project>/.metaensemble/` of a project the Principal authored | Yes. The Principal opted in via `metaensemble setup` or `metaensemble adopt`. | — |
| `<project>/.metaensemble/` of an *arbitrary* adopted repo | Partial. Inspect/install steps prompt; the Principal approves; `install-decisions.yaml` is editable before apply. | YAML/Markdown content is parsed defensively; no arbitrary code from the project's `.metaensemble/` is executed. |
| `install-decisions.yaml` | Trusted only if the Principal wrote/reviewed it. | A repo that ships a pre-populated `install-decisions.yaml` is not honored — the installer regenerates it from the inspection unless the Principal explicitly opts in. |
| The agent runtime's `settings.json` | Yes, the Principal owns it. The installer reads and writes it via JSON parse + targeted merge; user keys are preserved verbatim. | — |
| Subagent outputs (tool_response, transcript JSONL) | Partial. The Ledger records what was returned; the quality gate runs locally; nothing is automatically executed from the response. | Returned shell snippets and code blocks are inert until the Principal runs them. |

## Asset inventory per hook

The lifecycle hooks under `metaensemble/hooks/` each touch a small, named set
of paths. The audit test
(`metaensemble/tests/test_hook_security_invariants.py`) keeps this table true.

| Hook | Reads | Writes | Executes |
|---|---|---|---|
| `session_start.py` | Ledger DB, `pending/` sidecars, runtime rate-limits feed | Ledger DB (reconcile rows), deletes sidecars older than 1h | None |
| `pre_task.py` | Ledger DB, Manifest YAML at the path the prompt names, budgets.yaml | Pending-Run sidecar, notify/blocks sentinels under `.metaensemble/state/` | None |
| `post_task.py` | Ledger DB, pending sidecar, manifest YAML, runtime transcript JSONL (if `transcript_path` provided), changed files for quality runners | Run row, deliverables_index.jsonl, deletes the sidecar | Quality runners (bandit, radon, ruff, coverage) on locally-changed files |
| `file_event.py` | Active-dispatch sidecar, file-tool payload paths, current transcript tail for `/dispatch` detection | File-tool event JSONL under `.metaensemble/state/file-events/` | None |
| `deliverable_sync.py` | Output of the runtime's Write tool | `deliverables_index.jsonl` | None |
| `session_summary.py` | Ledger DB, runtime rate-limits feed | Ledger DB (Layer-1 reconcile rows) | None |

## Engineering invariants (enforced by tests)

The hook-security audit test
(`metaensemble/tests/test_hook_security_invariants.py`) scans every file under
`metaensemble/hooks/` and fails on regression of any of these patterns:

| Invariant | Why it matters |
|---|---|
| `subprocess.run(..., shell=True)` is never used. | A True shell makes hook input susceptible to shell injection. We pass argv lists instead. |
| `yaml.load(...)` is never used; `yaml.safe_load` only. | `yaml.load` instantiates arbitrary Python objects; `safe_load` is restricted to scalars and containers. |
| `os.system(...)` is never used. | Same reason as `shell=True`: a True shell expands metacharacters. |
| `eval(...)` and `exec(...)` are not present. | Either turns text into code. |
| Every JSON parse is wrapped in `try/except` that handles `json.JSONDecodeError`. | A malformed transcript line must not crash a hook. |
| Every file write targets a path inside `~/.metaensemble/`, `<project>/.metaensemble/`, or `<home>/.claude/`. | Hooks must not write outside their own state and the Principal-configured runtime directory. |

Run the audit:

```bash
pytest metaensemble/tests/test_hook_security_invariants.py -v
```

The audit is part of the default `pytest` run, so any regression is
caught on PR.

## Failure modes acknowledged out of scope

- **Malicious agent runtime.** MetaEnsemble trusts the runtime that
  invokes its hooks. A compromised runtime can do anything the Principal
  could do; MetaEnsemble does not defend against that boundary.
- **Malicious Principal.** The Principal owns the local runtime. MetaEnsemble
  records what they do; it does not prevent them from doing it.
- **Filesystem races.** Hooks are short-lived and idempotent; the
  pending-sidecar protocol assumes one runtime instance at a time per
  project. Multi-runtime concurrency on the same project is not a
  supported configuration in v0.1.0.

## v0.2.0 roadmap items

- **Signed install plans.** A `signature.yaml` shipped under
  `<project>/.metaensemble/` would let the installer verify that an
  adopted project's pre-populated decisions came from a trusted
  source before applying them. Until that ships, the Principal's
  edit-then-apply step is the trust gate.
- **Per-Executor permission scopes.** Today every Executor inherits the
  Principal's runtime permissions. A future protocol would let the
  Manifest declare a narrower tool-set per Executor and have the
  PreToolUse hook enforce it. This requires runtime-side cooperation
  that does not yet exist in Claude Code.
- **Audit log signing.** The Ledger is currently a plain SQLite + JSONL
  pair. Append-only signed logs are a future hardening step; the
  application-side invariant ("never `UPDATE` a Run row") is already
  enforced, but a malicious local actor can still rewrite the file.

## Reporting

Security issues found in MetaEnsemble core should be reported by opening
an issue on the project repository and tagging it `security`. There is no
public-facing service to disclose against; the Principal's machine is the
trust boundary, and patches land via the normal review process.
