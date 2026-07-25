# Stream 1 Correction Sweep (#222 phase 2c) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Stream 1's correction sweep — a post-convergence pass that walks upward through frontier-high's already-claimed territory, converting phase 2b's provisional `:introduced-by` facts to authoritative and correcting 2b's documented `:modified-in` over-assertion, per `docs/superpowers/specs/2026-07-25-stream1-correction-sweep-design.md`.

**Architecture:** Five new functions added to `mcp_server.py`, split for independent executor scheduling per the spec's "Execution context" section: a cheap DB-reading position selector, a CPU-bound-but-reused extraction call (`_extract_commit`, already exists), a DB-writing per-commit classifier, a synchronous convenience wrapper composing all three, and a driving loop. No caller is wired into `_run_ingestion` — that is phase 2d's job. This branch stacks on `design-222-phase2b-reverse-bulk-fill-walk` (PR #228) and calls its `_entity_introduced_by_query`/`_lineage_is_provisional`/`_candidate_diff_clear`/`_lineage_confirm` directly.

**Tech Stack:** Python 3, minigraf (real backend in tests, no mocks), pytest, real git repos via `subprocess` in test fixtures — matching every existing convention in `tests/test_mcp_server.py`.

## Global Constraints

- Real `MiniGrafDb` in every test — no `MagicMock` of the DB (`docs/testing-conventions.md`). Use the `real_db` fixture (in-memory) unless a test needs state to survive across separate `open()` calls.
- Every new DB-touching helper takes `index_con: Optional[Any] = None` and forwards it to `_transact`/`_retract`, matching every existing 2a/2b primitive.
- Every write must be safe to repeat: query-before-write, or (for `:modified-in`) reliant on `commit_ts_iso` matching what the other stream would have used at the same position — never a blind unconditional `_transact` with a fresh/different timestamp.
- No new module ever gets imported — `sys`, `json`, and all needed `typing` names (`Any, Dict, List, Optional, Sequence, Tuple`) are already imported at the top of `mcp_server.py`; `frontier_registry` is already imported as `import frontier_registry`.
- Insert all new `mcp_server.py` code in one place: immediately after `_reverse_bulk_fill_walk` (currently ends at line 7437), before `_run_ingestion` (currently starts at line 7440). Re-check the exact line numbers before each edit — earlier tasks in this plan shift them.
- Append all new tests to `tests/test_mcp_server.py`, after `TestReverseBulkFillWalk` (currently the last class in the file, ending at line 14114/EOF).
- Every new test class defines its own private repo-building helpers (`_init_repo`, `_commit`, or a `_repo_with_...` builder) rather than reaching for a shared cross-class fixture — this file has no shared helper for that, by established convention (three separate near-identical `_init_repo` methods already exist in different classes).

---

### Task 1: `:ingestion/correction-sweep-through` watermark + log-cap constant

**Files:**
- Modify: `mcp_server.py` — insert after `_reverse_bulk_fill_walk` (currently ends line 7437).
- Test: `tests/test_mcp_server.py` — new `TestCorrectionSweepThroughWatermark` class, appended at EOF.

**Interfaces:**
- Consumes: `_db_execute(db, datalog) -> str` (pre-existing, line 3278), `_transact(db, facts, valid_from, ..., index_con=None) -> str` (pre-existing, line 3532), `_retract(db, facts, ..., index_con=None) -> str` (pre-existing, line 3568), `_edn_escape(s) -> str` (pre-existing, line 4427), `handle_minigraf_audit()` (pre-existing, no-arg, operates on module global `_db`).
- Produces: `_CORRECTION_SWEEP_THROUGH_IDENT = ":ingestion/correction-sweep-through"`, `_CORRECTION_SWEEP_LOG_CAP = 10`, `_correction_sweep_through_query(db: Any) -> Optional[str]`, `_correction_sweep_through_update(db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None) -> None`.

This mirrors `_lineage_confirmed_through_query`/`_update` (`mcp_server.py:5080-5128`) exactly — same `:type/ingestion` entity type (already registered in `MINIGRAF_SCHEMA["ingestion"]`, `mcp_server.py:5390-5393`, requiring only `:description` and allowing `:hash` among its optional attrs), same retract-only-if-changed pattern — but with its own ident and its own `:description` string, not a copy of lineage-confirmed-through's (two `:type/ingestion` entities with byte-identical descriptions would both pass audit but be indistinguishable from each other in the fact index and in `minigraf_audit` output).

- [ ] **Step 1: Write the failing tests**

```python
class TestCorrectionSweepThroughWatermark:
    def test_unset_reads_as_none(self, real_db):
        import mcp_server
        assert mcp_server._correction_sweep_through_query(real_db) is None

    def test_update_then_query_round_trip(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._correction_sweep_through_update(db, "h1", "2026-01-01T00:00:00Z")
        assert mcp_server._correction_sweep_through_query(db) == "h1"

        mcp_server._correction_sweep_through_update(db, "h2", "2026-01-02T00:00:00Z")
        assert mcp_server._correction_sweep_through_query(db) == "h2"

    def test_update_does_not_duplicate_hash_fact(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._correction_sweep_through_update(db, "h1", "2026-01-01T00:00:00Z")
        mcp_server._correction_sweep_through_update(db, "h2", "2026-01-02T00:00:00Z")

        ident = mcp_server._CORRECTION_SWEEP_THROUGH_IDENT
        raw = mcp_server._db_execute(db, f"(query [:find (count ?h) :where [{ident} :hash ?h]])")
        assert json.loads(raw)["results"] == [[1]]

    def test_entity_carries_expected_constants_and_survives_audit(self, real_db):
        import mcp_server
        db = real_db
        mcp_server._correction_sweep_through_update(db, "h1", "2026-01-01T00:00:00Z")

        ident = mcp_server._CORRECTION_SWEEP_THROUGH_IDENT
        raw = mcp_server._db_execute(db, f"(query [:find ?a ?v :where [{ident} ?a ?v]])")
        attrs = dict(json.loads(raw)["results"])
        assert attrs[":entity-type"] == ":type/ingestion"
        assert attrs[":ident"] == ident
        assert isinstance(attrs[":description"], str) and attrs[":description"]

        result = mcp_server.handle_minigraf_audit()
        assert result["retracted"] == 0
        assert mcp_server._correction_sweep_through_query(db) == "h1"

    def test_description_is_distinct_from_lineage_confirmed_through(self, real_db):
        """Two :type/ingestion watermarks with byte-identical :description
        strings would both pass audit but be indistinguishable from each
        other in the fact index and in minigraf_audit output -- this
        watermark must not just copy lineage-confirmed-through's string."""
        import mcp_server
        db = real_db
        mcp_server._correction_sweep_through_update(db, "h1", "2026-01-01T00:00:00Z")
        mcp_server._lineage_confirmed_through_update(db, "h1", "2026-01-01T00:00:00Z")

        sweep_desc = dict(json.loads(mcp_server._db_execute(
            db, f"(query [:find ?a ?v :where [{mcp_server._CORRECTION_SWEEP_THROUGH_IDENT} ?a ?v]])"
        ))["results"])[":description"]
        lineage_desc = dict(json.loads(mcp_server._db_execute(
            db, f"(query [:find ?a ?v :where [{mcp_server._LINEAGE_CONFIRMED_THROUGH_IDENT} ?a ?v]])"
        ))["results"])[":description"]
        assert sweep_desc != lineage_desc
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepThroughWatermark -v`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_correction_sweep_through_query'` (or similar) on every test.

- [ ] **Step 3: Implement the watermark primitives**

In `mcp_server.py`, immediately after `_reverse_bulk_fill_walk`'s closing (currently ends line 7437, right before `async def _run_ingestion` at line 7440), insert:

```python
_CORRECTION_SWEEP_THROUGH_IDENT = ":ingestion/correction-sweep-through"
# Max per-ident skip lines this sweep writes to stderr per run; see the
# Observability section of the design spec for why this must be
# caller-threaded state (skipped_so_far), not a module-level counter.
_CORRECTION_SWEEP_LOG_CAP = 10


def _correction_sweep_through_query(db: Any) -> Optional[str]:
    """Return the hash of the last commit this sweep has itself confirmed/
    corrected, or None if it has never successfully processed one yet."""
    raw = _db_execute(
        db, f"(query [:find ?h :where [{_CORRECTION_SWEEP_THROUGH_IDENT} :hash ?h]])"
    )
    results = json.loads(raw).get("results", [])
    return results[0][0] if results else None


def _correction_sweep_through_update(
    db: Any, commit_hash: str, commit_ts_iso: str, index_con: Optional[Any] = None
) -> None:
    """Record the last commit this sweep processed. Mirrors
    _lineage_confirmed_through_update's retract-only-if-changed pattern
    exactly, at a different ident with its own :description -- tracks this
    sweep's own progress through frontier-high's territory, independent of
    lineage-confirmed-through's "contiguous from C0" semantics.
    """
    current_raw = _db_execute(
        db, f"(query [:find ?a ?v :where [{_CORRECTION_SWEEP_THROUGH_IDENT} ?a ?v]])"
    )
    current: Dict[str, str] = dict(json.loads(current_raw).get("results", []))

    def _edn(attr: str, value: str) -> str:
        return value if attr == ":entity-type" else f'"{_edn_escape(value)}"'

    constants = {
        ":entity-type": ":type/ingestion",
        ":ident": _CORRECTION_SWEEP_THROUGH_IDENT,
        ":description": "correction sweep progress watermark",
    }

    to_retract: List[str] = []
    to_transact: List[str] = []
    for attr, value in constants.items():
        if current.get(attr) == value:
            continue
        if attr in current:
            to_retract.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} {attr} {_edn(attr, current[attr])}]")
        to_transact.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} {attr} {_edn(attr, value)}]")

    if ":hash" in current:
        to_retract.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} :hash {_edn(':hash', current[':hash'])}]")
    to_transact.append(f"[{_CORRECTION_SWEEP_THROUGH_IDENT} :hash {_edn(':hash', commit_hash)}]")

    if to_retract:
        _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
    _transact(db, "[" + " ".join(to_transact) + "]", commit_ts_iso, index_con=index_con)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepThroughWatermark -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add :ingestion/correction-sweep-through watermark for #222 phase 2c"
```

---

### Task 2: `_correction_sweep_select_position`

**Files:**
- Modify: `mcp_server.py` — insert after Task 1's new code.
- Test: `tests/test_mcp_server.py` — new `TestCorrectionSweepSelectPosition` class, appended at EOF.

**Interfaces:**
- Consumes: `_frontier_read_bounds(db, ident) -> Optional[Tuple[str, str]]` (pre-existing, line 4917), `_FRONTIER_LOW_IDENT`/`_FRONTIER_HIGH_IDENT` (pre-existing, lines 4913-4914), `_correction_sweep_through_query` (Task 1), `_frontier_load(db, linearization, run_ts_iso, index_con=None) -> FrontierAllocator` (pre-existing, line 4950), `_frontier_persist_claim(db, linearization, pos, from_low, commit_ts_iso, index_con=None) -> None` (pre-existing, line 4982), `_reverse_fill_claim_and_process` (2b, line 7251), `frontier_registry.FrontierAllocator` (`claim_low`, `claim_high`, `is_gap_empty`).
- Produces: `_correction_sweep_select_position(db: Any, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]], hash_to_pos: Optional[Dict[str, int]] = None) -> Optional[Tuple[str, str]]`.

This is the gap-closed precondition, the persisted-`:hi-hash` ceiling, and the resume-point logic from the design spec's "Position tracker" section — a pure DB-reading function (no parsing), returning `(commit_hash, commit_ts_iso)` for the next commit to process, or `None` if there is nothing safe to do yet.

- [ ] **Step 1: Write the failing tests**

```python
class TestCorrectionSweepSelectPosition:
    def _init_repo(self, repo):
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _commit(self, repo, filename, content, msg):
        (repo / filename).write_text(content)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

    def _repo_with_n_commits(self, tmp_path, n):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        for i in range(n):
            self._commit(repo, f"f{i}.py", f"def f{i}(): pass\n", f"h{i}")
        return repo

    def _linearization_and_metadata(self, repo):
        import mcp_server
        import frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        return linearization, commit_metadata

    def _close_gap(self, repo, real_db, linearization, commit_metadata):
        """Claim exactly one position from the low side via a real
        FrontierAllocator + _frontier_persist_claim (standing in for 2d's
        future ordinary forward walk), then claim the rest from the high
        side via 2b's real _reverse_fill_claim_and_process until the gap is
        empty. Claiming the *whole* gap from the low side alone (an
        unbounded claim_low() loop) would never touch frontier-high at
        all -- claim_low() alone can empty the gap without claim_high()
        ever running, since gap_hi only moves when claim_high() does."""
        import mcp_server
        import frontier_registry
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        pos = allocator.claim_low()
        if pos is not None:
            mcp_server._frontier_persist_claim(
                real_db, linearization, pos, from_low=True,
                commit_ts_iso=commit_metadata[pos][1],
            )
        while not allocator.is_gap_empty():
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        return allocator

    def test_no_op_when_frontier_high_unclaimed(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")  # seeds frontier-low only

        result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert result is None

    def test_no_op_while_gap_remains_open(self, real_db, tmp_path):
        """Direct regression test for the race the design spec's "Why
        confirming requires the gap to already be closed" section
        describes: claim only from the high side, leaving a non-empty gap
        below, and assert select_position refuses to hand out a position
        even though frontier-high has real claimed territory."""
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        assert allocator.is_gap_empty() is False  # only claimed the newest position so far

        result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert result is None

    def test_returns_frontier_high_lo_hash_once_gap_closed(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        allocator = self._close_gap(repo, real_db, linearization, commit_metadata)
        assert allocator.is_gap_empty() is True

        result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert result is not None
        commit_hash, commit_ts_iso = result
        bounds = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT)
        assert commit_hash == bounds[0]  # frontier-high's current lo-hash
        pos = linearization.index(commit_hash)
        assert commit_ts_iso == commit_metadata[pos][1]

    def test_resumes_from_correction_sweep_through_not_frontier_high_lo_hash(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)

        first = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert first is not None
        first_hash, first_ts = first
        mcp_server._correction_sweep_through_update(real_db, first_hash, first_ts)

        second = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert second is not None
        second_hash, _second_ts = second
        assert second_hash != first_hash
        assert linearization.index(second_hash) == linearization.index(first_hash) + 1

    def test_no_op_once_reached_frontier_high_hi_hash(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)

        # Walk to exhaustion by hand (Task 5 builds the real driving loop --
        # this test only needs select_position's own termination).
        while True:
            result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
            if result is None:
                break
            mcp_server._correction_sweep_through_update(real_db, result[0], result[1])

        assert mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata) is None

    def test_respects_persisted_hi_hash_not_len_linearization(self, real_db, tmp_path):
        """On an incremental re-ingest, linearization grows but frontier-
        high's persisted :hi-hash does not move with it -- the ceiling must
        stop at the persisted hash, not walk into brand-new, unclaimed
        commits."""
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)
        while True:
            result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
            if result is None:
                break
            mcp_server._correction_sweep_through_update(real_db, result[0], result[1])

        # Simulate an incremental re-ingest: more commits land, but neither
        # stream has claimed them yet.
        self._commit(repo, "new.py", "def new(): pass\n", "h_new")
        grown_linearization, grown_commit_metadata = self._linearization_and_metadata(repo)
        assert len(grown_linearization) == 3

        result = mcp_server._correction_sweep_select_position(
            real_db, grown_linearization, grown_commit_metadata
        )
        assert result is None

    def test_falls_back_to_frontier_high_lo_hash_when_stored_hash_is_stale(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)
        mcp_server._correction_sweep_through_update(real_db, "deadbeef_not_in_linearization", "2026-01-01T00:00:00Z")

        result = mcp_server._correction_sweep_select_position(real_db, linearization, commit_metadata)
        assert result is not None
        commit_hash, _ts = result
        bounds = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT)
        assert commit_hash == bounds[0]

    def test_hash_to_pos_reused_when_passed_in(self, real_db, tmp_path):
        """hash_to_pos, when supplied, is used as-is rather than rebuilt --
        pass a deliberately wrong map for an unrelated hash to prove the
        function trusts the caller's map rather than silently recomputing
        its own."""
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)
        hash_to_pos = {h: i for i, h in enumerate(linearization)}

        result = mcp_server._correction_sweep_select_position(
            real_db, linearization, commit_metadata, hash_to_pos=hash_to_pos
        )
        assert result is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepSelectPosition -v`
Expected: FAIL with `AttributeError` on every test.

- [ ] **Step 3: Implement `_correction_sweep_select_position`**

Insert after Task 1's code:

```python
def _correction_sweep_select_position(
    db: Any,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    hash_to_pos: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, str]]:
    """Returns (commit_hash, commit_ts_iso) for the next commit this sweep
    should process, upward through frontier-high's own claimed territory,
    or None if there is nothing safe to correct yet: the gap is still open
    (Stream 2 could still descend past a position this call would confirm
    -- see the design spec's "Why confirming requires the gap to already
    be closed"), frontier-high hasn't claimed anything yet, a required
    boundary hash is stale, commit_metadata doesn't match linearization, or
    the sweep has already reached frontier-high's own :hi-hash.

    DB-bound, parse-free -- must run off the event-loop thread (per the
    design spec's Execution context) but never on the same executor as
    _extract_commit, since the two must be independently schedulable.

    hash_to_pos, if omitted, is built fresh from linearization -- callers
    doing a full sweep should build it once and pass it in instead to
    avoid rebuilding an N-entry map on every one of a full sweep's N calls.
    """
    low_bounds = _frontier_read_bounds(db, _FRONTIER_LOW_IDENT)
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if low_bounds is None or high_bounds is None:
        return None  # migration hasn't run yet, or Stream 2 hasn't claimed anything

    if hash_to_pos is None:
        hash_to_pos = {h: i for i, h in enumerate(linearization)}

    if low_bounds[1] not in hash_to_pos or high_bounds[0] not in hash_to_pos:
        return None  # a boundary hash is stale (rewritten history); nothing safe to do

    if hash_to_pos[low_bounds[1]] + 1 != hash_to_pos[high_bounds[0]]:
        return None  # gap still open -- Stream 2 may still descend past a position
                     # this sweep would otherwise confirm

    if high_bounds[1] not in hash_to_pos:
        return None  # frontier-high's :hi-hash is stale; nothing safe to do
    ceiling_pos = hash_to_pos[high_bounds[1]]

    through_hash = _correction_sweep_through_query(db)
    if through_hash is not None and through_hash in hash_to_pos:
        pos = hash_to_pos[through_hash] + 1
    else:
        # Unset (first-ever call), or a stale hash from rewritten/rebased
        # history -- (re)start from frontier-high's current lo-hash,
        # mirroring _frontier_load's own precedent of dropping a bound
        # that no longer resolves rather than erroring.
        pos = hash_to_pos[high_bounds[0]]  # already validated above

    if pos > ceiling_pos:
        return None  # reached frontier-high's own :hi-hash; nothing left to correct

    if len(commit_metadata) != len(linearization) or commit_metadata[pos][0] != linearization[pos]:
        return None  # commit_metadata violates its stated contract -- nothing safe to
                     # do, rather than an IndexError or a wrong-commit read

    commit_hash, commit_ts_iso, _author, _subject = commit_metadata[pos]
    return commit_hash, commit_ts_iso
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepSelectPosition -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _correction_sweep_select_position for #222 phase 2c"
```

---

### Task 3: `_correction_sweep_apply`

**Files:**
- Modify: `mcp_server.py` — insert after Task 2's new code.
- Test: `tests/test_mcp_server.py` — new `TestCorrectionSweepApply` class, appended at EOF.

**Interfaces:**
- Consumes: `_extract_commit(repo_path, commit_hash, ignore_patterns) -> Tuple[file_results, gitlink_changes, gitmodules_map, renamed_pairs]` (pre-existing, line 6944 — `file_results` is `List[Tuple[status, file_path, extracted, precomputed, old_path]]`; `precomputed` has keys `module_ident`, `function_entries`/`class_entries`/`global_entries`/`field_entries` (each `List[Tuple[ident, name, triples]]`), `unchanged_idents` (`Set[str]`)), `_lineage_is_provisional(db, ident) -> bool` / `_lineage_confirm(db, ident, index_con=None) -> None` (2a, lines 5069/5053), `_candidate_diff_clear(db, commit_hash, ident, index_con=None) -> None` (2a, line 5207), `_db_execute`/`_transact`/`_retract`/`_db_checkpoint` (pre-existing), `_correction_sweep_through_update` (Task 1), `_CORRECTION_SWEEP_LOG_CAP` (Task 1).
- Produces: `_correction_sweep_apply(db: Any, commit_hash: str, commit_ts_iso: str, file_results: List[tuple], index_con: Optional[Any] = None, skipped_so_far: int = 0) -> int` and `_correction_sweep_log_summary(skipped_events: int) -> None`.

This is the design spec's full three-case per-commit algorithm: confirm a correct provisional guess, fail safe on an ambiguous/wrong one, and reconcile (not merely assert) `:modified-in` for already-authoritative entities — including retracting 2b's documented `:modified-in` over-assertion where this sweep's own parse disagrees. It never calls `_extract_commit` itself (that stays the caller's job, on a different executor) and never writes the commit's own `:type/commit` entity (2b already wrote it for every commit in this sweep's range).

