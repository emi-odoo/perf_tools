"""End-to-end tests: bad code in, expected findings out.

Each test writes a small fake addon into a temp directory, runs the real
lint() pipeline over it (parsing → scanning → checkers → noqa filtering)
and asserts the finding codes. The fixtures double as living documentation
of what each check catches — and, just as important, what it must NOT flag.

Run from the pyproject directory:
    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from perf_lint import Config, lint  # noqa: E402
from perf_lint.cli import main  # noqa: E402

HEADER = "from odoo import api, fields, models\n\n\n"


def lint_sources(sources, select=None, ignore=None):
    """Write {filename: source} into a temp dir and lint the directory.

    Returns (findings, n_suppressed)."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, src in sources.items():
            src = textwrap.dedent(src)
            compile(src, name, "exec")  # broken fixture must fail the test
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(src)
        return lint([tmp], [], Config(select, ignore))


def lint_model(body, **kw):
    """Lint one file holding a single my.ticket model with `body` inside."""
    src = HEADER + (
        'class Ticket(models.Model):\n'
        '    _name = "my.ticket"\n\n'
        '    name = fields.Char(index=True)\n'
        '    partner_id = fields.Many2one("res.partner")\n\n'
        + textwrap.indent(textwrap.dedent(body), "    ")
    )
    return lint_sources({"models.py": src}, **kw)


def codes(findings):
    return [f.code for f in findings]


class LoopCheckTests(unittest.TestCase):
    """SD10x — a query per iteration instead of one batch."""

    def test_sd101_query_in_loop(self):
        findings, _ = lint_model("""
            def slow(self):
                for rec in self:
                    self.env["res.partner"].search([("id", "=", rec.id)])
        """)
        self.assertEqual(codes(findings), ["SD101"])
        self.assertEqual(findings[0].severity, "error")

    def test_sd101_lambda_in_filtered_counts_as_loop(self):
        # a lambda passed to filtered() runs once per record
        findings, _ = lint_model("""
            def slow(self):
                return self.filtered(
                    lambda r: self.env["res.partner"].search(
                        [("id", "=", r.id)]))
        """)
        self.assertEqual(codes(findings), ["SD101"])

    def test_sd102_write_in_loop(self):
        findings, _ = lint_model("""
            def tag_all(self):
                for rec in self:
                    rec.write({"name": "x"})
        """)
        self.assertEqual(codes(findings), ["SD102"])

    def test_sd102_chunked_batch_create_is_clean(self):
        # create([listcomp]) per iteration is chunked batching, not N+1
        findings, _ = lint_model("""
            def import_batches(self, batches):
                for batch in batches:
                    self.env["my.ticket"].create(
                        [{"name": n} for n in batch])
        """)
        self.assertEqual(codes(findings), [])

    def test_sd103_query_in_compute(self):
        findings, _ = lint_model("""
            count = fields.Integer(compute="_compute_count")

            @api.depends("partner_id")
            def _compute_count(self):
                for rec in self:
                    rec.count = self.env["my.ticket"].search_count(
                        [("partner_id", "=", rec.partner_id.id)])
        """)
        self.assertEqual(codes(findings), ["SD103"])

    def test_sd103_falls_back_to_sd101_when_disabled(self):
        findings, _ = lint_model("""
            count = fields.Integer(compute="_compute_count")

            @api.depends("partner_id")
            def _compute_count(self):
                for rec in self:
                    rec.count = self.env["my.ticket"].search_count(
                        [("partner_id", "=", rec.partner_id.id)])
        """, ignore=["SD103"])
        self.assertEqual(codes(findings), ["SD101"])

    def test_sd104_query_in_create_override(self):
        findings, _ = lint_model("""
            def create(self, vals_list):
                records = super().create(vals_list)
                for rec in records:
                    rec.code = self.env["ir.sequence"].next_by_code(
                        "my.ticket")
                return records
        """)
        self.assertEqual(codes(findings), ["SD104"])
        self.assertIn("next_by_code", findings[0].message)

    def test_sd105_uniqueness_check_in_constrains(self):
        findings, _ = lint_model("""
            @api.constrains("name")
            def _check_unique_name(self):
                for rec in self:
                    if self.search_count([("name", "=", rec.name),
                                          ("id", "!=", rec.id)]):
                        raise ValueError("duplicate name")
        """)
        self.assertEqual(codes(findings), ["SD105"])
        # ('id', '!=', ...) in the domain = hand-rolled uniqueness check
        self.assertIn("models.Constraint", findings[0].message)

    def test_sd106_raw_sql_in_loop_is_info_only(self):
        findings, _ = lint_model("""
            def _archive_old(self):
                while True:
                    self.env.cr.execute("UPDATE my_ticket SET active=false")
        """)
        self.assertEqual(codes(findings), ["SD106"])
        self.assertEqual(findings[0].severity, "info")

    def test_sd107_invalidate_in_loop(self):
        findings, _ = lint_model("""
            def refresh(self):
                for rec in self:
                    self.env.invalidate_all()
        """)
        self.assertEqual(codes(findings), ["SD107"])
        self.assertEqual(findings[0].severity, "warning")

    def test_sd107_flush_outside_loop_is_clean(self):
        findings, _ = lint_model("""
            def refresh(self):
                self.env.flush_all()
        """)
        self.assertEqual(codes(findings), [])

    def test_sd106_module_level_migration_function(self):
        # module-level functions (migration scripts) are scanned too
        findings, _ = lint_sources({"migrate.py": """
            def migrate(cr, version):
                for chunk in range(10):
                    cr.execute("UPDATE my_ticket SET x=1 WHERE id > %s",
                               (chunk,))
        """})
        self.assertEqual(codes(findings), ["SD106"])


