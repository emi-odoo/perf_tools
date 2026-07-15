# perf-lint

Static analysis for Odoo ORM performance anti-patterns.

`perf-lint` is a stdlib-only AST linter — it never imports Odoo, never
connects to a database, and has zero dependencies. It parses your addon
source, builds a cross-file registry of models and fields, then walks every
method with a small dataflow scanner that knows which names hold recordsets
and which statements run inside loops. On top of that machinery, a set of
checkers flag the classic ways Odoo code gets slow: queries in loops,
per-record work in `create()`/`write()`/computes, unbounded searches,
missing indexes, and recompute grenades.

## Installation

From this directory (the one containing `pyproject.toml`):

```bash
pip install -e .
# or, isolated:
pipx install -e /full/path/to/cli_tools/perf_lint
```

> **pipx note:** the argument must be a *path* (contain a `/`). A bare
> `pipx install perf-lint` is treated as a PyPI package name and fails with
> "No matching distribution found".

This installs the `perf-lint` command. Without installing, you can also run
it as a module from this directory: `python3 -m perf_lint`.

Requires Python ≥ 3.10.

## Usage

```bash
perf-lint my_addon/                    # lint an addon
perf-lint addon_a/ addon_b/           # several paths
perf-lint --list-checks               # every check + on/off status
perf-lint --explain SD103             # what / why / how to fix
perf-lint . --ignore SD3,SD106        # disable by code prefix
perf-lint . --select SD1              # enable ONLY the SD1xx family
perf-lint . --exclude "*/tests/*"     # skip paths (repeatable)
perf-lint . --format json             # machine-readable output
perf-lint . --fail-on error           # exit 1 only on errors
perf-lint . --plugin my_checks.py     # load external checkers
```

**Lint whole directories, not single files.** The cross-file checks
(SD302, SD402) need to see every model definition to resolve comodels,
indexes and unique constraints; linting one file at a time blinds them.

### Options

| Flag | Meaning |
|------|---------|
| `--select PREFIXES` | comma-separated code prefixes to enable *exclusively* (`--select SD1` = only SD1xx) |
| `--ignore PREFIXES` | comma-separated code prefixes to disable (`--ignore SD3` kills all SD3xx) |
| `--exclude GLOB` | path pattern to skip — substring or glob, repeatable |
| `--plugin FILE` | Python file with extra `@register`'d checkers, repeatable |
| `--format text\|json` | output format (default `text`) |
| `--fail-on info\|warning\|error\|never` | minimum severity that makes the exit code 1 (default `warning`) |
| `--list-checks` | table of all checks with their enabled state under the current `--select`/`--ignore` |
| `--explain CODE` | long what/why/fix description of one check |
| `--no-color` | disable ANSI colors (auto-disabled when stdout is not a tty) |

`.git`, `__pycache__`, `node_modules`, `.venv` and `venv` directories are
always skipped. Unparseable files and nonexistent paths are reported to
stderr, never silently ignored.

### Exit codes

`0` — no finding at or above the `--fail-on` threshold.
`1` — at least one finding at or above the threshold (default: warning).
`2` — `--explain` was given an unknown code.

This makes it drop straight into CI:

```bash
perf-lint addons/ --fail-on error --format json > perf_report.json
```

### Suppressing findings

Flake8-style, inline:

```python
tags = self.env["my.tag"].search([])   # noqa: SD201  (known-small table)
whatever = ...                         # noqa          (suppress everything)
```

A `noqa` comment on **any physical line of the flagged node** works, so
multi-line calls can carry the comment wherever it reads best:

```python
records = self.env["res.partner"].search(
    [],            # noqa: SD201
    order="name",
)
```

To skip an entire file, put this in its first five lines:

```python
# perf-lint: skip-file
```

Suppressed findings are counted and reported in the summary line so they
never disappear silently.

## The checks

Codes are grouped by family: **SD1xx** loops/N+1 · **SD2xx** ORM misuse ·
**SD3xx** indexing · **SD4xx** storage. Run `--explain CODE` for the long
version of any of them.

