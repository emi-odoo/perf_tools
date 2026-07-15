"""Second pass: per-function walk collecting query events and domain terms.

Runs after the whole project is parsed so recordset resolution can use the
cross-file field registry (e.g. partner_id -> res.partner)."""
from __future__ import annotations

import ast

from .astutils import const_str, is_cursor, is_env
from .constants import (
    CHAIN_METHODS, DOMAIN_METHODS, FLUSH_METHODS, PER_RECORD_LAMBDA,
    PY_AGGREGATES, QUERY_METHODS, RELATIONAL, SEARCHY, WRITE_METHODS,
)
from .model import (
    DomainTerm, MethodInfo, ModelClass, ModuleCtx, Project, QueryEvent,
)

#: what a name/expression holds:
#: ("model" | "record" | "search_result", model name when resolvable)
Resolved = tuple[str, "str | None"]


def searchy_call(e: ast.AST, depth: int = 0) -> ast.Call | None:
    """The search()/search_fetch() call at the root of a chain, or None.

    Follows chain methods so `self.search(d).sudo()` yields the search."""
    if depth > 8 or not isinstance(e, ast.Call) \
            or not isinstance(e.func, ast.Attribute):
        return None
    if e.func.attr in SEARCHY:
        return e
    if e.func.attr in CHAIN_METHODS:
        return searchy_call(e.func.value, depth + 1)
    return None


def _has_limit(call: ast.Call) -> bool:
    return any(kw.arg == "limit" for kw in call.keywords)


