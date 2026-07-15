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
from .model import Finding

CHECKERS = []
ALL_CODES = {}  # code -> (name, severity, summary)
EXPLAIN = {}  # code -> long what/why/fix text


def register(cls):
    """Class decorator: instantiate and enrol a Checker subclass."""
    inst = cls()
    CHECKERS.append(inst)
    ALL_CODES.update(cls.codes)
    EXPLAIN.update(getattr(cls, "explain", {}))
    return cls


class Checker:
    #: code -> (kebab-name, severity, one-line summary)
    codes = {}
    #: optional: code -> long explanation shown by --explain
    explain = {}

    def check_module(self, mod, project, cfg):
        """Yield Findings for one analyzed file."""
        return ()

    def check_project(self, project, cfg):
        """Yield Findings needing the cross-file registry (runs once)."""
        return ()

    def finding(self, code, mod, node, message=None):
        name, sev, summary = ALL_CODES[code]
        return Finding(mod.path, node.lineno, node.col_offset + 1,
                       code, name, sev, message or summary,
                       getattr(node, "end_lineno", node.lineno))
