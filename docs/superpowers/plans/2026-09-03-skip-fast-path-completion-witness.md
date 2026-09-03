# Skip Fast-Path for Already-Ingested Positions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reverse ingestion stream skip a position it can PROVE was
already written completely, so a discarded frontier interval costs seconds
instead of the ~18 hours #325 measured — without ever skipping a #313-style
torn write.

**Architecture:** `_frontier_load`'s discard branch records the doomed
interval's bounds into a new `:type/completed-region` entity before retracting
them. Because `_frontier_persist_claim` is the LAST write of a position, a
position inside a persisted interval provably completed, and the archive
inherits that. `_run_ingestion` loads the regions once, and `submit_next` skips
a `rev` claim landing inside a `:provisional` region before it queues
`_extract_commit`. A torn position was never inside a persisted interval, so it
is in no region and is never skipped.

**Tech Stack:** Python 3.12, minigraf 2.0.0 (Rust bi-temporal graph), pytest,
SQLite FTS5 fact index, tree-sitter.

**Spec:** `docs/superpowers/specs/2026-09-03-skip-fast-path-completion-witness-design.md`

## Global Constraints

- **Interpreter is always `.venv/bin/python`.** System python has minigraf
  1.1.1 against a `>=2.0.0,<3.0.0` floor and fakes ~122 test failures. Every
  command in this plan uses it.
- **Real backend only.** No `MagicMock` fake of `MiniGrafDb`. Use the `real_db`
  fixture (in-memory) or a real file-backed DB — `docs/testing-conventions.md`.
- **New persistence functions call internal `_transact`/`_retract` only**, never
  `handle_minigraf_transact`/`handle_minigraf_retract`. The public handler's
  `_validate_facts` rejects any string-valued triple naming an entity type not
  in `MINIGRAF_SCHEMA`, and `:type/completed-region` is deliberately unregistered.
- **`:type/completed-region` must NOT be added to `MINIGRAF_SCHEMA`.**
  `handle_minigraf_audit` iterates exactly the registered types and retracts any
  attribute outside a registered type's allowed set. Staying unregistered is
  what makes it invisible to audit — the same status `:type/ingest-interval`
  holds.
