"""Tests for `metaensemble/lib/topology.py` and the C12 doctor check.

The editable-install detector matters because MetaEnsemble's documented
install promise is "install once, dev source tree dispensable." An
editable install (`pip install -e .`) silently breaks that promise: the
runner gets pinned to a Python whose `metaensemble` resolves back to the
dev tree, so deleting the source breaks every hook. C12 surfaces the
state on every doctor run; the same detector also drives the user-setup
warning so users see the implication before the runner is pinned.
"""
from __future__ import annotations

import json
import subprocess

from metaensemble.lib import topology
from metaensemble.lib.doctor import check_install_topology
from metaensemble.lib.topology import (
    InstallTopology,
    detect_editable_install,
    editable_install_notice,
    runner_python_path,
)


# --- detect_editable_install ----------------------------------------------

def _stub_run(stdout: str, returncode: int = 0):
    """Build a subprocess.run substitute that returns canned stdout."""
    def fake_run(*_args, **_kwargs):
        result = subprocess.CompletedProcess(
            args=_args[0] if _args else [], returncode=returncode,
            stdout=stdout, stderr="",
        )
        return result
    return fake_run


def test_detect_editable_install_returns_editable_true_for_editable_dist(monkeypatch):
    """PEP 610 direct_url.json with dir_info.editable=true → editable=True."""
    monkeypatch.setattr(subprocess, "run", _stub_run(json.dumps({
        "installed": True, "editable": True,
        "source": "/Users/jane/dev/metaensemble",
    })))
    result = detect_editable_install("/usr/bin/python3")
    assert result.installed is True
    assert result.editable is True
    assert result.source == "/Users/jane/dev/metaensemble"
    assert result.python_executable == "/usr/bin/python3"


def test_detect_editable_install_returns_editable_false_for_wheel_install(monkeypatch):
    """A wheel install has no direct_url.json or has editable=false."""
    monkeypatch.setattr(subprocess, "run", _stub_run(json.dumps({
        "installed": True, "editable": False, "source": None,
    })))
    result = detect_editable_install("/usr/bin/python3")
    assert result.installed is True
    assert result.editable is False
    assert result.source is None


def test_detect_editable_install_returns_not_installed_when_package_missing(monkeypatch):
    """Interpreter has no metaensemble at all → installed=False, never raises."""
    monkeypatch.setattr(subprocess, "run", _stub_run(json.dumps({
        "installed": False, "editable": False, "source": None,
    })))
    result = detect_editable_install("/usr/bin/python3")
    assert result.installed is False
    assert result.editable is False


def test_detect_editable_install_handles_interpreter_failure(monkeypatch):
    """A nonzero exit or OS error must return a safe sentinel, never raise."""
    def raising_run(*_args, **_kwargs):
        raise FileNotFoundError("no such interpreter")
    monkeypatch.setattr(subprocess, "run", raising_run)
    result = detect_editable_install("/nonexistent/python")
    assert result.installed is False
    assert result.editable is False


def test_detect_editable_install_handles_subprocess_timeout(monkeypatch):
    """A hung interpreter must time out gracefully and report no install."""
    def timing_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=10)
    monkeypatch.setattr(subprocess, "run", timing_out)
    result = detect_editable_install("/some/python")
    assert result.installed is False
    assert result.editable is False


def test_detect_editable_install_handles_garbage_stdout(monkeypatch):
    """If the probe somehow returns non-JSON, treat it as not installed."""
    monkeypatch.setattr(subprocess, "run", _stub_run("not json at all"))
    result = detect_editable_install("/usr/bin/python3")
    assert result.installed is False


def test_detect_editable_install_against_current_python_does_not_crash():
    """Smoke test against the running interpreter — must complete and return
    a meaningful InstallTopology regardless of the install shape."""
    result = detect_editable_install()
    assert isinstance(result, InstallTopology)
    # Either installed or not — both are valid outcomes here.


# --- runner_python_path ---------------------------------------------------

def test_runner_python_path_extracts_quoted_python(tmp_path):
    runner = tmp_path / "me-run"
    runner.write_text(
        "#!/bin/sh\n"
        "exec '/Users/jane/anaconda3/bin/python' -m metaensemble.cli \"$@\"\n"
    )
    assert runner_python_path(runner_path=runner) == "/Users/jane/anaconda3/bin/python"


def test_runner_python_path_extracts_unquoted_python(tmp_path):
    runner = tmp_path / "me-run"
    runner.write_text(
        "#!/bin/sh\n"
        "exec /usr/local/bin/python3.13 -m metaensemble.cli \"$@\"\n"
    )
    assert runner_python_path(runner_path=runner) == "/usr/local/bin/python3.13"


def test_runner_python_path_returns_none_when_missing(tmp_path):
    assert runner_python_path(runner_path=tmp_path / "missing") is None


def test_runner_python_path_returns_none_when_template_changes(tmp_path):
    """A runner that no longer matches the generated shape must fail loudly
    here rather than silently extracting the wrong path."""
    runner = tmp_path / "me-run"
    runner.write_text("#!/bin/sh\necho 'something else entirely'\n")
    assert runner_python_path(runner_path=runner) is None


def test_runner_python_path_uses_home_when_no_explicit_path(tmp_path, monkeypatch):
    """When called without arguments, the lookup must root at the given
    home and find ~/.metaensemble/runtime/bin/me-run."""
    runtime_bin = tmp_path / ".metaensemble" / "runtime" / "bin"
    runtime_bin.mkdir(parents=True)
    (runtime_bin / "me-run").write_text(
        "#!/bin/sh\nexec '/x/python' -m metaensemble.cli \"$@\"\n"
    )
    assert runner_python_path(home=tmp_path) == "/x/python"