| Code  | Name | Severity | Detects |
|-------|------|----------|---------|
| SD101 | query-in-loop | error | `search`/`read_group`/`next_by_code`/… executed once per loop iteration |
| SD102 | write-in-loop | error | `write`/`create`/`unlink` per iteration; `create([listcomp])` in a loop is recognized as chunked batching and *not* flagged |
| SD103 | query-in-compute | error | per-record query inside a compute method |
| SD104 | query-in-create-write | error | per-record work inside `create()`/`write()` overrides |
| SD105 | query-in-constrains | error | query inside `@api.constrains`; suggests a `models.Constraint` UNIQUE when the domain shape allows it |
| SD106 | raw-sql-in-loop | info | `cr.execute` in a loop — info-only, chunked migrations are legitimate |
| SD107 | flush-in-loop | warning | `flush_*()`/`invalidate_*()` per iteration — defeats write batching and prefetch |
| SD201 | unbounded-search-all | warning | `search([])` with no domain and no `limit=` |
| SD202 | filtered-after-search | warning | `.filtered()` on a `search()` result — the predicate belongs in the domain |
| SD203 | len-search | warning | `len(search(...))` — use `search_count()` / `_read_group` |
| SD204 | sorted-after-search | info | `.sorted()` on a `search()` result — the sort belongs in `order=` |
| SD205 | search-for-existence | warning | `search()` result used only as a truth test — add `limit=1` |
| SD301 | index-disabled | warning | `Many2one(index=False)` |
| SD302 | unindexed-searched-field | warning | field used in a literal search domain but declared without `index=True` (cross-file; skips Selection/Boolean, non-stored compute/related, and unique-constraint columns) |
| SD303 | ilike-domain | info | `like`/`ilike` in domains, unless the field has `index='trigram'` |
| SD304 | dotted-domain-x2many | info | dotted domain path through a One2many/Many2many — nested sub-select over the whole relation |
| SD401 | binary-in-table | warning | `fields.Binary(attachment=False)` |
| SD402 | stored-compute-x2many-depends | error | `store=True` compute whose `@api.depends` path traverses a One2many/Many2many — a recompute grenade |

Loop findings classify to the most specific applicable code
(SD103/104/105 by context) and fall back to SD101/SD102 when the specific
code is disabled — so `--ignore SD103` doesn't hide the underlying
query-in-loop, it just re-labels it.

Severity vocabulary: `info` < `warning` < `error` (this is the ordering
`--fail-on` uses).

## How it works

Two passes over the whole path set:

1. **Parsing** (`parsing.py`) — every file is `ast.parse`d and each Odoo
   model class is reduced to a `ModelClass`: its `_name`/`_inherit`, its
   `FieldDecl`s (type, kwargs, comodel), its methods with their
   `@api.depends`/`@api.constrains` decorators, and the columns covered by
   UNIQUE constraints. All of this feeds a project-wide registry
   (`Project.registry`: model name → field name → `FieldDecl`).

2. **Scanning** (`scanner.py`) — with the full registry available, every
   method (and module-level function — migration scripts, hooks) is walked
   by `FuncScanner`, a dataflow visitor that tracks:
   - which names hold recordsets: `self`, `env["..."]` aliases, loop
     variables over recordsets, chained calls like `.sudo().filtered()`,
     `super().create()` results, relational-field traversals
     (`order.partner_id` → `res.partner`);
   - which statements execute inside a loop: `for`/`while`, all
     comprehension types, and lambdas passed to
     `filtered`/`mapped`/`sorted` (those run once per record);
   - every ORM query/write call, recorded as a `QueryEvent`, plus
     structured `DomainTerm`s extracted from literal domains.

The checkers then run over these pre-digested events rather than raw AST,
which is why most of them are only a few lines long.

### Package layout

```
perf_lint/
├── __init__.py     # usage docstring + public plugin API re-exports
├── __main__.py     # python3 -m perf_lint
├── cli.py          # argparse; main() returns the exit code
├── constants.py    # ORM method sets (QUERY/WRITE/CHAIN/SEARCHY...), severities
├── astutils.py     # small AST predicates (const_str, is_env, is_cursor, ...)
├── model.py        # dataclasses: Finding, FieldDecl, MethodInfo, ModelClass,
│                   #   QueryEvent, DomainTerm, ModuleCtx; class Project
├── parsing.py      # pass 1: models, fields, decorators → registry
├── scanner.py      # pass 2: recordset dataflow + loop-aware query events
├── registry.py     # Checker base class, @register, ALL_CODES, EXPLAIN
├── runner.py       # toggles, noqa, file discovery, plugins, lint()
├── output.py       # text/json rendering, --list-checks, --explain
└── checks/
    ├── loops.py    # SD101-106
    ├── orm.py      # SD201-203
    ├── indexes.py  # SD301-303
    └── storage.py  # SD401-402
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

No test dependencies — plain stdlib `unittest`. Each test in
`tests/test_perf_lint.py` writes a small bad-code fixture into a temp
directory, runs the real `lint()` pipeline over it and asserts the exact
finding codes, so the suite doubles as a catalog of examples: what each
check fires on, and the near-miss variants it must stay quiet about
(`create([listcomp])` in a loop, `search([], limit=...)`, indexed fields,
unstored computes, `# noqa`, `# perf-lint: skip-file`, ...). When you add
a checker, add one triggering fixture and one clean counter-example.

## Writing a new checker

Checkers are plain classes registered with a decorator. External plugins
and built-in checks use **exactly the same API** — the only difference is
where the file lives.

### Minimal example

Save this as `my_checks.py` and run
`perf-lint . --plugin my_checks.py`:

```python
from perf_lint import Checker, register
import ast


@register
class CommitInCode(Checker):
    codes = {"X901": ("cr-commit", "warning",
                      "explicit commit() — transaction control belongs "
                      "to the framework")}

    def check_module(self, mod, project, cfg):
        for node in ast.walk(mod.tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "commit"):
                yield self.finding("X901", mod, node)
```

