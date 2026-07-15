"""SD30x — indexing and domain shapes the planner can't serve."""
from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Iterator

from ..astutils import const_str
from ..constants import INDEXABLE_OPS, LOW_CARDINALITY, X2MANY
from ..model import FieldDecl, Finding, ModuleCtx, Project
from ..registry import Checker, register

if TYPE_CHECKING:
    from ..runner import Config


@register
class IndexChecker(Checker):
    codes = {
        "SD301": ("index-disabled", "warning",
                  "Many2one declared with index=False"),
        "SD302": ("unindexed-searched-field", "warning",
                  "field searched in a domain but declared without index"),
        "SD303": ("ilike-domain", "info",
                  "like/ilike in a domain — btree cannot help a leading "
                  "wildcard"),
        "SD304": ("dotted-domain-x2many", "info",
                  "dotted domain traverses a x2many — nested sub-select "
                  "over the whole relation"),
    }
    explain = {
        "SD301": "Many2one columns are indexed BY DEFAULT — index=False is "
                 "someone turning that off. Every compute, group-by and "
                 "portal page then seq-scans the table.",
        "SD302": "Heuristic, same-codebase only: the field appears in a "
                 "search domain with an indexable operator but its "
                 "declaration has no index=True. At 40k rows that is a seq "
                 "scan per lookup, growing linearly forever.",
        "SD303": "('name', 'ilike', x) becomes ILIKE '%x%' — a leading "
                 "wildcard no btree can serve. If fuzzy search is a real "
                 "feature: index='trigram' on the field (pg_trgm GIN index, "
                 "declarable in Odoo 19). Info-level because one scan is "
                 "cheap; the multipliers (name_search per keystroke) are "
                 "not.",
        "SD304": "Each dot in a domain becomes `id IN (SELECT ...)`; "
                 "through a One2many/Many2many that inner SELECT scans the "
                 "child table (or the m2m bridge) for the WHOLE relation "
                 "before the outer filter applies. Cheap through a "
                 "Many2one, expensive through a x2many on big tables. "
                 "Consider searching the child model directly and mapping "
                 "back, or a stored aggregate on the parent. Info-level: "
                 "on small relations it is fine.",
    }

    def check_module(self, mod: ModuleCtx, project: Project,
                     cfg: Config) -> Iterator[Finding]:
        if not cfg.enabled("SD301"):
            return
        for klass in mod.models:
            for f in klass.fields.values():
                if f.ftype == "Many2one" and f.kw("index", True) is False:
                    yield self.finding(
                        "SD301", mod, f.node,
                        f"{klass.model or klass.class_name}.{f.name}: "
                        f"Many2one is indexed by default — index=False "
                        f"forces a seq scan under every search/compute/"
                        f"group-by")

    def check_project(self, project: Project,
                      cfg: Config) -> Iterator[Finding]:
        seen: set[tuple[str | None, str]] = set()
        seen_dotted: set[tuple[str | None, str]] = set()
        for mod in project.modules:
            for t in mod.domain_terms:
                if t.dotted and cfg.enabled("SD304"):
                    path = const_str(t.node.elts[0]) or ""
                    hop = self._x2many_hop(project, t.model, path)
                    if hop and (t.model, path) not in seen_dotted:
                        seen_dotted.add((t.model, path))
                        yield self.finding(
                            "SD304", mod, t.node,
                            f"('{path}', ...) traverses x2many '{hop}' — "
                            f"this becomes a nested sub-select over the "
                            f"whole relation; search the child model "
                            f"directly or store an aggregate")
                if "like" in t.op and cfg.enabled("SD303"):
                    f = project.field(t.model, t.fname)
                    if f and f.kw("index") == "trigram":
                        continue
                    yield self.finding(
                        "SD303", mod, t.node,
                        f"('{t.fname}', '{t.op}', ...) — leading wildcard "
                        f"defeats btree; declare index='trigram' if fuzzy "
                        f"search is intended")
                if not cfg.enabled("SD302"):
                    continue
                if t.dotted or t.op not in INDEXABLE_OPS \
                        or t.fname == "id" or (t.model, t.fname) in seen:
                    continue
                f = project.field(t.model, t.fname)
                if f is None or f.ftype in LOW_CARDINALITY \
                        or f.ftype in X2MANY:
                    continue
                if f.kw("compute") and f.kw("store", False) is not True:
                    continue  # not a real column
                if f.kw("related") and f.kw("store", False) is not True:
                    continue
                if f.kw("index", False):  # True / 'btree_not_null' / ...
                    continue
                if f.ftype == "Many2one":
                    continue  # indexed by default; =False is SD301's job
                if t.fname in project.unique_cols.get(t.model or "", set()):
                    continue  # unique constraint ships its own index
                seen.add((t.model, t.fname))
                where = f"{mod.path}:{t.node.lineno}"
                fmod, fnode = self._decl_site(project, t.model, f)
                yield self.finding(
                    "SD302", fmod or mod, fnode or t.node,
                    f"{t.model}.{t.fname} is searched (e.g. '{t.op}' at "
                    f"{where}) but declared without index=True — every "
                    f"lookup seq-scans the table")

    @staticmethod
    def _x2many_hop(project: Project, model: str | None,
                    path: str) -> str | None:
        """First x2many segment of a dotted domain path, or None."""
        cur = model
        for seg in path.split("."):
            fdecl = project.field(cur, seg)
            if fdecl:
                if fdecl.ftype in X2MANY:
                    return seg
                cur = fdecl.comodel if fdecl.ftype == "Many2one" else None
            else:
                if seg.endswith("_ids"):  # unresolvable: name heuristic
                    return seg
                cur = None
        return None

    @staticmethod
    def _decl_site(
            project: Project, model: str | None, fdecl: FieldDecl,
    ) -> tuple[ModuleCtx | None, ast.Call | None]:
        for mod in project.modules:
            for klass in mod.models:
                if klass.model == model and \
                        klass.fields.get(fdecl.name) is fdecl:
                    return mod, fdecl.node
        return None, None
