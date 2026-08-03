# Fact-index delete by rowid — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `fact_index.delete_facts` seek instead of scan, by reusing `facts_dedup`'s implicit rowid as `facts_fts`'s rowid — removing 73.5% of an at-scale ingestion's wall clock (#236).

**Architecture:** `facts_dedup` is already a B-tree table holding exactly the key a delete needs, and `insert_facts` already writes it first and only touches `facts_fts` when that write was new. So the dedup row's rowid is assigned directly as the FTS5 rowid: the mapping is the identity function, with no new column and nothing to keep in sync. `_SCHEMA_VERSION` bumps to `"4"` and `ensure_schema` gains a version-aware wipe, because in a pre-existing v3 file the FTS5 rowids were auto-assigned and bear no relation to dedup rowids — running v4's delete against one would delete unrelated rows.

**Tech Stack:** Python 3, stdlib `sqlite3`, SQLite FTS5, pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-fact-index-delete-by-rowid-design.md`

**Branch:** `fix-236-fact-index-delete-by-rowid` (already created; the spec commit `0c74c1c` is its first commit).

## Global Constraints

- **Real backends only in tests — never mocked.** `tests/test_fact_index.py` opens real `sqlite3` files under `tmp_path`. See `docs/testing-conventions.md`.
- **Test command:** `.venv/bin/python -m pytest tests/test_fact_index.py -q` (50 tests, ~0.3 s at plan time — all must stay green).
- **`mcp` is pinned `<2.0.0`** in `pyproject.toml`. Do not raise it; 2.0.0 removed `Server.list_tools` and breaks CI.
- **`minigraf>=1.2.1`** floor, unchanged by this work.
- **No user-facing docs change.** `SKILL.md:648` describes the index by path only; `README.md` does not mention it. The schema is internal to `fact_index.py`, whose docstrings carry the rationale and must be updated in place.
- **Commit messages must not contain a GitHub closing keyword for #236.** Use `Refs #236`. Closing keywords are scanned in both commit messages and PR bodies, and even a negated one ("does not close #236") closes the issue. #236 closes only after the Task 4 validation run.
- **`master` requires an approving review on top of green CI.** Do not merge with `--admin` without asking.

## File Structure

Only one production file changes. The work is small in surface area and large in consequence, so the task split is by *reviewable claim*, not by file.

- **Modify `fact_index.py`** — the whole change lives here:
  - `_SCHEMA_VERSION`, new `_stored_schema_version()` helper, version-aware `ensure_schema()` (Task 1)
  - `insert_facts()` rowid assignment, `delete_facts()` rowid seek (Task 3)
- **Modify `tests/test_fact_index.py`** — one existing assertion updated, eight tests added.
- **Modify `evals/at_scale/bench_index_delete_cost.py`** — report old and new delete paths side by side (Task 2).

**Task ordering is deliberate and not arbitrary:**

1. **The migration guard lands first**, so at no commit does rowid-based delete code exist without the guard that keeps it away from a v3 file.
2. **The benchmark lands before the rewrite**, because it is this plan's only genuine red-green signal. Task 3 is a pure performance change that preserves semantics exactly — there is no functional test that passes after and fails before. Verified during planning: on current code `facts_fts` and `facts_dedup` rowids already coincide, because both tables assign `max(rowid)+1` and `insert_facts`/`delete_facts` write them in lockstep. The identity Task 3 depends on therefore holds *accidentally* today and becomes *load-bearing* after. So the invariant tests pin behaviour rather than driving it, and the measurement is what goes red then green.

Do not "fix" a task's expected-result step because a test passed when the plan said it would. Where a test is expected to pass before the change, the plan says so explicitly and explains why.

---

### Task 1: Version-aware `ensure_schema` (the migration guard)

Bump the schema version and make `ensure_schema` drop-and-recreate on a mismatch. This is worth landing on its own: independently of the rowid change it closes a real pre-existing gap — against a v1 file (3-column `facts_fts`, no `index_meta`) today's `CREATE ... IF NOT EXISTS` leaves the old 3-column table in place, and against a v2 file it creates `facts_dedup` empty and never bumps the version, so the dedup guard silently under-protects until some read happens to trigger a rebuild.

**Files:**
- Modify: `fact_index.py:43` (`_SCHEMA_VERSION`), `fact_index.py:63-98` (`ensure_schema`)
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_stored_schema_version(con: sqlite3.Connection) -> Optional[str]` — module-level helper returning the file's stamped version, or `None` when unreadable. `_SCHEMA_VERSION == "4"`. `ensure_schema(con) -> None`, same signature as today, now destructive on mismatch.

- [ ] **Step 1: Update the one existing assertion that pins the version**

In `tests/test_fact_index.py:47-60`, `test_open_writer_stamps_schema_version_but_not_backfilled` asserts `version == ("3",)`. Change that one line:

```python
        assert version == ("4",)
