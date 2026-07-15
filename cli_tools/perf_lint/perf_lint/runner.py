"""Orchestration: config/toggles, file discovery, noqa, plugins, lint()."""
from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import sys
from typing import Iterable, Iterator

from .model import Finding, ModuleCtx, Project
from .parsing import analyze_file
from .registry import CHECKERS
from .scanner import scan_module


class Config:
    """Which checks are enabled. Codes match by prefix (flake8-style):
    --ignore SD3 disables all SD3xx checks."""

    def __init__(self, select: list[str] | None = None,
                 ignore: list[str] | None = None) -> None:
        self.select = select or []
        self.ignore = ignore or []

    def enabled(self, code: str) -> bool:
        if self.select and not any(code.startswith(p) for p in self.select):
            return False
        return not any(code.startswith(p) for p in self.ignore)


NOQA_RE = re.compile(r"#\s*noqa(?::\s*(?P<codes>[A-Z0-9, ]+))?", re.I)


def suppressed(finding: Finding, lines: list[str]) -> bool:
    """A noqa on ANY physical line of the flagged node suppresses it."""
    last = max(finding.end_line, finding.line)
    for lineno in range(finding.line, min(last, len(lines)) + 1):
        m = NOQA_RE.search(lines[lineno - 1])
        if not m:
            continue
        codes = m.group("codes")
        if not codes:
            return True  # bare noqa
        if finding.code in {c.strip().upper() for c in codes.split(",")}:
            return True
    return False


def iter_py_files(paths: Iterable[str],
                  excludes: list[str]) -> Iterator[str]:
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    for path in paths:
        if os.path.isfile(path):
            yield path
            continue
        if not os.path.isdir(path):
            print(f"perf_lint: no such file or directory: {path}",
                  file=sys.stderr)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in sorted(files):
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(root, fn)
                if any(fnmatch.fnmatch(full, pat) or pat in full
                       for pat in excludes):
                    continue
                yield full


def load_plugin(path: str) -> None:
    """Execute a plugin file; its @register calls enrol extra checkers."""
    spec = importlib.util.spec_from_file_location(
        f"perf_lint_plugin_{os.path.basename(path).removesuffix('.py')}",
        path)
    assert spec and spec.loader, f"cannot load plugin: {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)


def lint(paths: list[str], excludes: list[str],
         cfg: Config) -> tuple[list[Finding], int]:
    """Analyze paths and run every registered checker.

    Returns (findings, n_suppressed)."""
    project = Project()
    modules: list[ModuleCtx] = []
    for path in iter_py_files(paths, excludes):
        mod = analyze_file(path)
        if mod:
            modules.append(mod)
            project.add(mod)
    for mod in modules:  # after project.add: scanning uses the full registry
        scan_module(mod, project)

    findings: list[Finding] = []
    n_suppressed = 0
    lines_by_path = {m.path: m.lines for m in modules}
    for checker in CHECKERS:
        produced: list[Finding] = []
        for mod in modules:
            produced.extend(checker.check_module(mod, project, cfg))
        produced.extend(checker.check_project(project, cfg))
        for f in produced:
            if not cfg.enabled(f.code):
                continue
            if suppressed(f, lines_by_path.get(f.path, [])):
                n_suppressed += 1
                continue
            findings.append(f)

    findings.sort(key=lambda f: f.sort_key())
    return findings, n_suppressed
