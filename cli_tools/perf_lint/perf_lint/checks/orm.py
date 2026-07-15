"""SD20x — asking the ORM the wrong way."""
from ..registry import Checker, register


@register
class OrmMisuseChecker(Checker):
    codes = {
        "SD201": ("unbounded-search-all", "warning",
                  "search([]) loads every record of the model"),
        "SD202": ("filtered-after-search", "warning",
                  ".filtered() on a search() result — filter in the domain"),
        "SD203": ("len-search", "warning",
                  "len(search(...)) — use search_count() / _read_group"),
    }
    explain = {
        "SD201": "search([]) with no domain and no limit loads the whole "
                 "table into memory. Push the condition into the domain — "
                 "the DB is a librarian: ask for the shelf, don't carry the "
                 "library home (bug #5). noqa it for known-tiny tables.",
        "SD202": "Fetching rows and then filtering in Python does the "
                 "database's job in the app server, with all rows loaded "
                 "for nothing. Move the predicate into the search domain "
                 "(bug #5).",
        "SD203": "len(search(...)) browses every matching record just to "
                 "count them. search_count() issues a single SELECT "
                 "count(*); _read_group when counting for many parents "
                 "(bug #2).",
    }

    def check_module(self, mod, project, cfg):
        for ev in mod.query_events:
            if ev.empty_domain and cfg.enabled("SD201"):
                model = f" of {ev.model}" if ev.model else ""
                yield self.finding(
                    "SD201", mod, ev.node,
                    f"{ev.fname}([]) loads every record{model}; add a "
                    f"domain or limit= (bug #5) — noqa for known-small "
                    f"tables")
        if cfg.enabled("SD202"):
            for node, klass, method in mod.filtered_after_search:
                yield self.finding(
                    "SD202", mod, node,
                    ".filtered() on a search() result — push the predicate "
                    "into the search domain so the DB does the filtering "
                    "(bug #5)")
        if cfg.enabled("SD203"):
            for node, klass, method in mod.len_search:
                yield self.finding("SD203", mod, node)
