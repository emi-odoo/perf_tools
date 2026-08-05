# Performance Tools: Method Execution

Times every model method in the registry and stores the slow ones as
`slow.execution` records, so that a report can point at the code responsible.

Installing the module is all it takes: there is nothing to configure and no
decorator to add. Uninstall it (or drop it from the addons path) to stop paying
for the instrumentation.

## How it works

### 1. Patching — `patches/registry.py`

`Registry.setup_models` is patched, so right after a registry is built every
function found in `vars()` of every class of every model's `mro()` is replaced by
a timing wrapper. A `_perf_tools_patched` marker keeps a class shared by several
models from being wrapped twice.

Note that the patch is on the `Registry` **class**: every database served by the
process gets instrumented, including those where this module is not installed.
Those simply never store anything.

`MODELS_TO_EXCLUDE` opts a model out. `ir.http` is excluded because it sits in
the path of every request, `slow.execution` because instrumenting the logger with
itself is asking for trouble.

### 2. Timing — own time vs. total time

A wrapper's wall time includes everything it called, so a slow `read` makes every
method above it look slow too. To separate the two, each thread keeps a stack of
the calls currently running, and each entry accumulates the time its children
took:

```
own_time = total_time - children_time     # and our total goes to our caller's children_time
```

Both end up on the record: `duration_ms` (inclusive) and `own_duration_ms`
(exclusive). This is the cumulative/self distinction any profiler makes.

A call is logged when **either**:

- `own_duration_ms > _THRESHOLD_MIN_TIME_MS` — it did slow work itself, or
- it is an entry point (`depth == 0`) and `duration_ms > _THRESHOLD_ROOT_TIME_MS`
  — it may own none of the time, but a slow request is still worth a row.

Thresholding on own time is what keeps the volume sane: the intermediate frames
that were only slow because of their children stop producing rows.

### 3. The caller

The same stack gives the caller away for free: once a call pops itself off,
`stack[-1]` *is* its caller. Only an entry point has no instrumented parent, and
there `_find_business_caller()` walks the frames to name whatever got us into the
ORM (`ir.http`, `ir.cron`, …) — expensive, but by then we know we are logging.

### 4. Which model — `CallIdentifier.on_record`

A `CallIdentifier` is built once per patched function, from the class that
*defines* it. For a generic method that is not the model it runs on: `web_read`
is defined on `base`, `read` on `BaseModel`, mixin methods on the mixin. Logging
that name would collapse every model into one bucket, so `on_record()` renames
the identifier after the record set the call actually ran on, keeping `filename`
and `line_number` on the definition site.

### 5. Storing — `models/slow_execution.py`

Writing a record per slow call inside the transaction being measured would
distort it, and a rollback would take the logs with it. So `_log_slow_call` only
appends to a buffer held on `cr.postcommit.data`, and registers
`_postcommit_create_logs` as a post-commit hook — once per transaction, not once
per call. Odoo clears `postcommit` on both `commit()` and `rollback()`, which
gives the buffer the right lifetime for free.

`_postcommit_create_logs` runs after the transaction is over, so it opens a
cursor of its own from `odoo.registry(dbname)`. Two things it must do:

- **Never raise.** `postcommit.run()` is called from inside `Cursor.commit()`, so
  an exception here would surface in the caller of `commit()` and break an
  already successful request. Everything is caught and logged.
- **Not log itself.** Its own `create()` goes through patched methods, which
  would buffer onto its own cursor, which commits, which flushes again… A thread
  local `flushing` flag makes `_log_slow_call` a no-op for the duration. Note
  that excluding `slow.execution` from patching is *not* enough on its own —
  `create()` calls out to other, instrumented models.

## Fields

| field | meaning |
| --- | --- |
| `model`, `method` | the call, named after the record set it ran on |
| `filename`, `line_number` | where the method is **defined** |
| `duration_ms` | wall time, nested calls included |
| `own_duration_ms` | wall time minus time spent in instrumented calls it made |
| `depth` | how many instrumented calls it runs inside of, `0` for an entry point |
| `caller_*` | same, for the call it was made from (empty for an entry point) |

## Reading the data

- **Where the time goes:** sum `own_duration_ms` grouped by `model` + `method`.
  Summing `duration_ms` double counts, and is only meaningful filtered to
  `depth = 0`.
- **Slowest entry points:** `depth = 0` ordered by `duration_ms`.
- **Who is responsible for a hot method:** group by `caller_model` +
  `caller_method`.

## Known limitations

- **Rows are edges, not a tree.** A cheap intermediate call produces no row even
  though the row below it names it as caller, so the chain cannot be fully
  rebuilt from the table.
- **Time in un-instrumented code counts as own time**: SQL, network, `models.py`
  internals, excluded models. Usually what you want — a method whose own time is
  all SQL *is* the slow method — but `own_duration_ms` is not "Python work".
- **Generators are only timed on creation.** The body runs when consumed, outside
  the wrapper, and its cost lands on whoever consumes it.
- **Nothing is logged in tests**: `TestCursor.commit()` discards post-commit
  hooks by design.
- **Calls on a class rather than a record set are never logged** (classmethods):
  there is no `env` to write through.
- `_MAX_PENDING` caps the buffer at 1000 rows per transaction; a transaction
  slower than that silently loses the rest.

## Tuning

| constant | in | default |
| --- | --- | --- |
| `_THRESHOLD_MIN_TIME_MS` | `patches/registry.py` | 90 |
| `_THRESHOLD_ROOT_TIME_MS` | `patches/registry.py` | 200 |
| `MODELS_TO_EXCLUDE` | `patches/registry.py` | `ir.http`, `slow.execution` |
| `_MAX_PENDING` | `models/slow_execution.py` | 1000 |