- [ ] **Step 1: Write the failing tests**

```python
class TestCorrectionSweepApply:
    def _init_repo(self, repo):
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _repo_with_evolving_function(self, tmp_path):
        """Three commits: login() genuinely changes body every commit (h0,
        h1, h2); extra() is added at h1 and left byte-identical at h2, so
        #221 marks it unchanged at h2 -- needed for the reconcile-retract
        test below."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)

        (repo / "auth.py").write_text("def login():\n    return 1\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h0"], cwd=repo, check=True, capture_output=True)

        (repo / "auth.py").write_text("def login():\n    return 2\n\ndef extra():\n    pass\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h1"], cwd=repo, check=True, capture_output=True)

        (repo / "auth.py").write_text(
            "def login():\n    return 3\n\ndef extra():\n    pass\n\ndef more():\n    pass\n"
        )
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", "h2"], cwd=repo, check=True, capture_output=True)
        return repo

    def _extract(self, repo, commit_hash):
        import mcp_server
        file_results, _gitlink, _gitmodules, _renamed = mcp_server._extract_commit(str(repo), commit_hash, ())
        return file_results

    def test_confirms_correct_provisional_guess(self, real_db, tmp_path):
        import mcp_server
        import frontier_registry
        repo = self._repo_with_evolving_function(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        # Real reverse walk to the end so login()'s guess settles at h0 --
        # exactly TestReverseFillClaimAndProcess's own converged-state setup.
        for _ in range(3):
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        h0_hash, h0_ts = linearization[0], commit_metadata[0][1]
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{h0_hash[:12]}"
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True

        file_results = self._extract(repo, h0_hash)
        skipped = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        assert skipped == 0
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{h0_hash[:12]}"
        assert mcp_server._candidate_diff_read(real_db, h0_hash, fn_ident) is None

    def test_does_not_duplicate_commit_metadata(self, real_db, tmp_path):
        import mcp_server
        import frontier_registry
        repo = self._repo_with_evolving_function(tmp_path)
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        for _ in range(3):
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        h0_hash, h0_ts = linearization[0], commit_metadata[0][1]
        commit_ident = f":commit/{h0_hash[:12]}"
        before_raw = mcp_server._db_execute(
            real_db, f"(query [:find ?a ?v :where [{commit_ident} ?a ?v]])"
        )
        before = sorted(json.loads(before_raw)["results"])

        file_results = self._extract(repo, h0_hash)
        mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        after_raw = mcp_server._db_execute(
            real_db, f"(query [:find ?a ?v :where [{commit_ident} ?a ?v]])"
        )
        after = sorted(json.loads(after_raw)["results"])
        assert after == before

    def test_leaves_entity_untouched_when_guess_points_elsewhere(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        h1_hash = commit_metadata[1][0]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        # Hand-construct a provisional guess pointing at the WRONG commit
        # for the one this call will visit -- simulating a precondition
        # violation directly rather than via a real interleaving.
        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, f":commit/{h1_hash[:12]}", "2025-01-01T00:00:00Z")

        file_results = self._extract(repo, h0_hash)
        skipped = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        assert skipped >= 1
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True
        assert mcp_server._entity_introduced_by_query(real_db, fn_ident) == f":commit/{h1_hash[:12]}"

    def test_fails_safe_on_duplicate_introduced_by_h0_asserted_first(self, real_db, tmp_path):
        """Two live :introduced-by facts for one entity (the uncoordinated-
        forward-walk state the design spec defers to 2d) must be a no-op
        regardless of which fact minigraf's query returns first -- tested
        here via physical assertion order, in a sibling test via the
        opposite order, since row order itself isn't observable/controllable
        from the test, only the insertion order that produces it."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        h1_hash = commit_metadata[1][0]
        commit_ident_h0 = f":commit/{h0_hash[:12]}"
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        file_results = self._extract(repo, h0_hash)

        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {commit_ident_h0}]]", "2025-01-01T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by :commit/{h1_hash[:12]}]]", "2025-01-02T00:00:00Z")
        mcp_server._lineage_mark_provisional(real_db, fn_ident, "2025-01-01T00:00:00Z")

        skipped = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        assert skipped >= 1
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True

    def test_fails_safe_on_duplicate_introduced_by_h1_asserted_first(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        h1_hash = commit_metadata[1][0]
        commit_ident_h0 = f":commit/{h0_hash[:12]}"
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        file_results = self._extract(repo, h0_hash)

        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by :commit/{h1_hash[:12]}]]", "2025-01-02T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by {commit_ident_h0}]]", "2025-01-01T00:00:00Z")
        mcp_server._lineage_mark_provisional(real_db, fn_ident, "2025-01-01T00:00:00Z")

        skipped = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        assert skipped >= 1
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is True

    def test_skips_modified_in_at_own_introduction_commit_on_resumed_sweep(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        commit_ident = f":commit/{h0_hash[:12]}"
        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, commit_ident, "2025-01-01T00:00:00Z")
        file_results = self._extract(repo, h0_hash)

        mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)  # confirms (case 1)
        assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False

        # Simulate a resumed sweep re-visiting the same commit (e.g. after a
        # crash between confirming and advancing correction-sweep-through).
        mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        raw = mcp_server._db_execute(real_db, f"(query [:find ?c :where [{fn_ident} :modified-in ?c]])")
        assert [commit_ident] not in json.loads(raw)["results"]

    def test_ordinary_modification_is_idempotent_when_2b_already_agrees(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash = commit_metadata[0][0]
        h1_hash, h1_ts = commit_metadata[1][0], commit_metadata[1][1]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        h1_ident = f":commit/{h1_hash[:12]}"
        # login() is already authoritative at h0, and 2b (or forward walk)
        # already wrote the correct :modified-in for h1's genuine change.
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by :commit/{h0_hash[:12]}]]", "2025-01-01T00:00:00Z")
        mcp_server._transact(real_db, f"[[{fn_ident} :modified-in {h1_ident}]]", h1_ts)

        file_results = self._extract(repo, h1_hash)
        skipped = mcp_server._correction_sweep_apply(real_db, h1_hash, h1_ts, file_results)

        assert skipped == 0
        raw = mcp_server._db_execute(
            real_db, f"(query [:find (count ?c) :where [{fn_ident} :modified-in {h1_ident}]])"
        )
        assert json.loads(raw)["results"] == [[1]]

    def test_retracts_2b_over_asserted_retroactive_modified_in(self, real_db, tmp_path):
        """extra() is byte-identical between h1 and h2 (see the fixture
        docstring), so #221 marks it unchanged at h2 -- but simulate 2b's
        documented limitation by asserting :modified-in there anyway, then
        assert this sweep retracts it once it reaches h2 in the ordinary
        (already-authoritative) path."""
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h1_hash = commit_metadata[1][0]
        h2_hash, h2_ts = commit_metadata[2][0], commit_metadata[2][1]
        extra_ident = mcp_server._code_ident("function", "auth.py", "extra")
        h1_ident = f":commit/{h1_hash[:12]}"
        h2_ident = f":commit/{h2_hash[:12]}"
        mcp_server._transact(real_db, f"[[{extra_ident} :introduced-by {h1_ident}]]", "2025-01-01T00:00:00Z")
        mcp_server._transact(real_db, f"[[{extra_ident} :modified-in {h2_ident}]]", h2_ts)  # 2b's over-assertion

        file_results = self._extract(repo, h2_hash)
        precomputed_unchanged = [
            precomputed.get("unchanged_idents", set())
            for _status, _fp, _extracted, precomputed, _old in file_results
            if precomputed is not None
        ]
        assert any(extra_ident in u for u in precomputed_unchanged), (
            "fixture assumption broken: extra() must read as unchanged at h2"
        )

        skipped = mcp_server._correction_sweep_apply(real_db, h2_hash, h2_ts, file_results)

        assert skipped == 0
        raw = mcp_server._db_execute(
            real_db, f"(query [:find ?c :where [{extra_ident} :modified-in {h2_ident}]])"
        )
        assert json.loads(raw)["results"] == []

    def test_opportunistic_stale_candidate_diff_cleanup(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash = commit_metadata[0][0]
        h1_hash, h1_ts = commit_metadata[1][0], commit_metadata[1][1]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        mcp_server._transact(real_db, f"[[{fn_ident} :introduced-by :commit/{h0_hash[:12]}]]", "2025-01-01T00:00:00Z")
        # An orphaned candidate-diff record from a since-superseded guess.
        mcp_server._candidate_diff_persist(real_db, h1_hash, fn_ident, "stale-hash", "2025-01-01T00:00:00Z")
        assert mcp_server._candidate_diff_read(real_db, h1_hash, fn_ident) is not None

        file_results = self._extract(repo, h1_hash)
        mcp_server._correction_sweep_apply(real_db, h1_hash, h1_ts, file_results)

        assert mcp_server._candidate_diff_read(real_db, h1_hash, fn_ident) is None

    def test_skipped_events_counts_events_not_entities(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        h1_hash, h1_ts = commit_metadata[1][0], commit_metadata[1][1]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        wrong_ident = f":commit/{commit_metadata[2][0][:12]}"
        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, wrong_ident, "2025-01-01T00:00:00Z")

        skipped_h0 = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, self._extract(repo, h0_hash))
        skipped_h1 = mcp_server._correction_sweep_apply(
            real_db, h1_hash, h1_ts, self._extract(repo, h1_hash), skipped_so_far=skipped_h0,
        )

        assert skipped_h0 == 1
        assert skipped_h1 == 1  # login() is a candidate at h1 too -- second event, same entity

    def test_skip_logging_capped_with_returned_count_uncapped(self, real_db, tmp_path, capsys):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        # 15 synthetic candidate idents in one file entry, each provisional
        # with a guess pointing elsewhere -- avoids needing 15 real commits
        # to exercise the logging cap; precomputed's shape is documented in
        # this task's Interfaces block.
        n = 15
        idents = [f":function/synthetic-{i}" for i in range(n)]
        wrong_ident = ":commit/does-not-matter"
        for ident in idents:
            mcp_server._entity_introduced_by_set_provisional(real_db, ident, wrong_ident, "2025-01-01T00:00:00Z")
        precomputed = {
            "module_ident": ":module/synthetic",
            "function_entries": [(ident, ident, []) for ident in idents],
            "class_entries": [],
            "global_entries": [],
            "field_entries": [],
            "unchanged_idents": set(),
        }
        file_results = [("M", "synthetic.py", {}, precomputed, "")]

        skipped = mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results)

        assert skipped == n
        err = capsys.readouterr().err
        logged_lines = [line for line in err.splitlines() if "synthetic-" in line]
        assert len(logged_lines) == mcp_server._CORRECTION_SWEEP_LOG_CAP

    def test_log_cap_is_caller_threaded_not_a_module_global(self, real_db, tmp_path, capsys):
        import mcp_server
        repo = self._repo_with_evolving_function(tmp_path)
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        h0_hash, h0_ts = commit_metadata[0][0], commit_metadata[0][1]
        fn_ident = mcp_server._code_ident("function", "auth.py", "login")
        wrong_ident = f":commit/{commit_metadata[1][0][:12]}"
        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, wrong_ident, "2025-01-01T00:00:00Z")
        file_results = self._extract(repo, h0_hash)

        capsys.readouterr()  # clear
        mcp_server._correction_sweep_apply(
            real_db, h0_hash, h0_ts, file_results, skipped_so_far=mcp_server._CORRECTION_SWEEP_LOG_CAP,
        )
        assert capsys.readouterr().err == ""  # budget already spent by caller's running total

        mcp_server._entity_introduced_by_set_provisional(real_db, fn_ident, wrong_ident, "2025-01-01T00:00:00Z")
        mcp_server._correction_sweep_apply(real_db, h0_hash, h0_ts, file_results, skipped_so_far=0)
        assert "login" in capsys.readouterr().err  # fresh run, budget reset

    def test_correction_sweep_log_summary(self, capsys):
        import mcp_server
        mcp_server._correction_sweep_log_summary(0)
        assert capsys.readouterr().err == ""

        mcp_server._correction_sweep_log_summary(3)
        err = capsys.readouterr().err
        assert err.strip() != ""
        assert "3" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepApply -v`
