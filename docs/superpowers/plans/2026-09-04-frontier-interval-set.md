# Frontier Interval Set (#222 phase 3 / #325) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the provisional side of the ingestion frontier as a set of disjoint intervals, so a branch tip that grows under a running or finished ingestion no longer discards the reverse frontier.

**Architecture:** `FrontierAllocator` stops modelling "two anchored intervals and one shared gap" and models "sorted disjoint intervals; the gap is their complement". `:ingestion/frontier-high` keeps its fixed ident and means *the lowest provisional interval* (the "base"); additional provisional intervals above it persist as their own ident-keyed `:type/ingest-interval` entities. `_frontier_load` then retains an interval whenever its bounds resolve and its stored `:pos-count` still matches its span, instead of only when it reaches the last position.

**Tech Stack:** Python 3.12, `minigraf>=2.0.0,<3.0.0` (UniFFI), pytest + pytest-asyncio, SQLite FTS5 fact index, git via `subprocess`.

**Spec:** `docs/superpowers/specs/2026-09-04-frontier-interval-set-design.md`

## Global Constraints

- **Interpreter is `.venv/bin/python`, always.** System python has minigraf 1.1.1 against a `>=2.0.0` floor and fakes ~122 test failures. Every command in this plan uses `.venv/bin/python -m pytest`.
- **Real backend only.** No `MagicMock` fake of `MiniGrafDb`. Use the `real_db` fixture (in-memory) or a real file-backed `MiniGrafDb.open()` against `tmp_path` for anything that must survive an open/close cycle. See `docs/testing-conventions.md`.
- **Never assert on mock call arguments.** Re-query the DB and assert on persisted facts.
- **Single-handle invariant.** At most one live `MiniGrafDb` per process. Release with `mcp_server._reset_db_state()`, never `mcp_server._db = None` (that global no longer exists).
- **Never batch `:contains` / `:depends-on` / `:parent`** into one transact (project-minigraf/minigraf#287, still open, version-invariant). Not directly touched here, but do not "simplify" any loop you pass.
- **`:type/ingest-interval` stays ABSENT from `MINIGRAF_SCHEMA`.** `handle_minigraf_audit` iterates exactly the registered types and would retract its attributes. All writes go through the internal `_transact` / `_retract`.
- **Interval entity idents are minted once at creation and never re-derived from current bounds** (#326: a bounds-keyed dedup collided two regions onto one entity, made `:pos-count` nondeterministic and made a retract destroy the surviving witness).
- **`:pos-count` originates at CLAIM time, never where an interval is read or archived.** A count computed from the very span it is later compared against always agrees and discriminates nothing.
- **No `GRAPH_FORMAT_VERSION` bump, no migration.** This only adds facts going forward.
- **Every regression test must be watched to FAIL before it is believed**, with the ablation being the real old code.
- Branch: `325-frontier-interval-set`, already created, spec already committed on it.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `frontier_registry.py` | Pure position-space interval algebra and gap allocation. No DB, no hashes. | Modify: `Interval` gains `anchor_pos` and `is_base`; allocator becomes complement-based; records the last claim's outcome. |
| `mcp_server.py` | Everything hash- and graph-facing: persistence, load, the walk, Stage B. | Modify: interval entity helpers, `_frontier_load`, `_frontier_persist_claim`, `_frontier_persist_span`, `_reverse_apply`, `_run_ingestion`, `_correction_sweep_select_position`, `_should_fold_lineage_watermark`. |
| `tests/test_frontier_registry.py` | Allocator unit tests (pure, no DB). | Modify: add fragmented-set cases. |
| `tests/test_mcp_server.py` | Everything else, real backend. | Modify: add persistence, load, floor, sweep and end-to-end classes. |
| `evals/at_scale/probe_resume_census.py` | The three-way census on a RESUMED graph — the scenario the nightly benchmark's fresh-graph run cannot reach. | Create. Reuses `commit_census.collect_commit_census` verbatim. |
| `tests/test_at_scale_resume_census.py` | Probe tests, following `tests/test_at_scale_ident_collision_new_history.py`. | Create. |
| `.github/workflows/at-scale-benchmark-nightly.yml` | Nightly step wiring. | Modify: new step beside the collision census at `:161`. |
| `evals/at_scale/benchmark.md` | Measured baselines and what a red run means. | Modify: new section. |
| `CLAUDE.md` | Agent-facing memory of the standing decisions. | Modify: new section. |

---

## Task 1: Complement-based allocator

**Files:**
- Modify: `frontier_registry.py:19-22` (`Interval`), `:39-146` (`FrontierAllocator`)
- Test: `tests/test_frontier_registry.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `Interval(lo_pos: int, hi_pos: int, tag: str, anchor_pos: Optional[int] = None, is_base: bool = False)`
  - `ClaimResult(pos: int, interval: Interval, absorbed: List[Interval])`
  - `FrontierAllocator.claim_low() -> Optional[int]` and `.claim_high() -> Optional[int]` — return types UNCHANGED
  - `FrontierAllocator.last_claim: Optional[ClaimResult]` — set by the claim that just returned
  - `FrontierAllocator.gap_lo`, `.gap_hi`, `.is_gap_empty()`, `.intervals()` — names unchanged

**Design notes for the implementer:**

`anchor_pos` is the position the interval was created at. It is an opaque identity token: the allocator carries it through extends and merges but never interprets it. `mcp_server.py` mints the persisted entity's ident from `linearization[anchor_pos]`.

`is_base` marks the ONE provisional interval that persists at the fixed `:ingestion/frontier-high` ident. On a merge the survivor is base if either participant was base; otherwise the survivor is the LOWER interval. A non-base interval can never end up below the base: `claim_high()` returns the highest unclaimed position, and everything below the base is the bulk gap, which is only reached after the holes above have closed and merged.

`gap_lo`/`gap_hi` today are defined off "the interval covering position 0 / the last position", which stops describing a coherent gap the moment the provisional side is fragmented — with `high=[lo,oldtip]` and nothing covering the tip, `gap_lo` returns a position `frontier-high` already owns. They become the lowest/highest unclaimed position.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_frontier_registry.py`:

```python
class TestFragmentedProvisionalSet:
    """#325: after tip growth the provisional side holds two disjoint
    intervals with a hole above the lower one. The old gap_lo/gap_hi, defined
    off 'the interval covering position 0 / the last position', hand out a
    position an interval already owns."""

    def _alloc(self):
        # 20 positions. Authoritative [0,3]; provisional base [4,11].
        # Positions 12..19 are the "new tip" hole.
        return frontier_registry.FrontierAllocator(20, [
            frontier_registry.Interval(0, 3, frontier_registry.TAG_AUTHORITATIVE,
                                       anchor_pos=0, is_base=True),
            frontier_registry.Interval(4, 11, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=11, is_base=True),
        ])

    def test_gap_lo_does_not_return_a_claimed_position(self):
        a = self._alloc()
        assert a.gap_lo == 12

    def test_gap_hi_is_the_topmost_unclaimed_position(self):
        a = self._alloc()
        assert a.gap_hi == 19

    def test_claim_high_serves_the_topmost_gap_first(self):
        a = self._alloc()
        assert [a.claim_high() for _ in range(3)] == [19, 18, 17]

    def test_claim_high_falls_through_to_the_bulk_gap_when_the_tip_closes(self):
        # A real bulk gap below the base: authoritative [0,1], base [8,11],
        # so positions 2..7 are unclaimed underneath and 12..19 above.
        b = frontier_registry.FrontierAllocator(20, [
            frontier_registry.Interval(0, 1, frontier_registry.TAG_AUTHORITATIVE,
                                       anchor_pos=0, is_base=True),
            frontier_registry.Interval(8, 11, frontier_registry.TAG_PROVISIONAL,
                                       anchor_pos=11, is_base=True),
        ])
        for _ in range(8):           # 19..12, closing the tip hole
            b.claim_high()
        assert b.claim_high() == 7, (
            "once the tip hole merges into the base, the topmost unclaimed "
            "position is the bulk gap's top"
        )

    def test_merge_keeps_the_base_and_reports_the_absorbed_interval(self):
        a = self._alloc()
        for _ in range(7):           # 19..13
            a.claim_high()
        result_before = a.last_claim
        assert result_before.absorbed == []
        assert a.claim_high() == 12  # this claim makes [12,19] touch [4,11]
        merged = a.last_claim
        assert merged.interval.lo_pos == 4 and merged.interval.hi_pos == 19
        assert merged.interval.is_base is True
        assert merged.interval.anchor_pos == 11
        assert [iv.anchor_pos for iv in merged.absorbed] == [19]

    def test_claim_low_still_ascends_across_a_fragmented_high_side(self):
        a = self._alloc()
        assert [a.claim_low() for _ in range(3)] == [12, 13, 14]

    def test_is_gap_empty_requires_every_hole_closed(self):
        a = self._alloc()
        assert a.is_gap_empty() is False
        for _ in range(8):
            a.claim_high()
        assert a.is_gap_empty() is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_frontier_registry.py::TestFragmentedProvisionalSet -v
```

Expected: FAIL. `test_gap_lo_does_not_return_a_claimed_position` asserts 12 and the current code returns 4; `Interval()` rejects `anchor_pos`; `last_claim` does not exist.

- [ ] **Step 3: Implement**

In `frontier_registry.py`:

```python
@dataclass
class Interval:
    lo_pos: int
    hi_pos: int
    tag: str
    anchor_pos: Optional[int] = None
    is_base: bool = False


@dataclass
class ClaimResult:
    """What a claim did, for the persistence layer to mirror without
    re-reading the persisted interval set once per commit.

    `interval` is the post-coalesce interval the position now belongs to;
    `absorbed` are the intervals that coalesce merged away, whose persisted
    entities must be retracted in the same write.
    """
    pos: int
    interval: "Interval"
    absorbed: List["Interval"]
```

Replace the gap accessors and `_extend`:

```python
    def _unclaimed(self) -> List[Tuple[int, int]]:
        """Maximal runs of unclaimed positions, ascending. The gap is the
        COMPLEMENT of the interval set, not the space between two anchored
        intervals -- a fragmented provisional side has more than one hole,
        and the old 'interval covering position 0 / the last position'
        definition hands out positions an interval already owns."""
        holes: List[Tuple[int, int]] = []
        cursor = 0
        for iv in sorted(self._intervals, key=lambda i: i.lo_pos):
            if iv.lo_pos > cursor:
                holes.append((cursor, iv.lo_pos - 1))
            cursor = max(cursor, iv.hi_pos + 1)
        if cursor <= self.total_positions - 1:
            holes.append((cursor, self.total_positions - 1))
        return holes

    @property
    def gap_lo(self) -> int:
        holes = self._unclaimed()
        return holes[0][0] if holes else self.total_positions

    @property
    def gap_hi(self) -> int:
        holes = self._unclaimed()
        return holes[-1][1] if holes else -1

    def is_gap_empty(self) -> bool:
        return not self._unclaimed()
```

`claim_low`/`claim_high` keep their signatures and set `self.last_claim`:

```python
    def claim_low(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_lo
        self._extend(pos, tag=TAG_AUTHORITATIVE, from_low=True)
        return pos

    def claim_high(self) -> Optional[int]:
        if self.is_gap_empty():
            return None
        pos = self.gap_hi
        self._extend(pos, tag=TAG_PROVISIONAL, from_low=False)
        return pos
```

`__init__` gains `self.last_claim: Optional[ClaimResult] = None`.

`_extend` records the outcome and marks a first-ever provisional interval as base:

```python
    def _extend(self, pos: int, tag: str, from_low: bool) -> None:
        target = self._adjacent_interval(pos, tag, from_low)
        if target is not None:
            idx = next(i for i, iv in enumerate(self._intervals) if iv is target)
            if from_low:
                grown = Interval(target.lo_pos, pos, tag, target.anchor_pos, target.is_base)
            else:
                grown = Interval(pos, target.hi_pos, tag, target.anchor_pos, target.is_base)
            self._intervals[idx] = grown
        else:
            # A brand-new interval. It is the base only if no same-tag
            # interval exists yet -- claim_high() serves the topmost hole, so
            # every later provisional interval is created ABOVE the base.
            is_base = not any(iv.tag == tag for iv in self._intervals)
            grown = Interval(pos, pos, tag, anchor_pos=pos, is_base=is_base)
            self._intervals.append(grown)
        absorbed = self._coalesce(tag, grown)
        surviving = next(
            iv for iv in self._intervals
            if iv.tag == tag and iv.lo_pos <= pos <= iv.hi_pos
        )
        self.last_claim = ClaimResult(pos=pos, interval=surviving, absorbed=absorbed)
```

`_coalesce` returns what it merged away and applies the survivor rule:

```python
    def _coalesce(self, tag: str, grown: "Interval") -> List["Interval"]:
        """Merge same-tag intervals that overlap or touch, keeping intervals
        disjoint and sorted. Returns the intervals that were merged AWAY, so
        the persistence layer can retract their entities.

        Survivor rule: the base wins if either participant is base;
        otherwise the LOWER one wins and keeps its anchor_pos. The base is
        what persists at the fixed :ingestion/frontier-high ident, so it must
        survive every merge it takes part in or that ident would have to be
        re-pointed at a different entity.

        Only same-tag intervals merge -- the authoritative/provisional
        boundary is the lineage frontier later phases read, and must survive
        the two sides becoming adjacent.
        """
        same = sorted((iv for iv in self._intervals if iv.tag == tag), key=lambda iv: iv.lo_pos)
        merged: List[Interval] = []
        absorbed: List[Interval] = []
        for iv in same:
            if merged and iv.lo_pos <= merged[-1].hi_pos + 1:
                prev = merged[-1]
                keeper, loser = (prev, iv) if (prev.is_base or not iv.is_base) else (iv, prev)
                absorbed.append(loser)
                merged[-1] = Interval(
                    prev.lo_pos, max(prev.hi_pos, iv.hi_pos), tag,
                    keeper.anchor_pos, prev.is_base or iv.is_base,
                )
            else:
                merged.append(iv)
        others = [iv for iv in self._intervals if iv.tag != tag]
        self._intervals = sorted(others + merged, key=lambda iv: iv.lo_pos)
        return absorbed
```

Note `_adjacent_interval` is UNCHANGED — its existing docstring already describes exactly this two-provisional-interval state and explains why "first interval covering the neighbour" is the wrong pick.

- [ ] **Step 4: Run the new tests and the whole existing allocator suite**

```bash
.venv/bin/python -m pytest tests/test_frontier_registry.py -v
```

Expected: PASS, including every pre-existing test. If a pre-existing test fails, the complement definition disagrees with the two-anchored-interval definition somewhere that matters — do not "fix" the old test; work out which behaviour is right and say so.

- [ ] **Step 5: Run the mcp_server tests that drive the allocator**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "Frontier or Claimer or SkipClaim or SkipFastPath" -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontier_registry.py tests/test_frontier_registry.py
git commit -m "Model the frontier gap as the interval set's complement (#325)

gap_lo/gap_hi were defined off 'the interval covering position 0 / the
last position', which hands out a position an interval already owns as
soon as the provisional side is fragmented. Intervals gain an opaque
anchor_pos identity token and an is_base flag, and claims report what
they merged so persistence can mirror it without a per-commit query.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 2: Interval entity persistence primitives

**Files:**
- Modify: `mcp_server.py` — add beside `_frontier_read_bounds` (`:5955`) and the `_completed_region_*` block (`:6290-6470`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: Task 1's `Interval.anchor_pos`.
- Produces:
  - `_INTERVAL_PROVISIONAL_IDENT_PREFIX = ":ingestion/interval-provisional-"`
  - `_interval_ident(anchor_hash: str) -> str`
  - `_intervals_read_extra(db) -> List[Tuple[str, str, str, Optional[int]]]` — `(ident, lo_hash, hi_hash, pos_count)` for every non-`frontier-high` provisional interval entity, sorted by ident
  - `_interval_discard(db, ident, bounds, index_con=None, pos_count=None) -> None`

**Design notes:** `_frontier_discard_interval` (`mcp_server.py:6262`) already does exactly the retract job for the two fixed idents. Generalize it rather than writing a second one: it currently derives `tag` from `ident == _FRONTIER_LOW_IDENT`, which is wrong for a minted provisional ident. Give it an explicit `tag` parameter defaulting to `None` (meaning "derive as today"), and add the `:ident` fact to the retract set when the ident is a minted one.

Extra intervals carry a string-valued `:ident` fact, exactly like completed regions, because `[?e :entity-type :type/ingest-interval]` answers in UUID space. `frontier-high` and `frontier-low` are still read by fixed ident and carry no `:ident` fact — that is what makes this migration-free.

- [ ] **Step 1: Write the failing test**

```python
class TestIntervalEntityPersistence:
    """#325: provisional intervals above frontier-high persist as their own
    ident-keyed entities. frontier-high and frontier-low keep their fixed
    idents and carry no :ident fact, so an existing graph needs no migration."""

    def test_ident_is_minted_from_the_anchor_hash(self):
        import mcp_server
        assert mcp_server._interval_ident("abcdef0123456789") == \
            ":ingestion/interval-provisional-abcdef012345"

    def test_extra_intervals_are_enumerable_and_frontier_high_is_not(self, real_db):
        import mcp_server
        ts = "2026-09-04T00:00:00Z"
        mcp_server._frontier_persist_claim(real_db, ["h0", "h1", "h2"], 2, False, ts)
        ident = mcp_server._interval_ident("h9")
        mcp_server._transact(real_db, "[" + " ".join([
            f"[{ident} :entity-type :type/ingest-interval]",
            f'[{ident} :ident "{ident}"]',
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "h8"]',
            f'[{ident} :hi-hash "h9"]',
            f"[{ident} :pos-count 2]",
        ]) + "]", ts)

        extras = mcp_server._intervals_read_extra(real_db)
        assert extras == [(ident, "h8", "h9", 2)], (
            "enumeration must return the minted-ident interval and must NOT "
            "return frontier-high, which is read by fixed ident"
        )

    def test_an_extra_interval_with_no_pos_count_is_still_enumerable(self, real_db):
        import mcp_server
        ts = "2026-09-04T00:00:00Z"
        ident = mcp_server._interval_ident("h9")
        mcp_server._transact(real_db, "[" + " ".join([
            f"[{ident} :entity-type :type/ingest-interval]",
            f'[{ident} :ident "{ident}"]',
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "h8"]',
            f'[{ident} :hi-hash "h9"]',
        ]) + "]", ts)
        assert mcp_server._intervals_read_extra(real_db) == [(ident, "h8", "h9", None)], (
            "a countless interval is untrustworthy but must stay enumerable, "
            "or its facts leak forever -- the same rule "
            "_completed_regions_read_full follows"
        )

    def test_discard_removes_every_fact_including_the_ident(self, real_db):
        import mcp_server
        ts = "2026-09-04T00:00:00Z"
        ident = mcp_server._interval_ident("h9")
        mcp_server._transact(real_db, "[" + " ".join([
            f"[{ident} :entity-type :type/ingest-interval]",
            f'[{ident} :ident "{ident}"]',
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "h8"]',
            f'[{ident} :hi-hash "h9"]',
            f"[{ident} :pos-count 2]",
        ]) + "]", ts)
        mcp_server._frontier_discard_interval(
            real_db, ident, ("h8", "h9"), pos_count=2, tag=":provisional",
        )
        assert mcp_server._intervals_read_extra(real_db) == []
        assert mcp_server._frontier_read_bounds(real_db, ident) is None
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIntervalEntityPersistence -v
```

Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_interval_ident'`.

- [ ] **Step 3: Implement**

Add beside `_frontier_read_pos_count`:

```python
_INTERVAL_PROVISIONAL_IDENT_PREFIX = ":ingestion/interval-provisional-"


def _interval_ident(anchor_hash: str) -> str:
    """Deterministic ident for a provisional interval created at anchor_hash.

    MINTED ONCE, at creation, and never re-derived from current bounds. A
    provisional interval grows DOWNWARD, so keying on :lo-hash would recreate
    the entity on every claim; keying on the CURRENT :hi-hash would rename it
    on every merge. #326 paid for the bounds-keyed version once: two regions
    collided onto one entity, :pos-count became nondeterministic through a
    last-write-wins join, and a retract destroyed the surviving witness.
    """
    return f"{_INTERVAL_PROVISIONAL_IDENT_PREFIX}{anchor_hash[:12]}"


def _intervals_read_extra(db: Any) -> List[Tuple[str, str, str, Optional[int]]]:
    """Every provisional interval entity ABOVE frontier-high, as
    (ident, lo_hash, hi_hash, pos_count), sorted by ident.

    Binds ?ident rather than ?e: `[?e :entity-type :type/ingest-interval]`
    answers in UUID space. frontier-high and frontier-low carry no :ident fact
    and are therefore invisible here BY CONSTRUCTION -- they are read by their
    fixed idents, which is what makes this change migration-free.

    :pos-count is a SECOND query, not a join. An interval carrying no count is
    untrustworthy but must still be enumerable -- it has to be retractable --
    and a single wide join would make it invisible instead, leaking its facts
    forever. Same rule as _completed_regions_read_full.
    """
    raw = _db_execute(
        db,
        "(query [:find ?ident ?lo ?hi :where"
        " [?e :entity-type :type/ingest-interval]"
        " [?e :ident ?ident] [?e :lo-hash ?lo] [?e :hi-hash ?hi]])",
    )
    raw_counts = _db_execute(
        db,
        "(query [:find ?ident ?c :where"
        " [?e :entity-type :type/ingest-interval]"
        " [?e :ident ?ident] [?e :pos-count ?c]])",
    )
    counts: Dict[str, Optional[int]] = {}
    for ident, c in json.loads(raw_counts).get("results", []):
        try:
            counts[str(ident)] = int(c)
        except (TypeError, ValueError):
            counts[str(ident)] = None
    seen = set()
    out: List[Tuple[str, str, str, Optional[int]]] = []
    for ident, lo, hi in json.loads(raw).get("results", []):
        if str(ident) in seen:
            continue
        seen.add(str(ident))
        out.append((str(ident), lo, hi, counts.get(str(ident))))
    return sorted(out, key=lambda r: r[0])
```

Generalize `_frontier_discard_interval` (`mcp_server.py:6262`): add `tag: Optional[str] = None` after `pos_count`, replace the derivation line with

```python
    if tag is None:
        tag = ":authoritative" if ident == _FRONTIER_LOW_IDENT else ":provisional"
```

and append the `:ident` retract when the ident is a minted one:

```python
    if ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
        facts.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
```

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestIntervalEntityPersistence -v
.venv/bin/python -m pytest tests/test_mcp_server.py -k "Frontier" -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Add ident-keyed provisional interval entities (#325)

Extra provisional intervals above frontier-high persist as their own
entities with a string-valued :ident for enumeration, following the
completed-region conventions. frontier-high and frontier-low keep their
fixed idents and carry no :ident fact, so no graph needs migrating.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 3: Route claims and spans to the right interval entity

**Files:**
- Modify: `mcp_server.py:6702` (`_frontier_persist_claim`), `:6783` (`_frontier_persist_span`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_interval_ident`, `_intervals_read_extra`, `_frontier_discard_interval(..., tag=...)` (Task 2).
- Produces:
  - `_frontier_persist_claim(db, linearization, pos, from_low, commit_ts_iso, index_con=None, ident: Optional[str] = None, absorbed_idents: Optional[List[str]] = None)`
  - `_frontier_persist_span(db, linearization, lo_pos, hi_pos, from_low, commit_ts_iso, index_con=None, ident: Optional[str] = None)`
  - Both default `ident=None` to today's `_FRONTIER_LOW_IDENT`/`_FRONTIER_HIGH_IDENT` choice, so every existing call site and test keeps working unchanged.

**Design notes:** `absorbed_idents` are retracted in the same call that extends the survivor. Retract BEFORE the survivor's write, so a crash between them leaves a duplicate region rather than a hole — a duplicate is re-walked, a hole is silent permanent loss.

- [ ] **Step 1: Write the failing test**

```python
class TestFrontierPersistClaimTargetsAnInterval:
    """#325: a claim extends the interval entity it belongs to, and a merge
    retracts the absorbed entity in the same call."""

    def test_claim_extends_a_minted_ident_when_given_one(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(10)]
        ident = mcp_server._interval_ident("h9")
        mcp_server._frontier_persist_claim(
            real_db, lin, 9, False, "2026-09-04T00:00:00Z", ident=ident,
        )
        mcp_server._frontier_persist_claim(
            real_db, lin, 8, False, "2026-09-04T00:00:01Z", ident=ident,
        )
        assert mcp_server._frontier_read_bounds(real_db, ident) == ("h8", "h9")
        assert mcp_server._frontier_read_pos_count(real_db, ident) == 2
        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) is None, (
            "the fixed high ident must be untouched when a claim names another"
        )

    def test_absorbed_idents_are_retracted_by_the_merging_claim(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(10)]
        upper = mcp_server._interval_ident("h9")
        for p, t in ((9, "00"), (8, "01")):
            mcp_server._frontier_persist_claim(
                real_db, lin, p, False, f"2026-09-04T00:00:{t}Z", ident=upper)
        for p, t in ((5, "02"), (4, "03")):
            mcp_server._frontier_persist_claim(
                real_db, lin, p, False, f"2026-09-04T00:00:{t}Z")
        # the claim at 6 makes [6,?] touch nothing yet; the claim at 7 merges.
        mcp_server._frontier_persist_claim(
            real_db, lin, 7, False, "2026-09-04T00:00:04Z",
            ident=mcp_server._FRONTIER_HIGH_IDENT, absorbed_idents=[upper],
        )
        assert mcp_server._intervals_read_extra(real_db) == [], (
            "the absorbed entity's facts must be gone, not merely unreferenced"
        )

    def test_span_flush_targets_a_named_ident(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(10)]
        ident = mcp_server._interval_ident("h9")
        mcp_server._frontier_persist_span(
            real_db, lin, 6, 9, False, "2026-09-04T00:00:00Z", ident=ident)
        assert mcp_server._frontier_read_bounds(real_db, ident) == ("h6", "h9")
        assert mcp_server._frontier_read_pos_count(real_db, ident) == 4
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestFrontierPersistClaimTargetsAnInterval -v
```

Expected: FAIL with `TypeError: _frontier_persist_claim() got an unexpected keyword argument 'ident'`.

- [ ] **Step 3: Implement**

In `_frontier_persist_claim`, replace the ident line and add the creation/absorb handling:

```python
    if ident is None:
        ident = _FRONTIER_LOW_IDENT if from_low else _FRONTIER_HIGH_IDENT
    tag = ":authoritative" if from_low else ":provisional"

    # Retract the absorbed entities BEFORE extending the survivor. A crash
    # between the two then leaves a DUPLICATE description of a region, which
    # the next _frontier_load re-walks; the other order leaves a HOLE, which
    # is silent permanent loss.
    for absorbed in absorbed_idents or []:
        absorbed_bounds = _frontier_read_bounds(db, absorbed)
        if absorbed_bounds is not None:
            _frontier_discard_interval(
                db, absorbed, absorbed_bounds, index_con=index_con,
                pos_count=_frontier_read_pos_count(db, absorbed), tag=tag,
            )
```

and in the `existing is None` branch, add the `:ident` fact for a minted ident:

```python
        if ident.startswith(_INTERVAL_PROVISIONAL_IDENT_PREFIX):
            to_transact.append(f'[{ident} :ident "{_edn_escape(ident)}"]')
```

Make the same two changes in `_frontier_persist_span` (the `ident` default and the `:ident` fact in its `existing is None` branch). `_frontier_persist_span`'s advance-only semantics are unchanged.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "FrontierPersist" -v
```

Expected: PASS, including the pre-existing `TestFrontierPersistClaim` and `TestFrontierPersistSpan`.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Route frontier claims and span flushes to a named interval (#325)

Both persist paths take an optional ident, defaulting to today's fixed
low/high choice so every existing call site is unchanged. A merging
claim retracts the absorbed entity first, so a crash mid-merge leaves a
re-walkable duplicate rather than a silent hole.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 4: `_frontier_load` retains instead of discarding

**Files:**
- Modify: `mcp_server.py:6150-6260` (`_frontier_load`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_intervals_read_extra`, `_interval_ident`, `_frontier_discard_interval(..., tag=...)` (Task 2).
- Produces: `_frontier_load` returns an allocator whose provisional side may hold several intervals, each with `anchor_pos` set and exactly one `is_base=True`.

**Design notes — the three rule changes:**

1. **Retention test.** Today: `hi_lo_pos <= hi_hi_pos and hi_hi_pos == len(linearization) - 1`. Now: `hi_lo_pos <= hi_hi_pos and stored_pos_count == hi_hi_pos - hi_lo_pos + 1`. Dropping `hi_hi_pos == last` is the feature; adding the count check is MANDATORY, not defensive. Today's retain path performs no count check and is safe only by accident — a genuinely new commit implies a new tip, so a commit landing strictly inside the bounds forces `hi != last` and pushes the case onto the discard path where #326's count check lives. Retaining `hi < last` removes that accident.
2. **`anchor_pos` on load.** For `frontier-high`, `anchor_pos = hi_hi_pos` and `is_base=True`. For an extra interval, `anchor_pos` is the position of the hash its ident was minted from, recovered as `hash_to_pos[ident_suffix_match]` — do NOT re-derive it from the current `hi`. If the anchor hash is not in the linearization, the ident is still the entity's identity; keep `anchor_pos = hi_pos` for gap math and never re-mint.
3. **The divergent-ref leak.** Today, when a bound hash is absent from the linearization the outer `if` fails: no interval loads AND the facts stay, so the next `_frontier_persist_claim` reads a non-`None` `existing` and extends bounds nobody believes in. Give that case an explicit branch: archive it as a completed region when it is otherwise well-formed (both bounds present in the FACTS, `pos_count` present), then retract.

- [ ] **Step 1: Write the failing test**

```python
class TestFrontierLoadRetainsAcrossTipGrowth:
    """#325: a high interval that no longer reaches the last position is
    RETAINED, not discarded, provided its stored :pos-count still matches its
    span in this linearization."""

    def _seed_high(self, db, lin, lo, hi, count):
        import mcp_server
        ident = mcp_server._FRONTIER_HIGH_IDENT
        facts = [
            f"[{ident} :entity-type :type/ingest-interval]",
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "{lin[lo]}"]',
            f'[{ident} :hi-hash "{lin[hi]}"]',
        ]
        if count is not None:
            facts.append(f"[{ident} :pos-count {count}]")
        mcp_server._transact(db, "[" + " ".join(facts) + "]", "2026-09-04T00:00:00Z")

    def test_grown_tip_retains_the_interval_and_leaves_a_hole_above(self, real_db):
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(20)]
        self._seed_high(real_db, lin, 4, 11, 8)
        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-04T00:00:01Z")
        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert [(iv.lo_pos, iv.hi_pos) for iv in prov] == [(4, 11)]
        assert prov[0].is_base is True
        assert alloc.gap_hi == 19, "the new tip must be unclaimed"
        assert mcp_server._completed_regions_read(real_db) == [], (
            "a retained interval must not also be archived -- two descriptions "
            "of one region is how #326's bounds-keyed dedup went wrong"
        )

    def test_a_countless_interval_is_still_discarded(self, real_db):
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(20)]
        self._seed_high(real_db, lin, 4, 11, None)
        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-04T00:00:01Z")
        assert [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL] == []
        assert mcp_server._frontier_read_bounds(
            real_db, mcp_server._FRONTIER_HIGH_IDENT) is None

    def test_a_stale_count_is_discarded_not_retained(self, real_db):
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(20)]
        self._seed_high(real_db, lin, 4, 11, 7)   # span is 8, count says 7
        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-04T00:00:01Z")
        assert [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL] == []

    def test_extra_intervals_load_alongside_the_base(self, real_db):
        import mcp_server, frontier_registry
        lin = [f"h{i}" for i in range(20)]
        self._seed_high(real_db, lin, 4, 11, 8)
        ident = mcp_server._interval_ident(lin[19])
        mcp_server._transact(real_db, "[" + " ".join([
            f"[{ident} :entity-type :type/ingest-interval]",
            f'[{ident} :ident "{ident}"]',
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "{lin[16]}"]',
            f'[{ident} :hi-hash "{lin[19]}"]',
            f"[{ident} :pos-count 4]",
        ]) + "]", "2026-09-04T00:00:00Z")
        alloc = mcp_server._frontier_load(real_db, lin, "2026-09-04T00:00:01Z")
        prov = sorted(
            (iv for iv in alloc.intervals()
             if iv.tag == frontier_registry.TAG_PROVISIONAL),
            key=lambda iv: iv.lo_pos)
        assert [(iv.lo_pos, iv.hi_pos, iv.is_base) for iv in prov] == \
            [(4, 11, True), (16, 19, False)]
        assert alloc.gap_hi == 15, "the hole between the two is what's unclaimed"


class TestFrontierLoadRetractsUnresolvableBounds:
    """#325: a bound hash absent from the linearization used to leave the facts
    in the graph while loading no interval -- so the next _frontier_persist_claim
    read a non-None `existing` and extended bounds the allocator did not
    believe in."""

    def test_unresolvable_bounds_are_retracted(self, real_db):
        import mcp_server
        ident = mcp_server._FRONTIER_HIGH_IDENT
        mcp_server._transact(real_db, "[" + " ".join([
            f"[{ident} :entity-type :type/ingest-interval]",
            f"[{ident} :tag :provisional]",
            f'[{ident} :lo-hash "gone-a"]',
            f'[{ident} :hi-hash "gone-b"]',
            f"[{ident} :pos-count 2]",
        ]) + "]", "2026-09-04T00:00:00Z")
        lin = [f"h{i}" for i in range(20)]
        mcp_server._frontier_load(real_db, lin, "2026-09-04T00:00:01Z")
        assert mcp_server._frontier_read_bounds(real_db, ident) is None, (
            "left behind, these bounds are extended by the next claim"
        )
        assert mcp_server._completed_regions_read(real_db) == \
            [("gone-a", "gone-b", ":provisional")], (
            "archive before retracting -- the branch may straighten out"
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "FrontierLoadRetains or FrontierLoadRetracts" -v
```

Expected: FAIL — `test_grown_tip_retains_the_interval_and_leaves_a_hole_above` finds no provisional interval (current code discards), and `test_unresolvable_bounds_are_retracted` finds the bounds still present.

- [ ] **Step 3: Implement**

Restructure the `high_bounds` block of `_frontier_load`. Replace the outer `if high_bounds is not None and both in hash_to_pos:` with:

```python
    high_bounds = _frontier_read_bounds(db, _FRONTIER_HIGH_IDENT)
    if high_bounds is not None:
        high_count = _frontier_read_pos_count(db, _FRONTIER_HIGH_IDENT)
        _load_one_interval(
            db, _FRONTIER_HIGH_IDENT, high_bounds, high_count, hash_to_pos,
            linearization, run_ts_iso, intervals, is_base=True, index_con=index_con,
        )
    for ident, lo_hash, hi_hash, count in _intervals_read_extra(db):
        _load_one_interval(
            db, ident, (lo_hash, hi_hash), count, hash_to_pos,
            linearization, run_ts_iso, intervals, is_base=False, index_con=index_con,
        )
```

and add the helper next to `_frontier_load`:

```python
def _load_one_interval(
    db, ident, bounds, pos_count, hash_to_pos, linearization, run_ts_iso,
    intervals, is_base, index_con=None,
) -> None:
    """Retain, or archive-and-retract, one persisted provisional interval.

    RETAINED iff both bounds resolve in this linearization, lo <= hi, and the
    STORED :pos-count still equals the current span. Dropping the old
    `hi == last` test is #325's whole point; adding the count check is
    mandatory, not defensive. The old retain path performed no count check and
    was safe only by accident: a genuinely new commit implies a new tip, so a
    commit landing strictly INSIDE the bounds forced hi != last and pushed the
    case onto the discard path where the count check lives. Retaining
    hi < last removes that accident, and an insertion inside a retained
    interval is silent permanent loss -- the commit reaches neither the graph
    nor the index, so fact_audit's two witnesses agree, both :introduced-by
    checks only examine entities that exist, and stderr carries nothing.

    An interval carrying NO count is not retained. "No denominator" and "a
    denominator that still checks out" must not be the same branch when the
    failure mode is silent permanent loss.

    Anything not retained is ARCHIVED as a :type/completed-region first, then
    retracted. Retracting without archiving was the old behaviour for
    unresolvable bounds only in the sense that it did NEITHER: the facts stayed
    in the graph while no interval loaded, so the next _frontier_persist_claim
    read a non-None `existing` and extended bounds the allocator did not
    believe in.
    """
    lo_hash, hi_hash = bounds
    lo_pos = hash_to_pos.get(lo_hash)
    hi_pos = hash_to_pos.get(hi_hash)
    tag = ":provisional"
    if (
        lo_pos is not None and hi_pos is not None and lo_pos <= hi_pos
        and pos_count == hi_pos - lo_pos + 1
    ):
        intervals.append(frontier_registry.Interval(
            lo_pos, hi_pos, frontier_registry.TAG_PROVISIONAL,
            anchor_pos=hi_pos, is_base=is_base,
        ))
        return
    if lo_pos is not None and hi_pos is not None and lo_pos <= hi_pos:
        _completed_region_record(
            db, lo_hash, hi_hash, tag, run_ts_iso, index_con=index_con,
            order={h: i for i, h in enumerate(linearization)},
        )
    elif pos_count is not None:
        # Bounds do not resolve: the branch moved under us. Archive on the
        # FACTS alone (no order map -- there is no position space to merge in)
        # so the witness survives, then retract so no later claim extends it.
        _completed_region_record(
            db, lo_hash, hi_hash, tag, run_ts_iso, index_con=index_con,
        )
    _frontier_discard_interval(
        db, ident, bounds, index_con=index_con, pos_count=pos_count, tag=tag,
    )
```

Delete the old inline `else:` discard block and its `hi_hi_pos == len(linearization) - 1` test; move its explanatory comment onto `_load_one_interval`, keeping the paragraphs about `_frontier_persist_claim` not being the last write, `rev_claim_floor_pos`, and the checksum residual.

Also set `anchor_pos=0, is_base=True` on the authoritative interval constructed a few lines above.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "FrontierLoad or CompletedRegion or SkipClaim" -v
```

Expected: PASS. `TestFrontierLoadNormalisesUnrepresentableIntervals` and `TestFrontierLoadArchivesDiscardedInterval` cover the inverted-bounds and failed-count cases and must still pass unchanged — if one now expects a discard that has become a retention, check whether its fixture supplies a matching `:pos-count`; a fixture that omits it is still discarded, which is the intended behaviour.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Retain the high interval across tip growth (#325)

_frontier_load kept the high interval only when it reached the last
position; a fetch landing during a run made that false and the whole
region was discarded. It is now retained whenever its bounds resolve and
its stored :pos-count still matches its span, and extra provisional
intervals load alongside it.

The count check on the retain path is mandatory: the old path had none
and was safe only because a new commit implies a new tip, which forced
an inside-insertion onto the discard path. Unresolvable bounds are now
archived and retracted instead of being left for the next claim to
extend.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 5: Per-interval floor and interval-aware walk

**Files:**
- Modify: `mcp_server.py:10277-10286` (`_reverse_apply` signature), `:10601` (its persist call), `:12240-12300` (`rev_claim_floor_pos`, `submit_next`, `pending`), `:12380-12425` (the write dispatch), `:12440-12495` (the end-of-walk flush)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `ClaimResult` (Task 1), `_frontier_persist_claim(..., ident=, absorbed_idents=)` and `_frontier_persist_span(..., ident=)` (Task 3).
- Produces: `_reverse_apply(..., persist_claim: bool = True, claim_ident: Optional[str] = None, absorbed_idents: Optional[List[str]] = None)`.

**Design notes — why the floor must become per-interval:**

`rev_claim_floor_pos` is #326 Finding A's fix: the highest reverse position this run failed to complete, past which `:lo-hash` may not descend, because `:lo-hash` is a CLOSED RANGE bound and a write that raises takes `_run_ingestion`'s per-commit `except`, which logs, does `processed += 1`, and CONTINUES THE DESCENT — so the next lower position that succeeds would otherwise sweep the failed one into the interval. With one contiguous reverse descent a run-global scalar is exactly right.

With multiple gaps it is not. The reverse stream descends gap A, closes it, then jumps to gap B entirely BELOW A. A failure anywhere in A leaves a floor above every position in B, so `pos > floor` is false for the whole bulk gap: every bulk-gap claim would do the work and withhold the bookkeeping, and the next run would re-walk all of it. Silent, and it reads as a mysterious loss of resume progress.

Keep the shutdown `break` untouched: it needs no floor, because nothing lower ever claims after a shutdown and `completed_all = False` already gates the flush off. Do not add a third `_note_incomplete_rev` call to match it.

- [ ] **Step 1: Write the failing test**

```python
class TestPerIntervalReverseFloor:
    """#325: a write failure in the tip gap must not withhold bookkeeping for
    the bulk gap below it. The run-global floor of #326 does exactly that once
    the reverse stream serves more than one gap."""

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
    async def test_tip_gap_failure_does_not_floor_the_bulk_gap(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = self._repo(tmp_path, 10)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        # Leave a bulk gap by rewinding frontier-high's lo, then grow the tip
        # so the run has two gaps: [tip region] above and [bulk] below.
        self._repo(tmp_path, 4, start=10)
        lin = frontier_registry.build_linearization(str(repo))

        real_apply = mcp_server._reverse_apply
        failed_at = {}

        def flaky(db, repo_path, linearization, commit_metadata, pos, files,
                  index_con=None, persist_claim=True, claim_ident=None,
                  absorbed_idents=None):
            if pos == len(linearization) - 2 and "tip" not in failed_at:
                failed_at["tip"] = pos
                raise RuntimeError("injected tip-gap write failure")
            return real_apply(db, repo_path, linearization, commit_metadata, pos,
                              files, index_con, persist_claim, claim_ident,
                              absorbed_idents)

        monkeypatch.setattr(mcp_server, "_reverse_apply", flaky)
        await mcp_server._run_ingestion(str(repo), "HEAD")

        db = mcp_server.get_db()
        base = mcp_server._frontier_read_bounds(db, mcp_server._FRONTIER_HIGH_IDENT)
        assert base is not None
        base_lo_pos = lin.index(base[0])
        assert base_lo_pos <= failed_at["tip"], (
            "bulk-gap claims below a tip-gap failure must still persist their "
            "bookkeeping; a run-global floor withholds all of them"
        )
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestPerIntervalReverseFloor -v
```

Expected: FAIL — either `TypeError` on the `claim_ident` keyword, or the assertion, because the run-global floor blocks every bulk-gap claim.

- [ ] **Step 3: Implement**

Replace the `rev_claim_floor_pos` scalar with a dict keyed by the target entity ident:

```python
                # #325: ONE FLOOR PER INTERVAL, not per run. The reverse stream
                # now serves the topmost gap first and falls through to the
                # bulk gap below, so a run-global floor set by a failure in the
                # tip gap sits above every position in the bulk gap and
                # withholds the bookkeeping for all of them -- the work is
                # done, the claims are not persisted, and the next run re-walks
                # the lot. Same guarantee as #326 Finding A, stated over the
                # unit it was always really about.
                rev_claim_floor: Dict[str, int] = {}

                def _note_incomplete_rev(claim_tag: str, claim_pos: int, ident: str) -> None:
                    if claim_tag != "rev":
                        return
                    prev = rev_claim_floor.get(ident)
                    rev_claim_floor[ident] = (
                        claim_pos if prev is None else max(prev, claim_pos)
                    )
```

In `submit_next`, resolve the target ident for EVERY claim — including one
that is about to be skipped. A skipped position still came out of the
allocator, so it still extended (and possibly merged) an in-memory interval,
and the end-of-walk flush has to persist that skipped span to the same entity
the claim belonged to. Resolve inside the claim loop, before the skip test:

```python
                def submit_next() -> bool:
                    while True:
                        claim = claimer.next_claim()
                        if claim is None:
                            return False
                        tag, pos = claim
                        target_ident, absorbed = _claim_targets(
                            claimer.allocator.last_claim, linearization
                        )
                        if not _skip_claim(tag, pos, completed_regions):
                            break
                        lo, hi = skipped_span.get(target_ident, (pos, pos))
                        skipped_span[target_ident] = (min(lo, pos), max(hi, pos))
                        _ingest_progress["positions_skipped"] += 1
                        # `processed` keeps its meaning -- positions retired by
                        # the walk -- which is what #317's commit_census reads
                        # as walk_claimed.
                        _ingest_progress["processed"] += 1
                    fut = loop.run_in_executor(
                        executor, _extract_commit, repo_path, linearization[pos], ignore_patterns
                    )
                    pending.append((tag, pos, fut, target_ident, absorbed))
                    return True
```

Add the resolver beside `_RoundRobinClaimer`:

```python
def _claim_targets(
    claim: "frontier_registry.ClaimResult", linearization: List[str]
) -> Tuple[str, List[str]]:
    """(entity ident this claim extends, idents its merge absorbed).

    The base provisional interval persists at the fixed :ingestion/frontier-high
    ident; every other provisional interval at an ident MINTED ONCE from the
    hash at its anchor_pos. Never re-derive an ident from an interval's current
    bounds -- a provisional interval's :lo-hash moves every claim and its
    :hi-hash moves on every merge.
    """
    iv = claim.interval
    if iv.tag == frontier_registry.TAG_AUTHORITATIVE:
        return _FRONTIER_LOW_IDENT, []
    ident = (
        _FRONTIER_HIGH_IDENT if iv.is_base
        else _interval_ident(linearization[iv.anchor_pos])
    )
    absorbed = [
        _FRONTIER_HIGH_IDENT if a.is_base else _interval_ident(linearization[a.anchor_pos])
        for a in claim.absorbed
    ]
    return ident, [a for a in absorbed if a != ident]
```

`_RoundRobinClaimer` gains `self.allocator = allocator` (a public alias for the existing private field) so `submit_next` can read `last_claim`.

Widen the two `pending` unpacks (`tag, pos, fut = pending.popleft()` becomes `tag, pos, fut, target_ident, absorbed = pending.popleft()`), pass `target_ident` to both `_note_incomplete_rev` calls, and change the reverse dispatch's floor test:

```python
                                    persist_claim=(
                                        target_ident not in rev_claim_floor
                                        or pos > rev_claim_floor[target_ident]
                                    ),
                                    claim_ident=target_ident,
                                    absorbed_idents=absorbed,
```

`_reverse_apply` gains `claim_ident` and `absorbed_idents` parameters and forwards them:

```python
    if persist_claim:
        _frontier_persist_claim(
            db, linearization, pos, from_low=False, commit_ts_iso=commit_ts_iso,
            index_con=index_con, ident=claim_ident, absorbed_idents=absorbed_idents,
        )
```

The end-of-walk flush tracks the skipped span per ident. Replace the two
scalars `lowest_skipped_pos` / `highest_skipped_pos` with
`skipped_span: Dict[str, Tuple[int, int]] = {}` (populated in `submit_next`
above) and issue one flush per entry:

```python
                # Gated on completed_all, like Stage B and both folds below,
                # and NOT merely for symmetry. On the shutdown break `pending`
                # is still non-empty, and those entries are positions claimed
                # and queued for extraction but never applied -- nothing
                # persisted a claim for them. :lo-hash is a RANGE bound, so an
                # ungated flush would swallow them and declare them complete.
                # #326's own shape makes that the LIKELY case: skips are
                # retired inline in submit_next and never occupy `pending`, so
                # the genuine tip claims sit there while the skipped span is
                # driven far below them. _shutdown_requested is set by plain
                # stdin EOF at session end, not just by a signal.
                #
                # The flush obeys the same floor the per-commit claims do, now
                # per interval (#325). _frontier_persist_span moves :lo-hash
                # DOWN, so an unclamped flush would re-open the exact hole the
                # floor closes. Clamping the lo bound rather than dropping the
                # flush keeps it doing its job for the span that IS above the
                # floor.
                if completed_all:
                    for ident, (lo_pos, hi_pos) in sorted(skipped_span.items()):
                        floor = rev_claim_floor.get(ident)
                        if floor is not None:
                            lo_pos = max(lo_pos, floor + 1)
                        if lo_pos > hi_pos:
                            continue
                        async with db_lease_async() as db:
                            await loop.run_in_executor(
                                write_executor, _frontier_persist_span, db,
                                linearization, lo_pos, hi_pos, False,
                                commit_metadata[hi_pos][1], index_con, ident,
                            )
```

`_frontier_persist_span`'s `hi` bound is still the highest SKIPPED position,
never the highest claimed: a reverse position whose write FAILED persists no
claim but leaves `completed_all` True, and a flush bounded by the highest
claim would raise the persisted top bound over it. Once `:hi-hash` reaches the
tip the interval is representable, the next `_frontier_load` retains it, and
nothing ever re-walks those positions.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestPerIntervalReverseFloor -v
.venv/bin/python -m pytest tests/test_mcp_server.py -k "SkipFlush or SkipFastPath or RunIngestion" -v
```

Expected: PASS, including `TestSkipFlushNeverCoversFailedWrites`, `TestSkipFastPathFailedWriteIsNotClaimed` and `TestSkipFastPathDoesNotSkipTornWrites`.

- [ ] **Step 5: Ablate the floor to prove the test bites**

Temporarily revert `rev_claim_floor` to a single scalar (ignore the ident key), re-run `TestPerIntervalReverseFloor`, and confirm it FAILS. Restore. Record the observed failure message in the commit body.

- [ ] **Step 6: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Make the reverse claim floor per-interval (#325)

#326's floor is a run-global scalar, correct while the reverse stream
made one contiguous descent. It now serves the topmost gap first and
falls through to the bulk gap below, so a failure in the tip gap sat
above every bulk-gap position and withheld the bookkeeping for all of
them -- work done, claims not persisted, whole region re-walked next
run. Ablated: with the scalar restored, TestPerIntervalReverseFloor
fails on the frontier-high lo bound never descending past the injected
failure.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 6: Stage B declines while the provisional side is fragmented

**Files:**
- Modify: `mcp_server.py:11358-11432` (`_correction_sweep_select_position`), `:11774-11795` (`_should_fold_lineage_watermark`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_intervals_read_extra` (Task 2).
- Produces: no signature changes.

**Design notes:** One added clause each — *no provisional interval exists above `frontier-high`*. While a hole remains the sweep declines, which is correct: Stream 2 can still descend past a position the sweep would otherwise confirm. Once everything has coalesced there is exactly one provisional interval and the existing `low.hi + 1 == high.lo` test is precisely right.

This clause is also what makes `:ingestion/correction-sweep-through` work across tip growth with no new semantics. With no discard, `frontier-high.lo` no longer moves; a finished run's watermark stays parked at the old tip; once the tip gap merges in, the ceiling becomes the new tip; and `through + 1 .. ceiling` is exactly the set of new commits.

- [ ] **Step 1: Write the failing test**

```python
class TestCorrectionSweepDeclinesWhileFragmented:
    """#325: with a hole above frontier-high, Stream 2 can still descend past a
    position the sweep would confirm, so the sweep must decline."""

    def _seed(self, db, lin, extra=None):
        import mcp_server
        hi = mcp_server._FRONTIER_HIGH_IDENT
        lo = mcp_server._FRONTIER_LOW_IDENT
        mcp_server._transact(db, "[" + " ".join([
            f"[{lo} :entity-type :type/ingest-interval]",
            f"[{lo} :tag :authoritative]",
            f'[{lo} :lo-hash "{lin[0]}"]', f'[{lo} :hi-hash "{lin[3]}"]',
            f"[{lo} :pos-count 4]",
            f"[{hi} :entity-type :type/ingest-interval]",
            f"[{hi} :tag :provisional]",
            f'[{hi} :lo-hash "{lin[4]}"]', f'[{hi} :hi-hash "{lin[11]}"]',
            f"[{hi} :pos-count 8]",
        ]) + "]", "2026-09-04T00:00:00Z")
        if extra is not None:
            ident = mcp_server._interval_ident(lin[extra[1]])
            mcp_server._transact(db, "[" + " ".join([
                f"[{ident} :entity-type :type/ingest-interval]",
                f'[{ident} :ident "{ident}"]',
                f"[{ident} :tag :provisional]",
                f'[{ident} :lo-hash "{lin[extra[0]]}"]',
                f'[{ident} :hi-hash "{lin[extra[1]]}"]',
                f"[{ident} :pos-count {extra[1] - extra[0] + 1}]",
            ]) + "]", "2026-09-04T00:00:00Z")

    def _meta(self, lin):
        return [(h, "2026-09-04T00:00:00Z", "a", f"s{i}") for i, h in enumerate(lin)]

    def test_declines_while_a_hole_remains_above(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(12)]
        self._seed(real_db, lin, extra=(9, 11))
        assert mcp_server._correction_sweep_select_position(
            real_db, lin, self._meta(lin)) is None

    def test_selects_once_the_provisional_side_is_one_interval(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(12)]
        self._seed(real_db, lin)
        assert mcp_server._correction_sweep_select_position(
            real_db, lin, self._meta(lin)) == (lin[4], "2026-09-04T00:00:00Z")

    def test_fold_gate_declines_while_fragmented(self, real_db):
        import mcp_server
        lin = [f"h{i}" for i in range(12)]
        self._seed(real_db, lin, extra=(9, 11))
        mcp_server._transact(
            real_db,
            f'[[:ingestion/correction-sweep-through :hash "{lin[11]}"]]',
            "2026-09-04T00:00:00Z")
        assert mcp_server._should_fold_lineage_watermark(real_db, lin) is False
```

Check `_correction_sweep_through_query`'s attribute name before writing the last test's transact and use whatever it actually reads.

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestCorrectionSweepDeclinesWhileFragmented -v
```

Expected: FAIL on `test_declines_while_a_hole_remains_above` — the current code returns a position because it never looks for extra intervals.

- [ ] **Step 3: Implement**

Add to `_correction_sweep_select_position`, immediately after the existing gap-closed test:

```python
    if _intervals_read_extra(db):
        return None  # #325: a hole remains above frontier-high, so Stream 2 can
                     # still descend past a position this sweep would confirm.
                     # Once everything coalesces there is exactly one provisional
                     # interval and the gap-closed test above is exact again.
```

Add the same guard to `_should_fold_lineage_watermark`, before its `through_hash` read.

- [ ] **Step 4: Run to verify it passes**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "CorrectionSweep or FoldLineage or ShouldFold" -v
```

Expected: PASS, including the pre-existing `TestCorrectionSweepSelectPosition` and `TestCorrectionSweepThroughWatermark`.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "Decline Stage B while the provisional side is fragmented (#325)

One clause in each gate: no provisional interval above frontier-high.
While a hole remains, Stream 2 can still descend past a position the
sweep would confirm. It is also what lets :ingestion/correction-sweep-
through keep its current meaning across tip growth -- with no discard,
frontier-high.lo stops moving, so through+1..ceiling is exactly the new
commits.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 7: End-to-end acceptance tests

**Files:**
- Test: `tests/test_mcp_server.py`

**Interfaces:** consumes everything from Tasks 1-6. Produces no production code.

**Design notes:** Model these on `TestSkipFastPathEndToEnd` (`tests/test_mcp_server.py:8645`), which already builds a real git repo, runs `_run_ingestion` twice against a real file-backed graph, and grows the tip between runs. Reuse its `_repo` helper shape and its `MINIGRAF_INGEST_STREAM_RATIO=1:20` setting — at the 1:1 default the forward stream claims the region before the reverse stream reaches it and the assertions pass vacuously, which is the exact failure these tests exist to rule out.

Use `MINIGRAF_INGEST_TRACE_PATH` as the independent witness of which commits were actually applied: it emits one record per APPLIED commit, so a position that was never claimed appears in no record. It is an existing mechanism, not one built for these tests.

- [ ] **Step 1: Write test 1 — tip growth does not re-walk**

```python
class TestTipGrowthRetainsTheReverseFrontier:
    """#325 acceptance 1: after the tip grows, the reverse stream walks only
    the new commits. On master the interval is discarded and every position is
    re-claimed."""

    @pytest.mark.asyncio
    async def test_second_run_applies_only_the_new_commits(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry, json as _json

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = TestSkipFastPathEndToEnd()._repo(tmp_path, 12)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")
        first_high = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT)
        assert first_high is not None

        TestSkipFastPathEndToEnd()._repo(tmp_path, 3, start=12)
        trace = tmp_path / "trace.jsonl"
        monkeypatch.setenv("MINIGRAF_INGEST_TRACE_PATH", str(trace))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        lin = frontier_registry.build_linearization(str(repo))
        applied_rev = {
            _json.loads(line)["pos"] for line in trace.read_text().splitlines()
            if _json.loads(line).get("tag") == "rev"
        }
        new_positions = set(range(len(lin) - 3, len(lin)))
        assert applied_rev <= new_positions, (
            f"reverse stream re-applied already-ingested positions: "
            f"{sorted(applied_rev - new_positions)}"
        )
        assert mcp_server._intervals_read_extra(mcp_server.get_db()) == [], (
            "the tip interval must have merged into frontier-high"
        )
        merged = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT)
        assert merged[0] == first_high[0] and merged[1] == lin[-1]
```

Confirm the trace record's field names against `_ingest_trace.emit` before relying on `pos` and `tag`.

- [ ] **Step 2: Run it, and ablate against master**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py::TestTipGrowthRetainsTheReverseFrontier -v
git stash && git checkout master -- mcp_server.py frontier_registry.py 2>/dev/null || true
```

Expected: PASS on the branch. Then verify it FAILS on master's `mcp_server.py` + `frontier_registry.py` (restore afterwards with `git checkout 325-frontier-interval-set -- mcp_server.py frontier_registry.py`). Note: `git stash push <file>` is NOT a valid ablation here — check out the file from master explicitly and confirm the content actually changed with `git diff --stat` before trusting the result.

- [ ] **Step 3: Write test 2 — second growth before merge**

```python
class TestSecondTipGrowthBeforeMerge:
    """#325: two disjoint provisional intervals must persist and reload. This
    is the case a single extra fixed ident cannot express."""

    @pytest.mark.asyncio
    async def test_two_intervals_persist_and_the_sweep_declines(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = TestSkipFastPathEndToEnd()._repo(tmp_path, 10)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")

        # Grow, then interrupt inside the tip gap so it never merges.
        TestSkipFastPathEndToEnd()._repo(tmp_path, 4, start=10)
        original = mcp_server._reverse_apply
        seen = {"n": 0}

        def stop_after_two(*a, **kw):
            seen["n"] += 1
            if seen["n"] > 2:
                mcp_server._shutdown_requested.set()
            return original(*a, **kw)

        monkeypatch.setattr(mcp_server, "_reverse_apply", stop_after_two)
        await mcp_server._run_ingestion(str(repo), "HEAD")
        mcp_server._shutdown_requested.clear()
        monkeypatch.setattr(mcp_server, "_reverse_apply", original)

        TestSkipFastPathEndToEnd()._repo(tmp_path, 3, start=14)
        db = mcp_server.get_db()
        lin = frontier_registry.build_linearization(str(repo))
        alloc = mcp_server._frontier_load(db, lin, "2026-09-04T00:00:00Z")
        prov = [iv for iv in alloc.intervals()
                if iv.tag == frontier_registry.TAG_PROVISIONAL]
        assert len(prov) >= 1
        meta = [(h, "2026-09-04T00:00:00Z", "a", "s") for h in lin]
        if mcp_server._intervals_read_extra(db):
            assert mcp_server._correction_sweep_select_position(db, lin, meta) is None
```

- [ ] **Step 4: Write test 3 — insertion inside a retained interval**

```python
class TestInsertionInsideRetainedIntervalIsRefused:
    """#325 / #326 Finding B: a region is stored as two hashes but consumed as
    a closed POSITION RANGE. `git log --topo-order --reverse` places a new
    commit right after its branch point whenever the old tip's line stalls, so
    'branch off an old commit, merge the mainline in, fast-forward' lands a
    commit INSIDE the bounds. The stored :pos-count must refuse the interval."""

    @pytest.mark.asyncio
    async def test_interleaved_commit_refuses_the_interval(self, tmp_path, monkeypatch):
        import mcp_server, frontier_registry

        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = TestSkipFastPathEndToEnd()._repo(tmp_path, 8)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(repo / "memory.graph"))
        await mcp_server._run_ingestion(str(repo), "HEAD")
        before = mcp_server._frontier_read_bounds(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT)
        assert before is not None

        def git(*args):
            return _subprocess.run(["git", *args], cwd=repo, check=True,
                                   capture_output=True, text=True)

        base = git("rev-parse", "HEAD~5").stdout.strip()
        git("checkout", "-b", "side", base)
        (repo / "side.py").write_text("def side():\n    return 1\n")
        git("add", "."); git("commit", "-m", "side")
        git("checkout", "-")
        git("merge", "--no-ff", "-m", "merge side", "side")

        lin = frontier_registry.build_linearization(str(repo))
        lo_pos, hi_pos = lin.index(before[0]), lin.index(before[1])
        stored = mcp_server._frontier_read_pos_count(
            mcp_server.get_db(), mcp_server._FRONTIER_HIGH_IDENT)
        if hi_pos - lo_pos + 1 != stored:
            alloc = mcp_server._frontier_load(
                mcp_server.get_db(), lin, "2026-09-04T00:00:00Z")
            assert [iv for iv in alloc.intervals()
                    if iv.tag == frontier_registry.TAG_PROVISIONAL] == [], (
                "an interval whose span no longer matches its stored count "
                "must be refused, not retained"
            )
        else:
            pytest.skip(
                "git's topo-order tie-breaking did not place the side commit "
                "inside the bounds in this construction -- the EQUAL-COUNT "
                "variant remains the stated residual, not a measured loss"
            )
```

The `pytest.skip` branch is deliberate and must not be removed: #326 already tried and failed to construct a repository realizing the equal-count variant, and a test that silently passed on the wrong branch would read as coverage it does not have.

- [ ] **Step 5: Run the whole acceptance set**

```bash
.venv/bin/python -m pytest tests/test_mcp_server.py -k "TipGrowth or SecondTipGrowth or InsertionInsideRetained" -v
```

Expected: PASS (test 3 may report SKIPPED — record which).

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "Acceptance tests for the frontier interval set (#325)

Tip growth applies only the new commits (ablated against master, which
re-applies every position); two disjoint provisional intervals persist
and reload across an interrupted tip gap, with Stage B declining; and an
interleaved commit is refused by the stored :pos-count. The last test
skips explicitly when git's tie-breaking does not produce the insertion,
because the equal-count variant is a stated residual, not coverage.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 8: At-scale resume census probe

**Files:**
- Create: `evals/at_scale/probe_resume_census.py`
- Create: `tests/test_at_scale_resume_census.py`
- Modify: `.github/workflows/at-scale-benchmark-nightly.yml` (add a step beside the one at `:161`)
- Modify: `evals/at_scale/benchmark.md`

**Interfaces:**
- Consumes: `evals.at_scale.commit_census.collect_commit_census(repo_path, ref, walk_claimed, db, final_status) -> dict` — reused VERBATIM, not reimplemented.
- Produces: `probe_resume_census.run_resume_census(repo_path, branch, graph_path, truncate_by) -> dict` and a `--fail-on-mismatch` CLI flag.

**Design notes — why this is a standalone probe, not a new key on the benchmark run.**
`collect_commit_census` is already exactly the comparison needed; what is missing is the SCENARIO. The nightly benchmark run ingests once into a fresh graph, so no interval can ever be retained there and the existing `commit_census` cannot observe this change's failure mode at all. Folding a second full ingestion into that run would double its cost. `probe_ident_collision_new_history.py` (#267) set the precedent: its own file, its own `--fail-on-*` flag, its own nightly step at `.github/workflows/at-scale-benchmark-nightly.yml:161`, its own test file. Follow it.

**Why `repo_vs_walk` and not `walk_vs_graph`.** `_ingest_progress["processed"]` is SEEDED with `prior_ingested = _count_commit_entities(db)` and then incremented for every position retired this run, INCLUDING positions already counted in that seed. So `walk_vs_graph` is nonzero on ANY resume that touches already-ingested territory and discriminates nothing there. `commit_census` gates `walk_vs_graph` ALWAYS, so the probe must not hand it a resumed `processed` unchanged — pass `walk_claimed` as the run's own `processed_this_run` plus the prior graph count, and record both raw numbers so a reader can see which is which.

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_scale_resume_census.py`:

```python
import subprocess as _subprocess

import pytest

from evals.at_scale import probe_resume_census


def _repo(tmp_path, n, start=0):
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    for i in range(start, start + n):
        (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
    return repo


class TestResumeCensus:
    """#325: the census the nightly could not run. A wrongly-retained interval
    means positions are never claimed, and every existing at-scale detector
    reads that clean -- fact_audit's two witnesses agree about a commit neither
    holds, both :introduced-by checks only examine entities that exist,
    stderr_capture has nothing to read, and the fresh-graph commit_census
    cannot reach a retention at all."""

    @pytest.mark.asyncio
    async def test_clean_resume_agrees_on_all_three_counts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = _repo(tmp_path, 12)
        _repo(tmp_path, 3, start=12)
        result = await probe_resume_census.run_resume_census(
            str(repo), "main", str(tmp_path / "memory.graph"), truncate_by=3,
        )
        assert result["ok"] is True, result["interpretation"]
        assert result["repo_commits"] == 15
        assert result["repo_vs_walk"] == 0
        assert result["proved_nothing"] is False, (
            "a run whose denominator is zero proves nothing and must say so "
            "rather than reading as a pass"
        )

    @pytest.mark.asyncio
    async def test_an_empty_repo_proves_nothing_and_does_not_fail(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        result = await probe_resume_census.run_resume_census(
            str(repo), "main", str(tmp_path / "memory.graph"), truncate_by=0,
        )
        assert result["ok"] is True
        assert result["census_error"] is not None or result["proved_nothing"] is True
```

- [ ] **Step 2: Run to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_at_scale_resume_census.py -v
```

Expected: FAIL with `ImportError: cannot import name 'probe_resume_census'`.

- [ ] **Step 3: Implement the probe**

Create `evals/at_scale/probe_resume_census.py`:

```python
# evals/at_scale/probe_resume_census.py
"""#325: the commit census on a RESUMED graph.

WHY THE EXISTING CENSUS CANNOT DO THIS. `commit_census` (#317) runs inside the
nightly benchmark, which ingests once into a fresh graph. #325 makes
`_frontier_load` RETAIN a high interval whose bounds no longer reach the last
position, and a wrongly-retained interval means its positions are never
claimed at all -- so no fresh-graph run can ever produce the failure. Every
other at-scale detector reads it clean too: `fact_audit`'s two witnesses agree
about a commit neither holds, both `:introduced-by` checks only examine
entities that EXIST, and `stderr_capture` has nothing to read.

WHAT IT DOES. Ingests `<branch>~<truncate_by>`, advances to `<branch>`,
re-ingests into the SAME graph, then hands the three counts to the existing
`collect_commit_census` -- reused verbatim rather than reimplemented, so this
probe and the nightly gate cannot drift into counting different things.

WALK_CLAIMED IS NOT `_ingest_progress["processed"]` ON A RESUME.
That counter is SEEDED with `prior_ingested = _count_commit_entities(db)` and
then incremented for every position retired this run, including ones already
in the seed. Handing it to a census that gates `walk_vs_graph` ALWAYS would
fail every healthy resume. Both raw numbers ship so a reader can see which is
which; `repo_vs_walk` is the delta that carries the finding.

THE REF IS THE RESOLVED BRANCH, NEVER `HEAD` -- `_run_ingestion`'s own
`repo_total` was hardcoded to `HEAD` while ingestion takes a `branch`
argument, live in the very run that measured #317.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from typing import Any

sys.path.insert(0, ".")

from evals.at_scale.commit_census import collect_commit_census  # noqa: E402


async def run_resume_census(
    repo_path: str, branch: str, graph_path: str, truncate_by: int
) -> dict[str, Any]:
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(graph_path)

    truncated = f"{branch}~{truncate_by}" if truncate_by else branch
    try:
        await mcp_server._run_ingestion(repo_path, truncated)
    except Exception:  # noqa: BLE001 -- an empty repo has no ~N ref
        pass
    prior = mcp_server._count_commit_entities(mcp_server.get_db())

    mcp_server._ingest_progress["processed"] = 0
    await mcp_server._run_ingestion(repo_path, branch)
    this_run = mcp_server._ingest_progress["processed"]
    final_status = mcp_server._ingest_progress.get("status", "complete")

    result = collect_commit_census(
        repo_path=repo_path,
        ref=branch,
        walk_claimed=prior + this_run,
        db=mcp_server.get_db(),
        final_status=final_status,
    )
    result["prior_ingested"] = prior
    result["processed_this_run"] = this_run
    result["truncate_by"] = truncate_by
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--graph", required=True)
    ap.add_argument("--truncate-by", type=int, default=50)
    ap.add_argument("--out")
    ap.add_argument("--fail-on-mismatch", action="store_true")
    args = ap.parse_args()

    result = asyncio.run(
        run_resume_census(args.repo, args.branch, args.graph, args.truncate_by)
    )
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
    return 1 if (args.fail_on_mismatch and result["ok"] is False) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Confirm `mcp_server._ingest_progress`'s status key name before relying on it; if the walk records status elsewhere, read it from there and say so in the docstring.

- [ ] **Step 4: MEASURE the clean value before wiring the gate**

```bash
.venv/bin/python evals/at_scale/probe_resume_census.py \
  --repo <at-scale repo> --branch <resolved branch> \
  --graph /tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/2b3cc081-12d9-4d6b-ab24-341873d96e11/scratchpad/resume.graph \
  --truncate-by 50 --out evals/at_scale/results/325-resume-census.json
```

Do NOT add `--fail-on-mismatch` to the nightly until this has run and `repo_vs_walk` is 0 with a nonzero `repo_commits`. A gate wired on a prediction is how `:type/external-dependency` would have made #316 permanently red on its first run. Record the wall-clock cost alongside the counts.

- [ ] **Step 5: Wire the nightly step**

Add a step to `.github/workflows/at-scale-benchmark-nightly.yml` beside the collision census at `:161`, invoking the probe with `--fail-on-mismatch` and `--out` under the same results directory the other probes use. A red step here is NOT a harness failure — it means a resume lost a commit, and #325's retention predicate is reopened.

- [ ] **Step 6: Document it in `evals/at_scale/benchmark.md`**

Follow the section shape the collision census uses at `:1127` — script path, run command, measured baseline with its date and cost, and what a red run means.

- [ ] **Step 7: Run the tests and commit**

```bash
.venv/bin/python -m pytest tests/test_at_scale_resume_census.py -v
git add evals/at_scale/ tests/test_at_scale_resume_census.py .github/workflows/at-scale-benchmark-nightly.yml
git commit -m "Add a resume-scenario commit census probe (#325)

commit_census runs inside the nightly benchmark, which ingests once into
a fresh graph, so it can never reach a retained interval -- and a
wrongly-retained interval is exactly what #325 makes possible. Every
other at-scale detector reads that clean: the commit reaches neither
witness, both :introduced-by checks only examine entities that exist,
and stderr carries nothing.

Reuses collect_commit_census verbatim. walk_claimed is prior_ingested +
processed_this_run, not the raw counter: processed is seeded with the
prior graph count, so walk_vs_graph is nonzero on any resume and
discriminates nothing. Clean value measured before the gate was wired;
see results/325-resume-census.json.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
```

---

## Task 9: Docs, cost check and full suite

**Files:**
- Modify: `CLAUDE.md`, `SKILL.md` (check only)
- Test: whole suite

- [ ] **Step 1: Check `SKILL.md`**

```bash
grep -n "frontier\|interval\|ingest" SKILL.md
```

`SKILL.md` documents MCP tool calls, and this change adds no tool and alters no argument, so it very likely needs nothing. Confirm rather than assume — the standing rule is to check docs sync on every change, not to wait to be asked. If `handle_minigraf_ingest_status`'s output shape changed, `SKILL.md` and `tools/*.json` both need updating and `tests/test_tool_schemas.py` will say so.

- [ ] **Step 2: Add the `CLAUDE.md` section**

Add after the #326 section. It must state: the model (`frontier-high` is the lowest provisional interval; extras are ident-keyed, minted once at creation, never re-derived from current bounds); that the retain path's `:pos-count` check is mandatory rather than defensive, and why the old path was safe only by accident; that the floor is per-interval and why a run-global one silently withholds the bulk gap's bookkeeping; that Stage B declines while fragmented and that this is what lets `:ingestion/correction-sweep-through` keep its meaning; that no existing at-scale gate catches a wrongly-retained interval and the resume census is what does; and that the `:pos-count` checksum residual is unchanged and must not be described as sound.

- [ ] **Step 3: Verify the per-commit cost did not regress**

```bash
MINIGRAF_INGEST_TRACE_PATH=/tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/2b3cc081-12d9-4d6b-ab24-341873d96e11/scratchpad/trace.jsonl \
  .venv/bin/python evals/at_scale/probe_per_commit_cost.py
```

The persist path must stay at its current query/write count — the allocator reports coalesces in memory, so nothing re-reads the interval set per commit. Compare against the pre-change trace; report the numbers, do not assert them.

- [ ] **Step 4: Run the full suite**

```bash
.venv/bin/python -m pytest tests/ -q
```

Expected: PASS. Report the exact collected/passed counts — collected total is not the pass count.

- [ ] **Step 5: Scan for closing keywords in BOTH channels**

```bash
git log master..HEAD --format=%B | grep -inE "clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed)" 
```

Commit messages and the PR body are separate channels and no single check sees both. Re-scan after every new commit, including any written after the push. A keyword you WANT (`Closes #325`) goes in the PR body; keep it out of commit messages unless the intent is to auto-close on merge.

- [ ] **Step 6: Commit and open the PR**

```bash
git add CLAUDE.md
git commit -m "Document the frontier interval set (#325)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1"
git push -u origin 325-frontier-interval-set
```

PR body ends with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01RPXCQaaXmkrULRy3Vcvag1
```

Note `master` requires an approving review on top of green CI — ask before using `--admin` to bypass.
