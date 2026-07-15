"""Rendering: text/json reports, --list-checks, --explain."""
import json
import os
import sys

from .constants import SEVERITIES
from .registry import ALL_CODES, EXPLAIN

COLORS = {"error": "\033[31m", "warning": "\033[33m", "info": "\033[36m"}
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"


def render_text(findings, n_suppressed, color):
    def c(code, text):
        return f"{code}{text}{RESET}" if color else text

    for f in sorted(findings, key=lambda f: f.sort_key()):
        # path:line:col must stay one uninterrupted token (no ANSI codes
        # inside it) so terminals recognize it as a clickable file link
        rel = os.path.relpath(f.path)
        path = f.path if rel.startswith("..") else rel
        loc = c(BOLD, f"{path}:{f.line}:{f.col}")
        sev = c(COLORS[f.severity], f.severity)
        print(f"{loc} {c(BOLD, f.code)} {c(DIM, f.name)} {sev} {f.message}")
    if findings:
        print()
    counts = {s: sum(1 for f in findings if f.severity == s)
              for s in SEVERITIES}
    parts = [f"{n} {s}" for s, n in counts.items() if n]
    summary = f"{len(findings)} finding(s)" + (
        f" ({', '.join(parts)})" if parts else "")
    if n_suppressed:
        summary += f", {n_suppressed} suppressed by noqa"
    print(summary)


def render_json(findings, n_suppressed):
    print(json.dumps({
        "findings": [vars(f) for f in findings],
        "suppressed": n_suppressed,
    }, indent=2))


def list_checks(cfg):
    print(f"{'CODE':<7} {'ON':<4} {'SEVERITY':<9} {'NAME':<32} SUMMARY")
    for code in sorted(ALL_CODES):
        name, sev, summary = ALL_CODES[code]
        on = "on" if cfg.enabled(code) else "off"
        print(f"{code:<7} {on:<4} {sev:<9} {name:<32} {summary}")


def explain(code):
    code = code.upper()
    if code not in ALL_CODES:
        print(f"unknown check: {code}", file=sys.stderr)
        return 2
    name, sev, summary = ALL_CODES[code]
    print(f"{code} ({name}, {sev})\n\n{summary}\n")
    if code in EXPLAIN:
        print(EXPLAIN[code])
    return 0