Expected: FAIL with `AttributeError` on every test.

- [ ] **Step 3: Implement `_correction_sweep_apply` and `_correction_sweep_log_summary`**

Insert after Task 2's code:

```python
def _correction_sweep_apply(
    db: Any,
    commit_hash: str,
    commit_ts_iso: str,
    file_results: List[tuple],
    index_con: Optional[Any] = None,
    skipped_so_far: int = 0,
) -> int:
    """Reconciles every candidate entity file_results describes for
    commit_hash, then records progress via _correction_sweep_through_update
    and checkpoints. Returns skipped_events -- how many candidate idents
    landed in the fail-safe skip (provisional with an ambiguous/wrong
    guess, or already-authoritative with an ambiguous introduced-by count),
    i.e. stayed provisional or unreconciled despite this call visiting
    their commit.

    Never calls _extract_commit itself (that's the caller's job, on a
    different executor -- see the design spec's Execution context) and
    never writes commit_hash's own :type/commit entity (2b already wrote
    it for every commit in this sweep's range). DB-bound, parse-free.

    skipped_so_far is the driving loop's running total of skipped_events
    from every previous call this run -- it exists solely to make the
    stderr log cap (_CORRECTION_SWEEP_LOG_CAP) work across calls without
    this function holding any state of its own. Deriving the budget from a
    caller-supplied running total, rather than a module-level counter, is
    what makes the cap reset per run automatically: this server is
    long-lived and runs many ingests, and a module counter would burn its
    budget on the first one and log nothing ever after. The RETURNED count
    is never capped; only what reaches stderr is.

    Never calls _frontier_persist_claim -- frontier-low is not touched by
    this sweep.
    """
    commit_ident = f":commit/{commit_hash[:12]}"
    skipped_events = 0

    for status, _file_path, _extracted, precomputed, _old_path in file_results:
        if status not in ("A", "M"):
            continue  # "D"/"R" deferred -- matches 2b's own scope cut
        candidate_idents = (
            [precomputed["module_ident"]]
            + [ident for ident, _name, _t in precomputed["function_entries"]]
            + [ident for ident, _name, _t in precomputed["class_entries"]]
            + [ident for ident, _name, _t in precomputed["global_entries"]]
            + [ident for ident, _name, _t in precomputed["field_entries"]]
        )
        unchanged_idents = precomputed.get("unchanged_idents", set())

        for ident in candidate_idents:
            raw = _db_execute(db, f"(query [:find ?c :where [{ident} :introduced-by ?c]])")
            introduced_by_values = {row[0] for row in json.loads(raw).get("results", [])}

            if _lineage_is_provisional(db, ident):
                if introduced_by_values == {commit_ident}:
                    # Case 1: the provisional guess matches this commit --
                    # confirm. The :introduced-by fact itself is untouched,
                    # since its value was already correct.
                    _lineage_confirm(db, ident, index_con=index_con)
                    _candidate_diff_clear(db, commit_hash, ident, index_con=index_con)
                else:
                    # Case 2: guess points elsewhere, or an ambiguous
                    # (zero/2+) value count -- fail safe, leave untouched.
                    skipped_events += 1
                    if skipped_so_far + skipped_events < _CORRECTION_SWEEP_LOG_CAP:
                        print(
                            f"[_correction_sweep] {ident} left provisional at {commit_hash} "
                            f"(introduced-by values: {sorted(introduced_by_values)})",
                            file=sys.stderr,
                        )
            else:
                # Case 3: already authoritative.
                if len(introduced_by_values) == 1:
                    (only_value,) = introduced_by_values
                    if only_value == commit_ident:
                        continue  # self-introduction guard: no self-:modified-in
                    raw2 = _db_execute(db, f"(query [:find ?c :where [{ident} :modified-in ?c]])")
                    modified_in_values = {row[0] for row in json.loads(raw2).get("results", [])}
                    already_has_modified_in = commit_ident in modified_in_values
                    if ident in unchanged_idents:
                        if already_has_modified_in:
                            _retract(db, f"[[{ident} :modified-in {commit_ident}]]", index_con=index_con)
                    else:
                        if not already_has_modified_in:
                            _transact(
                                db, f"[[{ident} :modified-in {commit_ident}]]", commit_ts_iso, index_con=index_con,
                            )
                    _candidate_diff_clear(db, commit_hash, ident, index_con=index_con)
                else:
                    # Zero or 2+ distinct values -- same duplicate-fact
                    # risk as case 2 -- skip, left alone rather than
                    # guessed at.
                    skipped_events += 1
                    if skipped_so_far + skipped_events < _CORRECTION_SWEEP_LOG_CAP:
                        print(
                            f"[_correction_sweep] {ident} left unreconciled at {commit_hash} "
                            f"(ambiguous introduced-by values: {sorted(introduced_by_values)})",
                            file=sys.stderr,
                        )

    _correction_sweep_through_update(db, commit_hash, commit_ts_iso, index_con=index_con)
    _db_checkpoint(db)
    return skipped_events


def _correction_sweep_log_summary(skipped_events: int) -> None:
    """Emit the one-line end-of-sweep summary to stderr if skipped_events
    is nonzero; no-op otherwise. A named function rather than an inline
    print in each loop precisely because there are two loops that must say
    the same thing -- _correction_sweep_walk (Task 5) and 2d's own -- and
    an operator grepping for this line should not have to know which drove
    the sweep.
    """
    if skipped_events:
        print(
            f"[_correction_sweep] {skipped_events} entities left provisional/unreconciled this run",
            file=sys.stderr,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepApply -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _correction_sweep_apply (three-case reconciliation) for #222 phase 2c"
```