- **A deterministic ident is not idempotency.** minigraf creates a duplicate
  live datom when the same (entity, attribute, value) is re-transacted at a new
  `valid_from` (#156). Every new write function reads current state first and
  writes only the difference. Every idempotency test counts RAW facts, never
  just the value read back through a `results[0]` shortcut.
- **Never batch `:contains`, `:depends-on` or `:parent`** into one transact
  (minigraf#287, still open and version-invariant). No task here touches those
  loops; do not "simplify" them if you pass through.
- **No `GRAPH_FORMAT_VERSION` bump.** This change only ADDS facts going forward;
  it does not make any stored fact unreadable by current code. Existing graphs
  stay readable and simply never skip until their first discard.
- **Commit messages** end with the two attribution lines used on this branch;
  use `Refs #326`, never a closing keyword (`Fixes`/`Closes`) — the issue is
  closed by the merge PR body, not by a commit.

---

## File Structure

- `mcp_server.py` — all production code. The repo is a flat single-module
  layout for the server; the new functions sit next to the frontier ones they
  extend (`_frontier_read_bounds` … `_frontier_persist_claim`, around lines
  5948–6260). No new module: these are four small functions plus a predicate,
  and splitting them out would separate them from `_frontier_load`, the only
  thing that writes them.
- `tests/test_mcp_server.py` — all tests, in new classes appended near the
  existing `TestFrontierLoad` / `TestFrontierPersistClaim` /
  `TestReverseApplyTornWriteResume` families.
- `SKILL.md`, `tools/ingest_status.json`, `CLAUDE.md` — documentation, Task 8.

---

### Task 1: `:type/completed-region` fact model

**Files:**
- Modify: `mcp_server.py` — insert after `_frontier_discard_interval` (~line 6172)
- Test: `tests/test_mcp_server.py` — new class `TestCompletedRegionRecord`, place it immediately after `TestFrontierLoadNormalisesUnrepresentableIntervals`

**Interfaces:**
- Consumes: `_transact`, `_retract`, `_db_execute`, `_edn_escape` (all existing in `mcp_server.py`)
- Produces:
  - `_COMPLETED_REGION_ENTITY_TYPE: str = ":type/completed-region"`
  - `_completed_region_ident(lo_hash: str) -> str`
  - `_completed_regions_read(db) -> List[Tuple[str, str, str]]` — list of `(lo_hash, hi_hash, tag)`, `tag` being the string `":provisional"` or `":authoritative"`
  - `_completed_region_record(db, lo_hash: str, hi_hash: str, tag: str, run_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestCompletedRegionRecord:
    """#326: the skip fast-path's completion witness. `_frontier_persist_claim`
    is the LAST write of a position, so a position inside a persisted interval
    provably completed. `_frontier_load` throws that interval away on tip
    growth; these facts are the archive that keeps the proof.

    Deliberately NOT in MINIGRAF_SCHEMA: handle_minigraf_audit iterates exactly
    the registered types and retracts any attribute outside a registered type's
    allowed set, so an unregistered companion type is invisible to it -- the
    same status :type/ingest-interval already holds.
    """

    def test_records_a_region_and_reads_it_back(self, real_db):
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-01T00:00:00Z"
        )
        assert mcp_server._completed_regions_read(real_db) == [("h1", "h4", ":provisional")]

    def test_recording_the_same_region_twice_does_not_duplicate_facts(self, real_db):
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-01T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-02T00:00:00Z"
        )
        assert mcp_server._completed_regions_read(real_db) == [("h1", "h4", ":provisional")]
        # Count RAW facts. minigraf is not idempotent at the graph level for a
        # re-transact of the same (entity, attribute, value) at a new
        # valid-from (#156), and _completed_regions_read's own dedup would
        # collapse a duplicate live datom and hide a broken guard.
        ident = mcp_server._completed_region_ident("h1")
        raw = mcp_server._db_execute(
            real_db, f"(query [:find (count ?lo) :where [{ident} :lo-hash ?lo]])"
        )
        assert json.loads(raw)["results"] == [[1]]

    def test_re_recording_an_older_smaller_region_does_not_clobber_an_advanced_one(self, real_db):
        """The direction a "same value stays the same" test cannot see. If the
        guard were ignored, the second (larger) region would be recorded and the
        third call would either duplicate it or shrink it back."""
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h3", "h4", ":provisional", "2026-01-01T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-02T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h3", "h4", ":provisional", "2026-01-03T00:00:00Z"
        )
        assert mcp_server._completed_regions_read(real_db) == [("h1", "h4", ":provisional")]

    def test_overlapping_regions_coalesce(self, real_db):
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h3", "h6", ":provisional", "2026-01-01T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-02T00:00:00Z"
        )
        assert mcp_server._completed_regions_read(real_db) == [("h1", "h6", ":provisional")]

    def test_disjoint_regions_stay_separate(self, real_db):
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h5", "h6", ":provisional", "2026-01-01T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h1", "h2", ":provisional", "2026-01-02T00:00:00Z"
        )
        assert mcp_server._completed_regions_read(real_db) == [
            ("h1", "h2", ":provisional"),
            ("h5", "h6", ":provisional"),
        ]

    def test_regions_of_different_tags_never_coalesce(self, real_db):
        """The authoritative/provisional boundary is the lineage frontier, and
        merging across it would license a forward skip over a provisional
        region -- the exact thing _skip_claim's tag check exists to prevent."""
        import mcp_server
        mcp_server._completed_region_record(
            real_db, "h1", "h4", ":provisional", "2026-01-01T00:00:00Z"
        )
        mcp_server._completed_region_record(
            real_db, "h2", "h5", ":authoritative", "2026-01-02T00:00:00Z"
        )
        assert sorted(mcp_server._completed_regions_read(real_db)) == [
            ("h1", "h4", ":provisional"),
            ("h2", "h5", ":authoritative"),
        ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCompletedRegionRecord -v`
Expected: FAIL, `AttributeError: module 'mcp_server' has no attribute '_completed_region_record'`

- [ ] **Step 3: Write the implementation**

Insert into `mcp_server.py` directly after `_frontier_discard_interval`:

```python
_COMPLETED_REGION_ENTITY_TYPE = ":type/completed-region"


def _completed_region_ident(lo_hash: str) -> str:
    """Deterministic ident for the archived region starting at lo_hash.

    Not a public schema type -- :type/completed-region is deliberately absent
    from MINIGRAF_SCHEMA, so handle_minigraf_audit's registered-type loop never
    scans for it (same status as :type/ingest-interval). Every write below goes
    through the internal _transact/_retract helpers; the public handler's
    _validate_facts would reject an unregistered type outright.
    """
    return f":ingestion/completed-region-{lo_hash[:12]}"


def _completed_regions_read(db: Any) -> List[Tuple[str, str, str]]:
    """Every archived completed region, as (lo_hash, hi_hash, tag), sorted by
    lo_hash.

    Binds ?ident rather than ?e. `[?e :entity-type :type/completed-region]`
    answers in UUID space -- _count_commit_entities gets away with that pattern
    only because it counts and never reads ?e back. The string-valued :ident
    fact each region carries is what makes this enumeration (and the retract in
    _completed_region_record) work without UUID-to-ident resolution.
    """
    raw = _db_execute(
        db,
        "(query [:find ?ident ?lo ?hi ?tag :where"
        f" [?e :entity-type {_COMPLETED_REGION_ENTITY_TYPE}]"
        " [?e :ident ?ident] [?e :lo-hash ?lo] [?e :hi-hash ?hi] [?e :tag ?tag]])",
    )
    seen = set()
    out: List[Tuple[str, str, str]] = []
    for _ident, lo, hi, tag in json.loads(raw).get("results", []):
        key = (lo, hi, str(tag))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return sorted(out)


def _completed_region_facts(lo_hash: str, hi_hash: str, tag: str) -> str:
    ident = _completed_region_ident(lo_hash)
    return "[" + " ".join([
        f"[{ident} :entity-type {_COMPLETED_REGION_ENTITY_TYPE}]",
        f'[{ident} :ident "{ident}"]',
        f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]',
        f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]',
        f"[{ident} :tag {tag}]",
    ]) + "]"


def _completed_region_record(
    db: Any,
    lo_hash: str,
    hi_hash: str,
    tag: str,
    run_ts_iso: str,
    index_con: Optional[Any] = None,
    order: Optional[Dict[str, int]] = None,
) -> None:
    """Archive [lo_hash, hi_hash] as a completed region, coalescing it into the
    existing same-tag set.

    Query-before-write, the guard _watermark_update and _frontier_persist_claim
    established: a deterministic ident only guarantees repeated writes target
    the SAME entity, it does not stop minigraf creating a duplicate live datom
    for a re-transact at a new valid-from (#156). The current set is read first
    and only the difference is written -- a call that changes nothing writes
    nothing.

    `order` maps hash -> position for the current linearization. Regions can
    only be compared (and therefore coalesced) when both endpoints are
    orderable; a region whose hashes are not in `order` is kept as-is and never
    merged. Callers inside a run pass the linearization's map; the coalescing
    tests pass a lexicographic fallback via order=None, which sorts by hash.
    """
    def key(h: str) -> Any:
        return order[h] if order is not None and h in order else h

    current = _completed_regions_read(db)
    same_tag = sorted(
        [(lo, hi) for lo, hi, t in current if t == tag] + [(lo_hash, hi_hash)],
        key=lambda p: key(p[0]),
    )
    merged: List[Tuple[str, str]] = []
    for lo, hi in same_tag:
        if merged and key(lo) <= key(merged[-1][1]):
            prev_lo, prev_hi = merged[-1]
            merged[-1] = (prev_lo, hi if key(hi) > key(prev_hi) else prev_hi)
        else:
            merged.append((lo, hi))

    target = sorted(
        [(lo, hi, tag) for lo, hi in merged] + [r for r in current if r[2] != tag]
    )
    if target == current:
        return

    for lo, hi, t in current:
        if (lo, hi, t) not in target:
            _retract(db, _completed_region_facts(lo, hi, t), index_con=index_con)
    for lo, hi, t in target:
        if (lo, hi, t) not in current:
            _transact(
                db, _completed_region_facts(lo, hi, t), run_ts_iso, index_con=index_con
            )
```

Note on the coalescing test data: `h1 < h2 < … < h6` lexicographically, so the
`order=None` fallback orders them exactly as the tests expect. Real callers
always pass a position map.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCompletedRegionRecord -v`
Expected: 6 passed

- [ ] **Step 5: Verify nothing else regressed**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -x -q -k "Frontier or Audit"`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Add the :type/completed-region fact model (#326)

The archive that keeps the skip fast-path's proof. Deliberately unregistered
in MINIGRAF_SCHEMA so handle_minigraf_audit never scans for it, written only
through the internal _transact/_retract helpers, and guarded query-before-write
because a deterministic ident does not make a re-transact idempotent (#156).

Regions carry a string-valued :ident because enumerating by :entity-type binds
the entity in UUID space; :ingestion/format-version already sets that precedent.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 2: Archive the interval before `_frontier_load` discards it

**Files:**
- Modify: `mcp_server.py` — `_frontier_load`'s discard branch (~line 6152), immediately before the `_frontier_discard_interval(...)` call
- Test: `tests/test_mcp_server.py` — new class `TestFrontierLoadArchivesDiscardedInterval`, place immediately after `TestCompletedRegionRecord`

**Interfaces:**
- Consumes: `_completed_region_record`, `_completed_region_ident`, `_completed_regions_read` (Task 1)
- Produces: no new symbol. `_frontier_load`'s signature and return type are UNCHANGED — roughly a dozen existing call sites and tests use its result directly as an allocator.

- [ ] **Step 1: Write the failing tests**

```python
class TestFrontierLoadArchivesDiscardedInterval:
    """#326: the discard branch's bounds ARE the completion witness --
    _frontier_persist_claim is the last write of a position, so every position
    inside the persisted interval provably completed. Recording them before the
    retract is what makes the witness survive the one case (#325's tip growth)
    that ruled out reading the live interval directly."""

    def _seed_high(self, db, lo_hash, hi_hash):
        import mcp_server
        facts = [
            f"[{mcp_server._FRONTIER_HIGH_IDENT} :entity-type :type/ingest-interval]",
            f"[{mcp_server._FRONTIER_HIGH_IDENT} :tag :provisional]",
            f'[{mcp_server._FRONTIER_HIGH_IDENT} :lo-hash "{lo_hash}"]',
            f'[{mcp_server._FRONTIER_HIGH_IDENT} :hi-hash "{hi_hash}"]',
        ]
        mcp_server._transact(db, "[" + " ".join(facts) + "]", "2026-01-01T00:00:00Z")

    def test_discarded_interval_is_archived_as_a_provisional_region(self, real_db):
        import mcp_server
        self._seed_high(real_db, "h1", "h2")
        grown = ["h0", "h1", "h2", "h3", "h4"]

        mcp_server._frontier_load(real_db, grown, "2026-01-02T00:00:00Z")

        assert mcp_server._completed_regions_read(real_db) == [("h1", "h2", ":provisional")]
        assert mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT) is None

    def test_a_kept_interval_archives_nothing(self, real_db):
        """A live interval is already its own witness. Archiving one that was
        never discarded would grow the region set on every single run."""
        import mcp_server
        self._seed_high(real_db, "h2", "h3")

        mcp_server._frontier_load(real_db, ["h0", "h1", "h2", "h3"], "2026-01-02T00:00:00Z")

        assert mcp_server._completed_regions_read(real_db) == []

    def test_an_inverted_interval_is_discarded_but_not_archived(self, real_db):
        """An inverted pair (lo above hi) is what the pre-2b1 persist path
        produced; it does not describe a region that completed, so archiving it
        would license skipping positions that were never written."""
        import mcp_server
        self._seed_high(real_db, "h3", "h1")

        mcp_server._frontier_load(real_db, ["h0", "h1", "h2", "h3"], "2026-01-02T00:00:00Z")

        assert mcp_server._completed_regions_read(real_db) == []
        assert mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT) is None

    def test_two_discards_across_runs_coalesce_into_one_region(self, real_db):
        import mcp_server
        self._seed_high(real_db, "h3", "h4")
        mcp_server._frontier_load(real_db, ["h0", "h1", "h2", "h3", "h4", "h5"], "2026-01-02T00:00:00Z")
        self._seed_high(real_db, "h1", "h4")
        mcp_server._frontier_load(real_db, ["h0", "h1", "h2", "h3", "h4", "h5", "h6"], "2026-01-03T00:00:00Z")

        assert mcp_server._completed_regions_read(real_db) == [("h1", "h4", ":provisional")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLoadArchivesDiscardedInterval -v`
Expected: FAIL — `test_discarded_interval_is_archived_as_a_provisional_region` asserts `[] == [("h1","h2",":provisional")]`

- [ ] **Step 3: Write the implementation**

In `_frontier_load`, the discard branch currently reads:

```python
            _frontier_discard_interval(
                db, _FRONTIER_HIGH_IDENT, high_bounds, index_con=index_con
            )
```

Replace with:

```python
            # #326: the bounds are the completion witness -- archive before
            # retracting. _frontier_persist_claim is the LAST write of a
            # position, so every position inside this interval provably
            # completed; dropping the facts is what made that proof
            # unavailable in exactly the case (#325 tip growth) that most
            # needs it. Only a REPRESENTABLE interval is archived: the
            # inverted case below reaches the same branch and describes no
            # completed region at all.
            if hi_lo_pos <= hi_hi_pos:
                _completed_region_record(
                    db, high_bounds[0], high_bounds[1], ":provisional",
                    run_ts_iso, index_con=index_con,
                    order={h: i for i, h in enumerate(linearization)},
                )
            _frontier_discard_interval(
                db, _FRONTIER_HIGH_IDENT, high_bounds, index_con=index_con
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLoadArchivesDiscardedInterval -v`
Expected: 4 passed

- [ ] **Step 5: Verify the existing discard tests still hold**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLoad tests/test_mcp_server.py::TestFrontierLoadNormalisesUnrepresentableIntervals -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Archive the discarded high interval instead of only retracting it (#326)

_frontier_load drops frontier-high whenever it no longer tops out, and the
comment says the replay is safe. #325 measured what "only work is wasted"
costs: ~18h on ArangoDB origin/4.0. The bounds are recorded as a completed
region first, so the proof survives the discard.

Only a representable interval is archived. An inverted pair reaches the same
branch and describes no completed region, so archiving it would license
skipping positions that were never written.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 3: `_completed_regions_load` — map to positions, prune, drop unmappable

**Files:**
- Modify: `mcp_server.py` — insert after `_completed_region_record` (Task 1)
- Test: `tests/test_mcp_server.py` — new class `TestCompletedRegionsLoad`

**Interfaces:**
- Consumes: `_completed_regions_read`, `_completed_region_facts`, `_retract`, `frontier_registry.Interval`, `frontier_registry.TAG_PROVISIONAL` / `TAG_AUTHORITATIVE`
- Produces: `_completed_regions_load(db, linearization: List[str], allocator, index_con=None) -> List[frontier_registry.Interval]`

- [ ] **Step 1: Write the failing tests**

```python
class TestCompletedRegionsLoad:
    """#326: regions are persisted as HASHES (a position number is meaningless
    against a linearization that has grown) and consumed as POSITIONS. This is
    the mapping step, plus the two ways a region leaves the set."""

    def _record(self, db, lo, hi, tag=":provisional"):
        import mcp_server
        mcp_server._completed_region_record(db, lo, hi, tag, "2026-01-01T00:00:00Z")

    def test_maps_hashes_to_positions(self, real_db):
        import mcp_server
        import frontier_registry
        self._record(real_db, "h1", "h3")
        allocator = frontier_registry.FrontierAllocator(5, [])

        regions = mcp_server._completed_regions_load(
            real_db, ["h0", "h1", "h2", "h3", "h4"], allocator
        )

        assert regions == [frontier_registry.Interval(1, 3, frontier_registry.TAG_PROVISIONAL)]

    def test_region_covered_by_a_live_same_tag_interval_is_pruned_and_retracted(self, real_db):
        """The live interval is already its own witness, so the archive is
        redundant. Left in place it accumulates a fact set per run forever."""
        import mcp_server
        import frontier_registry
        self._record(real_db, "h1", "h2")
        allocator = frontier_registry.FrontierAllocator(
            4, [frontier_registry.Interval(1, 3, frontier_registry.TAG_PROVISIONAL)]
        )

        regions = mcp_server._completed_regions_load(real_db, ["h0", "h1", "h2", "h3"], allocator)

        assert regions == []
        assert mcp_server._completed_regions_read(real_db) == [], "the facts must go too"

    def test_region_covered_by_a_live_interval_of_the_OTHER_tag_is_kept(self, real_db):
        import mcp_server
        import frontier_registry
        self._record(real_db, "h1", "h2", ":provisional")
        allocator = frontier_registry.FrontierAllocator(
            4, [frontier_registry.Interval(0, 3, frontier_registry.TAG_AUTHORITATIVE)]
        )

        regions = mcp_server._completed_regions_load(real_db, ["h0", "h1", "h2", "h3"], allocator)

        assert regions == [frontier_registry.Interval(1, 2, frontier_registry.TAG_PROVISIONAL)]

    def test_unmappable_region_is_dropped_from_the_list_but_its_facts_are_kept(self, real_db):
        """A hash absent from this linearization means the branch moved under
        us -- mirrors _frontier_load's own precedent for a bound it cannot map.
        Dropping it only costs a re-walk; RETRACTING it would throw the witness
        away for good, and the branch may well come back."""
        import mcp_server
        import frontier_registry
        self._record(real_db, "gone1", "gone2")
        allocator = frontier_registry.FrontierAllocator(3, [])

        regions = mcp_server._completed_regions_load(real_db, ["h0", "h1", "h2"], allocator)

        assert regions == []
        assert mcp_server._completed_regions_read(real_db) == [
            ("gone1", "gone2", ":provisional")
        ]

    def test_no_regions_yields_an_empty_list(self, real_db):
        import mcp_server
        import frontier_registry
        allocator = frontier_registry.FrontierAllocator(3, [])
        assert mcp_server._completed_regions_load(real_db, ["h0", "h1", "h2"], allocator) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCompletedRegionsLoad -v`
Expected: FAIL, `AttributeError: module 'mcp_server' has no attribute '_completed_regions_load'`

- [ ] **Step 3: Write the implementation**

```python
_REGION_TAG_TO_FRONTIER_TAG = {
    ":provisional": frontier_registry.TAG_PROVISIONAL,
    ":authoritative": frontier_registry.TAG_AUTHORITATIVE,
}


def _completed_regions_load(
    db: Any,
    linearization: List[str],
    allocator: "frontier_registry.FrontierAllocator",
    index_con: Optional[Any] = None,
) -> List["frontier_registry.Interval"]:
    """Archived regions mapped into this run's position space, pruned.

    Kept separate from _frontier_load on purpose: the ARCHIVING has to live
    there (that is where the doomed bounds are), but widening _frontier_load's
    return to a tuple would break roughly a dozen call sites and tests that use
    its result directly as an allocator, for no gain.

    Two ways a region leaves the set, and they are not the same:
      * fully covered by a live SAME-TAG interval -- redundant, so the facts are
        RETRACTED as well, or the set grows by one per run forever;
      * an endpoint not in this linearization -- the branch moved under us, so
        it is dropped from the returned list but its facts are KEPT. Dropping
        costs a re-walk; retracting would destroy the witness for good.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    out: List["frontier_registry.Interval"] = []
    for lo_hash, hi_hash, tag in _completed_regions_read(db):
        frontier_tag = _REGION_TAG_TO_FRONTIER_TAG.get(str(tag))
        if frontier_tag is None:
            continue
        if lo_hash not in hash_to_pos or hi_hash not in hash_to_pos:
            continue
        lo_pos, hi_pos = hash_to_pos[lo_hash], hash_to_pos[hi_hash]
        if lo_pos > hi_pos:
            continue
        covered = any(
            iv.tag == frontier_tag and iv.lo_pos <= lo_pos and hi_pos <= iv.hi_pos
            for iv in allocator.intervals()
        )
        if covered:
            _retract(
                db, _completed_region_facts(lo_hash, hi_hash, str(tag)), index_con=index_con
            )
            continue
        out.append(frontier_registry.Interval(lo_pos, hi_pos, frontier_tag))
    return sorted(out, key=lambda iv: iv.lo_pos)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestCompletedRegionsLoad -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Map archived regions into position space and prune them (#326)

Regions persist as hashes because a position number is meaningless against a
linearization that has grown. Two ways a region leaves the set, and they are
not the same: covered by a live same-tag interval means redundant, so the facts
go too; an endpoint absent from this linearization means the branch moved, so
it drops from the list but its facts are KEPT -- dropping costs a re-walk,
retracting would destroy the witness for good.

Separate from _frontier_load rather than folded into its return, which a dozen
call sites use directly as an allocator.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 4: `_skip_claim` — the predicate

**Files:**
- Modify: `mcp_server.py` — insert after `_completed_regions_load`
- Test: `tests/test_mcp_server.py` — new class `TestSkipClaim`

**Interfaces:**
- Consumes: `frontier_registry.Interval`, `frontier_registry.TAG_PROVISIONAL` / `TAG_AUTHORITATIVE`
- Produces: `_skip_claim(tag: str, pos: int, regions: Sequence["frontier_registry.Interval"]) -> bool` — `tag` is the claimer's stream tag, the string `"fwd"` or `"rev"`

- [ ] **Step 1: Write the failing tests**

```python
class TestSkipClaim:
    """#326: the predicate. It consults NEITHER the :commit/<hash> entity NOR
    any lineage fact.

    #325 words the fast path as "skips a position whose :commit/<hash> entity
    already exists". That is unsound: in _reverse_apply the commit entity is the
    FIRST element of all_triples, written before any file result is looked at,
    while _frontier_persist_claim runs LAST -- so its presence is the WEAKEST
    available witness of a completed write, and it is present on exactly the
    torn positions #313 needs re-walked.
    """

    PROV = frontier_registry.TAG_PROVISIONAL
    AUTH = frontier_registry.TAG_AUTHORITATIVE

    def test_reverse_claim_inside_a_provisional_region_is_skipped(self):
        import mcp_server
        regions = [frontier_registry.Interval(2, 5, self.PROV)]
        assert mcp_server._skip_claim("rev", 3, regions) is True
        assert mcp_server._skip_claim("rev", 2, regions) is True
        assert mcp_server._skip_claim("rev", 5, regions) is True

    def test_reverse_claim_outside_every_region_is_not_skipped(self):
        import mcp_server
        regions = [frontier_registry.Interval(2, 5, self.PROV)]
        assert mcp_server._skip_claim("rev", 1, regions) is False
        assert mcp_server._skip_claim("rev", 6, regions) is False

    def test_forward_claim_is_never_skipped_even_inside_a_region(self):
        """Two reasons, either one sufficient. A forward claim inside a
        provisional region is the authority upgrade that must still happen --
        skipping would make a provisional region look confirmed. And
        _forward_apply mutates _ForwardWalkState's ten cross-position preload
        dicts in place, so a skipped forward position leaves that state
        desynchronized for every later forward position, silently.
        _reverse_apply takes no state object and reads what it needs from the
        graph, which is why reverse skips carry no equivalent hazard."""
        import mcp_server
        regions = [frontier_registry.Interval(2, 5, self.PROV)]
        assert mcp_server._skip_claim("fwd", 3, regions) is False

    def test_reverse_claim_inside_an_authoritative_region_is_not_skipped(self):
        """Tags are checked, never assumed. _frontier_load only ever discards
        frontier-high today, so archived regions are provisional in practice --
        but a future discard of an authoritative interval must not silently
        license a reverse skip over it."""
        import mcp_server
        regions = [frontier_registry.Interval(2, 5, self.AUTH)]
        assert mcp_server._skip_claim("rev", 3, regions) is False

    def test_empty_region_set_skips_nothing(self):
        import mcp_server
        assert mcp_server._skip_claim("rev", 3, []) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipClaim -v`
Expected: FAIL, `AttributeError: module 'mcp_server' has no attribute '_skip_claim'`

- [ ] **Step 3: Write the implementation**

```python
def _skip_claim(
    tag: str, pos: int, regions: Sequence["frontier_registry.Interval"]
) -> bool:
    """True iff this claim can be retired without parsing or writing the commit.

    Sound because of what it does NOT read. The witness is membership in an
    archived completed region, whose bounds came from a persisted frontier
    interval, whose last write per position is _frontier_persist_claim. A #313
    torn position's claim never persisted, so that position was never inside the
    interval that got archived, so it is in no region and is never skipped --
    correct by construction rather than by care.

    'fwd' never skips: see TestSkipClaim's forward case for the two independent
    reasons. Restricting to same-tag skipping gets both from one clause.
    """
    if tag != "rev":
        return False
    return any(
        iv.tag == frontier_registry.TAG_PROVISIONAL and iv.lo_pos <= pos <= iv.hi_pos
        for iv in regions
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipClaim -v`
Expected: 5 passed

- [ ] **Step 5: Ablation — prove the forward clause is load-bearing**

Temporarily delete `_skip_claim`'s `if tag != "rev": return False` guard.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipClaim -v`
Expected: FAIL on `test_forward_claim_is_never_skipped_even_inside_a_region`.

Then restore the guard and instead delete the `iv.tag ==
frontier_registry.TAG_PROVISIONAL` term.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipClaim -v`
Expected: FAIL on `test_reverse_claim_inside_an_authoritative_region_is_not_skipped`.

Restore both and re-run to confirm 5 passed. Two separate ablations because the
two clauses are independent: a test suite that only exercised one would leave
the other uncovered while looking complete.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Add _skip_claim, the fast path's predicate (#326)

Sound because of what it does not read. #325 words the fast path as skipping a
position whose :commit/<hash> entity exists; that entity is _reverse_apply's
FIRST write, so it is the weakest available witness of a completed write and is
present on exactly the torn positions #313 needs re-walked.

The witness is membership in an archived region, whose bounds came from a
persisted interval, whose last write per position is _frontier_persist_claim.
'fwd' never skips -- both because a forward claim inside a provisional region
is the authority upgrade, and because _forward_apply mutates cross-position
preload state a skip would desynchronize.

Ablation: dropping the tag != "rev" guard fails the forward case; dropping the
provisional-tag term fails the authoritative case. Both clauses are separately
load-bearing.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 5: `_frontier_persist_span` — the end-of-walk flush primitive

**Files:**
- Modify: `mcp_server.py` — insert after `_frontier_persist_claim` (~line 6260)
- Test: `tests/test_mcp_server.py` — new class `TestFrontierPersistSpan`, place immediately after `TestFrontierPersistClaim`

**Interfaces:**
- Consumes: `_frontier_read_bounds`, `_transact`, `_retract`, `_edn_escape`, `_FRONTIER_LOW_IDENT`, `_FRONTIER_HIGH_IDENT`
- Produces: `_frontier_persist_span(db, linearization: List[str], lo_pos: int, hi_pos: int, from_low: bool, commit_ts_iso: str, index_con=None) -> None`

- [ ] **Step 1: Write the failing tests**

```python
class TestFrontierPersistSpan:
    """#326: _frontier_persist_claim cannot serve the skip flush. After a
    discard the interval's facts are gone, so its `existing is None` branch
    fires and writes lo == hi == moved_hash -- collapsing the interval to a
    point and losing the top bound. _frontier_persist_span writes BOTH bounds
    when the interval is absent and moves only the one bound when it is not."""

    def test_absent_interval_gets_both_bounds(self, real_db):
        import mcp_server
        linearization = ["h0", "h1", "h2", "h3", "h4"]

        mcp_server._frontier_persist_span(
            real_db, linearization, 1, 4, from_low=False,
            commit_ts_iso="2026-01-01T00:00:00Z",
        )

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT
        ) == ("h1", "h4")

    def test_present_interval_moves_only_the_low_bound_when_growing_downward(self, real_db):
        import mcp_server
        linearization = ["h0", "h1", "h2", "h3", "h4"]
        mcp_server._frontier_persist_claim(
            real_db, linearization, 4, from_low=False, commit_ts_iso="2026-01-01T00:00:00Z"
        )

        mcp_server._frontier_persist_span(
            real_db, linearization, 1, 4, from_low=False,
            commit_ts_iso="2026-01-01T00:00:01Z",
        )

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT
        ) == ("h1", "h4")
        raw = mcp_server._db_execute(
            real_db,
            f"(query [:find (count ?lo) :where [{mcp_server._FRONTIER_HIGH_IDENT} :lo-hash ?lo]])",
        )
        assert json.loads(raw)["results"] == [[1]], "the stale :lo-hash datom must be retracted"

    def test_present_interval_moves_only_the_high_bound_when_growing_upward(self, real_db):
        import mcp_server
        linearization = ["h0", "h1", "h2", "h3", "h4"]
        mcp_server._frontier_persist_claim(
            real_db, linearization, 0, from_low=True, commit_ts_iso="2026-01-01T00:00:00Z"
        )

        mcp_server._frontier_persist_span(
            real_db, linearization, 0, 3, from_low=True,
            commit_ts_iso="2026-01-01T00:00:01Z",
        )

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_LOW_IDENT
        ) == ("h0", "h3")

    def test_a_span_that_would_shrink_the_interval_is_a_no_op(self, real_db):
        """The flush is bookkeeping catching up with the allocator; it must
        never retreat a bound another write already advanced."""
        import mcp_server
        linearization = ["h0", "h1", "h2", "h3", "h4"]
        mcp_server._frontier_persist_claim(
            real_db, linearization, 4, from_low=False, commit_ts_iso="2026-01-01T00:00:00Z"
        )
        mcp_server._frontier_persist_claim(
            real_db, linearization, 1, from_low=False, commit_ts_iso="2026-01-01T00:00:01Z"
        )

        mcp_server._frontier_persist_span(
            real_db, linearization, 3, 4, from_low=False,
            commit_ts_iso="2026-01-01T00:00:02Z",
        )

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT
        ) == ("h1", "h4")

    def test_repeating_the_same_span_writes_nothing(self, real_db):
        import mcp_server
        linearization = ["h0", "h1", "h2", "h3", "h4"]
        mcp_server._frontier_persist_span(
            real_db, linearization, 1, 4, from_low=False, commit_ts_iso="2026-01-01T00:00:00Z"
        )
        mcp_server._frontier_persist_span(
            real_db, linearization, 1, 4, from_low=False, commit_ts_iso="2026-01-01T00:00:01Z"
        )

        raw = mcp_server._db_execute(
            real_db,
            f"(query [:find (count ?lo) :where [{mcp_server._FRONTIER_HIGH_IDENT} :lo-hash ?lo]])",
        )
        assert json.loads(raw)["results"] == [[1]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierPersistSpan -v`
Expected: FAIL, `AttributeError: module 'mcp_server' has no attribute '_frontier_persist_span'`

- [ ] **Step 3: Write the implementation**

```python
def _frontier_persist_span(
    db: Any,
    linearization: List[str],
    lo_pos: int,
    hi_pos: int,
    from_low: bool,
    commit_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """Persist a whole claimed SPAN in one write, for #326's end-of-walk flush.

    _frontier_persist_claim cannot do this job. After a discard the interval's
    facts are gone, so its `existing is None` branch writes lo == hi ==
    moved_hash: the interval collapses to a point and the top bound is lost.

    Advance-only in both directions -- the flush is bookkeeping catching up with
    the allocator, and must never retreat a bound a real per-commit claim
    already moved. A span that changes nothing writes nothing (#156: a
    re-transact at a new valid-from is not a graph-level no-op).
    """
    ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"
    lo_hash, hi_hash = linearization[lo_pos], linearization[hi_pos]
    existing = _frontier_read_bounds(db, ident)

    if existing is None:
        _transact(
            db,
            "[" + " ".join([
                f"[{ident} :entity-type :type/ingest-interval]",
                f"[{ident} :tag {tag}]",
                f'[{ident} :lo-hash "{_edn_escape(lo_hash)}"]',
                f'[{ident} :hi-hash "{_edn_escape(hi_hash)}"]',
            ]) + "]",
            commit_ts_iso,
            index_con=index_con,
        )
        return

    pos_of = {h: i for i, h in enumerate(linearization)}
    cur_lo, cur_hi = existing
    new_lo = cur_lo if pos_of.get(cur_lo, lo_pos) <= lo_pos else lo_hash
    new_hi = cur_hi if pos_of.get(cur_hi, hi_pos) >= hi_pos else hi_hash

    to_retract: List[str] = []
    to_transact: List[str] = []
    if new_lo != cur_lo:
        to_retract.append(f'[{ident} :lo-hash "{_edn_escape(cur_lo)}"]')
        to_transact.append(f'[{ident} :lo-hash "{_edn_escape(new_lo)}"]')
    if new_hi != cur_hi:
        to_retract.append(f'[{ident} :hi-hash "{_edn_escape(cur_hi)}"]')
        to_transact.append(f'[{ident} :hi-hash "{_edn_escape(new_hi)}"]')
    if not to_transact:
        return
    _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierPersistSpan -v`
Expected: 5 passed

- [ ] **Step 5: Ablation — prove the span helper is not just a rename**

Temporarily make `_frontier_persist_span` delegate to `_frontier_persist_claim`
for the absent-interval case:

```python
    if existing is None:
        _frontier_persist_claim(
            db, linearization, lo_pos, from_low, commit_ts_iso, index_con=index_con
        )
        return
```

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierPersistSpan -v`
Expected: FAIL on `test_absent_interval_gets_both_bounds` — the interval reads
`("h1", "h1")`, collapsed to a point with the top bound lost. That is the exact
failure the helper exists to prevent.

Revert and re-run to confirm 5 passed.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Add _frontier_persist_span for the skip flush (#326)

_frontier_persist_claim cannot serve it: after a discard the interval's facts
are gone, so its `existing is None` branch writes lo == hi == moved_hash, which
collapses the interval to a point and loses the top bound.

Advance-only in both directions, since the flush is bookkeeping catching up
with the allocator and must never retreat a bound a real per-commit claim
already moved.

Ablation: delegating the absent-interval case to _frontier_persist_claim reads
back ("h1", "h1") instead of ("h1", "h4") -- the collapse this exists to
prevent.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 6: Wire the fast path into `_run_ingestion`, with counters

**Files:**
- Modify: `mcp_server.py` — `_ingest_progress` module-level dict (~line 168); the reset in `handle_minigraf_ingest_git` (~line 11972); `handle_minigraf_ingest_status` (~line 11987); `_run_ingestion`'s `_frontier_load` call site (~line 11476) and `submit_next` (~line 11520); the walk-loop exit (~line 11657)
- Test: `tests/test_mcp_server.py` — new class `TestSkipFastPathEndToEnd`

**Interfaces:**
- Consumes: `_completed_regions_load` (Task 3), `_skip_claim` (Task 4), `_frontier_persist_span` (Task 5)
- Produces: `_ingest_progress["positions_skipped"]`; `handle_minigraf_ingest_status()["positions_skipped_this_run"]`

- [ ] **Step 1: Write the failing tests**

```python
class TestSkipFastPathEndToEnd:
    """#326 acceptance test 1: a real re-walk of an ingested region, driven by
    #325's actual trigger (the branch tip growing past the persisted :hi-hash)
    rather than a simulation of it.

    Two independent witnesses that the work did not happen:
      * the per-commit trace (MINIGRAF_INGEST_TRACE_PATH) emits one record per
        APPLIED commit, so a skipped position appears in no record -- an
        existing mechanism, not one built for this test;
      * positions_skipped_this_run, incremented in the one branch that skips.
    """

    def _repo(self, tmp_path, n, start=0):
        repo = tmp_path / "repo"
        if not repo.exists():
            repo.mkdir()
            _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
        for i in range(start, start + n):
            (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
            _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
            _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
        return repo

    @pytest.mark.asyncio
    async def test_archived_region_is_skipped_on_the_next_run(self, tmp_path, monkeypatch):
        import mcp_server
        import frontier_registry

        repo = self._repo(tmp_path, 6)
        graph = tmp_path / "g.graph"
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph))
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))

        await mcp_server._run_ingestion(str(repo), "HEAD")
        first_high = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT
        )
        assert first_high is not None, (
            "run 1 must leave a persisted frontier-high, or there is nothing to "
            "discard and this test proves nothing"
        )

        # #325's trigger, reproduced: the tip moves past the persisted :hi-hash,
        # so _frontier_load's discard branch fires on its own.
        self._repo(tmp_path, 2, start=6)
        grown = frontier_registry.build_linearization(str(repo))
        archived_lo, archived_hi = first_high
        archived_positions = set(
            range(grown.index(archived_lo), grown.index(archived_hi) + 1)
        )

        trace = tmp_path / "trace.jsonl"
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(trace))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        applied = {
            json.loads(line)["pos"]
            for line in trace.read_text().splitlines() if line.strip()
        }
        assert not (applied & archived_positions), (
            f"positions {sorted(applied & archived_positions)} were re-applied; the "
            "archived region must cost no write at all"
        )
        assert applied, "run 2 must still apply the genuinely-new tip commits"

        status = mcp_server.handle_minigraf_ingest_status()
        assert status["positions_skipped_this_run"] >= len(archived_positions), (
            f"expected at least {len(archived_positions)} skips, got "
            f"{status['positions_skipped_this_run']}"
        )

    @pytest.mark.asyncio
    async def test_skipped_positions_still_advance_the_persisted_frontier(self, tmp_path, monkeypatch):
        """#326 requirement 3. Skipping the work is not skipping the
        bookkeeping: a position skipped without the interval growing leaves the
        same region unclaimed for the next run to replay again."""
        import mcp_server
        import frontier_registry

        repo = self._repo(tmp_path, 6)
        graph = tmp_path / "g.graph"
        monkeypatch.setenv("MINIGRAF_GRAPH_PATH", str(graph))
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))

        await mcp_server._run_ingestion(str(repo), "HEAD")
        self._repo(tmp_path, 2, start=6)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        grown = frontier_registry.build_linearization(str(repo))
        high = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT
        )
        low = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_LOW_IDENT
        )
        assert high is not None or low is not None
        if high is not None:
            lo_pos, hi_pos = grown.index(high[0]), grown.index(high[1])
            assert lo_pos <= hi_pos, f"persisted frontier-high is inverted: {high}"
            assert hi_pos == len(grown) - 1, (
                "a completed run's high interval must reach the tip, or the next "
                "run discards it again and the skip bought nothing"
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipFastPathEndToEnd -v`
Expected: FAIL — `KeyError: 'positions_skipped_this_run'`, and the trace assertion shows archived positions re-applied

- [ ] **Step 3a: Add the counter to both initializers**

In `mcp_server.py` at the module-level dict (~line 168):

```python
_ingest_progress: Dict[str, Any] = {
    "status": "idle", "processed": 0, "total": 0, "prior_ingested": 0,
    "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
    "phase": None,
    # #326. Deliberately NOT named "skipped": _ingest_progress["status"] already
    # takes the value "skipped" (the whole run declined because another process
    # owns the graph) and commit_census already reports skipped_commits (commits
    # dropped for extraction failure). A third bare "skipped" reads as one of
    # those two on sight.
    "positions_skipped": 0,
}
```

And the identical key in `handle_minigraf_ingest_git`'s reset (~line 11972):

```python
    _ingest_progress = {
        "status": "starting", "processed": 0, "total": 0, "prior_ingested": 0,
        "current_commit": "", "error": None, "owner_pid": None, "error_at": None,
        "phase": None, "positions_skipped": 0,
    }
```

A key present in one initializer and absent from the other reads as 0 in
exactly the runs that matter — add both.

Then reset it per RUN, next to the existing `prior_ingested` seeding
(~line 11409), so `positions_skipped_this_run` means this attempt rather than
the process's lifetime — the same relationship `processed_this_run` gets from
`prior_ingested`. `_run_ingestion` is entered directly by the server's
auto-start path as well as by `handle_minigraf_ingest_git`, so the handler's
reset alone would leave a stale count on an auto-started run:

```python
        _ingest_progress["processed"] = prior_ingested
        _ingest_progress["prior_ingested"] = prior_ingested
        _ingest_progress["positions_skipped"] = 0   # #326: per-run, like prior_ingested
```

- [ ] **Step 3b: Derive the per-run figure in the status handler**

In `handle_minigraf_ingest_status`, immediately after the existing
`result["processed_this_run"] = ...` block:

```python
    # #326: a run whose positions_skipped_this_run climbs alongside
    # processed_this_run is REPLAYING an already-ingested region, not making
    # progress. #325's incident looked healthy for 98 minutes because
    # `processed` advances on replayed positions and nothing else did.
    result["positions_skipped_this_run"] = _ingest_progress.get("positions_skipped", 0)
```

- [ ] **Step 3c: Load the regions in `_run_ingestion`**

Immediately after the existing `allocator = await loop.run_in_executor(... _frontier_load ...)` call, still inside the same `async with db_lease_async() as db:` block:

```python
            # #326: archived completion witnesses, mapped into this run's
            # position space. _frontier_load does the ARCHIVING (that is where
            # the doomed bounds are) but not the loading -- widening its return
            # would break a dozen call sites that use it directly as an
            # allocator.
            completed_regions = await loop.run_in_executor(
                write_executor, _completed_regions_load, db, linearization, allocator, index_con,
            )
```

- [ ] **Step 3d: Make `submit_next` skip**

Replace the body of `submit_next` with:

```python
                # #326: a skippable claim is retired here, BEFORE the parse is
                # queued, so it costs neither the git show + tree-sitter parse
                # nor _reverse_apply's write batch nor the checkpoint nor the
                # per-commit handle drop. The loop body is a pure in-memory
                # interval scan, so a long run of skips costs microseconds per
                # position and never stalls the event loop.
                def submit_next() -> bool:
                    nonlocal lowest_skipped_pos, highest_rev_pos
                    while True:
                        claim = claimer.next_claim()
                        if claim is None:
                            return False
                        tag, pos = claim
                        if tag == "rev":
                            highest_rev_pos = (
                                pos if highest_rev_pos is None else max(highest_rev_pos, pos)
                            )
                        if not _skip_claim(tag, pos, completed_regions):
                            break
                        lowest_skipped_pos = (
                            pos if lowest_skipped_pos is None else min(lowest_skipped_pos, pos)
                        )
                        _ingest_progress["positions_skipped"] += 1
                        # `processed` keeps its meaning -- positions retired by
                        # the walk -- which is what #317's commit_census reads
                        # as walk_claimed. Excluding skips would silently
                        # redefine the number that gate compares against
                        # git rev-list, turning a clean skip-heavy resume into
                        # a reported lost commit.
                        _ingest_progress["processed"] += 1
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, linearization[pos], ignore_patterns
                    )
                    pending.append((tag, pos, fut))
                    return True
```

Declare the two trackers just above `submit_next`, next to `pending`:

```python
                # #326: the end-of-walk flush's bounds. A run of skips is
                # normally subsumed for free -- _frontier_persist_claim moves
                # :lo-hash, a RANGE bound, so the next genuinely-walked reverse
                # position below the skips persists a bound covering them. These
                # exist for the one case that is not covered: the walk ending
                # while still inside a run of skips.
                lowest_skipped_pos: Optional[int] = None
                highest_rev_pos: Optional[int] = None
```

- [ ] **Step 3e: Flush at the walk-loop exit**

Immediately after the `while pending:` loop ends (before the Stage B correction-sweep block that begins `# Stage B: the correction sweep.`):

```python
                # #326: the walk may have ended -- gap empty, or shutdown --
                # while still inside a run of skips, which nothing below
                # persisted. Not _frontier_persist_claim: after a discard the
                # interval's facts are gone, so its `existing is None` branch
                # would write lo == hi and lose the top bound.
                if lowest_skipped_pos is not None and highest_rev_pos is not None:
                    async with db_lease_async() as db:
                        await loop.run_in_executor(
                            write_executor, _frontier_persist_span, db, linearization,
                            lowest_skipped_pos, highest_rev_pos, False,
                            commit_metadata[highest_rev_pos][1], index_con,
                        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipFastPathEndToEnd -v`
Expected: 2 passed

- [ ] **Step 5: Run the ingestion and status suites**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -q -k "Ingest or Frontier or Reverse or Status or Census"`
Expected: all pass

- [ ] **Step 6: Ablation — prove the end-to-end test is not vacuous**

Temporarily make `_skip_claim` return `False` unconditionally (insert
`return False` as its first statement), then:

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipFastPathEndToEnd::test_archived_region_is_skipped_on_the_next_run -v`
Expected: FAIL — archived positions appear in the trace and
`positions_skipped_this_run` is 0.

Revert the ablation and re-run to confirm PASS. Record both outcomes in the
commit message.

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Skip already-completed positions at claim time (#326)

submit_next retires a skippable claim before queueing _extract_commit, so a
skipped position costs neither the git show + tree-sitter parse nor
_reverse_apply's write batch nor the checkpoint nor the per-commit handle drop
#280 attributes ~47% of write time to.

A run of skips is normally persisted for free: _frontier_persist_claim moves
:lo-hash, a RANGE bound, so the next genuinely-walked reverse position below
the skips covers them. The flush handles the one case that misses -- the walk
ending while still inside a run of skips.

`processed` still counts a skipped position. Excluding it would silently
redefine the number #317's commit_census reads as walk_claimed, turning a clean
skip-heavy resume into a reported lost commit. The new counter is
positions_skipped, not skipped: status already takes the value "skipped" and
commit_census already reports skipped_commits.

Ablation: forcing _skip_claim to return False makes the end-to-end test fail
with the archived positions back in the trace and positions_skipped_this_run
at 0; reverting restores the pass.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 7: The #313 positive control

**Files:**
- Modify: none (production code is complete after Task 6)
- Test: `tests/test_mcp_server.py` — new class `TestSkipFastPathDoesNotSkipTornWrites`, place immediately after `TestReverseApplyTornWriteResume` so the two read together

**Interfaces:**
- Consumes: `_skip_claim`, `_completed_regions_read`, `_frontier_read_bounds`, `_reverse_apply`, `_extract_commit`, `_git_commits`, `_entity_introduced_by_values_query`, `_entity_ident_is_live`, `_code_ident`, `_SimulatedKill` (existing test helper used by `TestReverseApplyTornWriteResume`)
- Produces: nothing consumed by later tasks

- [ ] **Step 1: Write the tests**

```python
class TestSkipFastPathDoesNotSkipTornWrites:
    """#326 acceptance test 2, and the one that matters. A fast path whose
    predicate quietly matched nothing would pass the skip test by doing no
    skipping at all; this is the test that would catch the OPPOSITE error.

    #325 words the fast path as skipping a position whose :commit/<hash> entity
    already exists. That entity is the FIRST element of _reverse_apply's
    all_triples, written before any file result is looked at, while
    _frontier_persist_claim runs LAST -- so it is present on exactly the torn
    positions #313 needs re-walked. Skipping one would make the orphaned lineage
    permanent, surfacing later as #316's entities_without_introduced_by going
    red on a graph with no other symptom, or on an older graph not at all.
    """

    def _tear_position_zero(self, monkeypatch, real_db, repo):
        """Tear position 0 through the REAL write path -- kill the lineage batch
        mid-_reverse_apply, exactly as TestReverseApplyTornWriteResume does, so
        nothing here depends on hitting a timing window."""
        import mcp_server
        import frontier_registry

        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        file_results, _g, _m, _r = mcp_server._extract_commit(str(repo), linearization[0], ())

        def die(*_a, **_k):
            raise _SimulatedKill()

        with monkeypatch.context() as mp:
            mp.setattr(mcp_server, "_entity_introduced_by_set_provisional_batch", die)
            with pytest.raises(_SimulatedKill):
                mcp_server._reverse_apply(
                    real_db, str(repo), linearization, commit_metadata, 0, file_results,
                )
        return linearization

    def test_a_torn_position_is_in_no_archived_region(self, real_db, tmp_path, monkeypatch):
        """The soundness argument, asserted rather than reasoned about: the torn
        position's _frontier_persist_claim never ran, so it was never inside the
        interval an archive could be taken from."""
        import mcp_server

        repo = TestReverseApplyTornWriteResume._repo_with_one_commit(tmp_path)
        linearization = self._tear_position_zero(monkeypatch, real_db, repo)

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT
        ) is None, "the torn write must not have persisted a claim"
        regions = mcp_server._completed_regions_load(
            real_db, linearization,
            frontier_registry.FrontierAllocator(len(linearization), []),
        )
        assert mcp_server._skip_claim("rev", 0, regions) is False, (
            "the torn position must be re-walked; skipping it makes #313's "
            "orphaned lineage permanent"
        )

    def test_the_unsound_commit_entity_predicate_WOULD_have_skipped_it(
        self, real_db, tmp_path, monkeypatch
    ):
        """The trap is real, not hypothetical. Pins that the commit entity IS
        present on a torn position -- so this test goes red the day someone
        'simplifies' _skip_claim to the lookup #325 proposed."""
        import mcp_server

        repo = TestReverseApplyTornWriteResume._repo_with_one_commit(tmp_path)
        linearization = self._tear_position_zero(monkeypatch, real_db, repo)

        commit_ident = f":commit/{linearization[0][:12]}"
        raw = mcp_server._db_execute(
            real_db,
            f"(query [:find ?t :where [{commit_ident} :entity-type ?t]])",
        )
        assert json.loads(raw)["results"], (
            "the commit entity must be present on the torn position -- if it is "
            "not, the kill no longer lands after the first transact and this "
            "test has stopped guarding the predicate #326 rejected"
        )

    def test_the_torn_position_still_recovers_its_lineage_when_re_walked(
        self, real_db, tmp_path, monkeypatch
    ):
        """End of the chain: not skipped, therefore re-walked, therefore
        repaired -- #313's fix still reachable with the fast path in place."""
        import mcp_server

        repo = TestReverseApplyTornWriteResume._repo_with_one_commit(tmp_path)
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        linearization = self._tear_position_zero(monkeypatch, real_db, repo)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        file_results, _g, _m, _r = mcp_server._extract_commit(str(repo), linearization[0], ())

        assert mcp_server._entity_introduced_by_values_query(real_db, fn_ident) == []
        mcp_server._reverse_apply(
            real_db, str(repo), linearization, commit_metadata, 0, file_results,
        )
        assert mcp_server._entity_introduced_by_values_query(real_db, fn_ident) == [
            f":commit/{linearization[0][:12]}"
        ]
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipFastPathDoesNotSkipTornWrites -v`
Expected: 3 passed (production code is already correct; these are the guard)

- [ ] **Step 3: Ablation — install a predicate that skips the torn position**

Temporarily insert this as `_skip_claim`'s first statement:

```python
    return tag == "rev"   # ABLATION ONLY
```

This is strictly WEAKER than the commit-entity predicate #326 rejected (it
matches everything that one would match, and more), so a guard that catches it
catches that one too — and unlike the commit-entity form it needs no DB handle,
so the ablation is a one-line edit with no scaffolding to get wrong.

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py::TestSkipFastPathDoesNotSkipTornWrites::test_a_torn_position_is_in_no_archived_region -v`
Expected: FAIL — `assert True is False`

Revert and re-run to confirm PASS. Record both outcomes in the commit message.

- [ ] **Step 4: Run the full #313 family together**

Run: `.venv/bin/python -m pytest tests/test_mcp_server.py -v -k "Torn or SkipFastPath or SkipClaim"`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "$(cat <<'MSG'
Guard that the fast path never skips a #313 torn write (#326)

The acceptance test that matters. A predicate quietly matching nothing would
pass the skip test by doing no skipping at all; this catches the opposite
error, and the tear is produced through the REAL write path rather than
hand-seeded, so nothing depends on a timing window.

Three assertions, and the middle one is the trap: the commit entity IS present
on a torn position, so #325's proposed lookup would have skipped exactly it.
That test goes red the day someone simplifies _skip_claim back to it.

Ablation: forcing _skip_claim to skip every rev claim fails
test_a_torn_position_is_in_no_archived_region; reverting restores the pass.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

### Task 8: Documentation

**Files:**
- Modify: `CLAUDE.md` — new subsection at the end of "Graph Storage"
- Modify: `SKILL.md` — the `minigraf_ingest_status` example payload and the prose paragraph after it (~lines 313–345)
- Modify: `mcp_server.py` — the `minigraf_ingest_status` entry in `_TOOLS`
- Modify: `tools/ingest_status.json` — the same description text, verbatim

**Interfaces:**
- Consumes: everything above
- Produces: nothing

- [ ] **Step 1: Update SKILL.md's example payload**

Change the example to include the new field:

```python
minigraf_ingest_status()
# → {"ok": true, "status": "running", "processed": 21717, "processed_this_run": 2,
#    "positions_skipped_this_run": 0, "total": 47, "current_commit": "a3f2bc...",
#    "error": null}
```

- [ ] **Step 2: Add the prose that makes the field useful**

Append to the paragraph that explains `processed` vs `processed_this_run`:

```
`positions_skipped_this_run` counts positions this run retired WITHOUT parsing
or writing them, because they were already written completely by an earlier run
(#326). It is the signal that distinguishes a run replaying an already-ingested
region from one making progress: if it climbs alongside `processed_this_run`
while the graph's commit count stays flat, the run is re-walking territory it
already holds. Skipped positions are still counted in `processed`, since they
are genuinely retired.
```

- [ ] **Step 3: Update `_TOOLS` and `tools/ingest_status.json` together**

Append this sentence to the `minigraf_ingest_status` description in BOTH
`mcp_server._TOOLS` and `tools/ingest_status.json` — the two must match
character for character or `tests/test_tool_schemas.py` fails:

```
positions_skipped_this_run counts positions retired without parsing or writing them because an earlier run had already written them completely (#326); it climbing while the commit count stays flat means the run is replaying an already-ingested region.
```

- [ ] **Step 4: Run the doc guards**

Run: `.venv/bin/python -m pytest tests/test_skill_doc.py tests/test_tool_schemas.py -v`
Expected: all pass

- [ ] **Step 5: Add the CLAUDE.md section**

Append to the "Graph Storage" section:

```markdown
**Re-walking an already-ingested position is now skipped, and the witness is
the thing that decides whether that is safe (#326).** The obvious predicate is
unsound: in `_reverse_apply` `[:commit/<hash> :entity-type :type/commit]` is the
FIRST element of `all_triples`, written before any file result is looked at,
while `_frontier_persist_claim` runs LAST. So the commit entity's presence is
the WEAKEST available witness of a completed write — and it is present on
exactly the torn positions #313 needs re-walked. A fast path keyed on it would
make that orphaned lineage permanent.

The witness is membership in a `:type/completed-region` fact set, archived by
`_frontier_load` from the high interval it is about to discard. Because
`_frontier_persist_claim` is the last write of a position, every position inside
a persisted interval provably completed, and the archive inherits that. A torn
position's claim never persisted, so it was never inside the interval that got
archived: it is in no region and is never skipped, by construction rather than
by care. Only a REPRESENTABLE interval is archived — the inverted case reaches
the same branch and describes no completed region at all.

`fwd` never skips, and one clause buys two properties. A forward claim inside a
provisional region is the authority upgrade that must still happen; and
`_forward_apply` mutates `_ForwardWalkState`'s ten cross-position preload dicts
in place, so a skipped forward position would desynchronize that state for every
later forward position, silently. `_reverse_apply` takes no state object and
reads what it needs from the graph, which is why reverse carries no equivalent
hazard.

The region type is deliberately absent from `MINIGRAF_SCHEMA`, like
`:type/ingest-interval` — `handle_minigraf_audit` iterates exactly the registered
types and would otherwise retract its attributes — and every write goes through
the internal `_transact`/`_retract`, since the public handler rejects an
unregistered type outright. Regions carry a string-valued `:ident` because
enumerating by `:entity-type` binds the entity in UUID space.

**A skipped position still costs `processed`, and that is not sloppiness.**
#317's `commit_census` reads `_ingest_progress["processed"]` as `walk_claimed`,
and `walk_vs_graph` is gated ALWAYS. A skipped position increments it while its
`:type/commit` entity is already in the graph, so the comparison balances — but
only because the witness guarantees the graph holds that commit. **If the skip
predicate were ever wrong, that existing gate goes red.** Excluding skips from
`processed` would have thrown that away and turned a clean skip-heavy resume
into a reported lost commit. The counter is `positions_skipped`, never
`skipped`: `status` already takes the value `"skipped"` (run declined, another
process owns the graph) and `commit_census` already reports `skipped_commits`
(extraction failures).

There is no `GRAPH_FORMAT_VERSION` bump — this only adds facts going forward.
Existing graphs are not repaired and need no migration: a region is only
knowable from an interval that exists at discard time, so there is nothing to
seed. They simply never skip until their first discard, and because archiving
happens in `_frontier_load` at LOAD time, the run that discards is the run that
skips.
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass. Report the exact counts; do not claim green without the output.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md SKILL.md mcp_server.py tools/ingest_status.json
git commit -m "$(cat <<'MSG'
Document the skip fast-path and its witness (#326)

CLAUDE.md records why the commit entity is not a witness, why fwd never skips,
and why a skipped position still costs `processed` -- which turns #317's
always-gated walk_vs_graph into a free independent check on the predicate.

SKILL.md and the tool manifest gain positions_skipped_this_run, the signal that
distinguishes a run replaying an already-ingested region from one making
progress. #325's incident looked healthy for 98 minutes without it.

Refs #326

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01CDCDY7Uiye8Dvd6eYku34h
MSG
)"
```

---

## Verification before opening the PR

- [ ] `.venv/bin/python -m pytest tests/ -q` — full suite, paste the counts
- [ ] `git log --oneline master..HEAD` — confirm no commit message contains a
      closing keyword (`fix`, `fixes`, `close`, `closes`, `resolve`, `resolves`)
      followed by `#326`. Commit messages and the PR body are separate channels
      and no single check sees both; scan again after every new commit, not once
      before the push.
- [ ] The PR body carries `Closes #326` and the `🤖 Generated with [Claude Code]`
      footer.
- [ ] master requires an approving review on top of green CI — do not use
      `--admin` to bypass without asking.
