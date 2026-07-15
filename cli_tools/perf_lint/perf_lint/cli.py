"""Command line interface."""
from __future__ import annotations

import argparse
import sys

from .constants import SEVERITIES, SEV_RANK
from .output import explain, list_checks, render_json, render_text
from .runner import Config, lint, load_plugin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="perf_lint",
        description="Static analysis for Odoo ORM performance anti-patterns ",
        epilog="Codes support prefix matching: --ignore SD3 disables all "
               "SD3xx checks. Inline: `# noqa: SD201`.",
    )
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories to lint (default: .)")
    parser.add_argument("--select", default="",
                        help="comma-separated code prefixes to enable "
                             "exclusively")
    parser.add_argument("--ignore", default="",
                        help="comma-separated code prefixes to disable")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="GLOB", help="path patterns to skip "
                        "(substring or glob, repeatable)")
    parser.add_argument("--plugin", action="append", default=[],
                        metavar="FILE", help="python file with extra "
                        "@register'ed checkers (repeatable)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--fail-on", choices=SEVERITIES + ("never",),
                        default="warning",
                        help="minimum severity that causes exit code 1 "
                             "(default: warning)")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--list-checks", action="store_true")
    parser.add_argument("--explain", metavar="CODE",
                        help="describe a check and its fix, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for p in args.plugin:
        load_plugin(p)

    cfg = Config(
        [s.strip().upper() for s in args.select.split(",") if s.strip()],
        [s.strip().upper() for s in args.ignore.split(",") if s.strip()],
    )

    if args.explain:
        return explain(args.explain)
    if args.list_checks:
        list_checks(cfg)
        return 0

    findings, n_suppressed = lint(args.paths, args.exclude, cfg)

    if args.format == "json":
        render_json(findings, n_suppressed)
    else:
        color = sys.stdout.isatty() and not args.no_color
        render_text(findings, n_suppressed, color)

    if args.fail_on == "never":
        return 0
    threshold = SEV_RANK[args.fail_on]
    return 1 if any(SEV_RANK[f.severity] >= threshold for f in findings) \
        else 0