---

### Task 4: `_correction_sweep_claim_and_process`

**Files:**
- Modify: `mcp_server.py` — insert after Task 3's new code.
- Test: `tests/test_mcp_server.py` — new `TestCorrectionSweepClaimAndProcess` class, appended at EOF.

**Interfaces:**
- Consumes: `_correction_sweep_select_position` (Task 2), `_extract_commit` (pre-existing), `_correction_sweep_apply` (Task 3).
- Produces: `_correction_sweep_claim_and_process(db: Any, repo_path: str, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]], ignore_patterns: Sequence[str] = (), index_con: Optional[Any] = None, hash_to_pos: Optional[Dict[str, int]] = None, skipped_so_far: int = 0) -> Optional[Tuple[str, int]]`.

The synchronous convenience wrapper composing the three pieces above in order, for tests and any caller that doesn't need them on separate executors. **2d must not call this directly** from async code (see the design spec's Execution context) — it fuses the parse and DB phases back together into one function body, exactly the shape that can only be scheduled onto one executor.

- [ ] **Step 1: Write the failing tests**

```python
class TestCorrectionSweepClaimAndProcess:
    def _init_repo(self, repo):
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _commit(self, repo, filename, content, msg):
        (repo / filename).write_text(content)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

    def _repo_with_n_commits(self, tmp_path, n):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        for i in range(n):
            self._commit(repo, f"f{i}.py", f"def f{i}(): pass\n", f"h{i}")
        return repo

    def _linearization_and_metadata(self, repo):
        import mcp_server
        import frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        return linearization, commit_metadata

    def _close_gap(self, repo, db, linearization, commit_metadata):
        """Claim exactly one position from the low side via a real
        FrontierAllocator + _frontier_persist_claim (standing in for 2d's
        future ordinary forward walk), then claim the rest from the high
        side via 2b's real _reverse_fill_claim_and_process until the gap is
        empty. Claiming the *whole* gap from the low side alone (an
        unbounded claim_low() loop) would never touch frontier-high at
        all -- claim_low() alone can empty the gap without claim_high()
        ever running, since gap_hi only moves when claim_high() does -- so
        the low side must stop after a bounded number of claims for
        frontier-high to end up populated. db here is any MiniGrafDb
        instance -- the real_db fixture in most tests, or a
        manually-created MiniGrafDb.open_in_memory() in the two-graph
        composition test below."""
        import mcp_server
        allocator = mcp_server._frontier_load(db, linearization, "2026-01-04T00:00:00Z")
        pos = allocator.claim_low()
        if pos is not None:
            mcp_server._frontier_persist_claim(
                db, linearization, pos, from_low=True, commit_ts_iso=commit_metadata[pos][1],
            )
        while not allocator.is_gap_empty():
            mcp_server._reverse_fill_claim_and_process(
                db, str(repo), linearization, commit_metadata, allocator,
            )
        return allocator

    def test_no_op_propagates_from_select_position(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")  # frontier-high unclaimed

        result = mcp_server._correction_sweep_claim_and_process(
            real_db, str(repo), linearization, commit_metadata,
        )
        assert result is None

    def test_single_call_round_trip(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap(repo, real_db, linearization, commit_metadata)

        result = mcp_server._correction_sweep_claim_and_process(
            real_db, str(repo), linearization, commit_metadata,
        )
        assert result is not None
        commit_hash, skipped_events = result
        bounds = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT)
        assert commit_hash == bounds[0]
        assert skipped_events == 0
        assert mcp_server._correction_sweep_through_query(real_db) == commit_hash

    def test_wrapper_is_a_faithful_composition_of_the_three_pieces(self, tmp_path):
        """Build TWO independent graphs from the same repo fixture (the
        sweep mutates the state a second run would start from, so this
        cannot be two passes over one graph). Against the first, call the
        wrapper; against the second, call the three pieces directly.
        Assert the resulting graph states are identical. Both dbs are
        passed explicitly to every call -- none of these functions touch
        mcp_server's module-global _db -- so this test needs no real_db
        fixture and no open_db() call, just two independent
        MiniGrafDb.open_in_memory() instances."""
        import mcp_server
        from minigraf import MiniGrafDb
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)

        db_a = MiniGrafDb.open_in_memory()
        self._close_gap(repo, db_a, linearization, commit_metadata)
        mcp_server._correction_sweep_claim_and_process(db_a, str(repo), linearization, commit_metadata)

        db_b = MiniGrafDb.open_in_memory()
        self._close_gap(repo, db_b, linearization, commit_metadata)
        selected = mcp_server._correction_sweep_select_position(db_b, linearization, commit_metadata)
        assert selected is not None
        commit_hash, commit_ts_iso = selected
        file_results, _, _, _ = mcp_server._extract_commit(str(repo), commit_hash, ())
        mcp_server._correction_sweep_apply(db_b, commit_hash, commit_ts_iso, file_results)

        query = "(query [:find ?e ?a ?v :where [?e ?a ?v]])"
        results_a = sorted(json.loads(db_a.execute(query))["results"])
        results_b = sorted(json.loads(db_b.execute(query))["results"])
        assert results_a == results_b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepClaimAndProcess -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `_correction_sweep_claim_and_process`**

Insert after Task 3's new code:

```python
def _correction_sweep_claim_and_process(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
    hash_to_pos: Optional[Dict[str, int]] = None,
    skipped_so_far: int = 0,
) -> Optional[Tuple[str, int]]:
    """Synchronous convenience wrapper composing
    _correction_sweep_select_position, _extract_commit, and
    _correction_sweep_apply in order -- for tests and any caller that
    doesn't need them on separate executors. **2d must not call this
    directly** from async code: it fuses the CPU-bound parse and the
    DB-bound writes back into one function body, which can only be
    scheduled onto one executor as a unit. 2d's real loop should await
    each of the three pieces on its own executor instead (see the design
    spec's Execution context).

    Returns (commit_hash, skipped_events), or None if
    _correction_sweep_select_position found nothing safe to do.
    skipped_so_far is forwarded to _correction_sweep_apply unchanged (see
    its docstring for why it exists).
    """
    selected = _correction_sweep_select_position(db, linearization, commit_metadata, hash_to_pos)
    if selected is None:
        return None
    commit_hash, commit_ts_iso = selected
    file_results, _gitlink_changes, _gitmodules_map, _renamed_pairs = _extract_commit(
        repo_path, commit_hash, ignore_patterns
    )
    skipped_events = _correction_sweep_apply(
        db, commit_hash, commit_ts_iso, file_results,
        index_con=index_con, skipped_so_far=skipped_so_far,
    )
    return commit_hash, skipped_events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepClaimAndProcess -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _correction_sweep_claim_and_process wrapper for #222 phase 2c"
