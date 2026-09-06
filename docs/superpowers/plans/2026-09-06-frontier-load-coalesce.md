# Load-Time Provisional Interval Coalescing (#329) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_frontier_load` always return a provisional interval set that is disjoint and non-adjacent, persisting the merge to disk, so two contiguous loaded intervals can no longer decline Stage B forever.

**Architecture:** The merge rule already exists as `FrontierAllocator._coalesce`. Extract its body to a module-level `frontier_registry.coalesce_intervals(intervals, tag)`, make the method a thin wrapper, and call the same function from `_frontier_load` after `_frontier_promote_base_if_missing`. A new `_frontier_persist_merge` mirrors the in-memory merge onto the graph — absorbed entities retracted first, then survivors' bounds and `:pos-count` widened — and a new `_frontier_check_load_invariants` is the post-condition.

**Tech Stack:** Python 3.10+, `minigraf>=2.0.0,<3.0.0`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-frontier-load-coalesce-design.md`

## Global Constraints

- **ALWAYS use `.venv/bin/python`.** System python has minigraf 1.1.1 against a `>=2.0.0` floor and fakes ~122 test failures. Every command in this plan uses `.venv/bin/python -m pytest`.
- **Real backend, always.** No `MagicMock` of `MiniGrafDb`. Use the `real_db` fixture in `tests/test_mcp_server.py`. See `docs/testing-conventions.md`.
- **Every regression test must be ablation-proven.** Run it against the pre-fix code, observe it FAIL, and confirm it fails on the assertion that names the defect — not on an unrelated error. A guard that has never been seen red guarantees nothing.
- **No `GRAPH_FORMAT_VERSION` bump and no migration step.** This changes no fact shape.
- **Only `TAG_PROVISIONAL` is coalesced.** `_frontier_load` appends at most one authoritative interval, and the authoritative/provisional boundary must survive the two sides becoming adjacent.
- **Do not narrow `_intervals_read_extra`'s query.** Its permissiveness is documented as deliberate.
- **`:pos-count` is a CHECKSUM, not a proof of set identity.** Do not upgrade that language anywhere.
- Branch: `329-frontier-load-coalesce` (already created; spec committed as `af5fdc4`).

---

### Task 1: Extract the merge rule to `frontier_registry.coalesce_intervals`

**Files:**
- Modify: `frontier_registry.py` (`FrontierAllocator._coalesce`)
- Test: `tests/test_frontier_registry.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `frontier_registry.coalesce_intervals(intervals: List[Interval], tag: str) -> Tuple[List[Interval], List[Interval]]`, returning `(merged_full_list, absorbed)`. `merged_full_list` contains every interval of every tag, sorted by `lo_pos`, with same-`tag` overlaps and adjacencies merged. `absorbed` holds the intervals that were merged away, each still carrying its original `ident`. Task 3 calls this from `mcp_server._frontier_load`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_frontier_registry.py`:

```python
class TestCoalesceIntervalsModuleLevel:
    """#329: the merge rule has to be callable WITHOUT a claim, because
    _frontier_load needs it on a set it has just built from graph facts.
    _coalesce runs only from _extend, and _extend only from a claim, so a
    loaded-but-never-claimed set never merges."""

    def test_contiguous_provisional_intervals_merge_and_report_the_absorbed(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(11, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 25)]
        assert merged[0].is_base is True
        assert merged[0].ident == ":ingestion/frontier-high", (
            "the base must survive every merge it takes part in -- it is what "
            "persists at the fixed frontier-high ident"
        )
        assert [iv.ident for iv in absorbed] == [":ingestion/interval-provisional-abc"]

    def test_overlapping_provisional_intervals_merge_to_their_union(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(5, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 25)]
        assert len(absorbed) == 1

    def test_disjoint_intervals_with_a_real_gap_do_not_merge(self):
        loaded = [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(12, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos) for iv in merged] == [(0, 10), (12, 25)]
        assert absorbed == []

    def test_an_authoritative_interval_is_returned_untouched_and_never_merged(self):
        """The authoritative/provisional boundary is the lineage frontier
        later phases read, and must survive the two sides being adjacent."""
        loaded = [
            Interval(0, 10, TAG_AUTHORITATIVE, anchor_pos=0, is_base=True,
                     ident=":ingestion/frontier-low"),
            Interval(11, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=True,
                     ident=":ingestion/frontier-high"),
        ]
        merged, absorbed = frontier_registry.coalesce_intervals(loaded, TAG_PROVISIONAL)
        assert [(iv.lo_pos, iv.hi_pos, iv.tag) for iv in merged] == [
            (0, 10, TAG_AUTHORITATIVE), (11, 25, TAG_PROVISIONAL),
        ]
        assert absorbed == []

    def test_the_allocator_method_still_merges_on_a_claim(self):
        """_coalesce is now a wrapper -- claim-time behaviour must be
        byte-for-byte what it was, or every #325 guarantee moves."""
        alloc = FrontierAllocator(30, [
            Interval(0, 10, TAG_PROVISIONAL, anchor_pos=10, is_base=True,
                     ident=":ingestion/frontier-high"),
            Interval(12, 25, TAG_PROVISIONAL, anchor_pos=25, is_base=False,
                     ident=":ingestion/interval-provisional-abc"),
        ])
        assert alloc.claim_high() == 29
        assert alloc.claim_high() == 28
        prov = [iv for iv in alloc.intervals() if iv.tag == TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 10), (12, 25), (28, 29)]
        assert alloc.claim_high() == 27
        assert alloc.claim_high() == 26
        prov = [iv for iv in alloc.intervals() if iv.tag == TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 10), (12, 29)], (
            "the claim at 26 must merge the new top interval into the "
            "extra at [12,25], and the LOWER one wins as survivor"
        )
        assert alloc.last_claim.absorbed != [], (
            "the merging claim must still report what it swallowed, or "
            "_reverse_claim_persist_target has nothing to retract"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_frontier_registry.py::TestCoalesceIntervalsModuleLevel -v
```

Expected: the first four FAIL with `AttributeError: module 'frontier_registry' has no attribute 'coalesce_intervals'`. `test_the_allocator_method_still_merges_on_a_claim` should already PASS — it is the characterisation test that pins existing behaviour across the refactor, so a failure there means the test itself is wrong about today's code and must be corrected before proceeding.

- [ ] **Step 3: Extract the function**

In `frontier_registry.py`, add above `class FrontierAllocator`:

```python
def coalesce_intervals(
    intervals: List[Interval], tag: str
) -> Tuple[List[Interval], List[Interval]]:
    """Merge same-tag intervals that overlap or touch. Returns
    (every interval sorted by lo_pos with `tag`'s merged, the intervals
    merged AWAY so the persistence layer can retract their entities).

    Module-level rather than a method (#329) because _frontier_load needs
    the merge on a set it has just built from graph facts, with no claim in
    sight: _coalesce ran only from _extend, and _extend only from a claim,
    so two contiguous LOADED intervals with an already-empty gap never
    merged. Both callers share this one function so the load-time merge
    cannot drift from the claim-time merge -- it IS the claim-time merge.

    Survivor rule: the base wins if either participant is base; otherwise
    the LOWER one wins and keeps its anchor_pos. The base is what persists
    at the fixed :ingestion/frontier-high ident, so it must survive every
    merge it takes part in or that ident would have to be re-pointed at a
    different entity. The keeper's `ident` travels with it the same way
    anchor_pos does -- both name the surviving entity's identity, opaque to
    this module either way.

    Only same-tag intervals merge -- the authoritative/provisional boundary
    is the lineage frontier later phases read, and must survive the two
    sides becoming adjacent.
    """
    same = sorted((iv for iv in intervals if iv.tag == tag), key=lambda iv: iv.lo_pos)
    merged: List[Interval] = []
    absorbed: List[Interval] = []
    for iv in same:
        if merged and iv.lo_pos <= merged[-1].hi_pos + 1:
            prev = merged[-1]
            keeper, loser = (prev, iv) if (prev.is_base or not iv.is_base) else (iv, prev)
            absorbed.append(loser)
            merged[-1] = Interval(
                prev.lo_pos, max(prev.hi_pos, iv.hi_pos), tag,
                keeper.anchor_pos, prev.is_base or iv.is_base, keeper.ident,
            )
        else:
            merged.append(iv)
    others = [iv for iv in intervals if iv.tag != tag]
    return sorted(others + merged, key=lambda iv: iv.lo_pos), absorbed
```

Then replace `FrontierAllocator._coalesce`'s entire body with the wrapper, keeping a short docstring that points at the extracted function:

```python
    def _coalesce(self, tag: str) -> List[Interval]:
        """Claim-time merge. The rule itself lives in the module-level
        coalesce_intervals (#329), shared with mcp_server._frontier_load's
        load-time merge so the two cannot drift."""
        self._intervals, absorbed = coalesce_intervals(self._intervals, tag)
        return absorbed
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_frontier_registry.py -v
```

Expected: PASS, the whole file — the existing `TestCoalesceSurvivorRuleHigherBase`, `TestFragmentedProvisionalSet` and `TestFrontierAllocatorGrownLinearization` classes are the real regression net for this refactor and must all stay green.

- [ ] **Step 5: Commit**

```bash
git add frontier_registry.py tests/test_frontier_registry.py
git commit -m "Extract the interval merge rule to a module-level function (#329)

_coalesce ran only from _extend, and _extend only from a claim, so a set
built by _frontier_load and never claimed against never merged. Extracting
the rule lets the load path share it rather than mirror it.

Pure refactor: no behaviour change, and the claim-time characterisation
test pins that.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

### Task 2: The load post-condition

**Files:**
- Modify: `mcp_server.py` (add `_frontier_check_load_invariants` immediately after `_frontier_promote_base_if_missing`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `frontier_registry.TAG_PROVISIONAL`.
- Produces: `mcp_server._frontier_check_load_invariants(intervals: List[frontier_registry.Interval], strict: bool = True) -> None`. Raises `RuntimeError` on an adjacent/overlapping provisional pair when `strict`; prints to stderr and returns otherwise. Always prints (never raises) when the provisional base is not the lowest provisional interval. Task 3 calls it at the end of `_frontier_load`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestFrontierCheckLoadInvariants:
    """#329: the load post-condition. Two violations, two deliberately
    different consequences -- see the design spec's "Post-condition"."""

    def _prov(self, lo, hi, is_base=False, ident=":ingestion/interval-provisional-x"):
        import frontier_registry
        return frontier_registry.Interval(
            lo, hi, frontier_registry.TAG_PROVISIONAL,
            anchor_pos=hi, is_base=is_base, ident=ident,
        )

    def test_adjacent_provisional_intervals_raise(self):
        import mcp_server
        intervals = [
            self._prov(0, 10, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
            self._prov(11, 25),
        ]
        with pytest.raises(RuntimeError, match="adjacent or overlapping"):
            mcp_server._frontier_check_load_invariants(intervals)

    def test_overlapping_provisional_intervals_raise(self):
        import mcp_server
        intervals = [
            self._prov(0, 10, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
            self._prov(5, 25),
        ]
        with pytest.raises(RuntimeError, match="adjacent or overlapping"):
            mcp_server._frontier_check_load_invariants(intervals)

    def test_disjoint_non_adjacent_provisional_intervals_pass(self):
        import mcp_server
        intervals = [
            self._prov(0, 10, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
            self._prov(12, 25),
        ]
        mcp_server._frontier_check_load_invariants(intervals)  # must not raise

    def test_non_strict_downgrades_the_adjacency_raise_to_a_warning(self, capsys):
        """The defensive `ident is None` path in _frontier_coalesce_loaded
        leaves the graph exactly as found and re-walks. Raising there would
        make an unmergeable-but-harmless state fatal instead of fail-safe."""
        import mcp_server
        intervals = [
            self._prov(0, 10, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
            self._prov(11, 25),
        ]
        mcp_server._frontier_check_load_invariants(intervals, strict=False)
        assert "adjacent or overlapping" in capsys.readouterr().err

    def test_a_base_that_is_not_lowest_warns_and_does_not_raise(self, capsys):
        """Nothing produces this state today and it degrades conservatively.
        Raising would abort EVERY future run on such a graph forever, with
        no repair path -- a permanent denial of service, worse than the
        state being guarded against."""
        import mcp_server
        intervals = [
            self._prov(0, 10, ident=":ingestion/interval-provisional-below"),
            self._prov(12, 25, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
        ]
        mcp_server._frontier_check_load_invariants(intervals)  # must not raise
        assert "base is not the lowest" in capsys.readouterr().err

    def test_an_authoritative_interval_adjacent_to_a_provisional_one_is_fine(self):
        """The authoritative/provisional boundary is the lineage frontier;
        the two sides being adjacent is the NORMAL converged state, not a
        violation. A check that fired here would go red on every healthy
        graph."""
        import mcp_server, frontier_registry
        intervals = [
            frontier_registry.Interval(
                0, 10, frontier_registry.TAG_AUTHORITATIVE, anchor_pos=0,
                is_base=True, ident=mcp_server._FRONTIER_LOW_IDENT),
            self._prov(11, 25, is_base=True, ident=mcp_server._FRONTIER_HIGH_IDENT),
        ]
        mcp_server._frontier_check_load_invariants(intervals)  # must not raise
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierCheckLoadInvariants -v
```

Expected: all FAIL with `AttributeError: module 'mcp_server' has no attribute '_frontier_check_load_invariants'`.

- [ ] **Step 3: Implement the post-condition**

Add to `mcp_server.py`, immediately after `_frontier_promote_base_if_missing`:

```python
def _frontier_check_load_invariants(
    intervals: List["frontier_registry.Interval"], strict: bool = True
) -> None:
    """#329: what _frontier_load promises its callers about the provisional
    interval set it returns.

    ADJACENT OR OVERLAPPING -> raise (when `strict`). After
    _frontier_coalesce_loaded this is unreachable unless
    frontier_registry.coalesce_intervals or _frontier_persist_merge is
    broken, so it should never fire. A raise here is caught by
    _run_ingestion's run-level `except`: status goes to `error`, the
    traceback reaches fd 2 (so stderr_capture's `error_signals` and
    run_ingestion_benchmark._exit_code fail the at-scale gate), and no walk
    has started, so nothing is half-written. `strict=False` is for the one
    caller path that DELIBERATELY did not merge -- see
    _frontier_coalesce_loaded's ident guard -- where leaving the graph as
    found and re-walking is the fail-safe outcome and a raise would not be.

    BASE NOT LOWEST -> stderr, never a raise. Coalescing does not enforce
    this: two DISJOINT provisional intervals with a real gap between them
    never merge, and _intervals_read_extra's query carries no positional
    predicate, so a below-base extra would load. Nothing produces that state
    today and it degrades conservatively (one more interval a caller
    re-walks or folds defensively, never one silently dropped). Raising on
    it would abort every future run on such a graph forever, with no repair
    path -- turning conservative degradation into permanent denial of
    service, which is worse than the state being guarded against.

    Cross-tag overlap is deliberately NOT checked. It is unreachable
    (claim_low/claim_high are served from _unclaimed, the complement of the
    interval set, so no claim can land inside an interval of ANY tag), and
    adjacency ACROSS tags is the normal converged state -- the
    authoritative/provisional boundary is the lineage frontier, not a
    defect. A third raise would only widen the blast radius of a guard whose
    whole point is to stay quiet.
    """
    prov = sorted(
        (iv for iv in intervals if iv.tag == frontier_registry.TAG_PROVISIONAL),
        key=lambda iv: iv.lo_pos,
    )
    for prev, nxt in zip(prov, prov[1:]):
        if nxt.lo_pos <= prev.hi_pos + 1:
            message = (
                "[_frontier_load] provisional intervals still adjacent or "
                f"overlapping after coalescing: [{prev.lo_pos},{prev.hi_pos}] "
                f"and [{nxt.lo_pos},{nxt.hi_pos}] (#329)"
            )
            if strict:
                raise RuntimeError(message)
            print(message, file=sys.stderr)
            return
    bases = [iv for iv in prov if iv.is_base]
    if bases and bases[0].lo_pos != prov[0].lo_pos:
        print(
            "[_frontier_load] provisional base is not the lowest provisional "
            f"interval: base at [{bases[0].lo_pos},{bases[0].hi_pos}], lowest "
            f"at [{prov[0].lo_pos},{prov[0].hi_pos}] (#329)",
            file=sys.stderr,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierCheckLoadInvariants -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add the _frontier_load post-condition (#329)

Adjacent or overlapping provisional intervals raise; a base that is not the
lowest warns. The asymmetry is deliberate: adjacency is unreachable once
the coalesce lands, while base-order degrades conservatively and raising on
it would abort every future run on such a graph forever with no repair path.

Not yet wired into _frontier_load.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

### Task 3: Coalesce and persist in `_frontier_load`

**Files:**
- Modify: `mcp_server.py` — add `_frontier_persist_merge` and `_frontier_coalesce_loaded` after `_frontier_check_load_invariants`; modify `_frontier_load`'s tail (the two lines from `_frontier_promote_base_if_missing(...)` to `return frontier_registry.FrontierAllocator(...)`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `frontier_registry.coalesce_intervals` (Task 1), `mcp_server._frontier_check_load_invariants` (Task 2), and the existing `_interval_persist_ident`, `_frontier_read_bounds`, `_frontier_read_pos_count`, `_frontier_discard_interval`, `_frontier_pos_count_delta`, `_edn_escape`, `_retract`, `_transact`.
- Produces: `_frontier_persist_merge(db, linearization, merged, absorbed, run_ts_iso, index_con=None) -> None` and `_frontier_coalesce_loaded(db, linearization, intervals, run_ts_iso, index_con=None) -> bool` (returns whether the provisional set is now guaranteed disjoint and non-adjacent). No later task depends on these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestFrontierLoadCoalescesProvisionalIntervals:
    """#329: two live provisional entities that are contiguous or
    overlapping never merged, because frontier_registry._coalesce runs only
    from _extend and _extend only from a claim -- and with the gap already
    empty, no claim ever happens.

    _intervals_read_extra was then permanently non-empty, so
    _correction_sweep_select_position and _should_fold_lineage_watermark
    both returned early on every subsequent run: Stage B never ran again,
    :ingestion/lineage-confirmed-through never advanced, and provisional
    :introduced-by stayed provisional for the life of the graph -- on runs
    reporting status: complete, with a clean divergence and zero bytes on
    stderr.

    Seeded directly rather than driven through the issue's reachable path.
    That path runs through _skip_claim, which #325 made vestigial (it needs
    a loadable archived :type/completed-region, and after #325 that exists
    only on the divergent-ref-regained path). The defect is in the LOAD
    CONTRACT, which is where the fix lives and where every future producer
    of this state arrives -- pinning the test to one narrow route through a
    mechanism that may itself be removed would cover less, not more.
    """

    def _seed_interval(self, db, ident, lin, lo, hi, count, tag=":provisional"):
        import mcp_server
        facts = [
            f"[{ident} :entity-type :type/ingest-interval]",
            f"[{ident} :tag {tag}]",
            f'[{ident} :lo-hash "{lin[lo]}"]',
            f'[{ident} :hi-hash "{lin[hi]}"]',
        ]
        if count is not None:
            facts.append(f"[{ident} :pos-count {count}]")
        if ident.startswith(mcp_server._INTERVAL_PROVISIONAL_IDENT_PREFIX):
            facts.append(f'[{ident} :ident "{ident}"]')
        mcp_server._transact(db, "[" + " ".join(facts) + "]", "2026-09-06T00:00:00Z")

    def test_contiguous_loaded_intervals_merge_in_memory_and_on_disk(self, real_db):
        # The linearization stops exactly at the extra's hi, so the gap is
        # ALREADY EMPTY -- no claim will ever happen, which is what makes
        # the unmerged state permanent rather than merely temporary.
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(26)]
        self._seed_interval(real_db, mcp_server._FRONTIER_HIGH_IDENT, lin, 0, 10, 11)
        extra = mcp_server._interval_ident(lin[25])
        self._seed_interval(real_db, extra, lin, 11, 25, 15)

        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:01Z")

        assert alloc.is_gap_empty(), (
            "fixture check: with a non-empty gap a later claim could merge "
            "these by itself, and the test would not be about #329"
        )
        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 25)], (
            "two contiguous loaded intervals must merge -- this is #329"
        )
        assert prov[0].is_base is True
        assert prov[0].ident == mcp_server._FRONTIER_HIGH_IDENT

        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) == ("h0", "h25")
        assert mcp_server._frontier_read_pos_count(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) == 26
        assert mcp_server._intervals_read_extra(real_db) == [], (
            "the absorbed entity's facts must be gone -- a permanently "
            "non-empty _intervals_read_extra is what declines Stage B forever"
        )
        assert mcp_server._frontier_read_bounds(real_db, extra) is None

    def test_overlapping_loaded_intervals_merge_to_their_union(self, real_db):
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(26)]
        self._seed_interval(real_db, mcp_server._FRONTIER_HIGH_IDENT, lin, 0, 10, 11)
        extra = mcp_server._interval_ident(lin[25])
        self._seed_interval(real_db, extra, lin, 5, 25, 21)

        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:01Z")

        assert alloc.is_gap_empty()
        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 25)]
        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) == ("h0", "h25")
        assert mcp_server._frontier_read_pos_count(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) == 26
        assert mcp_server._intervals_read_extra(real_db) == []

    def test_the_merge_survives_a_reload(self, real_db):
        """The merged interval must itself be RETAINED next time: its
        :pos-count has to equal its span, or the whole union is discarded
        and re-walked on every run."""
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(26)]
        self._seed_interval(real_db, mcp_server._FRONTIER_HIGH_IDENT, lin, 0, 10, 11)
        extra = mcp_server._interval_ident(lin[25])
        self._seed_interval(real_db, extra, lin, 11, 25, 15)

        mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:01Z")
        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:02Z")

        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 25)], (
            "the merged interval must be RETAINED on reload, not discarded: "
            "its :pos-count has to equal its span or the whole union is "
            "re-walked on every run"
        )
        assert alloc.is_gap_empty()

    def test_a_real_gap_between_loaded_intervals_is_preserved(self, real_db):
        """The positive control. A fix that merged unconditionally would
        pass every test above while silently asserting completion over a
        span nothing ever walked."""
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(30)]
        self._seed_interval(real_db, mcp_server._FRONTIER_HIGH_IDENT, lin, 0, 10, 11)
        extra = mcp_server._interval_ident(lin[25])
        self._seed_interval(real_db, extra, lin, 13, 25, 13)

        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:01Z")

        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(0, 10), (13, 25)]
        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) == ("h0", "h10")
        assert [r[0] for r in mcp_server._intervals_read_extra(real_db)] == [extra]
        assert (11, 12) in alloc._unclaimed()

    def test_the_authoritative_side_is_untouched_when_it_abuts_the_base(self, real_db):
        """frontier-low ending exactly where frontier-high begins is the
        normal converged state. Merging across the tag boundary would
        destroy the lineage-authority frontier later phases read."""
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(30)]
        self._seed_interval(
            real_db, mcp_server._FRONTIER_LOW_IDENT, lin, 0, 10, None,
            tag=":authoritative",
        )
        self._seed_interval(real_db, mcp_server._FRONTIER_HIGH_IDENT, lin, 11, 20, 10)

        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-06T00:00:01Z")

        by_tag = sorted(
            ((iv.lo_pos, iv.hi_pos, iv.tag) for iv in alloc.intervals()),
            key=lambda t: t[0],
        )
        assert by_tag == [
            (0, 10, frontier_registry.TAG_AUTHORITATIVE),
            (11, 20, frontier_registry.TAG_PROVISIONAL),
        ]
        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_LOW_IDENT) == ("h0", "h10")