```

- [ ] **Step 2: Write the tests**

Add to `tests/test_fact_index.py`. `sqlite3` and `fact_index` are already imported at module top (lines 3 and 8) — do not re-import them inside the test bodies; two older tests in this file do `import sqlite3 as _sqlite3` locally, but that is not a pattern to copy.

```python
def _build_v3_index_file(path):
    """Hand-build a schema-v3 file: the exact pre-#236 shape, including a
    facts_fts row whose rowid was auto-assigned and therefore bears no
    relation to its facts_dedup row's rowid. This is the file that would be
    silently mis-deleted if v4 delete code ever ran against it."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "valid_from UNINDEXED, valid_to UNINDEXED, tokenize='unicode61')"
    )
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute(
        "CREATE TABLE facts_dedup ("
        "entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL, "
        "valid_from TEXT NOT NULL, valid_to TEXT NOT NULL, "
        "UNIQUE(entity, attribute, value, valid_from, valid_to))"
    )
    con.execute(
        "INSERT INTO facts_fts (entity, attribute, value, valid_from, valid_to) "
        "VALUES (':decision/old', ':description', 'stale', '2026-01-01T00:00:00.000Z', NULL)"
    )
    con.execute(
        "INSERT INTO facts_dedup VALUES (':decision/old', ':description', 'stale', "
        "'2026-01-01T00:00:00.000Z', '')"
    )
    con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '3')")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('backfilled', '1')")
    con.commit()
    con.close()


def test_open_writer_wipes_a_stale_schema_version_file(tmp_path):
    """#236: a v3 file's facts_fts rowids are auto-assigned and unrelated to
    its facts_dedup rowids, so v4's rowid-based delete would remove wrong
    rows. The version bump alone doesn't prevent that -- only the read path
    acts on needs_backfill(), while open_writer would happily write to the
    stale file. So ensure_schema must wipe it at writer-open time."""
    path = str(tmp_path / "t.fts.sqlite3")
    _build_v3_index_file(path)
    assert fact_index.needs_backfill(path) is True

    con = fact_index.open_writer(path)
    try:
        assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)
        assert con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        # The 'backfilled' sentinel went with index_meta, so a rebuild still
        # follows -- the wipe must not look like a completed backfill.
        assert con.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() is None
    finally:
        fact_index.close_writer(con)
    assert fact_index.needs_backfill(path) is True


def test_open_writer_does_not_wipe_a_current_version_file(tmp_path):
    """The wipe fires ONLY on a version mismatch. Every ingestion run after
    the first reopens a current-version file and must keep its rows -- this
    is the regression that would quietly destroy the index on every open."""
    path = str(tmp_path / "t.fts.sqlite3")
    con1 = fact_index.open_writer(path)
    fact_index.insert_facts(con1, [(":decision/x", ":description", "hello", None, None)])
    fact_index.close_writer(con1)

    con2 = fact_index.open_writer(path)
    try:
        assert con2.execute("SELECT entity FROM facts_fts").fetchall() == [(":decision/x",)]
    finally:
        fact_index.close_writer(con2)


def test_open_writer_wipes_a_v1_file_with_no_meta_table(tmp_path):
    """A v1 file has a 3-column facts_fts and no index_meta at all. Reading
    schema_version must not raise, and the drop must actually replace the
    old table -- CREATE ... IF NOT EXISTS alone would leave the 3-column
    shape in place, and every later insert would fail on column count."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "tokenize='unicode61')"
    )
    con.execute("INSERT INTO facts_fts VALUES (':decision/old', ':description', 'stale')")
    con.commit()
    con.close()

    writer = fact_index.open_writer(path)
    try:
        cols = [r[1] for r in writer.execute("PRAGMA table_info(facts_fts)").fetchall()]
        assert cols == ["entity", "attribute", "value", "valid_from", "valid_to"]
        assert writer.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        # And the wiped file is immediately usable, not just well-shaped.
        fact_index.insert_facts(
            writer, [(":decision/new", ":description", "fresh", None, None)])
        writer.commit()
        assert writer.execute("SELECT entity FROM facts_fts").fetchall() == [(":decision/new",)]
    finally:
        fact_index.close_writer(writer)


