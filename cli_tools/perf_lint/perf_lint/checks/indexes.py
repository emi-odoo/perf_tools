"""SD30x — indexing."""
from ..constants import INDEXABLE_OPS, LOW_CARDINALITY, X2MANY
from ..registry import Checker, register


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
    }

    def check_module(self, mod, project, cfg):
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

    def check_project(self, project, cfg):
        seen = set()
        for mod in project.modules:
            for t in mod.domain_terms:
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
                if t.fname in project.unique_cols.get(t.model, set()):
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
    def _decl_site(project, model, fdecl):
        for mod in project.modules:
            for klass in mod.models:
                if klass.model == model and \
                        klass.fields.get(fdecl.name) is fdecl:
                    return mod, fdecl.node
        return None, None