# --- editable_install_notice ----------------------------------------------

def test_notice_names_source_and_recovery_command():
    info = InstallTopology(
        python_executable="/Users/jane/dev/.venv/bin/python",
        installed=True, editable=True,
        source="/Users/jane/dev/metaensemble",
    )
    text = editable_install_notice(info, "top-level")
    assert "/Users/jane/dev/metaensemble" in text
    assert "/Users/jane/dev/.venv/bin/python" in text
    assert "python -m build --wheel" in text
    assert "--layout=top-level" in text
    assert "Proceeding" in text  # not blocking


def test_notice_falls_back_when_source_unknown():
    info = InstallTopology(
        python_executable="/x/python",
        installed=True, editable=True, source=None,
    )
    text = editable_install_notice(info, "namespaced")
    assert "<source path unknown>" in text


# --- C12 doctor check -----------------------------------------------------

def test_c12_returns_ok_when_pinned_python_is_wheel_installed(monkeypatch):
    monkeypatch.setattr(
        "metaensemble.lib.topology.runner_python_path",
        lambda *a, **kw: "/conda/bin/python",
    )
    monkeypatch.setattr(
        "metaensemble.lib.topology.detect_editable_install",
        lambda *_a, **_kw: InstallTopology(
            "/conda/bin/python", installed=True, editable=False, source=None,
        ),
    )
    result = check_install_topology()
    assert result.check_id == "C12"
    assert result.status == "OK"
    assert "/conda/bin/python" in result.detail
    assert "non-editable" in result.detail.lower()


def test_c12_returns_warn_when_pinned_python_is_editable(monkeypatch):
    monkeypatch.setattr(
        "metaensemble.lib.topology.runner_python_path",
        lambda *a, **kw: "/dev/.venv/bin/python",
    )
    monkeypatch.setattr(
        "metaensemble.lib.topology.detect_editable_install",
        lambda *_a, **_kw: InstallTopology(
            "/dev/.venv/bin/python", installed=True, editable=True,
            source="/Users/jane/dev/metaensemble",
        ),
    )
    result = check_install_topology()
    assert result.status == "WARN"
    assert "editable" in result.detail.lower()
    assert "/Users/jane/dev/metaensemble" in result.detail
    assert "build --wheel" in result.remediation
    assert "user-setup" in result.remediation


def test_c12_returns_fail_when_pinned_python_has_no_install(monkeypatch):
    monkeypatch.setattr(
        "metaensemble.lib.topology.runner_python_path",
        lambda *a, **kw: "/conda/bin/python",
    )
    monkeypatch.setattr(
        "metaensemble.lib.topology.detect_editable_install",
        lambda *_a, **_kw: InstallTopology(
            "/conda/bin/python", installed=False, editable=False, source=None,
        ),
    )
    result = check_install_topology()
    assert result.status == "FAIL"
    assert "ModuleNotFoundError" in result.detail
    assert "pip install" in result.remediation


def test_c12_returns_warn_when_runner_missing(monkeypatch):
    monkeypatch.setattr(
        "metaensemble.lib.topology.runner_python_path",
        lambda *a, **kw: None,
    )
    result = check_install_topology()
    assert result.status == "WARN"
    assert "user-setup" in result.remediation


# --- end-to-end real-fs detector (slow but proves the probe works) --------

def test_detector_real_filesystem_round_trip(tmp_path):
    """Build a synthetic dist-info layout, run the probe in an isolated
    Python (no PYTHONPATH inheritance, no site-packages), and confirm
    the detector reads PEP 610 metadata correctly for both editable and
    wheel-installed shapes."""
    import sys as _sys

    site = tmp_path / "site"
    site.mkdir()
    dist_info = site / "metaensemble-0.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: metaensemble\nVersion: 0.0.0\n"
    )
    (dist_info / "RECORD").write_text("")
    (dist_info / "direct_url.json").write_text(json.dumps({
        "url": f"file://{tmp_path}/source",
        "dir_info": {"editable": True},
    }))

    # `-I` runs the interpreter in isolated mode: ignores PYTHONPATH,
    # skips user site-packages, and does not prepend the script directory
    # to sys.path. We then inject the synthetic site as sys.path[0], so
    # importlib.metadata cannot find any other `metaensemble` distribution
    # the test environment happens to have installed.
    isolated_cmd = (
        f"import sys\n"
        f"sys.path.insert(0, {str(site)!r})\n"
        f"{topology._PROBE}"
    )
    out = subprocess.run(
        [_sys.executable, "-I", "-c", isolated_cmd],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip())
    assert data["installed"] is True
    assert data["editable"] is True
    assert data["source"] == f"{tmp_path}/source"

    # Flip dir_info.editable=false to model a wheel install and re-run.
    (dist_info / "direct_url.json").write_text(json.dumps({
        "url": f"file://{tmp_path}/source",
        "dir_info": {"editable": False},
    }))
    out2 = subprocess.run(
        [_sys.executable, "-I", "-c", isolated_cmd],
        capture_output=True, text=True, timeout=10, check=False,
    )
    data2 = json.loads(out2.stdout.strip())
    assert data2["installed"] is True
    assert data2["editable"] is False

    # NB: the "not installed at all" case is covered by the mocked test
    # `test_detect_editable_install_returns_not_installed_when_package_missing`;
    # we cannot reliably exercise it here because `-I` still leaves the
    # interpreter's own site-packages on sys.path, which (in this test
    # environment) contains a real metaensemble distribution.
