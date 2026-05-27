#!/bin/sh
# Build/install the wheel, then verify the installed CLI from outside the repo.
#
# This catches the stale-wheel class where source-backed pytest passes but
# the console script still imports an older site-packages copy.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEFAULT_PYTHON="$REPO_ROOT/.venv/bin/python"
if [ -x "$DEFAULT_PYTHON" ]; then
  PYTHON="${PYTHON:-$DEFAULT_PYTHON}"
else
  PYTHON="${PYTHON:-python3}"
fi
CLI="$(dirname "$PYTHON")/metaensemble"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/metaensemble-installed-cli.XXXXXX")"
cleanup() {
  chmod -R u+rwX "$TMP_ROOT" 2>/dev/null || true
  rm -rf "$TMP_ROOT"
}
trap cleanup EXIT INT TERM

DIST="$TMP_ROOT/dist"
OUTSIDE="$TMP_ROOT/outside"
HOME_DIR="$TMP_ROOT/home"
UPGRADE_HOME="$TMP_ROOT/upgrade-home"
PROJECT="$TMP_ROOT/project"
DOCTOR_PROJECT="$TMP_ROOT/doctor-project"

mkdir -p "$DIST" "$OUTSIDE" "$HOME_DIR/.claude" "$PROJECT" "$DOCTOR_PROJECT"
printf '{"hooks":{}}\n' > "$HOME_DIR/.claude/settings.json"

echo "==> building wheel"
rm -rf "$REPO_ROOT/build"
"$PYTHON" -m build --wheel --outdir "$DIST" "$REPO_ROOT"
WHEEL="$(find "$DIST" -maxdepth 1 -name 'metaensemble-*.whl' -print | sort | tail -n 1)"
if [ -z "$WHEEL" ]; then
  echo "verify-installed-cli: no wheel produced in $DIST" >&2
  exit 1
fi

"$PYTHON" - "$WHEEL" <<'PY'
import sys
import zipfile

wheel = sys.argv[1]
with zipfile.ZipFile(wheel) as zf:
    names = set(zf.namelist())

retired = {
    "metaensemble/tools/window.py",
    "metaensemble/commands/window.md",
}
present = sorted(retired & names)
if present:
    raise SystemExit(
        "verify-installed-cli: retired window assets packaged in wheel: "
        + ", ".join(present)
    )
for expected in ("metaensemble/tools/limits.py", "metaensemble/commands/limits.md"):
    if expected not in names:
        raise SystemExit(f"verify-installed-cli: missing packaged asset {expected}")
PY

echo "==> installing wheel into $(dirname "$PYTHON")"
"$PYTHON" -m pip install --force-reinstall --no-deps "$WHEEL"

echo "==> checking installed module symbols from outside the repo"
(
  cd "$OUTSIDE"
  "$PYTHON" -c '
import inspect
import importlib.resources as resources
import importlib.util
import metaensemble
import metaensemble.cli as cli
import metaensemble.lib.doctor as doctor
import metaensemble.lib.installer as installer

assert "site-packages" in str(metaensemble.__file__), metaensemble.__file__
assert importlib.util.find_spec("metaensemble.tools.window") is None
commands_root = resources.files("metaensemble").joinpath("commands")
assert commands_root.joinpath("limits.md").is_file()
assert not commands_root.joinpath("window.md").is_file()
assert hasattr(installer, "remap_user_scope_backup_paths")
assert hasattr(cli, "_render_adopt_dry_run")
assert "mode=ro" in inspect.getsource(doctor.check_project_state)
print(metaensemble.__file__)
'
)

echo "==> smoke: user-setup dry-run reports user-scope backup root"
USER_SETUP_OUT="$(
  cd "$OUTSIDE"
  HOME="$HOME_DIR" "$CLI" user-setup --layout top-level --dry-run
)"
case "$USER_SETUP_OUT" in
  *".metaensemble/installs/"*) ;;
  *)
    echo "$USER_SETUP_OUT"
    echo "verify-installed-cli: user-setup dry-run did not report user backup root" >&2
    exit 1
    ;;
esac
case "$USER_SETUP_OUT" in
  *".metaensemble/backups/"*)
    echo "$USER_SETUP_OUT"
    echo "verify-installed-cli: user-setup dry-run leaked cwd-relative backup root" >&2
    exit 1
    ;;
