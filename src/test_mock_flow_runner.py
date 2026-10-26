import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import test_mock_flows as runner


def junit_report(path, scenarios, *, problem=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    for scenario in scenarios:
        case = ET.SubElement(suite, "testcase", name=scenario.test_name, time="0.1")
        properties = ET.SubElement(case, "properties")
        ET.SubElement(properties, "property", name="result", value=json.dumps({"entry_price": 25000}))
        if problem:
            ET.SubElement(case, problem, message="scenario did not pass")
    ET.ElementTree(root).write(path, encoding="utf-8")


@pytest.mark.parametrize("problem", ["failure", "error", "skipped"])
def test_nonpassing_cases_never_report_success(tmp_path, problem):
    path = tmp_path / "results.xml"
    junit_report(path, runner.SCENARIOS, problem=problem)
    results = runner.read_results(path, list(runner.SCENARIOS))
    assert all(result["status"] == "failed" for result in results)


def test_missing_test_is_not_counted_as_a_pass(tmp_path):
    path = tmp_path / "results.xml"
    junit_report(path, runner.SCENARIOS[:1])
    results = runner.read_results(path, list(runner.SCENARIOS))
    assert [result["status"] for result in results] == ["passed"] + ["missing"] * (len(runner.SCENARIOS) - 1)


@pytest.mark.parametrize("case_key", ["curve-entry", "curve-entry-stop"])
def test_single_case_uses_current_environment_and_writes_report(tmp_path, monkeypatch, case_key):
    commands = []
    selected = next(scenario for scenario in runner.SCENARIOS if scenario.key == case_key)

    def run(command, **kwargs):
        commands.append((command, kwargs))
        path = Path(next(argument.split("=", 1)[1] for argument in command if argument.startswith("--junitxml=")))
        junit_report(path, [selected])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)
    report_path = tmp_path / "reports" / "result.json"
    assert runner.main(["--case", case_key, "--report", str(report_path)]) == 0
    command, kwargs = commands[0]
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert [argument for argument in command if "::" in argument] == [selected.node_id]
    assert kwargs["cwd"] == runner.ROOT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] and report["passed"] == report["total"] == 1
    assert report["results"][0]["details"]["entry_price"] == 25000


@pytest.mark.parametrize("exit_code,problem", [(1, "failure"), (0, "skipped"), (2, None)])
def test_runner_returns_failure_for_failed_skipped_or_interrupted_runs(tmp_path, monkeypatch, exit_code, problem):
    def run(command, **kwargs):
        path = Path(next(argument.split("=", 1)[1] for argument in command if argument.startswith("--junitxml=")))
        junit_report(path, runner.SCENARIOS, problem=problem)
        return subprocess.CompletedProcess(command, exit_code, stdout="pytest diagnostics", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run)
    report_path = tmp_path / "failure.json"
    assert runner.main(["--report", str(report_path)]) == 1
    assert not json.loads(report_path.read_text(encoding="utf-8"))["success"]


def test_collection_failure_without_xml_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 2, stdout="collection failed", stderr=""))
    report_path = tmp_path / "missing.json"
    assert runner.main(["--report", str(report_path)]) == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert not report["success"] and report["passed"] == 0
    assert all(result["status"] == "missing" for result in report["results"])
