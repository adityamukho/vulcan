# Fact-index delete by rowid — design

Issue: #236. Follows #233, which fixed the reverse walk's write *amplification*
and left a 65x acceptance-gate gap that this addresses most of.

## Problem

`fact_index.delete_facts` costs O(index size) per deleted triple:

```sql
DELETE FROM facts_fts WHERE entity = ? AND attribute = ? AND value = ?
                      AND valid_to IS NULL
```

`facts_fts` is an FTS5 virtual table. FTS5 maintains a full-text index, not a
B-tree over column values, so an equality predicate on `entity` cannot seek —
every delete is a full scan. `fact_index.py:31` already names this ("an O(n)
scan over `facts_fts`") as the reason `facts_dedup` exists for the *insert*
path. The delete path never got the same treatment.

On a full instrumented at-scale ingestion of this repo (553 commits, 5,627 s),
`mcp_server._retract` accounts for **4,137.8 s — 73.5% of wall clock**, nearly
all of it this one statement.

## Measured mechanics

A spike (`scratchpad/spike_rowid.py`, not committed — its numbers are recorded
here) settled four questions the fix depends on. All four came out favourable.

**1. `DELETE ... WHERE rowid = ?` on FTS5 is flat.** 200 rows deleted via a
b-tree rowid lookup plus a rowid delete, against the same index sizes the issue
benchmarked:

| index rows | equality DELETE (today) | rowid DELETE |
|---:|---:|---:|
| 5,000 | 0.805 ms/triple | 0.0095 ms/triple |
| 20,000 | 2.887 | 0.0124 |
| 80,000 | 11.088 | **0.0127** |

Flat across a 16x index-size range, and **873x faster at 80,000 rows**. The
residual 5k→20k wobble is constant overhead, not scaling.

**2. FTS5 accepts an explicitly assigned rowid** on insert
(`INSERT INTO facts_fts (rowid, ...) VALUES (?, ...)`), and `cur.lastrowid`
also reports correctly for an FTS5 insert. The design uses the explicit form,
so it does not depend on the latter.

**3. B-tree rowids are recycled.** Deleting the max rowid from a regular table
frees it for the next insert. Any scheme tying an FTS5 rowid to a b-tree rowid
must therefore keep the two in lockstep.

**4. Inserting at an already-used FTS5 rowid raises `IntegrityError`.** A broken
lockstep invariant fails loudly rather than silently clobbering a row.

Separately verified: after an **ignored** `INSERT OR IGNORE`, `lastrowid` is
stale (it retains the previous successful insert's value) while `rowcount` is
0. `insert_facts` already `continue`s on `rowcount == 0`, so `lastrowid` is only
ever read when it is valid. That existing guard is what makes this design safe.

## Approach

`facts_dedup.rowid` **is** `facts_fts.rowid`.

`facts_dedup` is already a real B-tree table holding exactly the key a delete
needs to look up, and `insert_facts` already writes it first and only touches
`facts_fts` when that write was genuinely new. So the dedup row's rowid can be
assigned directly as the FTS5 rowid: the mapping is the identity function, with
no column to add and no association to keep in sync.

Two alternatives were considered and rejected:

- **A nullable `fts_rowid` column with a fallback to the old equality DELETE
  when NULL.** Self-migrating with no index wipe, but it keeps the O(n) path
  alive permanently and adds a column plus a branch to carry state that only
  exists for un-rebuilt files — which stay slow until a rebuild happens anyway.
- **External-content or `contentless_delete=1` FTS5**, making `facts_dedup` the
  content table. Structurally the most correct model, but it rewrites
  `query_facts` (bm25 over external content needs a join to recover the
  columns), `rebuild_index`, and essentially all of `test_fact_index.py`, for
  the same asymptotics. `contentless_delete` also gates on SQLite >= 3.43.

## Schema

`_SCHEMA_VERSION` goes `"3"` -> `"4"`. **No DDL change** — `facts_dedup` is a
regular table and already has an implicit rowid.

The version bump exists solely to invalidate v3 index files. In a v3 file the
FTS5 rowids were auto-assigned and bear no relation to dedup rowids; running
v4's delete against one would delete *unrelated* rows. This is the one hazard
the approach introduces, and the migration below is what closes it.

## Insert path

`insert_facts` keeps its structure. The only change is the second statement:

```python
cur = con.execute(
    "INSERT OR IGNORE INTO facts_dedup "
    "(entity, attribute, value, valid_from, valid_to) VALUES (?, ?, ?, ?, ?)",
    (entity, attribute, value,
     valid_from if valid_from is not None else "",
     valid_to if valid_to is not None else ""),
)
if cur.rowcount == 0:
    continue
con.execute(
    "INSERT INTO facts_fts (rowid, entity, attribute, value, valid_from, valid_to) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (cur.lastrowid, entity, attribute, value, valid_from, valid_to),
)
```

## Delete path

```python
for e, a, v, _vf, _vt in triples:
    rowids = [r[0] for r in con.execute(
        "SELECT rowid FROM facts_dedup WHERE entity = ? AND attribute = ? "
        "AND value = ? AND valid_to = ''", (e, a, v))]
    if not rowids:
        continue
    con.executemany("DELETE FROM facts_fts WHERE rowid = ?", [(r,) for r in rowids])
    con.executemany("DELETE FROM facts_dedup WHERE rowid = ?", [(r,) for r in rowids])
```

The `SELECT` seeks the `(entity, attribute, value)` prefix of `facts_dedup`'s
existing `UNIQUE(entity, attribute, value, valid_from, valid_to)` index.

All three of the current path's semantics are preserved exactly:

- **Current rows only.** `facts_dedup.valid_to = ''` is the normalized-NULL
  sentinel `insert_facts` writes, and corresponds one-to-one with
  `facts_fts.valid_to IS NULL`. Historical rows for the same
  `(entity, attribute, value)` from an earlier lifecycle are untouched.
- **Not scoped to a specific `valid_from`.** The lookup omits `valid_from`
  exactly as today's `DELETE` does — `delete_facts` does not know which
  `valid_from` the current row carries.
- **Multiple matching current rows all go.** Distinct `valid_from` values for
  one `(e, a, v)` are distinct dedup rows, so the lookup returns a list, not a
  single rowid.

**FTS-first ordering is load-bearing.** A failure between the two deletes leaves
an orphan *dedup* row: harmless, invisible to queries, and cleared by the next
delete of that triple. Deleting dedup first would instead leave an orphan *fts*
row — permanently stale in query results, and holding a rowid that a later
dedup insert could recycle into a collision.

By the same argument, dedup-first ordering on **insert** means the only
inconsistency physically reachable is an orphan dedup row (the fts insert failed
after the dedup insert). The reverse — an fts row with no dedup row — cannot
occur through either path. Cost of an orphan dedup row is one unindexed fact.

## Migration

`ensure_schema` becomes version-aware. Before the existing
`CREATE ... IF NOT EXISTS` block:

1. Read `schema_version` from `index_meta`. A missing `index_meta` table (a v1
   file), a missing row, or any `sqlite3.Error` is treated as a mismatch.
2. If the stored version differs from `_SCHEMA_VERSION`, issue
   `DROP TABLE IF EXISTS` for `facts_fts`, `index_meta`, and `facts_dedup`.

Then the existing `CREATE`s and `INSERT OR IGNORE` for `schema_version` run
unchanged. After a drop, `index_meta` is empty, so the stamp inserts `"4"`;
after no drop it is already `"4"` and the insert is a no-op.

Dropping `index_meta` also drops the `backfilled` sentinel. `needs_backfill()`
therefore returns True, so the next caller that acts on it repopulates the index
from the graph's full history via `_rebuild_index_from_graph()` — either
`handle_memory_prepare_turn` (the read path) or `_run_startup_backfill` (eager,
at server start). A stale-version file becomes exactly the
existing "index file missing" case, which is a path that already works — the
index is a derived cache, so wiping it costs one rebuild, never data.

On a brand-new empty file the drops are no-ops, so the added `DROP`s do not
change the fresh-file path. Concurrent racers serialize on `busy_timeout` as
today; the second racer reads version `"4"` and drops nothing.

`rebuild_index` needs no change — it already drops and recreates all three
tables inside its `BEGIN IMMEDIATE` and stamps the version with
`INSERT OR REPLACE`. It calls `insert_facts`, which picks up the new rowid
assignment automatically.

This migration also closes a pre-existing gap `ensure_schema`'s own docstring
documents: against a v2 file, `facts_dedup` was created empty, never backfilled
from `facts_fts`, and the version never bumped — leaving the dedup guard
under-protecting until some read happened to trigger a rebuild.

## Error handling

Policy is unchanged: index maintenance must never block a graph write. A rowid
collision raises `IntegrityError`, which `_index_write` already catches and
reports to stderr. Under the lockstep invariant it is unreachable; leaving it
loud means a broken invariant surfaces instead of corrupting the index, which
mechanic 4 above confirms FTS5 will not do on its own.

Under the batched ingestion connection (`_open_index_writer_safe`), an exception
propagates to `_index_write`'s handler while the partially-applied statements
remain in the caller-controlled transaction and are committed later by
`_commit_index_writer_safe`. The resulting state is an orphan dedup row — the
harmless direction, per the ordering argument above.

## Testing

New unit tests in `tests/test_fact_index.py`:

- `facts_dedup.rowid == facts_fts.rowid` for every row after `insert_facts`.
- Delete-then-reinsert of the same triple round-trips, exercising the rowid
  recycling mechanic 3 confirmed.
- A v3 file (old tables plus `schema_version = '3'`), a v2 file (no
  `facts_dedup`), and a v1 file (no `index_meta`) are each wiped and re-stamped
  to `'4'` on `open_writer`, with `needs_backfill()` True afterward.
- A dedup row whose fts row is already missing deletes cleanly without raising.

Existing tests are the semantic oracle and must pass unchanged — in particular
`test_delete_facts_only_deletes_current_rows` and
`test_rebuild_index_resets_dedup_state_across_rebuilds`.

No timing assertions in unit tests; they are flaky. Flatness is evidenced by
`evals/at_scale/bench_index_delete_cost.py`, extended to report rowid-delete
numbers alongside the equality-delete ones it already produces.

## Validation

- Full at-scale ingestion benchmark (`evals/at_scale/run_ingestion_benchmark.py`,
  ~85 min) for a fresh per-call-site attribution table.
- Confirm `_candidate_diff_purge_legacy`'s O(N^2) behaviour disappears (500
  records 0.50 s, 2,000 records 7.24 s, 8,000 records 126.71 s on master). The
  issue attributes it to this same root cause rather than a distinct defect;
  if it survives, that attribution was wrong and needs its own issue.

## Docs

No user-facing docs change. `SKILL.md:648` describes the index only by path and
`MINIGRAF_INDEX_PATH`, with no schema details; `README.md` does not mention it.
The schema version and table layout are internal to `fact_index.py`, whose
module and function docstrings carry the rationale and must be updated in place
(notably `ensure_schema`'s v2-file caveat, which this change resolves).

## Scope

This fix alone does not close #233's acceptance gate. Removing the delete cost
projects a total of roughly 1,500-1,700 s against the 78.87 s forward-only
baseline — about **20x**, down from 65x. That is a ~3.4x total speedup, not an
end state.

Deliberately out of scope, to be chosen from the post-fix measurement rather
than predicted:

- `_resolved_facts_triples`' `_query_ident` calls, the expected largest residual
  inside `_retract`.
- Batching `_correction_sweep_apply`'s case-3 `:modified-in` retract. #236
  records at length why this is worthless *before* the rowid fix (cost follows
  facts, not calls). It only becomes worth evaluating after, and the post-fix
  attribution table should decide it.

Whatever the new dominant cost turns out to be gets filed as a fresh issue, and
#236 closes.
