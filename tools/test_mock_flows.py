"""Run offline Mock API workflows with a readable result summary.

Uses pytest's existing network/credential guards and temporary database fixtures.
No server is started; every run uses fixed scenarios independent of mock.json.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    test_name: str

    @property
    def node_id(self):
        return f"src/test_mock_runtime.py::{self.test_name}"


SCENARIOS = (
    Scenario("fixed-atr", "進場後固定 +2 ATR 停利", "test_fixed_exits_fill_and_flatten_position[Buy-target]"),
    Scenario("curve-exit", "反彈後回檔，跌破 Curve 曲線停利", "test_rebound_then_pullback_closes_long_at_a_profit_using_curve"),
    Scenario("curve-entry", "向上突破 Curve 曲線後進場一次", "test_3_curve_breakout_buys_once_and_creates_two_atr_levels"),
    Scenario("curve-entry-stop", "突破 Curve 曲線進場後固定 −2 ATR 停損", "test_curve_breakout_entry_then_fixed_two_atr_stop"),
)


def read_results(path: Path, selected: list[Scenario]) -> list[dict]:
    cases = ET.parse(path).getroot().findall(".//testcase")
    results = []
    for scenario in selected:
        matches = [case for case in cases if case.get("name") == scenario.test_name]
        result = {"case": scenario.key, "title": scenario.title,
                  "status": "missing", "details": {}, "message": "沒有唯一的測試結果"}
        if len(matches) == 1:
            case = matches[0]
            problem = next((child for child in case if child.tag in {"failure", "error", "skipped"}), None)
            result.update(status="passed" if problem is None else "failed",
                          seconds=float(case.get("time", "0")),
                          message="" if problem is None else problem.get("message", problem.tag))
            details = case.find("properties/property[@name='result']")
            if details is not None:
                result["details"] = json.loads(details.get("value", "{}"))
        results.append(result)
    return results


def print_result(result: dict):
    passed = result["status"] == "passed"
    print(f"[{'PASS' if passed else 'FAIL'}] {result['title']}")
    details = result["details"]
    if details:
        parts = []
        for key, label in (("entry_price", "進場"), ("atr", "ATR"), ("target_price", "固定停利價"),
                           ("stop_price", "固定停損價"),
                           ("curve_price", "曲線價"), ("exit_price", "出場"),
                           ("profit_points", "損益點數"), ("trigger_reason", "觸發原因"),
                           ("entry_count", "進場次數"), ("position", "剩餘部位")):
            value = details.get(key)
            if value is not None:
                parts.append(f"{label}={value:.2f}" if isinstance(value, float) else f"{label}={value}")
        print("  " + " · ".join(parts))
    if not passed:
        print("  " + result["message"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="離線自動測試：固定 2 ATR 停利、Curve 停利、突破進場及 −2 ATR 停損")
    parser.add_argument("--case", choices=["all", *(scenario.key for scenario in SCENARIOS)], default="all",
                        help="只測指定流程，預設 all 全部執行")
    parser.add_argument("--report", type=Path, help="另存 JSON 報告的路徑")
    args = parser.parse_args(argv)
    selected = [scenario for scenario in SCENARIOS if args.case in {"all", scenario.key}]
    print(f"執行 {len(selected)} 個離線 Mock 流程；使用固定行情與暫存資料庫。", flush=True)
    print("測試會自動推進 K 棒，不需啟動 Dashboard 或等待播放。", flush=True)
    with TemporaryDirectory(prefix="shioaji-mock-tests-") as directory:
        junit = Path(directory) / "results.xml"
        command = [sys.executable, "-m", "pytest", "-q", "-o", "junit_family=xunit1",
                   f"--junitxml={junit}", *(scenario.node_id for scenario in selected)]
        try:
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")
        except OSError as error:
            print(f"測試無法啟動：{error}", file=sys.stderr)
            return 1
        try:
            results = read_results(junit, selected)
        except (OSError, ET.ParseError, ValueError) as error:
            results = [{"case": scenario.key, "title": scenario.title, "status": "missing",
                        "details": {}, "message": f"無法讀取測試結果：{error}"} for scenario in selected]

    for result in results:
        print_result(result)
    passed = sum(result["status"] == "passed" for result in results)
    success = completed.returncode == 0 and passed == len(selected)
    print(f"結果：{passed}/{len(selected)} 通過；{'全部成功' if success else '測試未全部成功'}。")
    if not success:
        print(completed.stdout)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
    if args.report is not None:
        report = {"success": success, "passed": passed, "total": len(selected),
                  "mode": "offline_mock_api", "scenario_source": "fixed regression scenarios; independent of config/mock.json",
                  "pytest_exit_code": completed.returncode, "results": results}
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"報告儲存失敗：{error}", file=sys.stderr)
            return 1
        print(f"JSON 報告：{args.report.resolve()}")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