class FuncScanner(ast.NodeVisitor):
    """Walks one function, tracking which names hold recordsets, which
    statements execute inside a loop, and every ORM query/write call."""

    def __init__(self, mod: ModuleCtx, project: Project,
                 klass: ModelClass | None, method: MethodInfo) -> None:
        self.mod = mod
        self.project = project
        self.klass = klass
        self.method = method
        self.symbols: dict[str, Resolved] = {}
        if klass and klass.model:
            self.symbols["self"] = ("model", klass.model)
        elif klass:
            self.symbols["self"] = ("model", None)
        self.loops: list[ast.AST] = []
        self.lambda_loops: set[ast.Lambda] = set()
        #: name -> the search() call it was assigned from (for limit= checks)
        self.search_origin: dict[str, ast.Call] = {}

    # -- recordset resolution ---------------------------------------------
    def resolve(self, e: ast.AST, depth: int = 0) -> Resolved | None:
        if depth > 8:
            return None
        if isinstance(e, ast.Name):
            return self.symbols.get(e.id)
        if isinstance(e, ast.Attribute):
            if e.attr in ("env", "ids", "id"):
                return None
            base = self.resolve(e.value, depth + 1)
            if not base:
                return None
            fdecl = self.project.field(base[1], e.attr)
            if fdecl:  # known field: recordset only if relational
                if fdecl.ftype in RELATIONAL:
                    return ("record", fdecl.comodel)
                return None
            # unknown attribute on a recordset: assume it may be one too
            return ("record", None)
        if isinstance(e, ast.Subscript):
            if is_env(e.value):
                return ("model", const_str(e.slice))
            base = self.resolve(e.value, depth + 1)
            return base  # slicing a recordset keeps its nature
        if isinstance(e, ast.Call):
            f = e.func
            if isinstance(f, ast.Name) and f.id == "super":
                return self.symbols.get("self")
            if isinstance(f, ast.Attribute):
                if isinstance(f.value, ast.Call) \
                        and isinstance(f.value.func, ast.Name) \
                        and f.value.func.id == "super":
                    base = self.symbols.get("self")
                else:
                    base = self.resolve(f.value, depth + 1)
                if base and f.attr in CHAIN_METHODS:
                    if f.attr in SEARCHY:
                        return ("search_result", base[1])
                    if f.attr in ("browse", "create", "copy"):
                        return ("record", base[1])
                    return base
        return None

    # -- loops --------------------------------------------------------------
    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)  # evaluated once, outside the loop
        kind = self.resolve(node.iter)
        if kind and isinstance(node.target, ast.Name):
            self.symbols[node.target.id] = ("record", kind[1])
        self.loops.append(node)
        for stmt in node.body + node.orelse:
            self.visit(stmt)
        self.loops.pop()

    def visit_While(self, node: ast.While) -> None:
        self.loops.append(node)  # test + body both run per iteration
        self.visit(node.test)
        for stmt in node.body + node.orelse:
            self.visit(stmt)
        self.loops.pop()

    def _visit_comp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp
                    | ast.DictComp, elts: list[ast.expr]) -> None:
        for gen in node.generators:
            self.visit(gen.iter)
            kind = self.resolve(gen.iter)
            if kind and isinstance(gen.target, ast.Name):
                self.symbols[gen.target.id] = ("record", kind[1])
        self.loops.append(node)
        for gen in node.generators:
            for cond in gen.ifs:
                self.visit(cond)
        for e in elts:
            self.visit(e)
        self.loops.pop()

    def visit_ListComp(
            self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp,
    ) -> None:
        self._visit_comp(node, [node.elt])

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comp(node, [node.key, node.value])

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if node in self.lambda_loops:  # runs once per record
            self.loops.append(node)
            self.visit(node.body)
            self.loops.pop()
        else:
            self.generic_visit(node)

    # -- assignments ----------------------------------------------------------
    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        kind = self.resolve(node.value)
        origin = (searchy_call(node.value)
                  if kind and kind[0] == "search_result" else None)
        for t in node.targets:
            if isinstance(t, ast.Name):
                if kind:
                    self.symbols[t.id] = kind
                else:
                    self.symbols.pop(t.id, None)
                if origin:
                    self.search_origin[t.id] = origin
                else:
                    self.search_origin.pop(t.id, None)
            else:
                self.visit(t)

    # -- subscripts -----------------------------------------------------------
    def visit_Subscript(self, node: ast.Subscript) -> None:
        self._check_search_slice(node)
        self.generic_visit(node)

    def _check_search_slice(self, node: ast.Subscript) -> None:
        """Collect search(...)[0] / search(...)[:N] over an unlimited
        search — the rows were all fetched just to keep the first few."""
        if isinstance(node.value, ast.Subscript):
            return  # search()[:1][0]: the inner subscript already hit
        idx = node.slice
        first_index = (isinstance(idx, ast.Constant)
                       and type(idx.value) is int and idx.value == 0)
        head_slice = (isinstance(idx, ast.Slice)
                      and idx.lower is None and idx.step is None
                      and isinstance(idx.upper, ast.Constant)
                      and type(idx.upper.value) is int)
        if not (first_index or head_slice):
            return
        recv = self.resolve(node.value)
        if not recv or recv[0] != "search_result":
            return
        call = searchy_call(node.value)
        if call is None and isinstance(node.value, ast.Name):
            call = self.search_origin.get(node.value.id)
        if call is None or _has_limit(call):
            return  # unknown origin or already limited: stay quiet
        self.mod.search_slice.append((node, self.klass, self.method))

    # -- calls ------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in PER_RECORD_LAMBDA:
            for a in list(node.args) + [kw.value for kw in node.keywords]:
                if isinstance(a, ast.Lambda):
                    self.lambda_loops.add(a)

        if isinstance(func, ast.Name) and func.id == "len" and node.args:
            a = node.args[0]
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr in SEARCHY
                    and self.resolve(a.func.value)):
                self.mod.len_search.append((node, self.klass, self.method))

        if isinstance(func, ast.Name) and func.id in PY_AGGREGATES \
                and node.args:
            a = node.args[0]
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and a.func.attr == "mapped" and a.args
                    and const_str(a.args[0])):
                recv = self.resolve(a.func.value)
                if recv and recv[0] == "search_result":
                    self.mod.agg_over_search.append(
                        (node, self.klass, self.method))

        if isinstance(func, ast.Attribute):
            fname = func.attr
            if fname == "execute":
                if is_cursor(func.value):
                    self._add_event(node, fname, "read", None)
            elif fname in ("filtered", "sorted"):
                recv = self.resolve(func.value)
                if recv and recv[0] == "search_result":
                    lst = (self.mod.filtered_after_search
                           if fname == "filtered"
                           else self.mod.sorted_after_search)
                    lst.append((node, self.klass, self.method))
            elif fname in FLUSH_METHODS:
                v = func.value
                if is_env(v) or self.resolve(v) or (
                        isinstance(v, ast.Attribute)
                        and v.attr == "registry"):
                    self._add_event(node, fname, "flush", None)
            elif fname in QUERY_METHODS or fname in WRITE_METHODS:
                recv = self.resolve(func.value)
                if recv:
                    kind = "write" if fname in WRITE_METHODS else "read"
                    self._add_event(node, fname, kind, recv[1])
        self.generic_visit(node)

    def _add_event(self, node: ast.Call, fname: str, kind: str,
                   model: str | None) -> None:
        batched = bool(fname == "create" and node.args and isinstance(
            node.args[0], (ast.List, ast.ListComp, ast.GeneratorExp,
                           ast.Tuple)))
        empty = bool(fname in SEARCHY | {"search_read"}
                     and node.args and isinstance(node.args[0], ast.List)
                     and not node.args[0].elts
                     and not any(kw.arg == "limit" for kw in node.keywords))
        self.mod.query_events.append(QueryEvent(
            node, fname, kind, model, bool(self.loops), batched, empty,
            self.klass, self.method))
        if fname in DOMAIN_METHODS and node.args \
                and isinstance(node.args[0], ast.List):
            self._extract_domain(node.args[0], model)

    def _extract_domain(self, domain: ast.List, model: str | None) -> None:
        for term in domain.elts:
            if isinstance(term, (ast.Tuple, ast.List)) \
                    and len(term.elts) == 3:
                fname = const_str(term.elts[0])
                op = const_str(term.elts[1])
                if fname and op:
                    self.mod.domain_terms.append(DomainTerm(
                        model, fname.split(".")[0], "." in fname, op,
                        term.elts[2], term, self.klass, self.method))


def scan_module(mod: ModuleCtx, project: Project) -> None:
    """Scan every method of every model class, plus module-level functions
    (migration scripts, hooks)."""
    for klass in mod.models:
        for m in klass.methods.values():
            scanner = FuncScanner(mod, project, klass, m)
            for stmt in m.node.body:
                scanner.visit(stmt)
    for node in mod.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            m = MethodInfo(node, node.name)
            scanner = FuncScanner(mod, project, None, m)
            for stmt in node.body:
                scanner.visit(stmt)
