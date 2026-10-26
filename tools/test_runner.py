"""Run all offline tests, or watch saved files and select dependent tests.

Use with conda env tss. Selection follows transitive local Python imports;
shared configuration, deleted files and unknown dependencies run the full suite.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
IGNORED = {".git", ".pytest_cache", "__pycache__", "data", "out", ".claude", ".vscode"}
SUFFIXES = {".py", ".json", ".ini", ".html", ".js", ".css", ".toml", ".cfg"}


def file_snapshot(root=ROOT):
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if set(relative.parts) & IGNORED or path.is_symlink() or path.suffix not in SUFFIXES:
            continue
        try:
            if path.is_file():
                stat = path.stat()
                files[relative] = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            pass
    return files


def changed_files(before, after):
    return {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}


def related_tests(changes, root=ROOT):
    """Return relative test paths; None requests the entire suite."""
    changes = {Path(path) for path in changes}
    if not changes:
        return []
    if any(path.is_absolute() or path.suffix != ".py" or path.name == "conftest.py"
           or path.parts[0] not in {"src", "tools"} or not (root / path).is_file() for path in changes):
        return None
    sources = list((root / "src").glob("*.py")) + list((root / "tools").glob("*.py"))
    modules = {path.stem: path.relative_to(root) for path in sources}
    graph = {}
    try:
        for path in sources:
            imports = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
                # Dynamic imports cannot be resolved reliably by this selector.
                elif isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__" or
                    isinstance(node.func, ast.Attribute) and node.func.attr in {"import_module", "spec_from_file_location"}
                ):
                    return None
            graph[path.relative_to(root)] = {modules[name] for name in imports if name in modules}
    except (SyntaxError, OSError):
        return None
    if not changes <= graph.keys():
        return None
    selected = set()
    for path in changes:
        affected = {path}
        while True:
            more = {source for source, dependencies in graph.items() if dependencies & affected}
            if more <= affected:
                break
            affected |= more
        tests = {source for source in affected if source.name.startswith("test_") and source in graph}
        # A new module may have a paired test that doesn't import it yet.
        paired = Path("src") / f"test_{path.stem}.py"
        if paired in graph:
            tests.add(paired)
        if not tests:
            return None
        selected |= tests
    return sorted(selected)


def run_tests(paths=None, root=ROOT):
    command = [sys.executable, "-m", "pytest", "-q"]
    if paths:
        command.extend(str(path) for path in paths)
    print("執行完整離線測試" if paths is None else "測試：" + ", ".join(str(path) for path in paths), flush=True)
    return subprocess.run(command, cwd=root).returncode


def watch(root=ROOT):
    before = file_snapshot(root)
    run_tests(root=root)
    print("監看已啟動；存檔後執行相關測試。Ctrl+C 結束。", flush=True)
    try:
        while True:
            time.sleep(0.5)
            after = file_snapshot(root)
            changes = changed_files(before, after)
            if not changes:
                continue
            # Merge saves until files have been stable for half a second.
            while True:
                time.sleep(0.5)
                latest = file_snapshot(root)
                changes |= changed_files(after, latest)
                if latest == after:
                    break
                after = latest
            before = after  # Changes during the test run are picked up next time.
            run_tests(related_tests(changes, root), root)
    except KeyboardInterrupt:
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch", action="store_true")
    group.add_argument("--all", action="store_true")
    group.add_argument("--related", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if args.watch:
        return watch()
    selected = None
    if args.related:
        changes = []
        for path in args.related:
            try:
                changes.append(path.resolve().relative_to(ROOT))
            except ValueError:
                selected = None
                break
        else:
            selected = related_tests(changes)
    return run_tests(selected)


if __name__ == "__main__":
    sys.exit(main())
