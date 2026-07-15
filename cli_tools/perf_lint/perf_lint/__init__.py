"""perf_lint — static analysis for Odoo ORM performance anti-patterns.

Detects anti-patterns using pure AST analysis (stdlib only, no Odoo import
needed).

Usage (from the repo root):
    python3 -m perf_lint my_addon                  # lint a module
    python3 -m perf_lint --list-checks             # show all checks
    python3 -m perf_lint --explain SD103           # what/why/fix
    python3 -m perf_lint . --ignore SD3,SD303      # toggle checks off
    python3 -m perf_lint . --select SD1            # only the loop family
    python3 -m perf_lint . --format json           # machine-readable
    python3 -m perf_lint . --plugin my_checks.py   # load extra checks

Inline suppression (flake8-style):
    tags = self.env["my.tag"].search([])  # noqa: SD201  (known-small table)
    anything = ...                        # noqa          (suppress all)
A file whose first lines contain "# perf-lint: skip-file" is skipped.

Checks (SD1xx loops/N+1 · SD2xx ORM misuse · SD3xx indexing · SD4xx storage):
    see --list-checks / --explain CODE.

Writing your own check (put this in my_checks.py and pass --plugin):

    from perf_lint import Checker, register
    import ast

    @register
    class CommitInCode(Checker):
        codes = {"X901": ("cr-commit", "warning",
                          "explicit commit() — transaction control belongs "
                          "to the framework")}

        def check_module(self, mod, project, cfg):
            for node in ast.walk(mod.tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "commit"):
                    yield self.finding("X901", mod, node)

`mod` (ModuleCtx) exposes: .tree, .lines, .models (parsed fields/methods),
.query_events, .domain_terms, .len_search, .filtered_after_search.
`project` exposes: .modules, .field(model, name), .unique_cols.
Checkers may also implement check_project(project, cfg) for cross-file rules.

Package layout:
    constants.py   ORM method sets, severities, operator tables
    astutils.py    small AST predicates
    model.py       dataclasses (Finding, FieldDecl, ModuleCtx, Project, ...)
    parsing.py     pass 1: model classes, fields, decorators
    scanner.py     pass 2: recordset dataflow + loop-aware query events
    registry.py    Checker base class + @register
    checks/        built-in checks (loops, orm, indexes, storage)
    runner.py      toggles, noqa, file discovery, plugins, lint()
    output.py      text/json rendering, --list-checks, --explain
    cli.py         argparse entry point
"""
from .model import (  # noqa: F401  (public plugin API)
    DomainTerm, FieldDecl, Finding, MethodInfo, ModelClass, ModuleCtx,
    Project, QueryEvent,
)
from .registry import ALL_CODES, CHECKERS, Checker, register  # noqa: F401
from .runner import Config, lint  # noqa: F401

from . import checks  # noqa: E402,F401  (import registers built-in checks)

__all__ = [
    "ALL_CODES", "CHECKERS", "Checker", "Config", "DomainTerm", "FieldDecl",
    "Finding", "MethodInfo", "ModelClass", "ModuleCtx", "Project",
    "QueryEvent", "lint", "register",
]