class OrmCheckTests(unittest.TestCase):
    """SD20x — asking the ORM the wrong way."""

    def test_sd201_unbounded_search_all(self):
        findings, _ = lint_model("""
            def load_all(self):
                return self.env["res.partner"].search([])
        """)
        self.assertEqual(codes(findings), ["SD201"])
        self.assertEqual(findings[0].severity, "warning")

    def test_sd201_limit_makes_it_clean(self):
        findings, _ = lint_model("""
            def load_some(self):
                return self.env["res.partner"].search([], limit=100)
        """)
        self.assertEqual(codes(findings), [])

    def test_sd202_filtered_after_search(self):
        findings, _ = lint_model("""
            def actives(self):
                partners = self.env["res.partner"].search([("id", ">", 1)])
                return partners.filtered(lambda p: p.active)
        """)
        self.assertEqual(codes(findings), ["SD202"])

    def test_sd203_len_of_search(self):
        findings, _ = lint_model("""
            def count_named(self):
                return len(self.search([("name", "!=", False)]))
        """)
        self.assertEqual(codes(findings), ["SD203"])

    def test_sd204_sorted_after_search(self):
        findings, _ = lint_model("""
            def newest(self):
                tickets = self.env["my.ticket"].search(
                    [("name", "!=", False)])
                return tickets.sorted("name")
        """)
        self.assertEqual(codes(findings), ["SD204"])
        self.assertEqual(findings[0].severity, "info")

    def test_sd204_sorted_on_plain_recordset_is_clean(self):
        findings, _ = lint_model("""
            def by_name(self):
                return self.sorted("name")
        """)
        self.assertEqual(codes(findings), [])

    def test_sd205_search_as_truth_test(self):
        findings, _ = lint_model("""
            def has_named(self):
                if self.search([("name", "=", "x")]):
                    return True
                return False
        """)
        self.assertEqual(codes(findings), ["SD205"])
        self.assertEqual(findings[0].severity, "warning")

    def test_sd205_limit_1_is_clean(self):
        findings, _ = lint_model("""
            def has_named(self):
                if self.search([("name", "=", "x")], limit=1):
                    return True
                return False
        """)
        self.assertEqual(codes(findings), [])

    def test_sd205_result_kept_in_a_variable_is_not_flagged(self):
        # conservative: the recordset is bound and may be used afterwards
        # (but the [0] on the unlimited search is SD206's business)
        findings, _ = lint_model("""
            def first_named(self):
                records = self.search([("name", "=", "x")])
                if records:
                    return records[0]
        """)
        self.assertEqual(codes(findings), ["SD206"])

    def test_sd206_index_zero_on_search(self):
        findings, _ = lint_model("""
            def first(self):
                return self.search([("name", "=", "x")])[0]
        """)
        self.assertEqual(codes(findings), ["SD206"])
        self.assertEqual(findings[0].severity, "warning")
        self.assertIn("limit=1", findings[0].message)

    def test_sd206_head_slice_on_search(self):
        findings, _ = lint_model("""
            def first_five(self):
                return self.search([("name", "=", "x")])[:5]
        """)
        self.assertEqual(codes(findings), ["SD206"])
        self.assertIn("limit=5", findings[0].message)

    def test_sd206_through_variable(self):
        findings, _ = lint_model("""
            def first(self):
                records = self.search([("name", "=", "x")])
                return records[:1]
        """)
        self.assertEqual(codes(findings), ["SD206"])

    def test_sd206_limited_search_is_clean(self):
        findings, _ = lint_model("""
            def first(self):
                return self.search([("name", "=", "x")], limit=1)[0]
        """)
        self.assertEqual(codes(findings), [])

    def test_sd206_limited_variable_is_clean(self):
        findings, _ = lint_model("""
            def first(self):
                records = self.search([("name", "=", "x")], limit=5)
                return records[0]
        """)
        self.assertEqual(codes(findings), [])

    def test_sd206_plain_recordset_subscript_is_clean(self):
        findings, _ = lint_model("""
            def first(self):
                return self[0]
        """)
        self.assertEqual(codes(findings), [])

    def test_sd207_sum_over_mapped_search(self):
        findings, _ = lint_model("""
            def total(self):
                lines = self.search([("name", "=", "x")])
                return sum(lines.mapped("amount"))
        """)
        self.assertEqual(codes(findings), ["SD207"])
        self.assertEqual(findings[0].severity, "warning")
        self.assertIn("read_group", findings[0].message)

    def test_sd207_max_over_chained_search(self):
        findings, _ = lint_model("""
            def newest(self):
                return max(self.search(
                    [("name", "=", "x")]).mapped("amount"))
        """)
        self.assertEqual(codes(findings), ["SD207"])

    def test_sd207_mapped_on_plain_recordset_is_clean(self):
        # self is the already-loaded batch: no extra fetch to save
        findings, _ = lint_model("""
            def total(self):
                return sum(self.mapped("amount"))
        """)
        self.assertEqual(codes(findings), [])

    def test_sd207_mapped_lambda_is_clean(self):
        # a lambda cannot be expressed as a read_group aggregate
        findings, _ = lint_model("""
            def total(self):
                lines = self.search([("name", "=", "x")])
                return sum(lines.mapped(lambda r: 1))
        """)
        self.assertEqual(codes(findings), [])


