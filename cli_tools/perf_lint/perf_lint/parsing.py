"""First pass: parse Odoo model classes, fields and method decorators."""
from __future__ import annotations

import ast
import re
import sys

from .astutils import const_str, is_model_base
from .constants import RELATIONAL
from .model import FieldDecl, MethodInfo, ModelClass, ModuleCtx


def parse_field(name: str, call: ast.Call) -> FieldDecl | None:
    func = call.func
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "fields"):
        return None
    ftype = func.attr
    kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    comodel = None
    if ftype in RELATIONAL:
        if call.args:
            comodel = const_str(call.args[0])
        comodel = comodel or const_str(kwargs.get("comodel_name"))
    return FieldDecl(name, ftype, call, kwargs, comodel)


def parse_unique_cols(call: ast.Call,
                      declared_fields: dict[str, FieldDecl]) -> set[str]:
    """Columns covered by models.Constraint('UNIQUE (...)') / UniqueIndex."""
    func = call.func
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "models"
            and func.attr in ("Constraint", "UniqueIndex")):
        return set()
    sql = const_str(call.args[0]) if call.args else None
    if not sql:
        return set()
    if func.attr == "Constraint" and "unique" not in sql.lower():
        return set()
    words = set(re.findall(r"[a-z_][a-z0-9_]*", sql.lower()))
    return words & set(declared_fields)


def parse_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> MethodInfo:
    info = MethodInfo(node, node.name)
    for dec in node.decorator_list:
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "api"):
            args = [s for s in (const_str(a) for a in dec.args) if s]
            if dec.func.attr == "depends":
                info.depends.extend(args)
            elif dec.func.attr == "constrains":
                info.constrains.extend(args)
    return info


def parse_class(node: ast.ClassDef) -> ModelClass | None:
    is_model = any(is_model_base(b) for b in node.bases)
    name: str | None = None
    inherit: str | None = None
    order: str | None = None
    order_node: ast.stmt | None = None
    fields: dict[str, FieldDecl] = {}
    methods: dict[str, MethodInfo] = {}
    unique: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            tname = stmt.targets[0].id
            if tname == "_name":
                name = const_str(stmt.value)
            elif tname == "_order":
                order = const_str(stmt.value)
                order_node = stmt
            elif tname == "_inherit":
                inherit = const_str(stmt.value)
                if inherit is None and isinstance(stmt.value, ast.List) \
                        and stmt.value.elts:
                    inherit = const_str(stmt.value.elts[0])
            elif isinstance(stmt.value, ast.Call):
                f = parse_field(tname, stmt.value)
                if f:
                    fields[tname] = f
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m = parse_method(stmt)
            methods[m.name] = m
    # second pass for constraints (needs the full field list)
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
            unique |= parse_unique_cols(stmt.value, fields)
    if not (is_model or name or inherit):
        return None
    compute_names = {f.kw("compute") for f in fields.values()
                     if isinstance(f.kw("compute"), str)}
    for m in methods.values():
        m.is_compute = (m.name.startswith("_compute_")
                        or m.name in compute_names or bool(m.depends))
    return ModelClass(node, node.name, name or inherit, fields, methods,
                      unique, order, order_node)


def analyze_file(path: str) -> ModuleCtx | None:
    try:
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        print(f"perf_lint: cannot parse {path}: {exc}", file=sys.stderr)
        return None
    lines = source.splitlines()
    if any("perf-lint: skip-file" in ln for ln in lines[:5]):
        return None
    mod = ModuleCtx(path, tree, lines)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            klass = parse_class(node)
            if klass:
                mod.models.append(klass)
    return mod