```

---

### Task 5: `_correction_sweep_walk`

**Files:**
- Modify: `mcp_server.py` — insert after Task 4's new code.
- Test: `tests/test_mcp_server.py` — new `TestCorrectionSweepWalk` class, appended at EOF.

**Interfaces:**
- Consumes: `_correction_sweep_claim_and_process` (Task 4), `_correction_sweep_log_summary` (Task 3).
- Produces: `_correction_sweep_walk(db: Any, repo_path: str, linearization: List[str], commit_metadata: List[Tuple[str, str, str, str]], ignore_patterns: Sequence[str] = (), index_con: Optional[Any] = None) -> Tuple[int, int]`.

Builds `hash_to_pos` once, repeatedly calls `_correction_sweep_claim_and_process` (threading `skipped_so_far` through) until it returns `None`, then calls `_correction_sweep_log_summary`. This is the last piece of #222 phase 2c — no caller is wired into `_run_ingestion` yet (that's phase 2d).

- [ ] **Step 1: Write the failing tests**

```python
class TestCorrectionSweepWalk:
    def _init_repo(self, repo):
        _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)

    def _commit(self, repo, filename, content, msg):
        (repo / filename).write_text(content)
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

    def _repo_with_n_commits(self, tmp_path, n):
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_repo(repo)
        for i in range(n):
            self._commit(repo, f"f{i}.py", f"def f{i}(): pass\n", f"h{i}")
        return repo

    def _linearization_and_metadata(self, repo):
        import mcp_server
        import frontier_registry
        linearization = frontier_registry.build_linearization(str(repo))
        commit_metadata = mcp_server._git_commits(str(repo), watermark_hash=None)
        return linearization, commit_metadata

    def _close_gap_at(self, repo, real_db, linearization, commit_metadata, split_pos):
        """Close the gap with the low side claiming positions [0, split_pos)
        and the high side (2b) claiming the rest -- split_pos == len(linearization)
        closes the whole thing from the low side alone."""
        import mcp_server
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        for _ in range(split_pos):
            pos = allocator.claim_low()
            mcp_server._frontier_persist_claim(
                real_db, linearization, pos, from_low=True, commit_ts_iso=commit_metadata[pos][1],
            )
        while not allocator.is_gap_empty():
            mcp_server._reverse_fill_claim_and_process(
                real_db, str(repo), linearization, commit_metadata, allocator,
            )
        return allocator

    def test_walks_to_exhaustion_and_returns_counts(self, real_db, tmp_path):
        """split_pos must be >= 1: frontier-low's persisted bounds don't
        exist in the DB at all until the first claim_low() position is
        persisted via _frontier_persist_claim(from_low=True, ...) --
        split_pos=0 never calls that, so _correction_sweep_select_position's
        `low_bounds is None` precondition check would always fail even
        though the in-memory allocator's gap does become logically empty
        via high-side claims alone. With split_pos=1, one position is
        claimed by the low side (not part of this sweep's own range), so
        the correction sweep processes the remaining 3 of 4 commits."""
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 4)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap_at(repo, real_db, linearization, commit_metadata, split_pos=1)

        commits_processed, skipped_events = mcp_server._correction_sweep_walk(
            real_db, str(repo), linearization, commit_metadata,
        )

        assert commits_processed == 3
        assert skipped_events == 0
        assert mcp_server._correction_sweep_through_query(real_db) == linearization[-1]

    def test_returns_zero_zero_when_gap_still_open(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        allocator = mcp_server._frontier_load(real_db, linearization, "2026-01-04T00:00:00Z")
        mcp_server._reverse_fill_claim_and_process(
            real_db, str(repo), linearization, commit_metadata, allocator,
        )
        assert allocator.is_gap_empty() is False

        result = mcp_server._correction_sweep_walk(real_db, str(repo), linearization, commit_metadata)
        assert result == (0, 0)

    def test_no_op_when_already_fully_swept(self, real_db, tmp_path):
        """split_pos=1, not 0 -- see test_walks_to_exhaustion_and_returns_counts's
        docstring for why split_pos=0 never persists frontier-low at all."""
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 2)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap_at(repo, real_db, linearization, commit_metadata, split_pos=1)
        mcp_server._correction_sweep_walk(real_db, str(repo), linearization, commit_metadata)

        result = mcp_server._correction_sweep_walk(real_db, str(repo), linearization, commit_metadata)
        assert result == (0, 0)

    def test_frontier_low_and_lineage_confirmed_through_never_touched(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 3)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap_at(repo, real_db, linearization, commit_metadata, split_pos=1)

        low_before = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_LOW_IDENT)
        lineage_before = mcp_server._lineage_confirmed_through_query(real_db)

        mcp_server._correction_sweep_walk(real_db, str(repo), linearization, commit_metadata)

        low_after = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_LOW_IDENT)
        lineage_after = mcp_server._lineage_confirmed_through_query(real_db)
        assert low_after == low_before
        assert lineage_after == lineage_before
        assert mcp_server._correction_sweep_through_query(real_db) is not None

    def test_full_integration(self, real_db, tmp_path):
        import mcp_server
        repo = self._repo_with_n_commits(tmp_path, 5)
        linearization, commit_metadata = self._linearization_and_metadata(repo)
        self._close_gap_at(repo, real_db, linearization, commit_metadata, split_pos=2)

        commits_processed, skipped_events = mcp_server._correction_sweep_walk(
            real_db, str(repo), linearization, commit_metadata,
        )

        assert commits_processed == 3  # the 3 positions the high side claimed
        assert skipped_events == 0
        for i in range(5):
            fn_ident = mcp_server._code_ident("function", f"f{i}.py", f"f{i}")
            assert mcp_server._lineage_is_provisional(real_db, fn_ident) is False
        raw = mcp_server._db_execute(real_db, "(query [:find ?e :where [?e :entity-type :type/candidate-diff]])")
        assert json.loads(raw)["results"] == []
        bounds = mcp_server._frontier_read_bounds(real_db, mcp_server._FRONTIER_HIGH_IDENT)
        assert mcp_server._correction_sweep_through_query(real_db) == bounds[1]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepWalk -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement `_correction_sweep_walk`**