class IndexCheckTests(unittest.TestCase):
    """SD30x — indexing."""

    def test_sd301_many2one_index_disabled(self):
        findings, _ = lint_model("""
            company_id = fields.Many2one("res.company", index=False)
        """)
        self.assertEqual(codes(findings), ["SD301"])

    def test_sd302_searched_field_without_index_cross_file(self):
        findings, _ = lint_sources({
            "order.py": """
                from odoo import fields, models


                class Order(models.Model):
                    _name = "my.order"

                    ref = fields.Char()
            """,
            "service.py": """
                from odoo import models


                class OrderService(models.Model):
                    _inherit = "my.order"

                    def find_by_ref(self, code):
                        return self.env["my.order"].search(
                            [("ref", "=", code)])
            """,
        })
        self.assertEqual(codes(findings), ["SD302"])
        # the finding anchors at the field DECLARATION, not the search
        self.assertTrue(findings[0].path.endswith("order.py"))

    def test_sd302_indexed_field_is_clean(self):
        findings, _ = lint_sources({"order.py": """
            from odoo import fields, models


            class Order(models.Model):
                _name = "my.order"

                ref = fields.Char(index=True)

                def find_by_ref(self, code):
                    return self.env["my.order"].search([("ref", "=", code)])
        """})
        self.assertEqual(codes(findings), [])

    def test_sd302_skips_low_cardinality_selection(self):
        findings, _ = lint_sources({"order.py": """
            from odoo import fields, models


            class Order(models.Model):
                _name = "my.order"

                state = fields.Selection([("a", "A"), ("b", "B")])

                def drafts(self):
                    return self.env["my.order"].search(
                        [("state", "=", "a")], limit=80)
        """})
        self.assertEqual(codes(findings), [])

    def test_sd303_ilike_domain(self):
        findings, _ = lint_model("""
            def fuzzy(self, term):
                return self.env["my.ticket"].search([("name", "ilike", term)])
        """)
        self.assertEqual(codes(findings), ["SD303"])
        self.assertEqual(findings[0].severity, "info")

    def test_sd304_dotted_domain_through_one2many(self):
        findings, _ = lint_model("""
            line_ids = fields.One2many("my.line", "ticket_id")

            def done(self):
                return self.search([("line_ids.state", "=", "done")],
                                   limit=5)
        """)
        self.assertEqual(codes(findings), ["SD304"])
        self.assertEqual(findings[0].severity, "info")

    def test_sd304_dotted_through_many2one_is_clean(self):
        findings, _ = lint_model("""
            def by_partner_name(self):
                return self.search([("partner_id.name", "=", "x")],
                                   limit=5)
        """)
        self.assertEqual(codes(findings), [])

    def test_sd305_order_on_unindexed_field(self):
        findings, _ = lint_sources({"order.py": """
            from odoo import fields, models


            class Order(models.Model):
                _name = "my.order"
                _order = "ref desc, id"

                ref = fields.Char()
        """})
        self.assertEqual(codes(findings), ["SD305"])
        self.assertEqual(findings[0].severity, "warning")
        self.assertIn("'ref'", findings[0].message)

    def test_sd305_indexed_order_field_is_clean(self):
        findings, _ = lint_sources({"order.py": """
            from odoo import fields, models


            class Order(models.Model):
                _name = "my.order"
                _order = "ref desc, id"

                ref = fields.Char(index=True)
        """})
        self.assertEqual(codes(findings), [])

    def test_sd305_unknown_base_field_is_clean(self):
        # create_date is not declared in the linted source: stay quiet
        findings, _ = lint_sources({"order.py": """
            from odoo import fields, models


            class Order(models.Model):
                _name = "my.order"
                _order = "create_date desc, id"

                ref = fields.Char()
        """})
        self.assertEqual(codes(findings), [])

    def test_sd305_order_field_declared_in_other_file(self):
        # cross-file: _order in the inheriting class, field in the base
        findings, _ = lint_sources({
            "order.py": """
                from odoo import fields, models


                class Order(models.Model):
                    _name = "my.order"

                    ref = fields.Char()
            """,
            "sorted.py": """
                from odoo import models


                class OrderSorted(models.Model):
                    _inherit = "my.order"
                    _order = "ref"
            """,
        })
        self.assertEqual(codes(findings), ["SD305"])
        # the finding anchors at the _order declaration
        self.assertTrue(findings[0].path.endswith("sorted.py"))

    def test_sd303_trigram_index_is_clean(self):
        findings, _ = lint_sources({"tag.py": """
            from odoo import fields, models


            class Tag(models.Model):
                _name = "my.tag"

                name = fields.Char(index="trigram")

                def fuzzy(self, term):
                    return self.env["my.tag"].search(
                        [("name", "ilike", term)], limit=10)
        """})
        self.assertEqual(codes(findings), [])