```

- [ ] **Step 2: Run the tests to verify they fail, and confirm the failure names the defect**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLoadCoalescesProvisionalIntervals -v
```

Expected: `test_contiguous_loaded_intervals_merge_in_memory_and_on_disk`, `test_overlapping_loaded_intervals_merge_to_their_union` and `test_the_merge_survives_a_reload` FAIL on the interval-list assertion (two intervals where one is expected), **not** on an `AttributeError` or a seeding error. `test_a_real_gap_between_loaded_intervals_is_preserved` and `test_the_authoritative_side_is_untouched_when_it_abuts_the_base` should already PASS — they are positive controls, and a failure there means the fixture is wrong, not the production code.

**Record the observed failure text in the commit message at Step 5.** A test not seen red on the pre-fix code is not a regression test.

- [ ] **Step 3: Implement the merge and its persistence**

Add to `mcp_server.py`, after `_frontier_check_load_invariants`:

```python
def _frontier_persist_merge(
    db: Any,
    linearization: List[str],
    merged: List["frontier_registry.Interval"],
    absorbed: List["frontier_registry.Interval"],
    run_ts_iso: str,
    index_con: Optional[Any] = None,
) -> None:
    """#329: mirror a load-time coalesce onto the graph.

    Idents come from _interval_persist_ident for BOTH sides, so this agrees
    exactly with what the reverse walk's write dispatch
    (_reverse_claim_persist_target) would resolve for the same Interval --
    including #325 Finding 3's case, where a loaded extra's anchor_pos fell
    back to hi_pos and re-deriving _interval_ident would mint a DIFFERENT
    ident than the one actually on disk.

    ORDER: absorbed entities are discarded FIRST, then survivors are
    widened. Same order and same rationale as _frontier_persist_claim's
    absorb-then-extend. A crash between the two leaves the absorbed span
    described by NOBODY -- it reads unclaimed and is re-walked by the next
    _frontier_load, losing nothing. Widening first would risk the DUPLICATE
    outcome: the survivor already claiming the merged span while the
    absorbed entity's now-redundant facts are still live, invisible right up
    until the discard never runs, leaking a phantom into
    _intervals_read_extra forever.

    The survivor's new :pos-count is the merged span, and that is a
    CLAIM-TIME denominator, not #326's computed-where-it-is-read trap. The
    difference is which run does the comparing. Both components were
    validated against THIS run's linearization moments earlier
    (_load_one_interval retains only when the STORED claim-time count still
    equals the current span), and their adjacency was established in that
    same linearization -- so the merged count is a fresh assertion about
    THIS run, compared in a LATER run against a linearization that may
    differ. It discriminates. #326's archive case was different: archiving
    and loading ran in the SAME run against the SAME linearization, so a
    count computed at archive time always agreed.

    ACCEPTED COST: merging is coarser than keeping the components apart. A
    later commit landing inside what used to be the upper component now
    discards the whole union rather than that component alone. Bigger
    re-walk, never a loss. And :pos-count remains a CHECKSUM, not a proof of
    set identity -- the #326 residual applies verbatim to every interval
    this produces.
    """
    tag = ":provisional"
    for iv in absorbed:
        ident = _interval_persist_ident(iv, linearization)
        bounds = _frontier_read_bounds(db, ident)
        # Nothing on disk to retract: an interval minted and merged away
        # within one run before its first claim ever persisted. Skipped
        # rather than treated as an error, matching _frontier_persist_claim.
        if bounds is None:
            continue
        _frontier_discard_interval(
            db, ident, bounds, index_con=index_con,
            pos_count=_frontier_read_pos_count(db, ident), tag=tag,
        )

    for iv in merged:
        if iv.tag != frontier_registry.TAG_PROVISIONAL:
            continue
        ident = _interval_persist_ident(iv, linearization)
        existing = _frontier_read_bounds(db, ident)
        # The survivor's entity is not on disk. Fail safe: the absorbed
        # facts are already gone, so the whole span reads unclaimed and is
        # re-walked, rather than being described by a half-written entity.
        if existing is None:
            continue
        new_lo_hash, new_hi_hash = linearization[iv.lo_pos], linearization[iv.hi_pos]
        if existing == (new_lo_hash, new_hi_hash):
            continue
        to_retract: List[str] = []
        to_transact: List[str] = []
        if existing[0] != new_lo_hash:
            to_retract.append(f'[{ident} :lo-hash "{_edn_escape(existing[0])}"]')
            to_transact.append(f'[{ident} :lo-hash "{_edn_escape(new_lo_hash)}"]')
        if existing[1] != new_hi_hash:
            to_retract.append(f'[{ident} :hi-hash "{_edn_escape(existing[1])}"]')
            to_transact.append(f'[{ident} :hi-hash "{_edn_escape(new_hi_hash)}"]')
        _frontier_pos_count_delta(
            db, ident, iv.hi_pos - iv.lo_pos + 1, to_retract, to_transact,
        )
        if to_retract:
            _retract(db, "[" + " ".join(to_retract) + "]", index_con=index_con)
        if to_transact:
            _transact(db, "[" + " ".join(to_transact) + "]", run_ts_iso, index_con=index_con)


def _frontier_coalesce_loaded(
    db: Any,
    linearization: List[str],
    intervals: List["frontier_registry.Interval"],
    run_ts_iso: str,
    index_con: Optional[Any] = None,
) -> bool:
    """#329: merge contiguous or overlapping LOADED provisional intervals,
    in memory and on disk. Returns whether the set is now guaranteed
    disjoint and non-adjacent (the post-condition's `strict`).

    frontier_registry._coalesce runs only from _extend, and _extend only
    from a claim. FrontierAllocator.__init__ stores what it is handed
    verbatim. So a load producing two adjacent entities WITH AN ALREADY-EMPTY
    GAP never merged: no claim ever happens, and _intervals_read_extra stays
    permanently non-empty -- which makes both
    _correction_sweep_select_position and _should_fold_lineage_watermark
    return early on every subsequent run. Stage B never runs again,
    :ingestion/lineage-confirmed-through never advances, and provisional
    :introduced-by stays provisional for the life of the graph, on runs
    reporting status: complete.

    MUST run after _frontier_promote_base_if_missing: coalesce_intervals'
    survivor rule PRESERVES a base but never manufactures one, so merging
    before the base is restored would leave the union at a minted ident
    while :ingestion/frontier-high stays absent -- the very state that
    function exists to repair.

    Only TAG_PROVISIONAL: _frontier_load appends at most one authoritative
    interval, so that side has no same-tag pair to merge, and the
    authoritative/provisional boundary must survive the two sides becoming
    adjacent.
    """
    provisional = [
        iv for iv in intervals if iv.tag == frontier_registry.TAG_PROVISIONAL
    ]
    if any(iv.ident is None for iv in provisional):
        # Unreachable: _load_one_interval and _frontier_promote_base_if_missing
        # both always set an ident on a loaded interval. Defensive only, and
        # the fail-safe direction is to leave the graph EXACTLY as found and
        # re-walk -- never to retract an entity this function cannot name.
        # The caller drops to strict=False so this does not become a raise.
        print(
            "[_frontier_load] a loaded provisional interval carries no ident; "
            "skipping the load-time coalesce (#329)",
            file=sys.stderr,
        )
        return False
    merged, absorbed = frontier_registry.coalesce_intervals(
        intervals, frontier_registry.TAG_PROVISIONAL
    )
    if absorbed:
        _frontier_persist_merge(
            db, linearization, merged, absorbed, run_ts_iso, index_con=index_con
        )
        intervals[:] = merged
    return True
```