Insert after Task 4's new code:

```python
def _correction_sweep_walk(
    db: Any,
    repo_path: str,
    linearization: List[str],
    commit_metadata: List[Tuple[str, str, str, str]],
    ignore_patterns: Sequence[str] = (),
    index_con: Optional[Any] = None,
) -> Tuple[int, int]:
    """Build hash_to_pos once and repeatedly call
    _correction_sweep_claim_and_process (passing it down, along with the
    running skipped-events total as skipped_so_far) until that returns
    None, then call _correction_sweep_log_summary with the final total.

    Returns (commits_processed, skipped_events) -- summed across every
    call, both 0 both when the gap-closed precondition isn't met yet (the
    common case early in a run) and when the sweep has already fully
    caught up to frontier-high's :hi-hash.

    Also a synchronous convenience wrapper, same caveat as
    _correction_sweep_claim_and_process: 2d should drive the three-step
    pipeline directly in its own loop, not call this -- but 2d's loop owes
    the same two things this one does: threading skipped_so_far through
    every _correction_sweep_apply call, and calling
    _correction_sweep_log_summary when its own loop ends.
    """
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    commits_processed = 0
    skipped_events = 0
    while True:
        result = _correction_sweep_claim_and_process(
            db, repo_path, linearization, commit_metadata,
            ignore_patterns=ignore_patterns, index_con=index_con,
            hash_to_pos=hash_to_pos, skipped_so_far=skipped_events,
        )
        if result is None:
            break
        _commit_hash, call_skipped = result
        commits_processed += 1
        skipped_events += call_skipped
    _correction_sweep_log_summary(skipped_events)
    return commits_processed, skipped_events
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py::TestCorrectionSweepWalk -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: all tests pass, including every pre-existing phase 1/2a/2b test class.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add _correction_sweep_walk driving loop for #222 phase 2c"
```