class StorageCheckTests(unittest.TestCase):
    """SD40x — storage and recompute amplification."""

    def test_sd401_binary_in_table(self):
        findings, _ = lint_model("""
            photo = fields.Binary(attachment=False)
        """)
        self.assertEqual(codes(findings), ["SD401"])

    def test_sd401_default_attachment_is_clean(self):
        findings, _ = lint_model("""
            photo = fields.Binary()
        """)
        self.assertEqual(codes(findings), [])

    def test_sd402_stored_compute_through_one2many(self):
        findings, _ = lint_sources({"partner.py": """
            from odoo import api, fields, models


            class Partner(models.Model):
                _name = "my.partner"

                ticket_ids = fields.One2many("my.ticket", "partner_id")
                ticket_count = fields.Integer(
                    compute="_compute_ticket_count", store=True)

                @api.depends("ticket_ids")
                def _compute_ticket_count(self):
                    for rec in self:
                        rec.ticket_count = len(rec.ticket_ids)
        """})
        self.assertEqual(codes(findings), ["SD402"])
        self.assertEqual(findings[0].severity, "error")

    def test_sd402_ids_name_heuristic_without_registry(self):
        # line_ids is never declared: falls back to the *_ids name heuristic
        findings, _ = lint_model("""
            total = fields.Float(compute="_compute_total", store=True)

            @api.depends("line_ids.amount")
            def _compute_total(self):
                for rec in self:
                    rec.total = 0
        """)
        self.assertEqual(codes(findings), ["SD402"])

    def test_sd403_stored_related_through_x2many(self):
        findings, _ = lint_model("""
            line_ids = fields.One2many("my.line", "ticket_id")
            first_amount = fields.Float(
                related="line_ids.amount", store=True)
        """)
        self.assertEqual(codes(findings), ["SD403"])
        self.assertEqual(findings[0].severity, "error")
        self.assertIn("line_ids", findings[0].message)

    def test_sd403_unstored_related_is_clean(self):
        findings, _ = lint_model("""
            line_ids = fields.One2many("my.line", "ticket_id")
            first_amount = fields.Float(related="line_ids.amount")
        """)
        self.assertEqual(codes(findings), [])

    def test_sd403_stored_related_through_many2one_is_clean(self):
        findings, _ = lint_model("""
            partner_name = fields.Char(
                related="partner_id.name", store=True)
        """)
        self.assertEqual(codes(findings), [])

    def test_sd402_unstored_compute_is_clean(self):
        findings, _ = lint_sources({"partner.py": """
            from odoo import api, fields, models


            class Partner(models.Model):
                _name = "my.partner"

                ticket_ids = fields.One2many("my.ticket", "partner_id")
                ticket_count = fields.Integer(
                    compute="_compute_ticket_count")

                @api.depends("ticket_ids")
                def _compute_ticket_count(self):
                    for rec in self:
                        rec.ticket_count = len(rec.ticket_ids)
        """})
        self.assertEqual(codes(findings), [])