def test_open_writer_wipes_a_v2_file_and_stamps_current_version(tmp_path):
    """A v2 file (backfilled, no facts_dedup). Before #236, ensure_schema
    created facts_dedup empty here but left schema_version at '2' and left
    'backfilled'='1' standing -- the gap ensure_schema's own docstring
    documented. The wipe closes it."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE VIRTUAL TABLE facts_fts USING fts5(entity, attribute, value, "
        "valid_from UNINDEXED, valid_to UNINDEXED, tokenize='unicode61')"
    )
    con.execute("CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', '2')")
    con.execute("INSERT INTO index_meta (key, value) VALUES ('backfilled', '1')")
    con.commit()
    con.close()

    writer = fact_index.open_writer(path)
    try:
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone() == ("4",)
        assert writer.execute(
            "SELECT value FROM index_meta WHERE key = 'backfilled'"
        ).fetchone() is None
    finally:
        fact_index.close_writer(writer)
    assert fact_index.needs_backfill(path) is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_fact_index.py -q`

Expected: **3 of the 4 new tests FAIL, plus the updated existing one. One new test passes, and is supposed to.**

- `test_open_writer_stamps_schema_version_but_not_backfilled` (existing, edited in Step 1) — FAILS: version is still `"3"`.
- `test_open_writer_wipes_a_stale_schema_version_file` — FAILS on its *first* assertion, before it even opens a writer: a v3 file matches today's `_SCHEMA_VERSION` and carries `backfilled='1'`, so `needs_backfill()` returns False. After Step 4's version bump it will get past that line and fail on the wipe assertions instead.
- `test_open_writer_wipes_a_v1_file_with_no_meta_table` — FAILS on the column-count assertion: `CREATE ... IF NOT EXISTS` leaves the 3-column table standing.
- `test_open_writer_wipes_a_v2_file_and_stamps_current_version` — FAILS: `INSERT OR IGNORE` is a no-op against the existing `schema_version` row, so it stays `"2"`.
- `test_open_writer_does_not_wipe_a_current_version_file` — **PASSES**, because today there is no wipe at all. It is a regression guard for Step 6, not a driver. Do not "fix" it.

- [ ] **Step 4: Bump the version constant**

`fact_index.py:43`:

```python
_SCHEMA_VERSION = "4"
```

- [ ] **Step 5: Add the `_stored_schema_version` helper**

Insert immediately above `ensure_schema` in `fact_index.py`:

```python
def _stored_schema_version(con: sqlite3.Connection) -> Optional[str]:
    """Return the schema_version stamped in this index file, or None if
    there isn't one readable -- a v1 file with no index_meta table at all,
    a file whose index_meta exists but lacks the row, or anything that
    raises. All three mean "not the current schema" to the only caller,
    ensure_schema, which treats None exactly like a mismatched version."""
    try:
        row = con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None
```

- [ ] **Step 6: Make `ensure_schema` version-aware**

In `fact_index.py`, add the drop block at the top of `ensure_schema`'s body, before the existing `con.execute(_SCHEMA_SQL)`:

```python
    if _stored_schema_version(con) != _SCHEMA_VERSION:
        # A stale-version file must be emptied, not merely reshaped. The
        # index is a derived cache, so this costs one rebuild and never
        # data -- and dropping index_meta takes the 'backfilled' sentinel
        # with it, so needs_backfill() stays True and the next caller that
        # acts on it (handle_memory_prepare_turn, or _run_startup_backfill)
        # repopulates from the graph's full history. A stale-version file
        # thereby becomes exactly the already-working "index file missing"
        # case. On a brand-new empty file these are no-ops.
        con.execute("DROP TABLE IF EXISTS facts_fts")
        con.execute("DROP TABLE IF EXISTS index_meta")
        con.execute("DROP TABLE IF EXISTS facts_dedup")
```

- [ ] **Step 7: Replace the stale docstring caveat**

`ensure_schema`'s docstring currently ends with a five-sentence "Code-review note on #152's facts_dedup addition" paragraph (`fact_index.py:79-89`) describing the v2-file gap. That gap no longer exists. Delete that whole paragraph and put this in its place:

```
    On a schema_version mismatch -- including a v1 file with no index_meta
    at all -- all three tables are dropped and recreated empty before the
    CREATE block below. This is what lets delete_facts trust that
    facts_dedup.rowid IS facts_fts.rowid (#236): a v3 file's facts_fts
    rowids were auto-assigned and unrelated to its dedup rowids, so
    rowid-based deletes against one would remove unrelated rows.
    needs_backfill() already returned True for such a file, but only the
    read path acts on that -- open_writer would otherwise write straight
    into the stale file. Dropping here also supersedes the old #152 caveat
    (a v2 file got facts_dedup created empty, never backfilled, with the
    version never bumped, leaving the dedup guard under-protecting until
    some read happened to trigger a rebuild).
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fact_index.py -q`

Expected: PASS, 54 tests. Every pre-existing test must still pass — in particular `test_open_writer_is_idempotent` and `test_insert_facts_dedup_persists_across_writer_reopen`, which are the standing oracles for "reopening a writer is not destructive."

- [ ] **Step 9: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "Wipe the fact index on a schema_version mismatch

Bumps _SCHEMA_VERSION to 4 and makes ensure_schema drop-and-recreate all
three tables when the stamped version doesn't match. Dropping index_meta
takes the 'backfilled' sentinel with it, so needs_backfill() stays True and
the next read repopulates from the graph -- a stale-version file becomes the
already-working 'index file missing' case.

This is the guard that lets the next commit tie facts_fts.rowid to
facts_dedup.rowid: a v3 file's fts rowids are auto-assigned and unrelated to
its dedup rowids, and open_writer would otherwise write straight into one.

Also supersedes the #152 v2-file caveat, where facts_dedup was created empty
and the version never bumped.

Refs #236"
```

---

### Task 2: Measure both delete paths (the red-green signal)

This task lands **before** the rewrite, and that is the point. `evals/at_scale/bench_index_delete_cost.py` calls `fact_index.delete_facts`, so once Task 3 changes it the script would silently start reporting only the new numbers and the baseline would become unreproducible. Adding the legacy statement inline first gives a harness that measures both paths side by side — run now it shows them *identical and slow* (the red), and re-run after Task 3 it shows the split (the green). It is also what keeps the speedup claim checkable long after the fact.

**Files:**
- Modify: `evals/at_scale/bench_index_delete_cost.py`

**Interfaces:**
- Consumes: `fact_index.insert_facts` and `fact_index.delete_facts` as they exist today, pre-rewrite. Task 1 is not required, but landing after it keeps the branch linear.
- Produces: `bench_delete_legacy(con, n_delete, per_call) -> float` — seconds elapsed, mirroring the existing `bench_delete(con, n_delete, per_call) -> float`. Task 3's final step re-runs this script.

- [ ] **Step 1: Add a legacy-delete function**

Insert after `build_index` in `evals/at_scale/bench_index_delete_cost.py`:

```python
def bench_delete_legacy(con, n_delete, per_call):
    """The pre-#236 delete: an equality DELETE straight against facts_fts.
    Inlined here rather than called through fact_index so these numbers stay
    reproducible once delete_facts switches to rowids -- run before that
    switch, this is byte-for-byte what delete_facts itself does, so the two
    columns in D1 below should agree."""
    rows = [
        (f":e/n{i}", ":attr", f"value number {i} with some text")
        for i in range(n_delete)
    ]
    t0 = time.perf_counter()
    for start in range(0, n_delete, per_call):
        chunk = rows[start:start + per_call]
        con.executemany(
            "DELETE FROM facts_fts WHERE entity = ? AND attribute = ? AND value = ? "
            "AND valid_to IS NULL", chunk)
        con.executemany(
            "DELETE FROM facts_dedup WHERE entity = ? AND attribute = ? AND value = ? "
            "AND valid_to = ''", chunk)
    con.commit()
    return time.perf_counter() - t0
```

- [ ] **Step 2: Report both paths in D1**

Replace the `=== D1: ... ===` block in `main` with:

```python
    print("=== D1: delete 200 rows one call, vary index size ===")
    print(f"{'index rows':>11} {'legacy ms/triple':>18} {'delete_facts ms/triple':>24} {'ratio':>8}")
    for n_rows in (5_000, 20_000, 80_000):
        con, _ = build_index(tmpdir, f"d1_legacy_{n_rows}", n_rows)
        legacy = bench_delete_legacy(con, 200, 200) / 200 * 1000
        con.close()
        con, _ = build_index(tmpdir, f"d1_current_{n_rows}", n_rows)
        current = bench_delete(con, 200, 200) / 200 * 1000
        con.close()
        print(f"{n_rows:>11} {legacy:>18.4f} {current:>24.4f} {legacy / current:>7.0f}x")
```

Each path gets its own freshly built index so neither measures a table the other already shrank.

- [ ] **Step 3: Update the module docstring**

The docstring currently ends with a paragraph beginning "Prediction if that is the cause:" and closing with a `.venv/bin/python` invocation line. That prediction has been confirmed. Replace that final paragraph with:

```
Confirmed: delete cost per triple grows linearly with the number of rows in
facts_fts, and is unaffected by how many triples are batched into one
delete_facts call. D1 below runs the legacy equality DELETE (inlined, so it
stays measurable) against whatever delete_facts currently does, on freshly
built indexes of the same size. Before #236's rowid fix the two columns
agree, because they are the same statement; after it they diverge by orders
of magnitude, and that divergence is the fix's evidence.

    .venv/bin/python evals/at_scale/bench_index_delete_cost.py
```

- [ ] **Step 4: Run the benchmark and record the baseline**

Run: `.venv/bin/python evals/at_scale/bench_index_delete_cost.py`

Expected — and this is the red: **both columns agree**, at roughly 0.8 / 2.9 / 11.1 ms/triple across 5k / 20k / 80k rows, with the ratio column near `1x`. `delete_facts` is still the legacy statement at this point, so anything other than rough agreement means `bench_delete_legacy` does not faithfully reproduce it and must be fixed before proceeding.

Save the printed table — Task 3's final step re-runs this script and the two outputs together are the evidence.

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/bench_index_delete_cost.py
git commit -m "Measure the legacy fact-index delete alongside delete_facts

delete_facts is about to switch to rowids, at which point this script would
silently report only the new numbers and drop the baseline entirely. Inlines
the equality DELETE so both paths are measured against freshly built indexes
of the same size, and the speedup stays checkable afterwards.

Run now, before the switch, the two columns agree -- they are the same
statement. Paste the printed D1 table here as the recorded baseline.

Refs #236"
```

Replace the last line of that message with the actual table before committing.

---

### Task 3: Delete by rowid

**Files:**
- Modify: `fact_index.py:170-215` (`insert_facts`), `fact_index.py:217-246` (`delete_facts`), `fact_index.py:29-36` (the `facts_dedup` schema comment)
- Test: `tests/test_fact_index.py`

**Interfaces:**
- Consumes: `_SCHEMA_VERSION == "4"` and the version-aware `ensure_schema` from Task 1 — without them this change is unsafe against existing index files. Also the D1 baseline table from Task 2.
- Produces: no signature changes. `insert_facts(con, triples)` and `delete_facts(con, triples)` keep their exact parameter and return types (`-> None`). The new invariant other code may rely on: for every row in `facts_fts` there is a `facts_dedup` row at the same rowid.

- [ ] **Step 1: Write the invariant tests**

These pin the identity the rewrite makes load-bearing. **They are expected to pass before the change as well as after** — see the ordering note in File Structure: both tables already assign `max(rowid)+1` and are already written in lockstep, so the identity holds accidentally today. Writing them first still matters, for two reasons: they are written against the spec rather than fitted to the implementation, and if the rewrite breaks the identity they are the only thing that catches it.

Add to `tests/test_fact_index.py`:

```python
def test_insert_facts_assigns_dedup_rowid_as_fts_rowid(tmp_path):
    """#236: the whole fix rests on this identity. delete_facts seeks
    facts_dedup's B-tree for a rowid and then deletes facts_fts by that
    rowid -- if the two ever diverge, a retract silently removes an
    unrelated fact from the index."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(con, [
            (":decision/a", ":description", "first", None, None),
            (":decision/b", ":description", "second", None, None),
            (":decision/c", ":description", "third", None, None),
        ])
        con.commit()
        fts = dict(con.execute("SELECT entity, rowid FROM facts_fts").fetchall())
        dedup = dict(con.execute("SELECT entity, rowid FROM facts_dedup").fetchall())
        assert len(fts) == 3
        assert fts == dedup
    finally:
        fact_index.close_writer(con)


