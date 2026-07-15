"""--addons-path support: locate addons and resolve manifest dependencies.

A context tree can be huge (all of odoo/addons) while the lint targets only
depend on a handful of its addons. This module reads the targets'
__manifest__.py files and expands their `depends` transitively against the
addons found (recursively) under the context paths, so only the relevant
addon directories are parsed into the registry. Manifests are read with
ast.literal_eval — no code execution."""
from __future__ import annotations

import ast
import configparser
import os
import re
import sys
from typing import Iterable

MANIFEST = "__manifest__.py"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
#: project config files that declare addons paths, in lookup order
CONFIG_FILES = ("odools.toml", "odoo.conf", ".odoorc")


def read_depends(addon_dir: str) -> list[str]:
    """The `depends` list of an addon's manifest ([] on any problem)."""
    path = os.path.join(addon_dir, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            data = ast.literal_eval(fh.read())
    except (OSError, ValueError, SyntaxError, TypeError):
        print(f"perf_lint: cannot parse manifest: {path}", file=sys.stderr)
        return []
    if not isinstance(data, dict):
        return []
    return [d for d in data.get("depends", []) if isinstance(d, str)]


def target_addon_dirs(paths: Iterable[str]) -> set[str]:
    """Addon directories enclosing or inside the lint targets.

    Looks upward from each path (linting my_addon/models/ still finds
    my_addon's manifest) and downward through directories (linting a folder
    holding several addons finds them all)."""
    found: set[str] = set()
    for path in paths:
        cur = os.path.abspath(path if os.path.isdir(path)
                              else os.path.dirname(path) or ".")
        probe = cur
        while True:
            if os.path.isfile(os.path.join(probe, MANIFEST)):
                found.add(probe)
                break
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        for root, dirs, files in os.walk(cur):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if MANIFEST in files:
                found.add(root)
                dirs[:] = []  # addons don't nest
    return found


def index_addons(context_paths: Iterable[str]) -> dict[str, str]:
    """addon name -> directory, found recursively under the context paths.

    On duplicate names the first context path wins, mirroring how Odoo
    resolves its own --addons-path."""
    index: dict[str, str] = {}
    for path in context_paths:
        if not os.path.isdir(path):
            print(f"perf_lint: no such directory: {path}", file=sys.stderr)
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            if MANIFEST in files:
                index.setdefault(os.path.basename(root), root)
                dirs[:] = []
    return index


def _toml_addons_paths(text: str) -> list[str]:
    """addons_paths entries of an odools.toml (odoo-ls config).

    Uses tomllib when available (3.11+); a string-scraping fallback keeps
    3.10 support without adding a dependency."""
    try:
        import tomllib  # type: ignore[import-not-found]  # 3.11+
    except ImportError:
        m = re.search(r"addons_paths\s*=\s*\[(.*?)\]", text, re.S)
        return re.findall(r"""["']([^"']+)["']""", m.group(1)) if m else []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    out: list[str] = []
    for section in data.get("config", []):
        if isinstance(section, dict):
            out.extend(p for p in section.get("addons_paths", [])
                       if isinstance(p, str))
    return out


def _ini_addons_paths(text: str) -> list[str]:
    """addons_path entries of an odoo.conf / .odoorc."""
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return []
    raw = parser.get("options", "addons_path", fallback="")
    return [p.strip() for p in raw.split(",") if p.strip()]


def declared_addons_paths(paths: Iterable[str]) -> list[str]:
    """Existing addons directories declared by the project config found
    nearest above each lint target (odools.toml / odoo.conf / .odoorc).

    Relative entries resolve against the config file's directory; entries
    that don't exist locally (e.g. container paths like /opt/...) are
    skipped silently — they belong to another environment."""
    out: list[str] = []
    seen_cfg: set[str] = set()
    for path in paths:
        probe = os.path.abspath(path if os.path.isdir(path)
                                else os.path.dirname(path) or ".")
        while True:
            cfg = next((os.path.join(probe, n) for n in CONFIG_FILES
                        if os.path.isfile(os.path.join(probe, n))), None)
            if cfg:
                if cfg not in seen_cfg:
                    seen_cfg.add(cfg)
                    try:
                        with open(cfg, encoding="utf-8") as fh:
                            text = fh.read()
                    except OSError:
                        break
                    entries = (_toml_addons_paths(text)
                               if cfg.endswith(".toml")
                               else _ini_addons_paths(text))
                    for entry in entries:
                        full = os.path.normpath(os.path.join(
                            probe, os.path.expanduser(entry)))
                        if os.path.isdir(full) and full not in out:
                            out.append(full)
                break  # nearest config wins for this target
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
    return out


def direct_addons(parent: str) -> dict[str, str]:
    """addon name -> directory, for the immediate children of `parent`
    (the flat layout odoo expects of an addons_path entry)."""
    try:
        entries = sorted(os.listdir(parent))
    except OSError:
        return {}
    return {e: os.path.join(parent, e) for e in entries
            if os.path.isfile(os.path.join(parent, e, MANIFEST))}


def context_addon_dirs(paths: list[str],
                       context_paths: list[str]) -> list[str]:
    """Directories of the context addons the lint targets depend on,
    transitively, plus the implicit `base` when present.

    When no manifest is found in (or above) the targets, falls back to the
    raw context paths — a full crawl beats silently losing the registry."""
    targets = target_addon_dirs(paths)
    if not targets:
        return list(context_paths)
    index = index_addons(context_paths)
    # implicit local entries, flat (direct children only — the flat layout
    # odoo expects, so a same-named addon buried in an unrelated project
    # cannot shadow the explicit trees while a true local sibling can,
    # mirroring the usual custom-addons-first odoo addons_path):
    # 1. addons paths declared by the project's own config
    #    (odools.toml / odoo.conf found above the targets);
    # 2. the directory holding each target addon — dependencies routinely
    #    live NEXT TO the target in the same custom/OCA folder.
    local = declared_addons_paths(paths) \
        + sorted({os.path.dirname(d) for d in targets})
    for parent in local:
        index.update(direct_addons(parent))
    queue = ["base"] if "base" in index else []  # every addon depends on it
    for d in sorted(targets):
        queue.extend(read_depends(d))
    seen = {os.path.basename(d) for d in targets}  # targets are linted
    resolved: list[str] = []
    missing: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        addon = index.get(name)
        if addon is None:
            missing.add(name)
            continue
        resolved.append(addon)
        queue.extend(read_depends(addon))
    for name in sorted(missing):
        print(f"perf_lint: addons-path: dependency '{name}' not found",
              file=sys.stderr)
    return resolved
