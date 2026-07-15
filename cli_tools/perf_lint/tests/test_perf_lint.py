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

    def test_explain_unknown_code_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--explain", "SD999"]), 2)

    def test_explain_known_code(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--explain", "sd201"]), 0)
        self.assertIn("unbounded-search-all", out.getvalue())


if __name__ == "__main__":
    unittest.main()