def test_rowid_identity_survives_delete_and_reinsert(tmp_path):
    """SQLite recycles a b-tree rowid once it's freed. A delete/reinsert
    cycle therefore reuses the rowid its own fts row just vacated -- which
    is only safe because the two are deleted in lockstep. If a stale fts row
    survived at that rowid, the reinsert would raise IntegrityError."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        for _ in range(3):
            fact_index.insert_facts(con, [triple])
            con.commit()
            fts = con.execute("SELECT rowid FROM facts_fts").fetchall()
            dedup = con.execute("SELECT rowid FROM facts_dedup").fetchall()
            assert len(fts) == 1
            assert fts == dedup
            fact_index.delete_facts(con, [triple])
            con.commit()
            assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (0,)
            assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)
    finally:
        fact_index.close_writer(con)


def test_delete_facts_removes_every_current_row_regardless_of_valid_from(tmp_path):
    """insert_facts deliberately keeps the same (entity, attribute, value) at
    two distinct valid_from values as two rows (see
    test_insert_facts_keeps_distinct_valid_from_as_separate_rows), and
    delete_facts is deliberately not scoped to a valid_from -- it doesn't
    know which one the current row carries. So the rowid lookup must return
    a list and clear all of them, not seek a single row."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        fact_index.insert_facts(
            con, [(":tag/v1", ":name", "v1", "2026-01-01T00:00:00.000Z", None)])
        fact_index.insert_facts(
            con, [(":tag/v1", ":name", "v1", "2026-02-01T00:00:00.000Z", None)])
        con.commit()
        assert len(con.execute(
            "SELECT * FROM facts_fts WHERE entity = ':tag/v1'").fetchall()) == 2

        fact_index.delete_facts(con, [(":tag/v1", ":name", "v1", None, None)])
        con.commit()
        assert con.execute("SELECT * FROM facts_fts WHERE entity = ':tag/v1'").fetchall() == []
        assert con.execute("SELECT * FROM facts_dedup WHERE entity = ':tag/v1'").fetchall() == []
    finally:
        fact_index.close_writer(con)


