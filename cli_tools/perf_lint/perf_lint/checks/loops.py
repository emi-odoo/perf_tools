"""SD10x — the N+1 family: a query per iteration instead of one batch."""
import ast

from ..astutils import const_str
from ..registry import Checker, register


def _id_exclusion_domain(call):
    """True if the call's literal domain contains ('id', '!='/'not in', ...)
    — the smell of a hand-rolled uniqueness check."""
    if not (call.args and isinstance(call.args[0], ast.List)):
        return False
    for term in call.args[0].elts:
        if isinstance(term, (ast.Tuple, ast.List)) and len(term.elts) == 3:
            if const_str(term.elts[0]) == "id" \
                    and const_str(term.elts[1]) in ("!=", "not in"):
                return True
    return False


@register
class LoopQueryChecker(Checker):
    codes = {
        "SD101": ("query-in-loop", "error",
                  "database query inside a loop (N+1)"),
        "SD102": ("write-in-loop", "error",
                  "record write inside a loop"),
        "SD103": ("query-in-compute", "error",
                  "per-record query in a compute method"),
        "SD104": ("query-in-create-write", "error",
                  "per-record work in a create()/write() override"),
        "SD105": ("query-in-constrains", "error",
                  "per-record query in an @api.constrains method"),
        "SD106": ("raw-sql-in-loop", "info",
                  "raw cr.execute() inside a loop"),
    }
    explain = {
        "SD101": "A search/read_group/next_by_code per loop iteration turns "
                 "one operation into N round-trips. Hoist it out: one "
                 "_read_group/search over the whole set, then a dict lookup "
                 "per record. (ANTIPATTERNS.md — the golden thread)",
        "SD102": "write()/create()/unlink() per iteration. The 19.0 ORM "
                 "batches the SQL via towrite, but you still pay Python "
                 "overhead per call and defeat no-op skipping when values "
                 "differ per record (bug #5). Assign to the recordset once, "
                 "or build vals for a single batched call.",
        "SD103": "Compute methods receive the WHOLE recordset — the "
                 "framework already batched for you; one query per record "
                 "un-batches it. Opening a list view fires it per row "
                 "(bugs #2/#8: 120 queries -> 2 with _read_group).",
        "SD104": "create() is @model_create_multi and write() always gets "
                 "the full recordset. Per-record queries inside turn one "
                 "batched call into N+1 (bugs #1/#9: 815 queries vs 18).",
        "SD105": "@api.constrains fires on every create/write. A per-record "
                 "search_count is a full scan per saved record — and if it "
                 "guards uniqueness it is also WRONG under concurrency: "
                 "only the database can guarantee unique. Use "
                 "models.Constraint('UNIQUE (col)') (bug #10).",
        "SD106": "Raw SQL in a loop is flagged for review only: chunked "
                 "batch processing (migrations, _commit_progress crons) is "
                 "legitimate — silence with `# noqa: SD106` — but a "
                 "per-record statement is an N+1 like any other.",
    }

    def _classify(self, ev):
        m = ev.method
        if m and m.constrains:
            return "SD105"
        if m and m.is_compute:
            return "SD103"
        if m and ev.klass and m.name in ("create", "write"):
            return "SD104"
        return "SD101" if ev.kind == "read" else "SD102"

    def check_module(self, mod, project, cfg):
        for ev in mod.query_events:
            if not ev.in_loop:
                continue
            if ev.fname == "create" and ev.batched:
                continue  # create([...]) in a loop = chunked batching
            if ev.fname == "execute":
                if cfg.enabled("SD106"):
                    yield self.finding(
                        "SD106", mod, ev.node,
                        f"raw cr.execute() inside a loop in "
                        f"{ev.method.name}() — fine if it processes a batch "
                        f"per iteration, an N+1 if it runs per record")
                continue
            specific = self._classify(ev)
            generic = "SD101" if ev.kind == "read" else "SD102"
            code = next((c for c in (specific, generic) if cfg.enabled(c)),
                        None)
            if not code:
                continue
            where = ev.method.name if ev.method else "module level"
            msg = (f"{ev.fname}() runs once per iteration in {where}()"
                   f"{self._context(specific)}")
            if ev.fname == "next_by_code":
                msg += ("; next_by_code() costs 2 queries per call and has "
                        "no batch API — consider assigning references from "
                        "id after create (bug #1)")
            elif specific == "SD105" and _id_exclusion_domain(ev.node):
                msg += ("; this looks like a hand-rolled uniqueness check — "
                        "it is slow AND race-unsafe; declare "
                        "models.Constraint('UNIQUE (...)') instead (bug #10)")
            yield self.finding(code, mod, ev.node, msg)

    @staticmethod
    def _context(code):
        return {
            "SD103": " — computes are batched over the whole recordset; "
                     "batch with one _read_group instead (bugs #2/#8)",
            "SD104": " — create/write receive the full batch; one query "
                     "here means N+1 per call (bugs #1/#9)",
            "SD105": " — constraints fire on every save (bug #10)",
            "SD101": " — hoist one batched query out of the loop",
            "SD102": " — assign to the whole recordset / build one batch",
        }.get(code, "")