---

## Self-Review Notes

**Spec coverage:**
- Watermark + log cap constant → Task 1.
- Gap-closed precondition, persisted-`:hi-hash` ceiling, resume-from-watermark, stale-hash fallback, `commit_metadata` contract check → Task 2.
- Full three-case classification (confirm / fail-safe skip / reconcile-with-self-intro-guard), `:type/commit` non-write (implicit — `_correction_sweep_apply` never references commit metadata triples), candidate-diff cleanup (both the case-1 clear and case-3 opportunistic clear), capped+caller-threaded stderr logging, `_correction_sweep_log_summary` → Task 3.
- Synchronous wrapper composing the three independently-schedulable pieces, proven faithful via the two-graph comparison test → Task 4.
- Driving loop, `hash_to_pos` built once (not per-call), frontier-low/lineage-confirmed-through untouched, full integration → Task 5.
- The design spec's "Execution context" section (the actual `await`/executor wiring, and the extraction-pipelining note) is explicitly out of scope for this plan — it's 2d's job; this plan only produces the function *shape* that makes that wiring possible, per the spec itself.
- The design spec's `ignore_patterns` cross-run consistency caveat and the "wrong provisional guess" reconciliation case are both explicitly deferred in the spec itself (to 2d, or left unresolved) — no task implements them, matching the spec's own scope.

**Placeholder scan:** No task leaves a stub, `pass`, or "implement later" — every task's function is complete and independently correct for its own declared scope (e.g. Task 3's `_correction_sweep_apply` fully implements all three cases in one task, matching how 2b's own plan built `_reverse_fill_claim_and_process`'s full algorithm in a single task).

**Type consistency:** `_correction_sweep_select_position` returns `Optional[Tuple[str, str]]` in Task 2 and is destructured identically (`commit_hash, commit_ts_iso = selected`) in Task 4. `_correction_sweep_apply` returns `int` (Task 3) and is used as `skipped_events = _correction_sweep_apply(...)` in Task 4, then summed in Task 5. `_correction_sweep_claim_and_process` returns `Optional[Tuple[str, int]]` (Task 4) and is destructured as `_commit_hash, call_skipped = result` in Task 5. `skipped_so_far` is threaded as a plain `int` default `0` through Tasks 3→4→5 consistently.
