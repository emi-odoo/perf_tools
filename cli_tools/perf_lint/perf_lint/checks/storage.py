"""SD40x — storage and recompute amplification."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator

from ..constants import X2MANY
from ..model import Finding, ModelClass, ModuleCtx, Project
from ..registry import Checker, register

if TYPE_CHECKING:
    from ..runner import Config


@register
class StorageChecker(Checker):
    codes = {
        "SD401": ("binary-in-table", "warning",
                  "fields.Binary(attachment=False) bloats the table"),
        "SD402": ("stored-compute-x2many-depends", "error",
                  "stored compute depends through a x2many — recompute "
                  "grenade"),
        "SD403": ("stored-related-x2many", "error",
                  "stored related= path traverses a x2many — recompute "
                  "grenade"),
    }
    explain = {
        "SD401": "attachment=False stores binaries in the table/TOAST: "
                 "backups balloon and every seq scan walks the bloat. The "
                 "default attachment=True uses the filestore. Flipping it "
                 "back needs a data migration or existing values silently "
                 "vanish.",
        "SD402": "store=True + @api.depends through a One2many/Many2many "
                 "means one change on ANY related record marks the whole "
                 "family for recompute: creating 1 ticket for a partner "
                 "with 1093 tickets rewrites 1094 rows. In a create loop "
                 "that is O(N²) row rewrites. Don't store it, or "
                 "aggregate on the other model.",
        "SD403": "related='...' with store=True is a compute the framework "
                 "writes for you — same recompute semantics as SD402. When "
                 "the related path crosses a One2many/Many2many, one write "
                 "on any related record re-derives and rewrites the field "
                 "on every sibling. Drop store=True (related fields "
                 "default to unstored) or aggregate on the other model.",
    }

    def check_module(self, mod: ModuleCtx, project: Project,
                     cfg: Config) -> Iterator[Finding]:
        if not cfg.enabled("SD401"):
            return
        for klass in mod.models:
            for f in klass.fields.values():
                if f.ftype == "Binary" and f.kw("attachment", True) is False:
                    yield self.finding(
                        "SD401", mod, f.node,
                        f"{klass.model or klass.class_name}.{f.name}: "
                        f"Binary(attachment=False) stores blobs in the "
                        f"table — TOAST bloat, fat backups, slow seq scans; "
                        f"use the default attachment=True (+ migration for "
                        f"existing rows)")

    def check_project(self, project: Project,
                      cfg: Config) -> Iterator[Finding]:
        if not (cfg.enabled("SD402") or cfg.enabled("SD403")):
            return
        for mod in project.modules:
            if mod.is_context:
                continue
            for klass in mod.models:
                yield from self._check_class(mod, klass, project, cfg)

    def _check_class(self, mod: ModuleCtx, klass: ModelClass,
                     project: Project, cfg: Config) -> Iterator[Finding]:
        for f in klass.fields.values():
            if f.kw("store", False) is not True:
                continue
            related = f.kw("related")
            if isinstance(related, str) and cfg.enabled("SD403"):
                hit = self._x2many_segment(project, klass.model, related)
                if hit:
                    yield self.finding(
                        "SD403", mod, f.node,
                        f"stored related '{f.name}' follows '{related}' — "
                        f"'{hit}' is a x2many, so one change on any related "
                        f"record rewrites the field on every sibling; drop "
                        f"store=True or aggregate on the other model")
                continue
            compute = f.kw("compute")
            if not isinstance(compute, str) or not cfg.enabled("SD402"):
                continue
            method = klass.methods.get(compute)
            for path in (method.depends if method else []):
                hit = self._x2many_segment(project, klass.model, path)
                if hit:
                    yield self.finding(
                        "SD402", mod, f.node,
                        f"stored compute '{f.name}' depends on '{path}' — "
                        f"'{hit}' is a x2many, so one change on any related "
                        f"record marks every sibling for recompute (write "
                        f"amplification, O(N²) under record-by-record "
                        f"imports); don't store it or move the "
                        f"aggregate to the other model")
                    break

    @staticmethod
    def _x2many_segment(project: Project, model: str | None,
                        path: str) -> str | None:
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