def test_delete_facts_tolerates_an_orphan_dedup_row(tmp_path):
    """Dedup-first insert ordering plus fts-first delete ordering means the
    only inconsistency physically reachable is a dedup row whose fts row is
    missing (an fts insert that failed after the dedup insert committed).
    Deleting it must be a clean no-op on facts_fts and must still clear the
    dedup row, so the rowid is freed rather than blocking a later reinsert."""
    path = str(tmp_path / "t.fts.sqlite3")
    con = fact_index.open_writer(path)
    try:
        triple = (":decision/x", ":description", "hello", "2026-01-01T00:00:00.000Z", None)
        fact_index.insert_facts(con, [triple])
        con.commit()
        con.execute("DELETE FROM facts_fts")
        con.commit()

        fact_index.delete_facts(con, [triple])
        con.commit()
        assert con.execute("SELECT count(*) FROM facts_dedup").fetchone() == (0,)

        fact_index.insert_facts(con, [triple])
        con.commit()
        assert con.execute("SELECT count(*) FROM facts_fts").fetchone() == (1,)
    finally:
        fact_index.close_writer(con)
```

- [ ] **Step 2: Run the tests and confirm all four pass**

Run: `.venv/bin/python -m pytest tests/test_fact_index.py -q`

Expected: PASS, 58 tests. All four new tests pass against the *unmodified* `insert_facts`/`delete_facts`, for the reason given in Step 1.

If any of the four fails here, do not proceed and do not adjust the test to match — a failure means the rowid identity does not hold even accidentally today, which contradicts the measurement this plan was built on, and the whole approach needs re-examining before any code changes.

- [ ] **Step 3: Assign the FTS5 rowid explicitly in `insert_facts`**

Replace the second `con.execute` in `insert_facts`'s loop body (`fact_index.py:210-214`):

```python
        con.execute(
            "INSERT INTO facts_fts (rowid, entity, attribute, value, valid_from, valid_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cur.lastrowid, entity, attribute, value, valid_from, valid_to),
        )