Then change `_frontier_load`'s last two lines from:

```python
    _frontier_promote_base_if_missing(db, intervals, linearization, run_ts_iso, index_con=index_con)
    return frontier_registry.FrontierAllocator(len(linearization), intervals)
```

to:

```python
    _frontier_promote_base_if_missing(db, intervals, linearization, run_ts_iso, index_con=index_con)
    # #329: LAST, and after the base promotion -- see _frontier_coalesce_loaded.
    coalesced = _frontier_coalesce_loaded(
        db, linearization, intervals, run_ts_iso, index_con=index_con
    )
    _frontier_check_load_invariants(intervals, strict=coalesced)
    return frontier_registry.FrontierAllocator(len(linearization), intervals)
```

- [ ] **Step 4: Run the tests to verify they pass, then the whole frontier surface**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierLoadCoalescesProvisionalIntervals -v
.venv/bin/python -m pytest tests/test_mcp_server.py -k "Frontier or Interval or Sweep or Skip or Parity or Resume" -v
.venv/bin/python -m pytest tests/test_frontier_registry.py -v
```

Expected: all PASS. `TestMultiStreamParityWithForwardOnly`, `TestFrontierLoadRetainsAcrossTipGrowth`, `TestFrontierPromoteBaseIfMissing`, `TestPerIntervalReverseFloor` and `TestFoldedSkipSpanFlushDoesNotLaunderAFloor` are the load-path regression net and must all stay green.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Coalesce loaded provisional intervals in _frontier_load (#329)

Two live provisional entities that are contiguous or overlapping never
merged: _coalesce runs only from _extend, _extend only from a claim, and
with the gap already empty no claim ever happens. _intervals_read_extra was
then permanently non-empty, so Stage B declined on every subsequent run --
lineage stayed provisional for the life of the graph, on runs reporting
status: complete with a clean divergence and zero bytes on stderr.

The merge is mirrored to disk absorbed-first, so a crash between the two
writes leaves the span described by nobody and re-walked, rather than
leaving a phantom entity nothing can ever surface again.

Ablation: <paste the exact pre-fix failure text observed at Step 2>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

### Task 4: Documentation and full-suite verification

**Files:**
- Modify: `CLAUDE.md` (append a `#329` section after the `#325` section)
- Verify (expect no change): `SKILL.md`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: nothing code-facing.

