"""SD20x — asking the ORM the wrong way."""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Iterator

from ..astutils import const_str
from ..constants import SEARCHY
from ..model import Finding, ModuleCtx, Project
from ..registry import Checker, register

if TYPE_CHECKING:
    from ..runner import Config


def _truth_tested(tree: ast.Module) -> Iterator[ast.expr]:
    """Yield expressions whose value is used ONLY as a boolean.

    Deliberately conservative: `x or default` and `while (r := search())`
    also truth-test their operand but keep the value, so suggesting
    limit=1 there could change behavior — those shapes are not yielded."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            yield node.test
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield node.operand
        elif isinstance(node, ast.Assert):
            yield node.test
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "bool" and node.args):
            yield node.args[0]
        elif isinstance(node, ast.comprehension):
            yield from node.ifs


@register
class OrmMisuseChecker(Checker):
    codes = {
        "SD201": ("unbounded-search-all", "warning",
                  "search([]) loads every record of the model"),
        "SD202": ("filtered-after-search", "warning",
                  ".filtered() on a search() result — filter in the domain"),
        "SD203": ("len-search", "warning",
                  "len(search(...)) — use search_count() / _read_group"),
        "SD204": ("sorted-after-search", "info",
                  ".sorted() on a search() result — sort in the query with "
                  "order="),
        "SD205": ("search-for-existence", "warning",
                  "search() used as a truth test without limit=1"),
        "SD206": ("sliced-search", "warning",
                  "search(...)[0] / [:N] — fetch everything, keep N; pass "
                  "limit= instead"),
        "SD207": ("python-agg-after-search", "warning",
                  "sum()/max()/min() over mapped() on a search() result — "
                  "read_group does it in one query"),
    }
    explain = {
        "SD201": "search([]) with no domain and no limit loads the whole "
                 "table into memory. Push the condition into the domain — "
                 "the DB is a librarian: ask for the shelf, don't carry the "
                 "library home. noqa it for known-tiny tables.",
        "SD202": "Fetching rows and then filtering in Python does the "
                 "database's job in the app server, with all rows loaded "
                 "for nothing. Move the predicate into the search domain.",
        "SD203": "len(search(...)) browses every matching record just to "
                 "count them. search_count() issues a single SELECT "
                 "count(*); _read_group when counting for many parents.",
        "SD204": "Sorting a search() result in Python loads every row and "
                 "sorts in the app server, while order= lets PostgreSQL "
                 "sort — often straight off an index, and combined with "
                 "limit= it never materializes the rest. Info-level "
                 "because a key= lambda may express what SQL cannot.",
        "SD205": "`if search(domain):` fetches EVERY matching row just to "
                 "ask 'is there at least one?'. With limit=1 the database "
                 "stops at the first hit. search_count(domain, limit=1) "
                 "works too and skips creating the recordset.",
        "SD206": "Slicing or indexing a search() result keeps N records "
                 "but the query already fetched ALL of them. limit= (with "
                 "order= when the first match must be a specific one) "
                 "makes PostgreSQL stop early — often straight off an "
                 "index. Only unlimited searches are flagged.",
        "SD207": "sum(records.mapped('amount')) loads every matching row "
                 "into the cache to reduce them to one number. "
                 "read_group/_read_group returns the aggregate straight "
                 "from SQL — one query, no rows materialized, and it "
                 "batches over many parents with groupby=.",
    }

    def check_module(self, mod: ModuleCtx, project: Project,
                     cfg: Config) -> Iterator[Finding]:
        for ev in mod.query_events:
            if ev.empty_domain and cfg.enabled("SD201"):
                model = f" of {ev.model}" if ev.model else ""
                yield self.finding(
                    "SD201", mod, ev.node,
                    f"{ev.fname}([]) loads every record{model}; add a "
                    f"domain or limit= — noqa for known-small tables")
        if cfg.enabled("SD202"):
            for node, klass, method in mod.filtered_after_search:
                yield self.finding(
                    "SD202", mod, node,
                    ".filtered() on a search() result — push the predicate "
                    "into the search domain so the DB does the filtering")
        if cfg.enabled("SD203"):
            for node, klass, method in mod.len_search:
                yield self.finding("SD203", mod, node)
        if cfg.enabled("SD204"):
            for node, klass, method in mod.sorted_after_search:
                yield self.finding(
                    "SD204", mod, node,
                    ".sorted() on a search() result — pass order= to "
                    "search() so PostgreSQL sorts (and can use an index)")
        if cfg.enabled("SD205"):
            yield from self._existence_tests(mod)
        if cfg.enabled("SD206"):
            for node, klass, method in mod.search_slice:
                yield self.finding("SD206", mod, node,
                                   self._slice_message(node))
        if cfg.enabled("SD207"):
            for node, klass, method in mod.agg_over_search:
                yield self.finding("SD207", mod, node,
                                   self._agg_message(node))

    @staticmethod
    def _slice_message(node: ast.expr) -> str:
        n = 1
        shape = "[0]"
        if isinstance(node, ast.Subscript) \
                and isinstance(node.slice, ast.Slice) \
                and isinstance(node.slice.upper, ast.Constant) \
                and isinstance(node.slice.upper.value, int):
            n = node.slice.upper.value
            shape = f"[:{n}]"
        return (f"{shape} on an unlimited search() — every matching row "
                f"was fetched to keep {n}; pass limit={n} (and order= if "
                f"which record matters) so the DB stops early")

    @staticmethod
    def _agg_message(node: ast.expr) -> str:
        agg, fname = "sum", None
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                agg = node.func.id
            arg = node.args[0]
            if isinstance(arg, ast.Call) and arg.args:
                fname = const_str(arg.args[0])
        field = f"'{fname}'" if fname else "a field"
        return (f"{agg}() over mapped({field}) on a search() result "
                f"fetches every row to compute one number — "
                f"read_group/_read_group with '{fname or '...'}:{agg}' "
                f"returns it in a single query")

    def _existence_tests(self, mod: ModuleCtx) -> Iterator[Finding]:
        unlimited = {
            id(ev.node): ev for ev in mod.query_events
            if ev.fname in SEARCHY
            and not any(kw.arg == "limit" for kw in ev.node.keywords)
        }
        for expr in _truth_tested(mod.tree):
            ev = unlimited.get(id(expr))
            if ev:
                yield self.finding(
                    "SD205", mod, ev.node,
                    f"{ev.fname}() result is only truth-tested — add "
                    f"limit=1 so the DB stops at the first match, or use "
                    f"search_count(..., limit=1)")
