"""Data model: findings, parsed Odoo classes, scan events, the project."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field


@dataclass
class Finding:
    path: str
    line: int
    col: int
    code: str
    name: str
    severity: str
    message: str
    end_line: int = 0  # last physical line of the flagged node (for noqa)

    def sort_key(self):
        return (self.path, self.line, self.col, self.code)


@dataclass
class FieldDecl:
    name: str
    ftype: str
    node: ast.AST
    kwargs: dict  # name -> ast node
    comodel: str | None = None

    def kw(self, name, default=None):
        """Constant value of a kwarg; `default` if absent; ... if dynamic."""
        v = self.kwargs.get(name)
        if v is None:
            return default
        if isinstance(v, ast.Constant):
            return v.value
        return Ellipsis


@dataclass
class MethodInfo:
    node: ast.AST
    name: str
    depends: list = dc_field(default_factory=list)
    constrains: list = dc_field(default_factory=list)
    is_compute: bool = False


@dataclass
class ModelClass:
    node: ast.ClassDef
    class_name: str
    model: str | None  # _name, falling back to _inherit
    fields: dict  # name -> FieldDecl
    methods: dict  # name -> MethodInfo
    unique_cols: set  # columns covered by models.Constraint UNIQUE / UniqueIndex


@dataclass
class QueryEvent:
    node: ast.Call
    fname: str
    kind: str  # "read" | "write"
    model: str | None  # model name of the receiver, when resolvable
    in_loop: bool
    batched: bool  # create() called with a list/listcomp (batched)
    empty_domain: bool  # search([]) without limit=
    klass: ModelClass | None
    method: MethodInfo | None


@dataclass
class DomainTerm:
    model: str | None
    fname: str  # field name (first segment if dotted)
    dotted: bool
    op: str
    value: ast.AST
    node: ast.AST  # the term tuple, for line info
    klass: ModelClass | None
    method: MethodInfo | None


@dataclass
class ModuleCtx:
    path: str
    tree: ast.Module
    lines: list
    models: list = dc_field(default_factory=list)
    query_events: list = dc_field(default_factory=list)
    domain_terms: list = dc_field(default_factory=list)
    len_search: list = dc_field(default_factory=list)  # (node, klass, method)
    filtered_after_search: list = dc_field(default_factory=list)


class Project:
    """All analyzed modules plus a cross-file field registry."""

    def __init__(self):
        self.modules = []
        self.registry = {}  # model name -> {field name -> FieldDecl}
        self.unique_cols = {}  # model name -> set of column names

    def add(self, mod: ModuleCtx):
        self.modules.append(mod)
        for klass in mod.models:
            if not klass.model:
                continue
            self.registry.setdefault(klass.model, {}).update(klass.fields)
            self.unique_cols.setdefault(klass.model, set()).update(
                klass.unique_cols)

    def field(self, model, name) -> FieldDecl | None:
        if not model:
            return None
        return self.registry.get(model, {}).get(name)