- [ ] **Step 1: Confirm the tool surface is unchanged**

```bash
.venv/bin/python -m pytest tests/test_skill_doc.py tests/test_tool_schemas.py tests/test_packaging.py -v
git diff --stat master -- SKILL.md skill.json tools/
```

Expected: tests PASS and the diff is EMPTY. No new module was added, so `[tool.setuptools] py-modules` needs no entry — but `tests/test_packaging.py` is what proves that, so it must be run rather than reasoned about. If the diff is non-empty, stop: the spec asserts no tool-surface change and that assertion is then wrong.

- [ ] **Step 2: Add the CLAUDE.md section**

Insert as the LAST paragraphs of the `## Graph Storage` section — that is, immediately BEFORE the `## Claude Code Plugin Publishing` heading (`CLAUDE.md:942` at the time of writing; locate it by heading text, not line number). The `#325` block does not end where its "No `GRAPH_FORMAT_VERSION` bump, no migration" paragraph does — the resume-census-probe paragraph follows it and is still #325 — so appending after that paragraph would bury this section mid-#325.

```markdown
**A LOADED provisional set is now coalesced too, and #329 is why that is
not merely tidiness.** `frontier_registry._coalesce` runs only from
`_extend`, `_extend` only from a claim, and `FrontierAllocator.__init__`
stores what it is handed verbatim. So a load producing two contiguous or
overlapping provisional entities **with an already-empty gap** never merged:
no claim ever happens. `_intervals_read_extra` was then permanently
non-empty, and Task 6's gate makes both
`_correction_sweep_select_position` and `_should_fold_lineage_watermark`
return early on that condition — so Stage B never ran again,
`:ingestion/lineage-confirmed-through` never advanced, and provisional
`:introduced-by` stayed provisional for the life of the graph, on runs
reporting `status: complete` with a clean `divergence` and zero bytes on
stderr. **No detector saw it and it did not self-heal**, which is the
failure profile this arc exists to refuse: graph and index agree (nothing
is missing from either, the lineage is simply never upgraded), both
`:introduced-by` checks only examine entities that EXIST and are
well-formed, `stderr_capture` has nothing to read, and `commit_census`
compares commit counts that are correct.

The merge rule is now the module-level `frontier_registry.coalesce_
intervals`, called from BOTH `_coalesce` and `_frontier_load`'s
`_frontier_coalesce_loaded` — shared, not mirrored, so the load-time merge
cannot drift from the claim-time one. It runs AFTER
`_frontier_promote_base_if_missing`, and that order is load-bearing: the
survivor rule PRESERVES a base but never manufactures one, so merging first
would leave the union at a minted ident while `:ingestion/frontier-high`
stays absent — the very state that function exists to repair.

**The survivor's new `:pos-count` is the merged span, and that is a
CLAIM-TIME denominator, not #326's computed-where-it-is-read trap.** The
difference is which run does the comparing. Both components were validated
against THIS run's linearization moments earlier (`_load_one_interval`
retains only when the STORED claim-time count still equals the current
span), and their adjacency was established in that same linearization — so
the merged count is a fresh assertion about THIS run, compared in a LATER
run against a linearization that may differ. It discriminates. #326's
archive case was different: archiving and loading ran in the SAME run
against the SAME linearization, so the count always agreed. **Accepted
cost:** merging is coarser, so a later commit landing inside what used to be
the upper component now discards the whole union rather than that component
alone — a bigger re-walk, never a loss. The `:pos-count` residual is
unchanged: it stays a CHECKSUM, not a proof of set identity.

**The post-condition's two violations have deliberately different
consequences.** `_frontier_check_load_invariants` RAISES on an adjacent or
overlapping provisional pair — unreachable once the coalesce lands, so it
should never fire, and a raise reaches `_run_ingestion`'s run-level
`except` before any walk starts (`status: error`, traceback on fd 2, so
`stderr_capture`'s `error_signals` and `_exit_code` fail the at-scale
gate). It only WARNS when the base is not the lowest provisional interval:
coalescing does not enforce that — two DISJOINT intervals with a real gap
never merge, and `_intervals_read_extra` carries no positional predicate,
so a below-base extra would load. Nothing produces that state today and it
degrades conservatively, and raising on it would abort every future run on
such a graph forever with no repair path — permanent denial of service,
worse than the state being guarded. Cross-tag overlap is deliberately NOT
checked: it is unreachable (claims are served from `_unclaimed`, the
complement of the interval set), and cross-tag ADJACENCY is the normal
converged state, since the authoritative/provisional boundary is the
lineage frontier itself.

**No `GRAPH_FORMAT_VERSION` bump and no migration.** This changes no fact
shape — it retracts facts that already exist and widens bounds that already
exist, using the attributes `_frontier_persist_claim` already writes. This
is the one case in this arc that DOES self-heal an affected graph, because
the defective state is by definition present at load time and the fix runs
at load time.
```

- [ ] **Step 3: Run the full suite**

```bash
.venv/bin/python -m pytest tests/ -x -q
```

Expected: all PASS. Record the exact pass count in the commit message — do not write "all tests pass" without the number; a collected total is not a pass count.

- [ ] **Step 4: Check for closing keywords in BOTH channels**

```bash
git log master..HEAD --format=%B | grep -inE "clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)" || echo "none in commits"
```

The PR body is a SEPARATE channel that no commit grep sees, and `closingIssuesReferences` covers only body/title. Re-run this after every new commit. `Closes #329` belongs in the PR BODY, deliberately — not in a commit message.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "Document the load-time coalesce invariant (#329)

Records why a loaded provisional set needed its own merge, why the merged
:pos-count is a claim-time denominator rather than #326's
computed-where-it-is-read trap, and why the post-condition raises on
adjacency but only warns on base order.

Full suite: <N> passed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```