```

`cur.lastrowid` is only read here, after the `if cur.rowcount == 0: continue` guard above it. That ordering is load-bearing: on an *ignored* `INSERT OR IGNORE`, `lastrowid` retains the previous successful insert's value rather than being cleared, so reading it unguarded would point at the wrong row.

Append to `insert_facts`'s docstring:

```
    The dedup row's rowid is assigned explicitly as the facts_fts rowid
    (#236), making the two tables' rowids the same number by construction --
    that identity is what lets delete_facts seek facts_dedup's B-tree and
    then delete from facts_fts by rowid, instead of scanning the FTS5 table.
    Reading cur.lastrowid is only valid because of the rowcount guard above
    it: on an ignored INSERT OR IGNORE, lastrowid holds the PREVIOUS
    successful insert's rowid rather than being cleared.

    Dedup is written first, so the only inconsistency this path can leave
    behind is a dedup row whose fts insert failed -- costing one unindexed
    fact. The reverse, an fts row with no dedup row, would be far worse (its
    rowid could later be recycled by a new dedup row into a collision) and
    is unreachable from here.
```

- [ ] **Step 4: Rewrite `delete_facts` to seek and delete by rowid**

Replace the two `con.executemany` calls in `delete_facts` (`fact_index.py:237-246`) with:

```python
    for entity, attribute, value, _valid_from, _valid_to in triples:
        rowids = [
            row[0] for row in con.execute(
                "SELECT rowid FROM facts_dedup WHERE entity = ? AND attribute = ? "
                "AND value = ? AND valid_to = ''",
                (entity, attribute, value),
            )
        ]
        if not rowids:
            continue
        params = [(rowid,) for rowid in rowids]
        con.executemany("DELETE FROM facts_fts WHERE rowid = ?", params)
        con.executemany("DELETE FROM facts_dedup WHERE rowid = ?", params)
```

Replace `delete_facts`'s docstring body (keeping the first paragraph about current-rows-only semantics) with:

```python
def delete_facts(
    con: sqlite3.Connection,
    triples: Sequence[Tuple[str, str, str, Optional[str], Optional[str]]],
) -> None:
    """Delete matching CURRENT rows from facts_fts (valid_to IS NULL only).
    Does not commit -- see insert_facts. Historical rows for the same
    (entity, attribute, value) from an earlier lifecycle are never touched
    by a retract -- only the live, open-ended assertion is removed.

    Seeks facts_dedup for the rowid and then deletes facts_fts BY that rowid
    (#236). The obvious equality DELETE against facts_fts costs O(index size)
    per triple: FTS5 maintains a full-text index, not a B-tree over column
    values, so `WHERE entity = ?` cannot seek and scans the whole table.
    Measured at an 80,000-row index, that was 11.088 ms per deleted triple
    against 0.0127 ms for the rowid form, and it was 73.5% of a full at-scale
    ingestion's wall clock. Batching does not help -- the cost follows facts,
    not calls -- which is why this stayed a per-triple loop rather than one
    big executemany.

    The lookup is a seek on the (entity, attribute, value) prefix of
    facts_dedup's UNIQUE(entity, attribute, value, valid_from, valid_to)
    index. It deliberately does NOT constrain valid_from, matching the
    equality DELETE it replaced -- delete_facts doesn't know which valid_from
    the current row carries, and the same (entity, attribute, value) can hold
    several current rows at distinct valid_from values, all of which go. The
    valid_to = '' filter is the normalized-NULL sentinel insert_facts writes,
    corresponding one-to-one with facts_fts's valid_to IS NULL.

    facts_fts is deleted BEFORE facts_dedup, and the order is load-bearing. A
    failure between the two leaves an orphan dedup row: harmless, invisible
    to queries, and cleared by the next delete of that triple. The reverse
    order would leave an orphan facts_fts row -- permanently stale in query
    results, holding a rowid that a later dedup insert could recycle into an
    IntegrityError.

    Clearing facts_dedup is also required for correctness independent of the
    rowid scheme -- code-review finding on #152: a stale dedup row left after
    a retract would make a later insert_facts call for the same (entity,
    attribute, value, valid_from) silently no-op, since the dedup guard can't
    distinguish "already indexed and still live" from "was indexed once,
    since retracted."
    """
```

- [ ] **Step 5: Update the `facts_dedup` schema comment**

The comment at `fact_index.py:29-36` explains `facts_dedup` as an insert-path concern only. Append to it, immediately before `_DEDUP_SCHEMA_SQL`:

```python
# As of #236 this table also serves the DELETE path: its implicit rowid IS
# the corresponding facts_fts rowid (assigned explicitly by insert_facts), so
# delete_facts seeks here and then deletes from facts_fts by rowid rather
# than scanning it. That is why _SCHEMA_VERSION moved to 4 -- in a v3 file
# the facts_fts rowids were auto-assigned and unrelated to these, so the
# identity does not hold and ensure_schema wipes such a file.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fact_index.py -q`

Expected: PASS, 58 tests. The pre-existing delete tests are the semantic oracle for this rewrite and must pass untouched: `test_delete_facts_only_deletes_current_rows`, `test_delete_removes_matching_row`, `test_delete_only_removes_exact_match`, and `test_insert_facts_after_delete_is_not_silently_swallowed`.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`

Expected: PASS. `tests/test_mcp_server.py:8563` has a test that depends on `delete_facts`' `valid_to IS NULL` scoping through the MCP write path — it must stay green, since that scoping is preserved exactly.

- [ ] **Step 8: Re-run the benchmark — this is the green**

Run: `.venv/bin/python evals/at_scale/bench_index_delete_cost.py`

Expected: D1's two columns, which agreed in Task 2 Step 4, now diverge. Legacy still grows roughly 4x per 4x index size (~0.8 / ~2.9 / ~11.1 ms/triple); `delete_facts` stays flat near ~0.01 ms/triple, with the ratio column reaching several hundred x at 80,000 rows. D2 (batching, now on the rowid path) should be flat *and* fast across all three call shapes, where before it was flat and slow.

A ratio near `1x` at 80,000 rows means the rewrite did not take effect — most likely `delete_facts` is still falling back to a scan, or the index under test was built by a stale-version file that Task 1's wipe emptied. Investigate before committing.

- [ ] **Step 9: Commit**

```bash
git add fact_index.py tests/test_fact_index.py
git commit -m "Delete fact-index rows by rowid instead of scanning FTS5

facts_fts is an FTS5 virtual table, which maintains a full-text index rather
than a B-tree over its columns, so delete_facts' equality DELETE could not
seek and scanned the whole table per deleted triple: 11.088 ms/triple at an
80,000-row index, and 73.5% of a full at-scale ingestion's wall clock.
Batching never helped because the cost follows facts, not calls.

facts_dedup is already a B-tree table holding exactly the key a delete needs.
insert_facts now assigns that row's rowid explicitly as the facts_fts rowid,
so delete_facts seeks the B-tree and deletes by rowid: 0.0127 ms/triple, and
flat in index size rather than linear.

facts_fts is deleted before facts_dedup so a failure between the two leaves
an orphan dedup row (harmless, self-clearing) rather than an orphan fts row
(permanently stale, and its rowid recyclable into a collision).

Paste the post-change D1 table here; the pre-change one is in the previous
commit's message.

Refs #236"
```

Replace that instruction line with the actual table before committing.

---

### Task 4: Validate at scale and file the successor issue

The spec commits to measuring rather than predicting what comes next. This task produces the evidence and hands off.

**Files:**
- No source changes. Produces a benchmark result under `evals/at_scale/results/` and a new GitHub issue.

**Interfaces:**
- Consumes: Tasks 1-3, all merged or at least all committed on the branch.
- Produces: a fresh per-call-site attribution table, and a new issue number for the next bottleneck.

- [ ] **Step 1: Run the full at-scale ingestion benchmark**

Run: `.venv/bin/python evals/at_scale/run_ingestion_benchmark.py`

This takes roughly 85 minutes. Run it in the background and check back rather than blocking on it. Read `evals/at_scale/benchmark.md` first for the tier's conventions.

Expected: completion (not a kill), with total wall clock in the 1,500-1,700 s range against the 78.87 s forward-only baseline — roughly 20x, down from 65x. A result far outside that band means the projection was wrong and needs explaining before anything is claimed.

- [ ] **Step 2: Confirm the `_candidate_diff_purge_legacy` regression is gone**

The spec attributes its O(N²) behaviour (500 records 0.50 s, 2,000 records 7.24 s, 8,000 records 126.71 s on master) to this same root cause. Verify against the new build.

If it survives, that attribution was wrong — say so plainly and file it as its own issue rather than folding it into the next one.

- [ ] **Step 3: File the successor issue**

Whatever the post-fix attribution table shows as the new dominant cost gets its own issue, with the measured table in the body — the same shape #236 itself used. `_resolved_facts_triples`' `_query_ident` calls are the expected candidate, but the table decides, not the expectation.

Do **not** re-file batching `_correction_sweep_apply`'s case-3 `:modified-in` retract without checking it against the new numbers first. #236's "What NOT to do" section explains at length why it was worthless *before* this fix; whether it is worth anything after is exactly what this table answers.

- [ ] **Step 4: Open the PR**

Body must reference `#236` without a closing keyword (see Global Constraints) if the successor work is still outstanding, or close it if the validation is complete and the successor issue is filed. Include both D1 tables (Task 2 Step 4's baseline and Task 3 Step 8's result) and the attribution table from Step 1 above.

`master` requires an approving review on top of green CI. Ask before using `--admin`.

---

## Notes for the implementer

**The one thing that can go badly wrong here** is the rowid identity silently not holding — a delete would then remove an unrelated fact from the index, and nothing would fail loudly. `test_insert_facts_assigns_dedup_rowid_as_fts_rowid` and `test_rowid_identity_survives_delete_and_reinsert` are the guards. Because they pass before the change as well as after (see File Structure), they are easy to mistake for redundant and delete during review. They are not: they are the only thing standing between a future edit and silent index corruption.

**Mechanics already verified** (do not re-derive; they were spiked before the spec was written):

- `DELETE FROM facts_fts WHERE rowid = ?` is flat in index size — 0.0095 / 0.0124 / 0.0127 ms/triple at 5k / 20k / 80k rows.
- FTS5 accepts an explicitly assigned rowid on insert.
- B-tree rowids are recycled after a delete frees them.
- Inserting at an already-used FTS5 rowid raises `IntegrityError` — a broken invariant fails loudly rather than clobbering.
- After an *ignored* `INSERT OR IGNORE`, `lastrowid` is stale while `rowcount` is 0.

**`rebuild_index` needs no change.** It already drops and recreates all three tables inside its `BEGIN IMMEDIATE`, stamps the version with `INSERT OR REPLACE`, and calls `insert_facts` — so it picks up the rowid assignment automatically. Do not add the version check to it; its docstring explains why it inlines the schema statements instead of calling `ensure_schema`.