That's the whole contract. Piece by piece:

### 1. Declare your codes

```python
codes = {"X901": ("cr-commit", "warning", "one-line summary")}
```

`codes` maps each code to a `(kebab-name, severity, summary)` tuple.
Severity must be one of `info`, `warning`, `error`. One checker class may
own several codes. Registered codes automatically show up in
`--list-checks` and participate in `--select`/`--ignore` prefix matching
and `# noqa:` suppression — you get all of that for free.

Pick a code prefix that doesn't collide with the built-in `SD` families;
plugins conventionally use `X9xx`.

Optionally add a long description for `--explain`:

```python
explain = {"X901": "Committing mid-transaction breaks atomicity because ..."}
```

### 2. Implement `check_module` and/or `check_project`

```python
def check_module(self, mod, project, cfg):   # once per analyzed file
    ...
def check_project(self, project, cfg):       # once per run, cross-file
    ...
```

Both are generators that `yield` `Finding` objects. Use `check_module` for
anything decidable from one file; use `check_project` when you need to
correlate across files (e.g. "field searched in module A but declared
without an index in module B" — that's how SD302 works).

You don't need to check `cfg.enabled(...)` or handle `noqa` yourself — the
runner filters disabled codes and suppressed lines after your checker
yields. Checking `cfg.enabled("X901")` before an expensive walk is purely
an optimization (the built-ins do it to skip work).

### 3. Yield findings

```python
yield self.finding("X901", mod, node)                    # uses the summary
yield self.finding("X901", mod, node, "custom message")  # overrides it
```

`self.finding(...)` builds the `Finding` from the AST node's position,
including `end_lineno` so `# noqa` works on any physical line of a
multi-line statement.

### What you get to look at

**`mod` — a `ModuleCtx`, one per file** (`model.py`):

| Attribute | Contents |
|-----------|----------|
| `mod.path` / `mod.lines` | file path and source lines |
| `mod.tree` | the raw `ast.Module`, for free-form walking |
| `mod.models` | list of `ModelClass` — parsed Odoo classes with `.model` (the `_name`/`_inherit`), `.fields` (name → `FieldDecl`), `.methods` (name → `MethodInfo`), `.unique_cols` |
| `mod.query_events` | list of `QueryEvent` — every ORM read/write call, with `.fname`, `.kind` (`"read"`/`"write"`), `.model`, `.in_loop`, `.batched`, `.empty_domain`, and the enclosing `.klass`/`.method` |
| `mod.domain_terms` | list of `DomainTerm` — every `(field, op, value)` triple from literal search domains, with the resolved `.model` |
| `mod.len_search`, `mod.filtered_after_search`, `mod.sorted_after_search` | pre-collected `(node, klass, method)` tuples for those specific shapes |

**`project` — the cross-file `Project`:**

| Member | Contents |
|--------|----------|
| `project.modules` | every `ModuleCtx` in the run |
| `project.field(model, name)` | the `FieldDecl` for a field, wherever it was declared (also across `_inherit` extensions) — `None` if unknown |
| `project.unique_cols` | model name → set of columns covered by UNIQUE constraints |

The high-leverage move: **prefer the pre-digested events over raw AST.**
`mod.query_events` already carries loop context and recordset resolution
that took the scanner real work to compute — `ev.in_loop` on a
`QueryEvent` is the entire implementation of "is this query in a loop?".
Reach for `ast.walk(mod.tree)` only when you're matching a shape the
scanner doesn't model.

Useful helpers:

- `FieldDecl.kw("index", default=None)` — constant value of a field kwarg;
  returns `Ellipsis` when the kwarg exists but isn't a literal (treat that
  as "unknown", not as the default).
- `MethodInfo.is_compute`, `.depends`, `.constrains` — decorator info,
  already parsed.
- `perf_lint.astutils.const_str(node)` — the string value of a constant
  node, else `None`.
- `perf_lint.constants` — the shared ORM vocabulary (`QUERY_METHODS`,
  `WRITE_METHODS`, `X2MANY`, `INDEXABLE_OPS`, ...). Extend your checks from
  these sets instead of hand-rolling method lists.

### Making it a built-in instead

Same class, different home:

1. Create `perf_lint/checks/mytopic.py` with your `@register`'d checker.
2. Import it in `perf_lint/checks/__init__.py` (the import *is* the
   registration).
3. Give it a code in an existing family or start a new hundred-block, and
   add it to the table in this README.

Before shipping a new check, run it against a real addon tree (e.g. a few
odoo/addons modules) and eyeball the hits — the difference between a useful
check and a noisy one is almost always one more guard condition, and the
existing checks (see SD302's skip-list for compute/related/low-cardinality
fields) show what those guards look like in practice.

### Testing a checker quickly

```bash
# see only your code, on a known-bad file
perf-lint path/to/sample --plugin my_checks.py --select X9

# confirm the clean version stays clean
perf-lint path/to/fixed_sample --plugin my_checks.py --select X9 --fail-on never
```