class SuppressionTests(unittest.TestCase):
    def test_noqa_with_code(self):
        findings, n_suppressed = lint_model("""
            def load_all(self):
                return self.env["res.partner"].search([])  # noqa: SD201
        """)
        self.assertEqual(codes(findings), [])
        self.assertEqual(n_suppressed, 1)

    def test_bare_noqa_suppresses_everything(self):
        findings, n_suppressed = lint_model("""
            def load_all(self):
                return self.env["res.partner"].search([])  # noqa
        """)
        self.assertEqual(codes(findings), [])
        self.assertEqual(n_suppressed, 1)

    def test_noqa_for_another_code_does_not_suppress(self):
        findings, n_suppressed = lint_model("""
            def load_all(self):
                return self.env["res.partner"].search([])  # noqa: SD999
        """)
        self.assertEqual(codes(findings), ["SD201"])
        self.assertEqual(n_suppressed, 0)

    def test_noqa_on_any_physical_line_of_the_call(self):
        findings, n_suppressed = lint_model("""
            def load_all(self):
                return self.env["res.partner"].search(
                    [],
                    order="name",  # noqa: SD201
                )
        """)
        self.assertEqual(codes(findings), [])
        self.assertEqual(n_suppressed, 1)

    def test_skip_file_pragma(self):
        findings, n_suppressed = lint_sources({"skipped.py": """\
            # perf-lint: skip-file
            from odoo import models


            class Junk(models.Model):
                _name = "my.junk"

                def load_all(self):
                    return self.env["res.partner"].search([])
        """})
        self.assertEqual(codes(findings), [])
        self.assertEqual(n_suppressed, 0)


class ConfigToggleTests(unittest.TestCase):
    BODY = """
        def both(self):
            self.env["res.partner"].search([])
            for rec in self:
                self.env["res.partner"].search([("id", "=", rec.id)])
    """

    def test_all_enabled_by_default(self):
        findings, _ = lint_model(self.BODY)
        self.assertEqual(sorted(codes(findings)), ["SD101", "SD201"])

    def test_select_prefix_enables_exclusively(self):
        findings, _ = lint_model(self.BODY, select=["SD2"])
        self.assertEqual(codes(findings), ["SD201"])

    def test_ignore_prefix_disables_family(self):
        findings, _ = lint_model(self.BODY, ignore=["SD1"])
        self.assertEqual(codes(findings), ["SD201"])


class CleanCodeTests(unittest.TestCase):
    def test_well_written_module_has_zero_findings(self):
        findings, n_suppressed = lint_sources({"good.py": """
            from odoo import fields, models


            class Ticket(models.Model):
                _name = "my.ticket"

                name = fields.Char(index=True)
                partner_id = fields.Many2one("res.partner")
                photo = fields.Binary()

                def open_named(self, names):
                    tickets = self.env["my.ticket"].search(
                        [("name", "in", names)], limit=1000)
                    tickets.write({"name": "seen"})
                    return self.env["my.ticket"].search_count(
                        [("name", "in", names)])
        """})
        self.assertEqual(codes(findings), [])
        self.assertEqual(n_suppressed, 0)


class AddonsPathTests(unittest.TestCase):
    """--addons-path: registry-only context, never linted.

    The tests below (no __manifest__.py anywhere) exercise the full-crawl
    fallback; DependencyResolutionTests cover the manifest-driven mode."""

    @staticmethod
    def _write(directory, name, src):
        with open(os.path.join(directory, name), "w",
                  encoding="utf-8") as fh:
            fh.write(textwrap.dedent(src))

    def test_context_resolves_field_searched_from_linted_code(self):
        # the field declaration lives in the context tree: SD302 fires but
        # anchors at the search site (the declaration is not ours to edit)
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as tmp:
            self._write(ctx, "order.py", """
                from odoo import fields, models


                class Order(models.Model):
                    _name = "ctx.order"

                    ref = fields.Char()
            """)
            self._write(tmp, "service.py", """
                from odoo import models


                class Service(models.Model):
                    _inherit = "ctx.order"

                    def find(self, code):
                        return self.env["ctx.order"].search(
                            [("ref", "=", code)])
            """)
            findings, _ = lint([tmp], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])
            self.assertTrue(findings[0].path.endswith("service.py"))

    def test_context_files_are_never_reported(self):
        # blatant findings inside the context tree stay silent
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as tmp:
            self._write(ctx, "bad.py", """
                from odoo import fields, models


                class Bad(models.Model):
                    _name = "ctx.bad"

                    photo = fields.Binary(attachment=False)

                    def load_all(self):
                        return self.env["ctx.bad"].search([])
            """)
            self._write(tmp, "clean.py", """
                from odoo import models


                class Clean(models.Model):
                    _inherit = "ctx.bad"
            """)
            findings, _ = lint([tmp], [], Config(), [ctx])
            self.assertEqual(codes(findings), [])

    def test_context_resolves_x2many_for_sd403(self):
        # 'line_set' has no *_ids suffix: only the context registry can
        # reveal it is a One2many
        ctx_src = """
            from odoo import fields, models


            class Partner(models.Model):
                _name = "ctx.partner"

                line_set = fields.One2many("ctx.line", "partner_id")
        """
        linted_src = """
            from odoo import fields, models


            class PartnerExt(models.Model):
                _inherit = "ctx.partner"

                first_amount = fields.Float(
                    related="line_set.amount", store=True)
        """
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as tmp:
            self._write(ctx, "partner.py", ctx_src)
            self._write(tmp, "ext.py", linted_src)
            without_ctx, _ = lint([tmp], [], Config())
            self.assertEqual(codes(without_ctx), [])
            with_ctx, _ = lint([tmp], [], Config(), [ctx])
            self.assertEqual(codes(with_ctx), ["SD403"])
            self.assertTrue(with_ctx[0].path.endswith("ext.py"))


