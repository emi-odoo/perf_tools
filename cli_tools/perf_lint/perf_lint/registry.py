"""Checker base class and the registration machinery.

Built-in checks live in perf_lint.checks; external ones are loaded with
--plugin and use exactly the same API:

    from perf_lint import Checker, register

    @register
    class MyCheck(Checker):
        codes = {"X901": ("my-check", "warning", "one-line summary")}

        def check_module(self, mod, project, cfg):
            yield self.finding("X901", mod, some_ast_node)
"""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Iterable

from .model import Finding, ModuleCtx, Project

if TYPE_CHECKING:
    from .runner import Config

#: code -> (kebab-name, severity, one-line summary)
CodeInfo = tuple[str, str, str]

CHECKERS: list[Checker] = []
ALL_CODES: dict[str, CodeInfo] = {}
EXPLAIN: dict[str, str] = {}  # code -> long what/why/fix text


def register(cls: type[Checker]) -> type[Checker]:
    """Class decorator: instantiate and enrol a Checker subclass."""
    inst = cls()
    CHECKERS.append(inst)
    ALL_CODES.update(cls.codes)
    EXPLAIN.update(getattr(cls, "explain", {}))
    return cls


class Checker:
    #: code -> (kebab-name, severity, one-line summary)
    codes: dict[str, CodeInfo] = {}
    #: optional: code -> long explanation shown by --explain
    explain: dict[str, str] = {}

    def check_module(self, mod: ModuleCtx, project: Project,
                     cfg: Config) -> Iterable[Finding]:
        """Yield Findings for one analyzed file."""
        return ()

    def check_project(self, project: Project,
                      cfg: Config) -> Iterable[Finding]:
        """Yield Findings needing the cross-file registry (runs once)."""
        return ()

    def finding(self, code: str, mod: ModuleCtx, node: ast.stmt | ast.expr,
                message: str | None = None) -> Finding:
        name, sev, summary = ALL_CODES[code]
        return Finding(mod.path, node.lineno, node.col_offset + 1,
                       code, name, sev, message or summary,
                       node.end_lineno or node.lineno)