esac

echo "==> installing into temp HOME for adopt smoke"
(
  cd "$OUTSIDE"
  HOME="$HOME_DIR" "$CLI" user-setup --layout top-level >/dev/null
)

echo "==> smoke: adopt dry-run is honest and side-effect-free"
ADOPT_OUT="$(
  cd "$OUTSIDE"
  HOME="$HOME_DIR" "$CLI" adopt "$PROJECT" --dry-run
)"
case "$ADOPT_OUT" in
  *"## Project setup actions"*"Actions: 0"*) ;;
  *)
    echo "$ADOPT_OUT"
    echo "verify-installed-cli: adopt dry-run did not render setup actions" >&2
    exit 1
    ;;
esac
if [ -e "$PROJECT/.metaensemble" ]; then
  echo "verify-installed-cli: adopt dry-run created $PROJECT/.metaensemble" >&2
  exit 1
fi

echo "==> smoke: doctor permission failure is WARN, not corruption"
mkdir -p "$DOCTOR_PROJECT/.metaensemble/state"
DB="$DOCTOR_PROJECT/.metaensemble/state/department.db"
"$PYTHON" -c '
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
for table in ("roles", "executors", "tasks", "runs"):
    conn.execute(f"CREATE TABLE {table} (id INTEGER)")
conn.commit()
conn.close()
' "$DB"
chmod 000 "$DB"
DOCTOR_OUT="$(
  cd "$DOCTOR_PROJECT"
  HOME="$HOME_DIR" "$CLI" doctor
)"
chmod 600 "$DB"
case "$DOCTOR_OUT" in
  *"### [WARN] C4"*"not database corruption"*) ;;
  *)
    echo "$DOCTOR_OUT"
    echo "verify-installed-cli: doctor did not classify unreadable DB as C4 WARN" >&2
    exit 1
    ;;
esac
case "$DOCTOR_OUT" in
  *"### [FAIL] C4"*)
    echo "$DOCTOR_OUT"
    echo "verify-installed-cli: doctor reported C4 FAIL for permission issue" >&2
    exit 1
    ;;
esac

echo "==> smoke: upgrade-from-prior-layout remediates stale managed symlinks"
# Stage a synthetic prior-state HOME: a dangling ~/.claude/commands/window.md
# symlink (the runtime file was renamed to limits.md in this release) plus a
# user-authored file at ~/.claude/commands/user-cmd.md that must NOT be touched.
mkdir -p "$UPGRADE_HOME/.claude/commands"
printf '{"hooks":{}}\n' > "$UPGRADE_HOME/.claude/settings.json"
# Note: the link target intentionally does not exist (runtime not vendored yet)
ln -s "$UPGRADE_HOME/.metaensemble/runtime/commands/window.md" "$UPGRADE_HOME/.claude/commands/window.md"
echo "user-content" > "$UPGRADE_HOME/.claude/commands/user-cmd.md"

UPGRADE_OUT="$(
  cd "$OUTSIDE"
  HOME="$UPGRADE_HOME" "$CLI" user-setup --layout top-level
)"
case "$UPGRADE_OUT" in
  *"Cleaned up 1 stale managed symlink"*"window.md"*) ;;
  *)
    echo "$UPGRADE_OUT"
    echo "verify-installed-cli: user-setup did not announce stale-symlink cleanup" >&2
    exit 1
    ;;
esac
if [ -e "$UPGRADE_HOME/.claude/commands/window.md" ] || \
   [ -L "$UPGRADE_HOME/.claude/commands/window.md" ]; then
  echo "verify-installed-cli: stale window.md symlink survived user-setup" >&2
  exit 1
fi
if [ ! -L "$UPGRADE_HOME/.claude/commands/limits.md" ]; then
  echo "verify-installed-cli: limits.md was not installed by user-setup" >&2
  exit 1
fi
# User-authored file must be untouched (content + presence)
USER_CONTENT="$(cat "$UPGRADE_HOME/.claude/commands/user-cmd.md")"
if [ "$USER_CONTENT" != "user-content" ]; then
  echo "verify-installed-cli: user-authored user-cmd.md was modified" >&2
  exit 1
fi

echo "verify-installed-cli: ok"