class DependencyResolutionTests(unittest.TestCase):
    """--addons-path with manifests: only transitive `depends` are parsed."""

    # a context addon declaring one model with one unindexed Char
    CTX_MODEL = """
        from odoo import fields, models


        class M(models.Model):
            _name = "{model}"

            {field} = fields.Char()
    """
    # a linted addon searching that field (SD302 bait when resolvable)
    SEARCHER = """
        from odoo import models


        class S(models.Model):
            _name = "target.model"

            def find(self, code):
                return self.env["{model}"].search(
                    [("{field}", "=", code)])
    """

    @staticmethod
    def _addon(root, name, depends=(), files=None):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "__manifest__.py"), "w",
                  encoding="utf-8") as fh:
            fh.write(repr({"name": name, "depends": list(depends)}))
        for fname, src in (files or {}).items():
            src = textwrap.dedent(src)
            compile(src, fname, "exec")  # broken fixture must fail the test
            with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
                fh.write(src)
        return d

    def _ctx_addon(self, root, name, model, depends=()):
        return self._addon(root, name, depends, {
            "models.py": self.CTX_MODEL.format(model=model, field="foo")})

    def test_only_declared_dependencies_are_parsed(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "dep_a", "ctx.a")
            self._ctx_addon(ctx, "unrelated", "ctx.u")
            target = self._addon(work, "my_addon", ["dep_a"], {
                "models.py": (
                    textwrap.dedent(
                        self.SEARCHER.format(model="ctx.a", field="foo"))
                    + textwrap.dedent("""
                        class S2(models.Model):
                            _name = "target.other"

                            def find_u(self, code):
                                return self.env["ctx.u"].search(
                                    [("foo", "=", code)])
                    """)),
            })
            findings, _ = lint([target], [], Config(), [ctx])
            # ctx.a.foo resolved via dep_a -> SD302; ctx.u.foo stays
            # unknown because `unrelated` is not a dependency
            self.assertEqual(codes(findings), ["SD302"])
            self.assertIn("ctx.a.foo", findings[0].message)

    def test_transitive_dependencies_are_parsed(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "dep_a", "ctx.a", depends=["dep_b"])
            self._ctx_addon(ctx, "dep_b", "ctx.b")
            target = self._addon(work, "my_addon", ["dep_a"], {
                "models.py": self.SEARCHER.format(model="ctx.b",
                                                  field="foo")})
            findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])

    def test_missing_dependency_warns_on_stderr(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "dep_a", "ctx.a")
            target = self._addon(work, "my_addon", ["dep_a", "ghost"], {
                "models.py": self.SEARCHER.format(model="ctx.a",
                                                  field="foo")})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])
            self.assertIn("'ghost' not found", err.getvalue())

    def test_base_is_an_implicit_dependency(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "base", "res.thing")
            target = self._addon(work, "my_addon", [], {
                "models.py": self.SEARCHER.format(model="res.thing",
                                                  field="foo")})
            findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])

    def test_sibling_addons_of_the_target_resolve(self):
        # deps living NEXT TO the target (custom/OCA folder) are found
        # even though their directory is not in --addons-path
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(work, "sibling_dep", "ctx.s")
            target = self._addon(work, "my_addon", ["sibling_dep"], {
                "models.py": self.SEARCHER.format(model="ctx.s",
                                                  field="foo")})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])
            self.assertNotIn("not found", err.getvalue())

    def test_sibling_addon_shadows_context_addon_with_same_name(self):
        # the local copy of a module wins over one in the explicit trees
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            # context copy declares the field WITH an index...
            self._addon(ctx, "dep_a", [], {"models.py": """
                from odoo import fields, models


                class M(models.Model):
                    _name = "ctx.a"

                    foo = fields.Char(index=True)
            """})
            # ...the local sibling copy does not: it must be the one used
            self._ctx_addon(work, "dep_a", "ctx.a")
            target = self._addon(work, "my_addon", ["dep_a"], {
                "models.py": self.SEARCHER.format(model="ctx.a",
                                                  field="foo")})
            findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])

    def test_addon_nested_in_a_sibling_project_is_ignored(self):
        # the target's parent may hold OTHER projects: an addon buried
        # inside one must neither resolve a dependency nor shadow the
        # explicit --addons-path trees
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            # dep_a exists in the explicit context WITH an index...
            self._addon(ctx, "dep_a", [], {"models.py": """
                from odoo import fields, models


                class M(models.Model):
                    _name = "ctx.a"

                    foo = fields.Char(index=True)
            """})
            # ...and a stale copy (no index) sits nested in another
            # project under the same parent as the target
            self._ctx_addon(os.path.join(work, "other_project"),
                            "dep_a", "ctx.a")
            # a genuinely-nested dependency is not found either
            self._ctx_addon(os.path.join(work, "other_project"),
                            "nested_dep", "ctx.n")
            target = self._addon(work, "my_addon",
                                 ["dep_a", "nested_dep"], {
                "models.py": self.SEARCHER.format(model="ctx.a",
                                                  field="foo")})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                findings, _ = lint([target], [], Config(), [ctx])
            # the indexed ctx copy of dep_a wins: foo is indexed -> clean
            self.assertEqual(codes(findings), [])
            self.assertIn("'nested_dep' not found", err.getvalue())

    def test_project_config_declares_local_addons_paths(self):
        # mirrors a real project: odools.toml at the root declares group
        # folders (addons/community, addons/edi); deps of the linted
        # addons/edi/* live in addons/community, NOT next to the targets
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            proj = os.path.join(work, "proj")
            community = os.path.join(proj, "addons", "community")
            edi = os.path.join(proj, "addons", "edi")
            os.makedirs(proj)
            with open(os.path.join(proj, "odools.toml"), "w",
                      encoding="utf-8") as fh:
                fh.write(textwrap.dedent("""\
                    [[config]]
                    name = "proj"
                    odoo_path = "/opt/odoo"
                    addons_paths = [
                        "./addons/community",
                        "./addons/edi",
                        "/opt/odoo-addons/enterprise",
                    ]
                """))
            self._ctx_addon(community, "queue_job", "queue.job")
            target = self._addon(edi, "edi_account", ["queue_job"], {
                "models.py": self.SEARCHER.format(model="queue.job",
                                                  field="foo")})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])
            self.assertNotIn("not found", err.getvalue())

    def test_odoo_conf_declares_local_addons_paths(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            proj = os.path.join(work, "proj")
            os.makedirs(proj)
            with open(os.path.join(proj, "odoo.conf"), "w",
                      encoding="utf-8") as fh:
                fh.write("[options]\naddons_path = ./extra, /opt/gone\n")
            self._ctx_addon(os.path.join(proj, "extra"), "dep_a", "ctx.a")
            target = self._addon(os.path.join(proj, "mods"), "my_addon",
                                 ["dep_a"], {
                "models.py": self.SEARCHER.format(model="ctx.a",
                                                  field="foo")})
            with contextlib.redirect_stderr(io.StringIO()) as err:
                findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])
            self.assertNotIn("not found", err.getvalue())

    def test_toml_fallback_parser_without_tomllib(self):
        # 3.10 has no tomllib: the string-scraping fallback must agree
        from perf_lint.context import _toml_addons_paths
        text = ('[[config]]\nname = "x"\n'
                'addons_paths = [\n    "./a",\n    \'/opt/b\',\n]\n')
        with unittest.mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(_toml_addons_paths(text), ["./a", "/opt/b"])
        self.assertEqual(_toml_addons_paths(text), ["./a", "/opt/b"])

    def test_linting_a_subfolder_walks_up_to_the_manifest(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "dep_a", "ctx.a")
            self._ctx_addon(ctx, "unrelated", "ctx.u")
            target = self._addon(work, "my_addon", ["dep_a"])
            models_dir = os.path.join(target, "models")
            os.makedirs(models_dir)
            AddonsPathTests._write(
                models_dir, "service.py",
                self.SEARCHER.format(model="ctx.a", field="foo"))
            # lint only the subfolder: deps still come from ../__manifest__
            findings, _ = lint([models_dir], [], Config(), [ctx])
            self.assertEqual(codes(findings), ["SD302"])

    def test_linted_field_redefinition_wins_over_context(self):
        # the target re-declares ctx.a.foo WITH an index: no SD302 even
        # though the context declaration has none (dependency order)
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as work:
            self._ctx_addon(ctx, "dep_a", "ctx.a")
            target = self._addon(work, "my_addon", ["dep_a"], {
                "models.py": """
                    from odoo import fields, models


                    class A(models.Model):
                        _inherit = "ctx.a"

                        foo = fields.Char(index=True)

                        def find(self, code):
                            return self.env["ctx.a"].search(
                                [("foo", "=", code)])
                """})
            findings, _ = lint([target], [], Config(), [ctx])
            self.assertEqual(codes(findings), [])


