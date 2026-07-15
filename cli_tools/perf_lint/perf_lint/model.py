"""Data model: findings, parsed Odoo classes, scan events, the project."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field as dc_field
from typing import Any

#: a pre-collected AST shape: (node, enclosing class, enclosing method)
ShapeHit = tuple[ast.expr, "ModelClass | None", "MethodInfo | None"]


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

    def sort_key(self) -> tuple[str, int, int, str]:
        return (self.path, self.line, self.col, self.code)


@dataclass
class FieldDecl:
    name: str
    ftype: str
    node: ast.Call
    kwargs: dict[str, ast.expr]
    comodel: str | None = None

    def kw(self, name: str, default: Any = None) -> Any:
        """Constant value of a kwarg; `default` if absent; ... if dynamic."""
        v = self.kwargs.get(name)
        if v is None:
            return default
        if isinstance(v, ast.Constant):
            return v.value
        return Ellipsis


@dataclass
class MethodInfo:
    node: ast.FunctionDef | ast.AsyncFunctionDef
    name: str
    depends: list[str] = dc_field(default_factory=list)
    constrains: list[str] = dc_field(default_factory=list)
    is_compute: bool = False


@dataclass
class ModelClass:
    node: ast.ClassDef
    class_name: str
    model: str | None  # _name, falling back to _inherit
    fields: dict[str, FieldDecl]
    methods: dict[str, MethodInfo]
    unique_cols: set[str]  # covered by models.Constraint UNIQUE / UniqueIndex
    order: str | None = None  # literal _order, when declared
    order_node: ast.stmt | None = None  # the _order assignment, for line info


@dataclass
class QueryEvent:
    node: ast.Call
    fname: str
    kind: str  # "read" | "write" | "flush"
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
    value: ast.expr
    node: ast.Tuple | ast.List  # the term tuple, for line info
    klass: ModelClass | None
    method: MethodInfo | None


@dataclass
class ModuleCtx:
    path: str
    tree: ast.Module
    lines: list[str]
    #: registry-only module from --addons-path: feeds field/model resolution
    #: but is never scanned and never anchors a finding
    is_context: bool = False
    models: list[ModelClass] = dc_field(default_factory=list)
    query_events: list[QueryEvent] = dc_field(default_factory=list)
    domain_terms: list[DomainTerm] = dc_field(default_factory=list)
    len_search: list[ShapeHit] = dc_field(default_factory=list)
    filtered_after_search: list[ShapeHit] = dc_field(default_factory=list)
    sorted_after_search: list[ShapeHit] = dc_field(default_factory=list)
    search_slice: list[ShapeHit] = dc_field(default_factory=list)
    agg_over_search: list[ShapeHit] = dc_field(default_factory=list)


class Project:
    """All analyzed modules plus a cross-file field registry."""

    def __init__(self) -> None:
        self.modules: list[ModuleCtx] = []
        #: model name -> {field name -> FieldDecl}
        self.registry: dict[str, dict[str, FieldDecl]] = {}
        #: model name -> set of column names
        self.unique_cols: dict[str, set[str]] = {}

    def add(self, mod: ModuleCtx) -> None:
        self.modules.append(mod)
        for klass in mod.models:
            if not klass.model:
                continue
            self.registry.setdefault(klass.model, {}).update(klass.fields)
            self.unique_cols.setdefault(klass.model, set()).update(
                klass.unique_cols)

    def field(self, model: str | None, name: str) -> FieldDecl | None:
        if not model:
            return None
        return self.registry.get(model, {}).get(name)
