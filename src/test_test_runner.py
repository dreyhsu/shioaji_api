from pathlib import Path
import subprocess
from unittest.mock import Mock
import os
import pytest

import test_runner as runner


def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "tools").mkdir()
    for name, source in {
        "signal.py": "def signal(): return True",
        "service.py": "import signal",
        "test_signal.py": "import signal",
        "test_service.py": "from service import value",
        "unrelated.py": "pass",
        "test_unrelated.py": "import unrelated",
    }.items():
        (tmp_path / "src" / name).write_text(source)
    return tmp_path


def test_selects_transitive_dependents_and_test_itself(tmp_path):
    root = project(tmp_path)
    assert runner.related_tests([Path("src/signal.py")], root) == [Path("src/test_service.py"), Path("src/test_signal.py")]
    assert runner.related_tests([Path("src/test_unrelated.py")], root) == [Path("src/test_unrelated.py")]


def test_shared_deleted_unknown_or_invalid_changes_run_everything(tmp_path):
    root = project(tmp_path)
    for path in ("conftest.py", "pytest.ini", "config/trading.json", "src/web/trading.js", "src/deleted.py"):
        assert runner.related_tests([Path(path)], root) is None
    (root / "src/signal.py").write_text("def bad(:")
    assert runner.related_tests([Path("src/signal.py")], root) is None


def test_mixed_changes_with_an_unmapped_module_run_everything(tmp_path):
    root = project(tmp_path)
    (root / "src/new_module.py").write_text("pass")
    assert runner.related_tests([Path("src/signal.py"), Path("src/new_module.py")], root) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hook; native Python test runner is covered separately")
def test_precommit_hook_uses_tss_and_propagates_test_failure(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "git").write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_PROJECT_ROOT"\n')
    (commands / "conda").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$MOCK_CONDA_ARGS"\nexit 7\n')
    for path in commands.iterdir():
        path.chmod(0o755)
    log = tmp_path / "args.txt"
    env = dict(os.environ, PATH=str(commands), TEST_PROJECT_ROOT=str(runner.ROOT), MOCK_CONDA_ARGS=str(log))
    result = subprocess.run(["/bin/sh", str(runner.ROOT / ".githooks/pre-commit")], env=env, capture_output=True, text=True)
    assert result.returncode == 7
    assert log.read_text().splitlines() == ["run", "--no-capture-output", "-n", "tss", "python", "tools/test_runner.py", "--all"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell hook; native Python test runner is covered separately")
def test_precommit_hook_reports_missing_conda(tmp_path):
    result = subprocess.run(["/bin/sh", str(runner.ROOT / ".githooks/pre-commit")],
                            env=dict(os.environ, PATH=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 1 and "conda tss" in result.stderr


def test_snapshot_ignores_generated_files_and_detects_deletion(tmp_path):
    root = project(tmp_path)
    before = runner.file_snapshot(root)
    (root / "data").mkdir()
    (root / "data/generated.json").write_text("{}")
    (root / "src/signal.py").unlink()
    after = runner.file_snapshot(root)
    assert runner.changed_files(before, after) == {Path("src/signal.py")}


def test_runner_propagates_failure_and_uses_current_environment(monkeypatch, tmp_path):
    process = Mock(return_value=subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(runner.subprocess, "run", process)
    assert runner.run_tests([Path("src/test_signal.py")], tmp_path) == 1
    assert process.call_args.args[0] == [runner.sys.executable, "-m", "pytest", "-q", str(Path("src/test_signal.py"))]


def test_watch_merges_saves_and_rechecks_changes_during_tests(monkeypatch):
    a, b = Path("src/signal.py"), Path("src/service.py")
    snapshots = iter([{a: (1, 1)}, {a: (2, 1)}, {a: (3, 1)}, {a: (3, 1)}, {a: (3, 1), b: (1, 1)}, {a: (3, 1), b: (1, 1)}])
    monkeypatch.setattr(runner, "file_snapshot", lambda root: next(snapshots))
    monkeypatch.setattr(runner, "related_tests", lambda changes, root: sorted(changes))
    sleeps = iter([None] * 5 + [KeyboardInterrupt()])
    def sleep(seconds):
        value = next(sleeps)
        if value is not None:
            raise value
    monkeypatch.setattr(runner.time, "sleep", sleep)
    run = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_tests", run)
    assert runner.watch() == 0
    assert run.call_count == 3
    assert run.call_args_list[1].args[0] == [a]
    assert run.call_args_list[2].args[0] == [b]