class CliTests(unittest.TestCase):
    def _tmp_bad_addon(self, tmp):
        path = os.path.join(tmp, "models.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(HEADER + textwrap.dedent("""\
                class Ticket(models.Model):
                    _name = "my.ticket"

                    def load_all(self):
                        return self.env["res.partner"].search([])
                """))

    def test_exit_1_on_warning_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([tmp]), 1)
            self.assertIn("SD201", out.getvalue())

    def test_text_output_has_clickable_path_line_col(self):
        # `path:line:col` on one line = Ctrl/Cmd+clickable in IDE terminals
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main([tmp, "--fail-on", "never", "--no-color"])
            self.assertRegex(out.getvalue(), r"models\.py:\d+:\d+ SD201")

    def test_fail_on_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                # SD201 is a warning: raising the bar to error passes
                self.assertEqual(main([tmp, "--fail-on", "error"]), 0)
                self.assertEqual(main([tmp, "--fail-on", "never"]), 0)

    def test_json_output_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main([tmp, "--format", "json", "--fail-on", "never"])
            report = json.loads(out.getvalue())
            self.assertEqual(report["suppressed"], 0)
            [finding] = report["findings"]
            self.assertEqual(finding["code"], "SD201")
            self.assertEqual(finding["severity"], "warning")
            self.assertTrue(finding["path"].endswith("models.py"))

    def test_severity_filter_hides_other_classes(self):
        # SD201 is a warning: filtering to error hides it AND exit code
        # follows what is shown, not what was found
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([tmp, "--severity", "error"]), 0)
            self.assertNotIn("SD201", out.getvalue())

    def test_severity_filter_keeps_matching_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._tmp_bad_addon(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([tmp, "--severity", "warning"]), 1)
            self.assertIn("SD201", out.getvalue())

    def test_severity_unknown_value_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main([".", "--severity", "fatal"]), 2)
        self.assertIn("unknown severity", err.getvalue())

    def test_addons_path_flag_comma_separated(self):
        with tempfile.TemporaryDirectory() as ctx, \
                tempfile.TemporaryDirectory() as tmp:
            AddonsPathTests._write(ctx, "order.py", """
                from odoo import fields, models


                class Order(models.Model):
                    _name = "ctx.order"

                    ref = fields.Char()
            """)
            AddonsPathTests._write(tmp, "service.py", """
                from odoo import models


                class Service(models.Model):
                    _inherit = "ctx.order"

                    def find(self, code):
                        return self.env["ctx.order"].search(
                            [("ref", "=", code)])
            """)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main([tmp, "--addons-path", f"{ctx},{ctx}",
                      "--fail-on", "never"])
            self.assertIn("SD302", out.getvalue())

    def test_addons_path_expands_mid_word_tilde(self):
        # the shell only expands the FIRST ~ of "~/a,~/b"; the CLI must
        # expanduser every entry
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as tmp:
            ctx = os.path.join(home, "ctx")
            os.makedirs(ctx)
            AddonsPathTests._write(ctx, "order.py", """
                from odoo import fields, models


                class Order(models.Model):
                    _name = "ctx.order"

                    ref = fields.Char()
            """)
            AddonsPathTests._write(tmp, "service.py", """
                from odoo import models


                class Service(models.Model):
                    _inherit = "ctx.order"

                    def find(self, code):
                        return self.env["ctx.order"].search(
                            [("ref", "=", code)])
            """)
            with unittest.mock.patch.dict(os.environ, {"HOME": home}):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    main([tmp, "--addons-path", "~/ctx,~/ctx",
                          "--fail-on", "never"])
            self.assertIn("SD302", out.getvalue())

    def test_explain_unknown_code_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--explain", "SD999"]), 2)

    def test_explain_known_code(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--explain", "sd201"]), 0)
        self.assertIn("unbounded-search-all", out.getvalue())


if __name__ == "__main__":
    unittest.main()
