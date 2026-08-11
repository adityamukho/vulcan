# Position-Exact Preload Close Side Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the forward walk's preload decide membership by linearization position at all four sites, closing #238's close-side residual and all of #245.

**Architecture:** Every `:depends-on`, `:pinned-commit` and `:ident` valid-time is written from `commit_ts_iso`, so it is always some commit's author date and its position is recoverable by inverting the timestamp against full-history `commit_metadata` — which PR #246 made available at preload time. A shared `_fact_is_live_at_position` decides every row; date clauses survive only as query prefilters and carry no safety property.

**Tech Stack:** Python 3, `minigraf>=1.2.3` (EDN/Datalog over a bi-temporal store), pytest with real-backend fixtures.

**Spec:** `docs/superpowers/specs/2026-08-10-position-exact-preload-close-side-design.md`

## Global Constraints

- **Real backend only.** No `MagicMock` fake of `MiniGrafDb`, ever. Use the `real_db` fixture (`tests/test_mcp_server.py:59`) or a file-backed `MiniGrafDb.open()` against `tmp_path`. See `docs/testing-conventions.md`.
- **Always verify results by re-querying**, never by asserting on mock call arguments.
- **Closing keywords: `Closes #238` and `Closes #245` belong in the PR body only.** `Refs #222` everywhere; **never** a closing keyword for #222. GitHub scans commit messages **and** the PR body, and on this project a *negated* "does not close #N" still auto-closed an issue. Re-scan after **every** commit written on this branch, not once before the push.
- **Every commit message on this branch uses `Refs #238` / `Refs #245`**, never `Closes`/`Fixes`. Only the final PR body carries the closing keywords.
- **Ambiguity resolves toward wrong-exclusion at both ends.** `end="intro"` takes `max` of colliding positions, `end="close"` takes `min`. This is the **inverse** of `evals/at_scale/probe_dep_preload_exposure.py`'s `edge_live_at`. The two must never be refactored into a shared helper.
- **Branch:** `fix-238-245-position-exact-preload`, already created off `master` (`463a922`).
- **Run the full suite** (`pytest tests/test_mcp_server.py -q`) before the final commit of each task, not just the new tests.
- **Nothing in `SKILL.md` or `CLAUDE.md` changes.** No query syntax, attribute, or tool surface moves.

## File Structure

| File | Responsibility |
| --- | --- |
| `mcp_server.py` | All production changes. Four new module-level helpers near `_valid_time_window_clauses` (`mcp_server.py:7674`); modifications to `_preload_known_entities` (7299), `_preload_known_deps` (7692), `_preload_pinned_commits` (7792), `_load_ingestion_preload_state` (7904). |
| `tests/test_mcp_server.py` | All tests. Unit-level classes placed immediately after `TestPreloadKnownEntitiesPositionBound` (ends ~9880). The end-to-end resume test **extends** PR #246's existing `_inverted_author_date_repo` (12475) and `TestResumeWithInvertedAuthorDates` (19641) rather than building a new fixture — see Task 6. |
| `evals/at_scale/probe_dep_preload_exposure.py` | Acceptance mode driving the fixed preloads; two stale docstrings corrected. |
| `evals/at_scale/results/245-dep-preload-exposure-fixed.json` | Acceptance run output. New file; the pre-fix `245-dep-preload-exposure.json` is kept unchanged as the baseline. |
| `docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md` | Revision section recording that its "close end is not recoverable at all" claim is superseded. |

---

### Task 1: Position-inversion primitives

**Files:**
- Modify: `mcp_server.py` — insert after `_valid_time_window_clauses` (ends at line 7689)
- Test: `tests/test_mcp_server.py` — new classes after `TestPreloadKnownEntitiesPositionBound`

**Interfaces:**
- Consumes: `_VALID_TIME_FOREVER_MS` (`mcp_server.py:7671`), `_iso_to_epoch_ms` (5146)
- Produces:
  - `_epoch_ms_to_iso(ms: int) -> str` — millisecond-precision ISO, `"%Y-%m-%dT%H:%M:%S.%fZ"` truncated to 3 fractional digits
  - `_build_ts_positions(commit_metadata: List[Tuple[str, str, str, str]]) -> Dict[str, List[int]]`
  - `_position_of_valid_time(ms: int, ts_positions: Dict[str, List[int]], *, end: str, stats: Optional[Dict[str, int]] = None) -> Optional[int]`
  - `_fact_is_live_at_position(vf_ms: int, vt_ms: int, watermark_pos: int, ts_positions: Dict[str, List[int]], stats: Optional[Dict[str, int]] = None) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
class TestValidTimePseudoAttributePredicates:
    """Two backend behaviours the position-exact preload depends on that no
    other code path exercises. Pinned so a minigraf bump that changes either
    fails loudly instead of silently mis-filtering the whole preload.

    Verified against minigraf's Rust source at ~/Work/AMC/Minigraf/minigraf
    before this was written: `<=` is a real BinOp (query/datalog/parser.rs
    "<=" => BinOp::Lte) and temporal.rs parse_timestamp routes any string
    containing 'T' through chrono's DateTime<Utc> parser, which accepts
    fractional seconds. These tests keep that true.
    """

    def test_upper_bound_on_valid_to_binds_and_excludes_the_sentinel(self, real_db):
        """_preload_known_entities' phase 2 filters on `[(<= ?vt N)]`. Every
        existing predicate on a pseudo-attribute comes from
        _valid_time_window_clauses, which only ever emits `[(= ?vt FOREVER)]`
        or the `[(<= ?vf N)] [(> ?vt N)]` pair -- an upper bound on ?vt alone
        is a new shape. Still-open facts must fall out of it, because
        i64::MAX <= N is false for any real timestamp."""
        import json
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-10T00:00:00Z"} '
            '[[:module/closed :ident ":module/closed"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z"} '
            '[[:module/open :ident ":module/open"]])'
        )
        cutoff = mcp_server._iso_to_epoch_ms("2026-05-01T00:00:00Z")
        raw = mcp_server._db_execute(
            real_db,
            "(query [:find ?i ?vt :any-valid-time "
            ":where [?e :ident ?i] "
            "[?e :db/valid-from ?vf] [?e :db/valid-to ?vt] "
            f"[(<= ?vt {cutoff})]])",
        )
        idents = {row[0] for row in json.loads(raw).get("results", [])}
        assert idents == {":module/closed"}

    def test_valid_at_accepts_millisecond_precision(self, real_db):
        """The re-admission pass queries at ISO(vt - 1ms), which carries a
        `.%f` field; every existing :valid-at caller passes a second-granular
        commit date. A truncating or rejecting parser would move the
        re-admission instant by a full second."""
        import json
        import mcp_server
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-10T00:00:00Z"} '
            '[[:module/m :ident ":module/m"]])'
        )
        live = mcp_server._db_execute(
            real_db,
            '(query [:find ?i :valid-at "2026-04-09T23:59:59.999Z" '
            ':where [?e :ident ?i]])',
        )
        assert [row[0] for row in json.loads(live).get("results", [])] == [":module/m"]
        dead = mcp_server._db_execute(
            real_db,
            '(query [:find ?i :valid-at "2026-04-10T00:00:00.000Z" '
            ':where [?e :ident ?i]])',
        )
        assert json.loads(dead).get("results", []) == []


class TestPositionOfValidTime:
    """#238/#245: recovering a linearization position from a fact's own
    :db/valid-from / :db/valid-to."""

    TS_POSITIONS = {
        "2026-04-01T00:00:00Z": [0],
        "2026-05-02T00:00:00Z": [1],
        "2026-04-26T00:00:00Z": [2, 5],  # a deliberate collision
    }

    def test_unique_instant_maps_to_its_position(self):
        import mcp_server
        ms = mcp_server._iso_to_epoch_ms("2026-05-02T00:00:00Z")
        assert mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="intro") == 1
        assert mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="close") == 1

    def test_collision_resolves_toward_exclusion_at_both_ends(self):
        """THE test that stops a later refactor unifying this policy with
        probe_dep_preload_exposure.edge_live_at's, which resolves the exact
        opposite way (min intro, max close) and is correct to do so: a
        measurement must not understate exposure, a fix must not risk the
        unrecoverable direction. An ambiguous INTRODUCTION takes the LATEST
        colliding position so it is more likely to read as above W; an
        ambiguous CLOSE takes the EARLIEST so it is more likely to read as at
        or below W. Both land on wrong-exclusion, which #235's sweep repairs."""
        import mcp_server
        ms = mcp_server._iso_to_epoch_ms("2026-04-26T00:00:00Z")
        assert mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="intro") == 5
        assert mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="close") == 2

    def test_unmappable_instant_returns_none_and_counts(self):
        import mcp_server
        stats = {}
        ms = mcp_server._iso_to_epoch_ms("2026-06-15T00:00:00Z")
        assert mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="intro", stats=stats) is None
        assert stats["unmappable_intro"] == 1

    def test_collision_is_counted(self):
        import mcp_server
        stats = {}
        ms = mcp_server._iso_to_epoch_ms("2026-04-26T00:00:00Z")
        mcp_server._position_of_valid_time(
            ms, self.TS_POSITIONS, end="close", stats=stats)
        assert stats["collisions"] == 1

    def test_unknown_end_raises(self):
        """A typo must not silently pick the intro policy for a close."""
        import mcp_server
        import pytest
        ms = mcp_server._iso_to_epoch_ms("2026-05-02T00:00:00Z")
        with pytest.raises(ValueError):
            mcp_server._position_of_valid_time(ms, self.TS_POSITIONS, end="closed")


class TestFactIsLiveAtPosition:
    """The membership rule, shared by all three position-filtered sites:
    intro_pos <= W AND (open OR close_pos > W)."""

    TS_POSITIONS = {
        "2026-04-01T00:00:00Z": [0],
        "2026-05-02T00:00:00Z": [1],
        "2026-04-26T00:00:00Z": [2],
    }
    WATERMARK_POS = 1

    def _ms(self, iso):
        import mcp_server
        return mcp_server._iso_to_epoch_ms(iso)

    def test_open_fact_introduced_below_w_is_live(self):
        import mcp_server
        assert mcp_server._fact_is_live_at_position(
            self._ms("2026-04-01T00:00:00Z"), mcp_server._VALID_TIME_FOREVER_MS,
            self.WATERMARK_POS, self.TS_POSITIONS)

    def test_open_fact_introduced_above_w_is_not_live(self):
        """#238's DATA-LOSS direction: dated 2026-04-26, EARLIER than the
        watermark's own 2026-05-02, but topologically above it."""
        import mcp_server
        assert not mcp_server._fact_is_live_at_position(
            self._ms("2026-04-26T00:00:00Z"), mcp_server._VALID_TIME_FOREVER_MS,
            self.WATERMARK_POS, self.TS_POSITIONS)

    def test_fact_closed_above_w_is_still_live(self):
        """The close-side residual: close DATE 2026-04-26 is below the
        envelope, so a date bound drops it, but close POSITION 2 is above W."""
        import mcp_server
        assert mcp_server._fact_is_live_at_position(
            self._ms("2026-04-01T00:00:00Z"), self._ms("2026-04-26T00:00:00Z"),
            self.WATERMARK_POS, self.TS_POSITIONS)

    def test_fact_closed_at_w_is_not_live(self):
        import mcp_server
        assert not mcp_server._fact_is_live_at_position(
            self._ms("2026-04-01T00:00:00Z"), self._ms("2026-05-02T00:00:00Z"),
            self.WATERMARK_POS, self.TS_POSITIONS)

    def test_unmappable_close_excludes(self):
        """Cannot place the close, so cannot prove the fact is still live.
        Exclusion is the recoverable direction."""
        import mcp_server
        stats = {}
        assert not mcp_server._fact_is_live_at_position(
            self._ms("2026-04-01T00:00:00Z"), self._ms("2026-06-15T00:00:00Z"),
            self.WATERMARK_POS, self.TS_POSITIONS, stats)
        assert stats["unmappable_close"] == 1


class TestEpochMsToIso:
    def test_round_trips_a_commit_instant(self):
        import mcp_server
        ms = mcp_server._iso_to_epoch_ms("2026-04-26T00:00:00Z")
        assert mcp_server._epoch_ms_to_iso(ms) == "2026-04-26T00:00:00.000Z"

    def test_one_millisecond_before_an_instant(self):
        """The re-admission pass's 'last instant this entity was live'."""
        import mcp_server
        ms = mcp_server._iso_to_epoch_ms("2026-04-26T00:00:00Z")
        assert mcp_server._epoch_ms_to_iso(ms - 1) == "2026-04-25T23:59:59.999Z"


class TestBuildTsPositions:
    def test_groups_colliding_instants(self):
        import mcp_server
        metadata = [
            ("a" * 40, "2026-04-01T00:00:00Z", "x@y", "s0"),
            ("b" * 40, "2026-04-26T00:00:00Z", "x@y", "s1"),
            ("c" * 40, "2026-04-26T00:00:00Z", "x@y", "s2"),
        ]
        assert mcp_server._build_ts_positions(metadata) == {
            "2026-04-01T00:00:00Z": [0],
            "2026-04-26T00:00:00Z": [1, 2],
        }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k "ValidTimePseudoAttributePredicates or PositionOfValidTime or FactIsLiveAtPosition or EpochMsToIso or BuildTsPositions" -q`

Expected: `TestValidTimePseudoAttributePredicates` PASSES already (it pins existing backend behaviour); the other four classes FAIL with `AttributeError: module 'mcp_server' has no attribute '_position_of_valid_time'` etc.

If `TestValidTimePseudoAttributePredicates` fails, **stop and report** — the design's backend assumptions are wrong and the spec's fallbacks must be revisited before continuing.

- [ ] **Step 3: Write the implementation**

Insert into `mcp_server.py` immediately after `_valid_time_window_clauses` (after line 7689):

```python
def _epoch_ms_to_iso(ms: int) -> str:
    """minigraf's epoch-ms valid-time scale back to millisecond-precision ISO.

    Millisecond precision is load-bearing for the re-admission pass in
    _preload_known_entities, which queries at ISO(vt - 1 ms). Verified against
    minigraf's temporal.rs parse_timestamp: any string containing 'T' goes
    through chrono's DateTime<Utc> parser, which accepts fractional seconds.
    Pinned by test_valid_at_accepts_millisecond_precision.

    Never pass _VALID_TIME_FOREVER_MS -- callers must test for the sentinel
    first, as minigraf's own millis_to_timestamp_string documents.
    """
    return (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def _build_ts_positions(
    commit_metadata: List[Tuple[str, str, str, str]]
) -> Dict[str, List[int]]:
    """Map each author-date instant to EVERY linearization position holding it.

    A list, not a single position, and deliberately so: _git_commits formats
    "%Y-%m-%dT%H:%M:%SZ" at second granularity, so distinct commits routinely
    share an instant. Collapsing that to one position would silently pick a
    winner; _position_of_valid_time resolves the ambiguity explicitly instead.
    """
    ts_positions: Dict[str, List[int]] = {}
    for pos, (_hash, ts_iso, _author, _subject) in enumerate(commit_metadata):
        ts_positions.setdefault(ts_iso, []).append(pos)
    return ts_positions


def _position_of_valid_time(
    ms: int,
    ts_positions: Dict[str, List[int]],
    *,
    end: str,
    stats: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Recover the linearization position a :db/valid-from or :db/valid-to
    was written at (#238 close side, #245).

    Every :depends-on, :pinned-commit and :ident valid-time is written from
    commit_ts_iso, i.e. commit_metadata[pos][1], so it is always some commit's
    author date and its position is recoverable WITHOUT a commit reference to
    join to. Both issues state these sites admit no position filter; that is
    true of joins and false of positions. The inversion needs full-history
    commit_metadata at preload time, which is exactly what PR #246 made
    available by moving build_linearization and _git_commits above the preload
    block.

    AMBIGUITY RESOLVES TOWARD WRONG-EXCLUSION AT BOTH ENDS. An ambiguous
    introduction takes the LATEST colliding position (more likely to read as
    above W); an ambiguous close takes the EARLIEST (more likely to read as at
    or below W). Both land on exclusion, the direction #235's correction sweep
    repairs, rather than on the unrecoverable inverted-interval direction.

    THIS IS THE INVERSE OF evals/at_scale/probe_dep_preload_exposure.py's
    edge_live_at, which takes min for an introduction and max for a close.
    That is correct THERE and wrong HERE: a measurement must not understate
    exposure, because a number rounded in our own favour would have argued for
    closing #245; a fix must not risk the unrecoverable direction. DO NOT
    refactor the two into a shared helper. Pinned by
    test_collision_resolves_toward_exclusion_at_both_ends.

    Returns None for an instant matching no commit -- a rewritten history, or
    a fact dated by something other than a commit. Callers exclude on None.
    """
    if end not in ("intro", "close"):
        raise ValueError(f"end must be 'intro' or 'close', got {end!r}")
    ts_iso = (
        datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    positions = ts_positions.get(ts_iso)
    if not positions:
        if stats is not None:
            key = f"unmappable_{end}"
            stats[key] = stats.get(key, 0) + 1
        return None
    if len(positions) > 1 and stats is not None:
        stats["collisions"] = stats.get("collisions", 0) + 1
    return max(positions) if end == "intro" else min(positions)


def _fact_is_live_at_position(
    vf_ms: int,
    vt_ms: int,
    watermark_pos: int,
    ts_positions: Dict[str, List[int]],
    stats: Optional[Dict[str, int]] = None,
) -> bool:
    """The membership rule for the forward walk's preload state (#238, #245):

        live at W  <=>  intro_pos <= W  AND  (open OR close_pos > W)

    Position alone. Date clauses in the callers' queries are prefilters for
    row-count reduction and carry NO safety property -- see the spec's
    "Why this is not the add-back union #238 forbids".

    An unplaceable endpoint excludes, because the fact cannot be proven live
    at W. That is the recoverable direction.
    """
    intro_pos = _position_of_valid_time(
        vf_ms, ts_positions, end="intro", stats=stats
    )
    if intro_pos is None or intro_pos > watermark_pos:
        return False
    if vt_ms >= _VALID_TIME_FOREVER_MS:
        return True
    close_pos = _position_of_valid_time(
        vt_ms, ts_positions, end="close", stats=stats
    )
    if close_pos is None:
        return False
    return close_pos > watermark_pos
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -k "ValidTimePseudoAttributePredicates or PositionOfValidTime or FactIsLiveAtPosition or EpochMsToIso or BuildTsPositions" -q`
Expected: all PASS.

Then the full suite: `pytest tests/test_mcp_server.py -q` — Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Add position-inversion primitives for the preload bounds

Every :depends-on, :pinned-commit and :ident valid-time is written from
commit_ts_iso, so its linearization position is recoverable by inverting the
timestamp rather than by joining to a :hash. PR #246 made full-history
commit_metadata available at preload time, which is what makes this possible
now and did not when #238 and #245 were written.

Ambiguity resolves toward wrong-exclusion at both ends, the inverse of the
exposure probe's oracle. The two policies are correct for their own purposes
and must not be shared.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_preload_known_deps` position mode

**Files:**
- Modify: `mcp_server.py:7692-7789` (`_preload_known_deps`)
- Test: `tests/test_mcp_server.py` — new class after `TestFactIsLiveAtPosition`

**Interfaces:**
- Consumes: `_fact_is_live_at_position`, `_build_ts_positions`, `_VALID_TIME_FOREVER_MS`, `_valid_time_window_clauses`, `_epoch_ms_to_iso`
- Produces: `_preload_known_deps(db, file_entities, valid_at_ms=None, ts_positions=None, watermark_pos=None, t_hi_ms=None, stats=None) -> tuple` — return shape `(file_deps, dep_valid_from)` is **unchanged**

**Mode switch (used identically in Tasks 2, 3 and 4):**

```python
position_mode = (
    watermark_pos is not None
    and ts_positions is not None
    and t_hi_ms is not None
)
```

When false, behaviour is byte-for-byte today's: `_valid_time_window_clauses(valid_at_ms)` and no Python position filter. This preserves the degraded path for a watermark that exists but is absent from this linearization, where `valid_at_ms` is a real `ts(W)` and dropping to "open facts only" would be a regression.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestPreloadKnownDepsPositionBound:
    """#245: :depends-on membership must be decided by position, not by the
    watermark commit's own author date.

    Measured on this repository at an affected position: the ts(W) bound
    wrongly excludes 32 of 68 genuinely live edges (47%), all in the
    wrong-exclusion direction, once #238's close-side residual is removed.
    Wrong inclusion is 0 in both framings.

    Linearization: 0 = c0 @ 2026-04-01, 1 = c1 (WATERMARK) @ 2026-05-02,
    2 = c2 @ 2026-04-26 -- above the watermark but dated EARLIER, the
    side-branch inversion.
    """

    TS_POSITIONS = {
        "2026-04-01T00:00:00Z": [0],
        "2026-05-02T00:00:00Z": [1],
        "2026-04-26T00:00:00Z": [2],
    }
    WATERMARK_POS = 1

    def _t_hi_ms(self):
        import mcp_server
        return mcp_server._iso_to_epoch_ms("2026-05-02T00:00:00Z")

    def _seed(self, real_db):
        """Four edges out of one module, one per quadrant of the rule."""
        import mcp_server
        src = mcp_server._code_ident("module", "mod_a.py")
        real_db.execute(
            f'(transact [[{src} :entity-type :type/module] '
            f'[{src} :ident "{src}"]])'
        )
        # open, introduced at c0 (pos 0 <= W): live.
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z"} '
            f'[[{src} :depends-on :module/open-below-w]])'
        )
        # open, introduced at c2 (pos 2 > W): NOT live. Today's ts(W) bound
        # admits it, because c2's date is earlier than the watermark's.
        real_db.execute(
            '(transact {:valid-from "2026-04-26T00:00:00Z"} '
            f'[[{src} :depends-on :module/open-above-w]])'
        )
        # closed at c2 (pos 2 > W): still live at W. Today's bound drops it,
        # because the close DATE is below the envelope. This is the 47%.
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-26T00:00:00Z"} '
            f'[[{src} :depends-on :module/closed-above-w]])'
        )
        # closed at c1 (pos 1 <= W): genuinely dead at W.
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-05-02T00:00:00Z"} '
            f'[[{src} :depends-on :module/closed-below-w]])'
        )
        return src

    def _run(self, real_db):
        import mcp_server
        file_deps, dep_valid_from = mcp_server._preload_known_deps(
            real_db, {"mod_a.py": []},
            ts_positions=self.TS_POSITIONS,
            watermark_pos=self.WATERMARK_POS,
            t_hi_ms=self._t_hi_ms(),
        )
        return file_deps.get("mod_a.py", set()), dep_valid_from

    def test_edge_closed_above_the_watermark_is_reloaded(self, real_db):
        """#245's measured direction. Excluded, the forward walk treats an
        already-standing dependency as newly introduced and overwrites its
        true :valid-from."""
        self._seed(real_db)
        deps, _ = self._run(real_db)
        assert ":module/closed-above-w" in deps

    def test_edge_introduced_above_the_watermark_is_excluded(self, real_db):
        """The data-loss direction: present in the preload but absent from the
        earlier commit's resolved imports on replay, so it is closed at that
        earlier commit -- an inverted valid interval on the edge."""
        self._seed(real_db)
        deps, _ = self._run(real_db)
        assert ":module/open-above-w" not in deps

    def test_edge_closed_at_or_below_the_watermark_is_excluded(self, real_db):
        self._seed(real_db)
        deps, _ = self._run(real_db)
        assert ":module/closed-below-w" not in deps

    def test_open_edge_below_the_watermark_is_reloaded(self, real_db):
        self._seed(real_db)
        deps, _ = self._run(real_db)
        assert ":module/open-below-w" in deps

    def test_valid_from_is_the_edge_s_own_introduction(self, real_db):
        """dep_valid_from must still carry the edge's true :valid-from, which
        is what removed-dependency detection compares against."""
        src = self._seed(real_db)
        _, dep_valid_from = self._run(real_db)
        assert dep_valid_from[(src, ":module/closed-above-w")].startswith(
            "2026-04-01T00:00:00"
        )

    def test_the_prefilter_alone_does_not_close_the_hole(self, real_db):
        """The close-side twin of
        TestPreloadKnownEntitiesPositionBound.test_the_envelope_alone_does_not
        _close_the_hole. Widening the date clause to `[(<= ?vf T_hi)]` WITHOUT
        the position filter re-admits the above-W edge -- the 'add-back union'
        #238 forbids. Position mode is off here because watermark_pos is None,
        so this call exercises exactly that shape."""
        import mcp_server
        self._seed(real_db)
        file_deps, _ = mcp_server._preload_known_deps(
            real_db, {"mod_a.py": []},
            ts_positions=self.TS_POSITIONS,
            watermark_pos=None,
            t_hi_ms=self._t_hi_ms(),
        )
        assert ":module/open-above-w" in file_deps.get("mod_a.py", set())

    def test_position_args_omitted_restores_today_s_behaviour(self, real_db):
        """The degraded path: a watermark absent from this linearization keeps
        the ts(W) date window rather than dropping to open-facts-only."""
        import mcp_server
        self._seed(real_db)
        file_deps, _ = mcp_server._preload_known_deps(
            real_db, {"mod_a.py": []},
            valid_at_ms=mcp_server._iso_to_epoch_ms("2026-05-02T00:00:00Z"),
        )
        deps = file_deps.get("mod_a.py", set())
        assert ":module/open-above-w" in deps       # the residual, unchanged
        assert ":module/closed-above-w" not in deps  # the residual, unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k TestPreloadKnownDepsPositionBound -q`

Expected: `test_edge_closed_above_the_watermark_is_reloaded`, `test_edge_introduced_above_the_watermark_is_excluded` and `test_valid_from_is_the_edge_s_own_introduction` FAIL with `TypeError: _preload_known_deps() got an unexpected keyword argument 'ts_positions'`.

- [ ] **Step 3: Write the implementation**

Replace `_preload_known_deps`' signature, the `#238/#245` docstring paragraph, the query, and the row loop:

```python
def _preload_known_deps(
    db: Any,
    file_entities: Dict[str, List[str]],
    valid_at_ms: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    watermark_pos: Optional[int] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> tuple:
```

Replace the docstring's `#238/#245: this bound is still ts(W)…` paragraph (lines 7711-7719) with:

```
    #238/#245: membership is now decided by POSITION, not by date. A
    :depends-on fact carries no commit reference, but its :db/valid-from and
    :db/valid-to are always some commit's author date (every write site dates
    them from commit_ts_iso), so _fact_is_live_at_position recovers both
    endpoints' positions by inverting the timestamp. #245's own text says
    these sites "admit no position filter"; that is true of JOINS and false of
    POSITIONS, and the inversion only became available once PR #246 moved the
    full-history commit_metadata above the preload block.

    The `[(<= ?vf t_hi_ms)]` clause is a PREFILTER for row-count reduction and
    carries no safety property: a fact introduced at position p <= W has
    vf = ts[p] <= T_hi(W), so it drops only rows the position rule would drop
    anyway. There is deliberately NO clause on ?vt -- a close above W can
    carry an arbitrarily early date, which is the whole defect.
    Widening the prefilter without the position filter is the "add-back union"
    #238 forbids; test_the_prefilter_alone_does_not_close_the_hole pins that.

    position_mode off (no watermark, or a watermark absent from this
    linearization) restores today's ts(W) date window exactly, which is
    narrower than open-facts-only and therefore the safer degradation.
```

Replace the query and loop:

```python
    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    window_clauses = (
        f"[(<= ?vf {t_hi_ms})]" if position_mode
        else _valid_time_window_clauses(valid_at_ms)
    )

    try:
        raw = _db_execute(
            db,
            "(query [:find ?srci ?dep ?vf ?vt "
            ":any-valid-time "
            ":where [?src :ident ?srci] "
            "[?src :depends-on ?dep] "
            "[?src :db/valid-from ?vf] "
            "[?src :db/valid-to ?vt] "
            f"{window_clauses}])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return file_deps, dep_valid_from

    for src_ident, dep_ident, vf_ms, vt_ms in rows:
        file_path = ident_to_file.get(src_ident)
        if file_path is None:
            continue
        if position_mode and not _fact_is_live_at_position(
            vf_ms, vt_ms, watermark_pos, ts_positions, stats
        ):
            continue
        vf_iso = _epoch_ms_to_iso(vf_ms)
        file_deps.setdefault(file_path, set()).add(dep_ident)
        dep_valid_from[(src_ident, dep_ident)] = vf_iso

    return file_deps, dep_valid_from
```

Keep the existing clause-ordering comment above the query verbatim — it is still load-bearing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -k "TestPreloadKnownDeps" -q`
Expected: all PASS, including the pre-existing `TestPreloadKnownDeps` class (the `valid_at_ms`-only and no-argument calls still work).

Then: `pytest tests/test_mcp_server.py -q` — Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Decide :depends-on preload membership by position

_preload_known_deps bounded its reload with the watermark commit's own author
date. Author dates are not monotonic in topological order, so at an affected
watermark position it wrongly excludes 32 of 68 genuinely live edges and
wrongly includes edges introduced above the watermark.

Both endpoints' positions come from inverting the fact's own
:db/valid-from / :db/valid-to. The date clause is demoted to a prefilter.

Refs #245
Refs #238
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `_preload_pinned_commits` position mode

**Files:**
- Modify: `mcp_server.py:7792-7844` (`_preload_pinned_commits`)
- Test: `tests/test_mcp_server.py` — new class after `TestPreloadKnownDepsPositionBound`

**Interfaces:**
- Consumes: same primitives as Task 2
- Produces: `_preload_pinned_commits(db, valid_at_ms=None, ts_positions=None, watermark_pos=None, t_hi_ms=None, stats=None) -> Dict[str, tuple]` — return shape `{ident: (sha, valid_from_iso)}` **unchanged**

**Note on evidence:** `:pinned-commit` exposure is **unmeasurable** on this repository — 0 gitlink events in 610 commits, so this history produces no such facts at all. This task ships on the argument that it shares the mechanism, not on measured exposure. Say so in the docstring; do not imply it was verified in the field.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestPreloadPinnedCommitsPositionBound:
    """#245: :pinned-commit gets the same position rule as :depends-on.

    Exposure is UNMEASURABLE on this repository -- 0 gitlink events in 610
    commits, so real history produces no :pinned-commit facts and the probe
    reports nothing for them. This fixture is synthetic, and the change ships
    on the argument that the mechanism is identical, not on field evidence.

    _preload_pinned_commits also has NO ident_to_file narrowing, so it lacks
    even the partial mitigation that makes :depends-on's narrow figure small.

    Same linearization as TestPreloadKnownDepsPositionBound.
    """

    TS_POSITIONS = {
        "2026-04-01T00:00:00Z": [0],
        "2026-05-02T00:00:00Z": [1],
        "2026-04-26T00:00:00Z": [2],
    }
    WATERMARK_POS = 1

    def _seed(self, real_db):
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z"} '
            '[[:external-dependency/sub-open-below :ident '
            '":external-dependency/sub-open-below"] '
            '[:external-dependency/sub-open-below :pinned-commit "aaa111"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-26T00:00:00Z"} '
            '[[:external-dependency/sub-open-above :ident '
            '":external-dependency/sub-open-above"] '
            '[:external-dependency/sub-open-above :pinned-commit "bbb222"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-26T00:00:00Z"} '
            '[[:external-dependency/sub-closed-above :ident '
            '":external-dependency/sub-closed-above"] '
            '[:external-dependency/sub-closed-above :pinned-commit "ccc333"]])'
        )

    def _run(self, real_db):
        import mcp_server
        return mcp_server._preload_pinned_commits(
            real_db,
            ts_positions=self.TS_POSITIONS,
            watermark_pos=self.WATERMARK_POS,
            t_hi_ms=mcp_server._iso_to_epoch_ms("2026-05-02T00:00:00Z"),
        )

    def test_pin_closed_above_the_watermark_is_reloaded(self, real_db):
        """Without it the server loses the prior SHA and closes the next bump
        against the wrong one, exactly as the docstring describes."""
        self._seed(real_db)
        pinned = self._run(real_db)
        assert pinned[":external-dependency/sub-closed-above"][0] == "ccc333"

    def test_pin_set_above_the_watermark_is_excluded(self, real_db):
        self._seed(real_db)
        pinned = self._run(real_db)
        assert ":external-dependency/sub-open-above" not in pinned

    def test_open_pin_below_the_watermark_is_reloaded(self, real_db):
        self._seed(real_db)
        pinned = self._run(real_db)
        assert pinned[":external-dependency/sub-open-below"][0] == "aaa111"

    def test_position_args_omitted_restores_today_s_behaviour(self, real_db):
        import mcp_server
        self._seed(real_db)
        pinned = mcp_server._preload_pinned_commits(real_db)
        assert ":external-dependency/sub-open-above" in pinned
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k TestPreloadPinnedCommitsPositionBound -q`
Expected: FAIL with `TypeError: _preload_pinned_commits() got an unexpected keyword argument 'ts_positions'`.

- [ ] **Step 3: Write the implementation**

```python
def _preload_pinned_commits(
    db: Any,
    valid_at_ms: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    watermark_pos: Optional[int] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> Dict[str, tuple]:
```

Replace the docstring's `#238/#245: still ts(W)…` paragraph (lines 7802-7807) with:

```
    #238/#245: membership is decided by POSITION, exactly as
    _preload_known_deps does and for the same reason -- a :pinned-commit fact
    carries no commit reference, but its :db/valid-from / :db/valid-to are
    always some commit's author date. See that function's docstring for the
    prefilter's role and the degradation path.

    UNMEASURABLE on the repository this was developed against: 0 gitlink
    events in 610 commits, so that history produces no :pinned-commit facts at
    all and the #245 exposure probe reports nothing here. This ships on the
    argument that the mechanism is identical to :depends-on's, NOT on measured
    field exposure. Unlike :depends-on this function has no ident_to_file
    narrowing, so it lacks even that partial mitigation.
```

Then the body:

```python
    pinned: Dict[str, tuple] = {}
    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    window_clauses = (
        f"[(<= ?vf {t_hi_ms})]" if position_mode
        else _valid_time_window_clauses(valid_at_ms)
    )
    try:
        # Bind the entity's :ident object, not the bare ?e subject variable —
        # same UUID-vs-ident pitfall _preload_known_deps guards against.
        # [?e :ident ?ei] must precede [?e :pinned-commit ?sha] so that the
        # :db/valid-from/:db/valid-to pseudo-attributes (which bind to
        # whichever EAV clause on ?e most recently precedes them) continue
        # to bind to the :pinned-commit fact, not the :ident fact.
        raw = _db_execute(
            db,
            "(query [:find ?ei ?sha ?vf ?vt "
            ":any-valid-time "
            ":where [?e :ident ?ei] "
            "[?e :pinned-commit ?sha] "
            "[?e :db/valid-from ?vf] "
            "[?e :db/valid-to ?vt] "
            f"{window_clauses}])"
        )
        rows = json.loads(raw).get("results", [])
    except Exception:
        return pinned
    for ident, sha, vf_ms, vt_ms in rows:
        if position_mode and not _fact_is_live_at_position(
            vf_ms, vt_ms, watermark_pos, ts_positions, stats
        ):
            continue
        pinned[ident] = (sha, _epoch_ms_to_iso(vf_ms))
    return pinned
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -k "PinnedCommits" -q`
Expected: all PASS, including the pre-existing `test_preload_pinned_commits_reloads_current_sha` and `test_preload_pinned_commits_returns_empty_on_query_failure`.

Then: `pytest tests/test_mcp_server.py -q` — Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Decide :pinned-commit preload membership by position

Same mechanism as :depends-on: both endpoints come from inverting the fact's
own :db/valid-from / :db/valid-to against full-history commit_metadata.

Exposure here is unmeasurable rather than zero -- the development repository
has 0 gitlink events in 610 commits, so it produces no :pinned-commit facts.
This ships on the shared mechanism, and the docstring says so.

Refs #245
Refs #238
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `_preload_known_entities` close side

**Files:**
- Modify: `mcp_server.py:7299-7476` (`_preload_known_entities`)
- Test: `tests/test_mcp_server.py` — new class after `TestPreloadPinnedCommitsPositionBound`

**Interfaces:**
- Consumes: `_fact_is_live_at_position`, `_epoch_ms_to_iso`, `_edn_escape`
- Produces: `_preload_known_entities(db, repo_path, valid_at=None, hash_to_pos=None, watermark_pos=None, ts_positions=None, t_hi_ms=None, stats=None) -> tuple` — return shape `(entity_valid_from, entity_descriptions, entity_introduced_by, file_entities, submodule_paths)` **unchanged**, `submodule_paths` **stays last** (`test_submodule_paths_stays_last` destructures with `*_, submodule_paths`)

**Structure:** the existing per-`entity_type` query and row loop become an inner `_collect(valid_at_str, accept)`. Phase 1 calls it once with the existing `valid_at`. Phase 2 identifies idents whose `:ident` interval closes above W, then re-runs `_collect` once per distinct closing instant at `ISO(vt - 1 ms)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_server.py`:

```python
class TestPreloadKnownEntitiesCloseSide:
    """#238's close-side residual, and the reason #245's observable loss was
    2 edges rather than 32.

    PR #246 made _preload_known_entities position-correct on its INTRODUCTION
    side only; its close side stayed a T_hi(W) date bound with the identical
    author-date inversion. On this repository the four modules deleted by
    df6b8be at position 124 (vulcan.py plus three test modules) have a close
    DATE below the envelope but a close POSITION above W, so they vanish from
    file_entities at positions 118-123 and take 30 misclassified :depends-on
    edges out of both sides of the diff before it is computed.

    Same linearization as TestPreloadKnownEntitiesPositionBound:
    0 = c0 @ 2026-04-01, 1 = c1 (WATERMARK) @ 2026-05-02, 2 = c2 @ 2026-04-26.
    """

    LINEARIZATION = ["c0" * 20, "c1" * 20, "c2" * 20]
    HASH_TO_POS = {h: i for i, h in enumerate(LINEARIZATION)}
    WATERMARK_POS = 1
    T_HI = "2026-05-02T00:00:00Z"
    TS_POSITIONS = {
        "2026-04-01T00:00:00Z": [0],
        "2026-05-02T00:00:00Z": [1],
        "2026-04-26T00:00:00Z": [2],
    }

    def _seed(self, real_db):
        """c0's commit facts stay open so the :introduced-by -> :date -> :hash
        join still resolves at the re-admission instant.

        closed_above_w: introduced at c0, CLOSED at c2's date. Close date
            2026-04-26 < T_hi 2026-05-02, so a :valid-at T_hi query misses it,
            but its close POSITION 2 is above W: still live at W. The
            vulcan.py shape.
        closed_below_w: introduced at c0, closed at c1's date, i.e. exactly at
            the envelope. Close position 1 <= W: genuinely dead at W.
        """
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z"} '
            f'[[:commit/c0 :hash "{self.LINEARIZATION[0]}"] '
            '[:commit/c0 :date "2026-04-01T00:00:00Z"]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-26T00:00:00Z"} '
            '[[:module/closed-above-w :entity-type :type/module] '
            '[:module/closed-above-w :ident ":module/closed-above-w"] '
            '[:module/closed-above-w :path "vulcan.py"] '
            '[:module/closed-above-w :description "vulcan.py"] '
            '[:module/closed-above-w :introduced-by :commit/c0]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-05-02T00:00:00Z"} '
            '[[:module/closed-below-w :entity-type :type/module] '
            '[:module/closed-below-w :ident ":module/closed-below-w"] '
            '[:module/closed-below-w :path "gone.py"] '
            '[:module/closed-below-w :description "gone.py"] '
            '[:module/closed-below-w :introduced-by :commit/c0]])'
        )

    def _run(self, real_db, tmp_path):
        import mcp_server
        return mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
            ts_positions=self.TS_POSITIONS,
            t_hi_ms=mcp_server._iso_to_epoch_ms(self.T_HI),
        )

    def test_entity_closed_above_the_watermark_is_readmitted(self, real_db, tmp_path):
        """Fails on master: the T_hi(W) date bound drops it."""
        self._seed(real_db)
        entity_valid_from, *_ = self._run(real_db, tmp_path)
        assert ":module/closed-above-w" in entity_valid_from

    def test_readmitted_entity_carries_its_path_and_description(self, real_db, tmp_path):
        """The re-admission pass must produce full rows, not bare idents --
        file_entities is what _preload_known_deps' ident_to_file narrows
        against, and it is where the 30 edges were lost."""
        self._seed(real_db)
        _vf, descriptions, _ib, file_entities, _sp = self._run(real_db, tmp_path)
        assert descriptions[":module/closed-above-w"] == "vulcan.py"
        assert ":module/closed-above-w" in file_entities["vulcan.py"]

    def test_readmitted_entity_carries_its_introducing_commit(self, real_db, tmp_path):
        """#231's retract value must survive re-admission."""
        self._seed(real_db)
        _vf, _d, entity_introduced_by, _fe, _sp = self._run(real_db, tmp_path)
        assert entity_introduced_by[":module/closed-above-w"] == (
            f":commit/{self.LINEARIZATION[0][:12]}"
        )

    def test_entity_closed_at_or_below_the_watermark_stays_excluded(
        self, real_db, tmp_path
    ):
        """Re-admission must not become an unconditional add-back."""
        self._seed(real_db)
        entity_valid_from, *_ = self._run(real_db, tmp_path)
        assert ":module/closed-below-w" not in entity_valid_from

    def test_readmission_is_position_gated_not_date_gated(self, real_db, tmp_path):
        """The close-side twin of test_the_envelope_alone_does_not_close_the
        _hole. Without ts_positions/t_hi_ms there is no phase 2 at all, so the
        entity stays lost -- proving re-admission comes from the position
        rule and not from a widened date bound."""
        import mcp_server
        self._seed(real_db)
        entity_valid_from, *_ = mcp_server._preload_known_entities(
            real_db, str(tmp_path), valid_at=self.T_HI,
            hash_to_pos=self.HASH_TO_POS, watermark_pos=self.WATERMARK_POS,
        )
        assert ":module/closed-above-w" not in entity_valid_from

    def test_reintroduced_entity_keeps_its_current_values(self, real_db, tmp_path):
        """An ident closed below W and re-introduced below W is live via phase
        1. Phase 2 must not overwrite it with the historical interval."""
        import mcp_server
        self._seed(real_db)
        real_db.execute(
            '(transact {:valid-from "2026-04-01T00:00:00Z" '
            ':valid-to "2026-04-20T00:00:00Z"} '
            '[[:module/recycled :entity-type :type/module] '
            '[:module/recycled :ident ":module/recycled"] '
            '[:module/recycled :path "recycled.py"] '
            '[:module/recycled :description "OLD"] '
            '[:module/recycled :introduced-by :commit/c0]])'
        )
        real_db.execute(
            '(transact {:valid-from "2026-04-26T00:00:00Z"} '
            '[[:module/recycled :entity-type :type/module] '
            '[:module/recycled :ident ":module/recycled"] '
            '[:module/recycled :path "recycled.py"] '
            '[:module/recycled :description "NEW"] '
            '[:module/recycled :introduced-by :commit/c0]])'
        )
        _vf, descriptions, _ib, _fe, _sp = self._run(real_db, tmp_path)
        assert descriptions[":module/recycled"] == "NEW"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k TestPreloadKnownEntitiesCloseSide -q`
Expected: FAIL with `TypeError: _preload_known_entities() got an unexpected keyword argument 'ts_positions'`.

- [ ] **Step 3: Write the implementation**

Signature:

```python
def _preload_known_entities(
    db: Any,
    repo_path: str,
    valid_at: Optional[str] = None,
    hash_to_pos: Optional[Dict[str, int]] = None,
    watermark_pos: Optional[int] = None,
    ts_positions: Optional[Dict[str, List[int]]] = None,
    t_hi_ms: Optional[int] = None,
    stats: Optional[Dict[str, int]] = None,
) -> tuple:
```

Replace the docstring's `The close END is not recoverable at all…` paragraph (lines 7333-7340) and the `:depends-on and :pinned-commit carry no commit reference…` paragraph (7353-7356) with:

```
    The close END is recovered too, as of #245's work, but by a different
    route: _ingest_close holds no reference to the closing commit, yet it
    records valid_to = commit_ts_iso, so the closing POSITION comes from
    inverting that instant against commit_metadata (_fact_is_live_at_position).
    An earlier version of this docstring called the close end "not recoverable
    at all"; that was true of joins and false of inversion.

    valid_at survives as phase 1's bound and is fed the monotone envelope
    T_hi(W) = max(ts[0..W]). It no longer carries a safety property in either
    direction: phase 2 below re-admits the entities it drops, gated on
    position. Passing ts_positions/t_hi_ms is what enables phase 2 --
    test_readmission_is_position_gated_not_date_gated pins that the
    re-admission comes from the position rule and not from a widened date
    bound, which is the "add-back union" #238 forbids.

    _preload_known_deps and _preload_pinned_commits are position-filtered the
    same way (#245). This function's close side is what made their exposure
    look 16x smaller than it is: four modules deleted above the watermark with
    an inverted close date dropped out of file_entities, taking 30
    misclassified :depends-on edges with them before any diff was computed.
```

Body — replace everything from `valid_at_clause = …` (line 7402) through the `return` with:

```python
    def _collect(valid_at_str: Optional[str], accept) -> None:
        """Run the structural preload query at one instant and fold accepted
        rows into the shared output dicts.

        accept(ident) -> bool selects rows; the introduction position clause
        is applied here, so it governs phase 1 and every re-admission pass
        alike.
        """
        valid_at_clause = (
            f':valid-at "{_edn_escape(valid_at_str)}" ' if valid_at_str else ""
        )
        for entity_type in (
            "module", "function", "class", "variable", "field",
            "external-dependency",
        ):
            path_attr = (
                "path" if entity_type in ("module", "external-dependency")
                else "file"
            )
            try:
                raw = _db_execute(
                    db,
                    f'(query [:find ?ident ?path ?desc ?date ?hash '
                    f'{valid_at_clause}'
                    f':where [?e :entity-type :type/{entity_type}] '
                    f'[?e :ident ?ident] '
                    f'[?e :{path_attr} ?path] '
                    f'[?e :description ?desc] '
                    f'[?e :introduced-by ?c] '
                    f'[?c :date ?date] '
                    f'[?c :hash ?hash]])',
                )
                rows = json.loads(raw).get("results", [])
                for ident, path, desc, date, hash_ in rows:
                    if not accept(ident):
                        continue
                    # #238: the resume bound, POSITION-indexed. CONJUNCTIVE
                    # over every row, in every pass. Wrong-INCLUSION (the
                    # unrecoverable direction) is caused solely by the
                    # introduction end, which this closes exactly. Never turn
                    # this into an "add-back" branch beside a date bound.
                    #
                    # pos is None means the introducing commit is not in this
                    # linearization (a rewritten or foreign history): exclude,
                    # which is the benign direction.
                    if watermark_pos is not None:
                        pos = (
                            hash_to_pos.get(hash_)
                            if hash_to_pos is not None else None
                        )
                        if pos is None or pos > watermark_pos:
                            continue
                    entity_valid_from[ident] = date
                    entity_descriptions[ident] = desc
                    # Reconstructed from ?hash, not read off a bound ?c:
                    # ?c is a SUBJECT variable here, and binding a subject in
                    # :find position returns minigraf's internal UUID, not the
                    # keyword ident string -- verified empirically for ?c
                    # specifically (adding it to :find returns a UUID like
                    # "bd5e9774-8fbd-5ec4-81e8-7310073fa5c3"), the same reason
                    # _preload_known_deps binds ?srci and
                    # _preload_pinned_commits binds ?ei. This value becomes
                    # the :introduced-by retract value at close time (#231),
                    # so it must stay byte-for-byte identical to the
                    # commit_ident both write sites build;
                    # test_entity_introduced_by_matches_commit_write_site
                    # pins that against the real write sites.
                    entity_introduced_by[ident] = f":commit/{hash_[:12]}"
                    file_entities.setdefault(path, [])
                    if ident not in file_entities[path]:
                        file_entities[path].append(ident)
                    if entity_type == "external-dependency":
                        submodule_paths[ident] = path
            except Exception:
                pass

    # Phase 1: everything live at the envelope.
    _collect(valid_at, lambda ident: True)

    # Phase 2 (#238 close side, #245): entities phase 1 missed because their
    # close DATE fell at or below the envelope while their close POSITION sits
    # above the watermark.
    #
    # `[(<= ?vt t_hi_ms)]` is NOT a sound bound in isolation -- a close above W
    # can carry an arbitrarily early date, which is the defect. It is sound
    # only as the COMPLEMENT of phase 1: an entity missing from a
    # :valid-at T_hi(W) query has either vt <= T_hi(W) or vf > T_hi(W), and
    # vf > T_hi(W) implies intro_pos > W (every position at or below W has a
    # date at or below the envelope) and is correctly excluded. Never lift
    # this clause into a standalone query.
    #
    # Only :ident's own window is bound here, not :path or :description:
    # :ident is written once per entity lifetime, so this is roughly one
    # interval per entity, while :description is rewritten on every body edit
    # and would explode the row count under :any-valid-time.
    position_mode = (
        watermark_pos is not None
        and ts_positions is not None
        and t_hi_ms is not None
    )
    if position_mode:
        readmit: Dict[str, int] = {}
        for entity_type in (
            "module", "function", "class", "variable", "field",
            "external-dependency",
        ):
            try:
                raw = _db_execute(
                    db,
                    f'(query [:find ?ident ?vf ?vt :any-valid-time '
                    f':where [?e :entity-type :type/{entity_type}] '
                    f'[?e :ident ?ident] '
                    f'[?e :db/valid-from ?vf] '
                    f'[?e :db/valid-to ?vt] '
                    f'[(<= ?vt {t_hi_ms})]])',
                )
                for ident, vf_ms, vt_ms in json.loads(raw).get("results", []):
                    if ident in entity_valid_from:
                        continue
                    # vf here is the INTERVAL's own start, used for interval
                    # selection among an ident's several lifetimes. The
                    # AUTHORITATIVE introduction gate is _collect's
                    # :introduced-by position clause, applied independently to
                    # every re-admitted row below. Both gates apply.
                    if not _fact_is_live_at_position(
                        vf_ms, vt_ms, watermark_pos, ts_positions, stats
                    ):
                        continue
                    readmit[ident] = vt_ms
            except Exception:
                pass

        # One re-admission pass per DISTINCT closing instant, at the last
        # instant those entities were live. On the repository this was
        # developed against that is a single extra query, for df6b8be.
        for close_ms in sorted(set(readmit.values())):
            _collect(
                _epoch_ms_to_iso(close_ms - 1),
                lambda ident, _c=close_ms: (
                    readmit.get(ident) == _c and ident not in entity_valid_from
                ),
            )

    return (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    )
```

Leave the `git ls-files` pre-seed block (lines 7386-7396) exactly where it is, above `_collect`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -k "PreloadKnownEntities" -q`
Expected: all PASS, including the whole pre-existing `TestPreloadKnownEntitiesPositionBound` class — especially `test_the_envelope_alone_does_not_close_the_hole`, `test_unknown_hash_is_excluded` and `test_submodule_paths_stays_last`.

Then: `pytest tests/test_mcp_server.py -q` — Expected: no new failures.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Re-admit entities closed above the watermark by close position

PR #246 position-indexed this preload's introduction side only; its close side
stayed a T_hi(W) date bound with the identical author-date inversion. Four
modules deleted above the watermark with an inverted close date dropped out of
file_entities entirely, taking 30 misclassified :depends-on edges with them
before any diff was computed -- which is why #245's observable loss read as 2
edges rather than 32.

A second pass binds only the :ident fact's window, selects the interval live
at W by position, and re-runs the structural query at the last instant those
entities were live. The introduction position clause gates every re-admitted
row, so this admits nothing the position rule rejects.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire `_load_ingestion_preload_state` and announce unplaceable facts

**Files:**
- Modify: `mcp_server.py:7904-8020` (`_load_ingestion_preload_state`)
- Test: `tests/test_mcp_server.py` — new class after `TestPreloadKnownEntitiesCloseSide`

**Interfaces:**
- Consumes: `_build_ts_positions`, `_resume_envelope`, `_iso_to_epoch_ms`, all three modified preloads
- Produces: no signature change to `_load_ingestion_preload_state` itself

**The `t_hi_ms` guard is load-bearing.** `t_hi_ms` must be derived from `_resume_envelope(commit_metadata, watermark_pos)` **before** the existing `if entity_valid_at is None: entity_valid_at = resume_valid_at` fallback. When `watermark_pos is None` but a watermark exists (absent from this linearization), `resume_valid_at` is a real `ts(W)`; letting that become `t_hi_ms` would hand the deps and pins queries a widened prefilter with the position filter disabled — precisely the widening #245 forbids.

- [ ] **Step 1: Write the failing test**

```python
class TestPreloadStateUnmappableAnnounce:
    """An unplaceable :db/valid-from or :db/valid-to means the position
    inversion's assumption is broken for that fact. Excluding is the
    recoverable direction, but it must not be silent -- the same reasoning
    _commit_date_query applies when a non-empty watermark has no :date."""

    def test_unmappable_close_is_announced_on_stderr(self, real_db, capsys):
        import mcp_server
        ts_positions = {"2026-04-01T00:00:00Z": [0], "2026-05-02T00:00:00Z": [1]}
        stats = {}
        mcp_server._fact_is_live_at_position(
            mcp_server._iso_to_epoch_ms("2026-04-01T00:00:00Z"),
            mcp_server._iso_to_epoch_ms("2026-06-15T00:00:00Z"),
            1, ts_positions, stats,
        )
        mcp_server._announce_unplaceable_facts(stats)
        assert "unplaceable" in capsys.readouterr().err

    def test_clean_stats_announce_nothing(self, real_db, capsys):
        import mcp_server
        mcp_server._announce_unplaceable_facts({"collisions": 3})
        assert capsys.readouterr().err == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py -k TestPreloadStateUnmappableAnnounce -q`
Expected: FAIL with `AttributeError: module 'mcp_server' has no attribute '_announce_unplaceable_facts'`.

- [ ] **Step 3: Write the implementation**

Add after `_fact_is_live_at_position`:

```python
def _announce_unplaceable_facts(stats: Dict[str, int]) -> None:
    """Report facts whose valid-time matched no commit in the linearization.

    Not a hard failure: aborting an ingestion because one fact is unplaceable
    is worse than excluding it, and the ordinary rewritten-history case is
    already handled by watermark_pos falling to None, which disables position
    filtering wholesale. But a silent exclusion here would look exactly like
    the bug #245 fixed, so it is announced -- the same reasoning
    _commit_date_query uses when a non-empty watermark has no :date.

    Collisions are NOT announced: they are expected at second granularity and
    are resolved deterministically toward exclusion.
    """
    intro = stats.get("unmappable_intro", 0)
    close = stats.get("unmappable_close", 0)
    if not intro and not close:
        return
    print(
        f"[ingest] preload: {intro} unplaceable :db/valid-from and {close} "
        "unplaceable :db/valid-to facts -- their instants match no commit in "
        "this linearization, so they were excluded from the resume state "
        "(#245). Expect duplicate introductions rather than data loss.",
        file=sys.stderr,
    )
```

In `_load_ingestion_preload_state`, replace the bound derivation and the three calls:

```python
    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    watermark_pos = hash_to_pos.get(watermark) if watermark is not None else None
    ts_positions = _build_ts_positions(commit_metadata)

    # #238/#245: membership at all four sites is decided by POSITION.
    #
    # t_hi_ms is derived from _resume_envelope BEFORE entity_valid_at's
    # fallback below, and stays None whenever watermark_pos is None. That
    # guard is load-bearing: a watermark that exists but is absent from this
    # linearization leaves resume_valid_at a real ts(W) while disabling the
    # position filter, and letting that become t_hi_ms would hand the deps and
    # pins queries a WIDENED prefilter with no position clause -- exactly the
    # widening #245 forbids. With t_hi_ms None they keep the ts(W) date
    # window, which is strictly no worse than today.
    #
    # resume_valid_at survives for _preload_unresolved_dep_idents only.
    resume_valid_at = _commit_date_query(db, watermark)
    resume_valid_at_ms = _iso_to_epoch_ms(resume_valid_at)
    entity_valid_at = _resume_envelope(commit_metadata, watermark_pos)
    t_hi_ms = _iso_to_epoch_ms(entity_valid_at)
    if entity_valid_at is None:
        entity_valid_at = resume_valid_at
    position_stats: Dict[str, int] = {}
    prior_ingested = _count_commit_entities(db)
    (
        entity_valid_from, entity_descriptions, entity_introduced_by,
        file_entities, submodule_paths,
    ) = _preload_known_entities(
        db, repo_path, valid_at=entity_valid_at,
        hash_to_pos=hash_to_pos, watermark_pos=watermark_pos,
        ts_positions=ts_positions, t_hi_ms=t_hi_ms, stats=position_stats,
    )
    file_deps, dep_valid_from = _preload_known_deps(
        db, file_entities, valid_at_ms=resume_valid_at_ms,
        ts_positions=ts_positions, watermark_pos=watermark_pos,
        t_hi_ms=t_hi_ms, stats=position_stats,
    )
    pinned_commit_state = _preload_pinned_commits(
        db, valid_at_ms=resume_valid_at_ms,
        ts_positions=ts_positions, watermark_pos=watermark_pos,
        t_hi_ms=t_hi_ms, stats=position_stats,
    )
    _announce_unplaceable_facts(position_stats)
```

Leave `_preload_field_class_idents`, `_preload_field_static_idents` and `_preload_unresolved_dep_idents` untouched.

Confirm `sys` is imported at module scope; if not, use the file's existing stderr-printing idiom from `_commit_date_query` instead.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_mcp_server.py -k TestPreloadStateUnmappableAnnounce -q`
Expected: PASS.

Then the full suite: `pytest tests/test_mcp_server.py -q` — Expected: no new failures. Pay particular attention to `TestRunIngestionShutdown` and any test calling `_load_ingestion_preload_state`.

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Wire position-exact bounds through the ingestion preload state

t_hi_ms is derived from _resume_envelope before entity_valid_at's fallback and
stays None whenever watermark_pos is None. A watermark absent from this
linearization would otherwise widen the deps and pins prefilter while the
position filter is disabled -- the widening #245 forbids.

Facts whose valid-time matches no commit are excluded and announced rather
than silently dropped.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: End-to-end resume regression test

**Files:**
- Modify: `tests/test_mcp_server.py:12475-12561` — add `_inverted_author_date_repo_with_deps` beside the existing `_inverted_author_date_repo`
- Modify: `tests/test_mcp_server.py:19641+` — new class beside `TestResumeWithInvertedAuthorDates`

**Interfaces:**
- Consumes: the whole wired path from Task 5; `TestResumeWithInvertedAuthorDates._run_resume`'s two-run drive as the model

**Why this task exists separately:** #238 requires it explicitly — *"any regression test for this needs to construct the resume explicitly, not rely on a fresh ingestion."* Every per-task review during #222 phase 2d saw only fresh runs and structurally could not observe the bug. Tasks 1–5 test the preloads in isolation; this is the only test that exercises a real interrupted-and-resumed ingestion.

**Do not build a fresh fixture — PR #246 already built most of it.** `_inverted_author_date_repo` (`tests/test_mcp_server.py:12475`) is a five-commit repo whose author dates and topological order disagree, and `TestResumeWithInvertedAuthorDates` (19641) already drives the exact interrupted two-stream resume this needs. Read both docstrings in full before writing anything: they encode a lot of hard-won detail, including why a forward-only resume **cannot** reach this class of bug at all, and why run 1 uses ratio `2:1000000` and stops at `processed == 4`.

**What that fixture lacks** for #245 and the close side: no `:depends-on` edge, and no module deletion above the watermark.

**Do not add commits to `_inverted_author_date_repo`.** `_run_resume` asserts `processed == 4` and depends on which commits each stream claims; changing the commit count breaks all three existing tests. Add files to the **existing** commits in a **copy** of the builder instead.

- [ ] **Step 1: Add the extended fixture**

Add beside `_inverted_author_date_repo`, keeping the same five commits, the same author/committer dates, and the same topological shape:

```python
def _inverted_author_date_repo_with_deps(path):
    """_inverted_author_date_repo plus the two shapes #245 and #238's close
    side need, added to the EXISTING five commits rather than as new ones --
    TestResumeWithInvertedAuthorDates._run_resume asserts processed == 4 and
    depends on which commits each stream claims, so the commit count must not
    move.

        pos 0  c0  + dep_src.py (imports doomed) and doomed.py   @ 2026-04-01
        pos 1  c1  mid.py                                        @ 2026-05-02  <- W
        pos 2  c2  late.py modified                              @ 2026-04-20
        pos 3  c3  late.py + late_fn, and DELETES doomed.py      @ 2026-04-26  <- above W, dated earlier
        pos 4  c4  base.py + base_fn2                            @ 2026-04-27

    doomed.py's module entity and the dep_src -> doomed edge both CLOSE at c3:
    close position 3 is above W, while the close DATE 2026-04-26 is below the
    envelope T_hi(W) = 2026-05-02. That is exactly the four-modules-deleted-by-
    df6b8be shape #238's close-side residual was measured on, and the edge is
    the :depends-on case #245 is about.
    """
```

Body: copy `_inverted_author_date_repo`'s `commit()` helper verbatim, then:

- c0 also writes `doomed.py` (`def doomed_fn():\n    return 7\n`) and `dep_src.py` (`import doomed\n\ndef src_fn():\n    return doomed.doomed_fn()\n`)
- c3 additionally removes `doomed.py`. The `commit()` helper only writes files, so add an explicit `git rm doomed.py` before the commit — mirror the existing `_subprocess.run([...], cwd=path, check=True, capture_output=True)` style.

Leave `_inverted_author_date_repo` untouched.

- [ ] **Step 2: Establish the precondition before asserting on it**

**This step is not optional and its outcome changes the rest of the task.** The bug needs the close to be durably in the graph *before run 2's preload runs*. Run 1 is interrupted before Stage B, and it is **not established** that the interrupted two-stream drive durably records c3's deletion of `doomed.py`. Do not assume it does.

Write a temporary diagnostic that runs `_run_resume`'s run 1 against the new fixture, reopens the graph, and prints:

```python
# Does a CLOSED :ident interval for doomed.py exist above W after run 1?
'(query [:find ?i ?vf ?vt :any-valid-time '
':where [?e :ident ?i] [?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])'
```

- **If a closed interval for `:module/doomed-py` is present** with `vt` = c3's instant: the precondition holds. Proceed to Step 3 as written.
- **If it is absent**: the interrupted run does not produce the state. Record that finding in the test's docstring, and instead drive **three** runs — run 1 interrupted as today, a second interrupted run long enough for Stage B to apply c3's lifecycle, then the resume under test. Adjust the sleep-count stop point the way `_run_resume`'s `stop_after_fourth` does, and keep the `if t: await original_sleep(t); return` guard — without it, `_ensure_db_async`'s lock-contention backoff consumes counts and the run stops a commit early, which was a real CI-only failure on this fixture.

Do **not** seed the close by hand-transacting into the graph between runs. That would make this an expensive restatement of Task 4's unit test rather than an end-to-end one.

- [ ] **Step 3: Write the test**

```python
class TestResumeWithInvertedAuthorDatesAndDeps:
    """#238's close side and #245, end-to-end on a resumed run.

    Sibling of TestResumeWithInvertedAuthorDates, which covers the
    INTRODUCTION side. Read that class's docstring first -- the run-1 stream
    ratio, the stop point, and why a forward-only resume cannot reach this
    class of bug at all are all explained there and all apply here.

    Both defects here are wrong-EXCLUSION: doomed.py and its incoming
    :depends-on edge close ABOVE the watermark with a date BELOW the envelope,
    so a date-bounded preload cannot see them at W. The resumed forward walk
    then replays c2, where dep_src.py still imports doomed, and treats an
    already-standing dependency as newly introduced.
    """

    _progress = TestResumeWithInvertedAuthorDates._progress
    _results = TestResumeWithInvertedAuthorDates._results
    _run_resume = TestResumeWithInvertedAuthorDates._run_resume

    @pytest.mark.asyncio
    async def test_resumed_run_preserves_the_standing_dep_edge_valid_from(
        self, tmp_path, monkeypatch
    ):
        """#245's measured harm, and the assertion is on :valid-from rather
        than on edge existence -- the edge exists in both the broken and the
        fixed graph. What the bug destroys is WHEN it was introduced:
        `current_deps - previous_deps` treats the edge as new at c2 and
        overwrites c0's timestamp with c2's."""
        import mcp_server
        repo = _inverted_author_date_repo_with_deps(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        db = await self._run_resume(repo, graph, monkeypatch)

        src = mcp_server._code_ident("module", "dep_src.py")
        rows = self._results(
            db,
            '(query [:find ?vf :any-valid-time '
            f':where [?e :ident "{src}"] '
            '[?e :depends-on ?dep] '
            '[?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])',
        )
        assert rows, "the dep_src -> doomed edge vanished entirely"
        earliest = min(int(r[0]) for r in rows)
        assert mcp_server._epoch_ms_to_iso(earliest).startswith("2026-04-01"), (
            "the standing :depends-on edge was re-introduced at the replayed "
            f"gap commit instead of keeping c0's :valid-from (#245): {rows}"
        )

    @pytest.mark.asyncio
    async def test_resumed_run_writes_no_inverted_valid_interval(
        self, tmp_path, monkeypatch
    ):
        """The general corruption signature both issues produce. A fact whose
        window closes BEFORE it opens is unrecoverable, and it is what a
        wrongly-preloaded entity produces when it is closed at a commit
        earlier than its own introduction."""
        repo = _inverted_author_date_repo_with_deps(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        db = await self._run_resume(repo, graph, monkeypatch)

        import mcp_server
        rows = self._results(
            db,
            '(query [:find ?i ?vf ?vt :any-valid-time '
            ':where [?e :ident ?i] '
            '[?e :db/valid-from ?vf] [?e :db/valid-to ?vt]])',
        )
        inverted = [
            r for r in rows
            if int(r[2]) < mcp_server._VALID_TIME_FOREVER_MS
            and int(r[2]) < int(r[1])
        ]
        assert inverted == [], f"inverted valid intervals written: {inverted}"

    @pytest.mark.asyncio
    async def test_resumed_run_mints_no_duplicate_introduced_by(
        self, tmp_path, monkeypatch
    ):
        """The wrong-EXCLUSION consequence: a module missing from the preload
        takes _build_code_triples' introduction branch on replay and gains a
        second live :introduced-by.

        Recoverable via #235's correction sweep, so this is the softer of the
        two assertions -- but it is the one that fails FIRST when the close
        side regresses, because it does not need the close to be applied to a
        dep edge, only to the module."""
        repo = _inverted_author_date_repo_with_deps(tmp_path / "repo")
        graph = str(repo / "memory.graph")
        db = await self._run_resume(repo, graph, monkeypatch)

        rows = self._results(
            db, '(query [:find ?e (count ?c) :where [?e :introduced-by ?c]])'
        )
        assert [r for r in rows if int(r[1]) > 1] == []
```

- [ ] **Step 4: Ablate — mandatory, and the results go in the commit body**

Run: `pytest tests/test_mcp_server.py -k TestResumeWithInvertedAuthorDatesAndDeps -q`
Expected: PASS on this branch.

Then prove each test has power. For each of the three, revert the relevant production change and confirm it fails:

```bash
git stash push mcp_server.py   # keep the tests, drop the fix
pytest tests/test_mcp_server.py -k TestResumeWithInvertedAuthorDatesAndDeps -q
git stash pop
```

**Record which of the three actually fail, and which do not.** A test that passes with and without the fix guards nothing, and this project has already shipped a branch whose four regression tests claimed guarantees they did not provide. `TestResumeWithInvertedAuthorDates`' own docstring is the model here: it states plainly that one of its three tests cannot fail under any ablation of the clause it nominally covers, and keeps it anyway for what it documents. Do the same — do not quietly drop a test that turns out to have no power, and do not claim power it does not have.

- [ ] **Step 5: Run the full suite**

Run: `pytest tests/test_mcp_server.py -q`
Expected: no new failures. In particular all three pre-existing `TestResumeWithInvertedAuthorDates` tests must still pass — if any broke, `_inverted_author_date_repo` was modified when it should not have been.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "$(cat <<'EOF'
Add the explicit-resume regression test for the close side and dep edges

#238 requires a test that constructs the resume rather than relying on a fresh
ingestion: every per-task review during #222 phase 2d saw only fresh runs and
structurally could not observe this bug.

Extends PR #246's _inverted_author_date_repo with a :depends-on edge and a
module deleted above the watermark with an earlier author date -- the
four-modules-deleted-by-df6b8be shape -- without changing its commit count,
which _run_resume's processed == 4 assertion depends on.

Ablation results, per test, recorded here rather than claimed in aggregate:
<fill in which of the three fail without the production change, and which do
not, following TestResumeWithInvertedAuthorDates' own precedent of stating
plainly that one of its tests has no power against the clause it covers>.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Probe acceptance mode and run

**Files:**
- Modify: `evals/at_scale/probe_dep_preload_exposure.py`
- Create: `evals/at_scale/results/245-dep-preload-exposure-fixed.json`

**Interfaces:**
- Consumes: the fixed preloads
- Produces: an acceptance record; the pre-fix `245-dep-preload-exposure.json` stays as the baseline and is **not** overwritten

**Read this before writing the mode.** Once the close side is position-correct, the probe's NARROW and WIDE framings **converge by construction** — narrow uses `_preload_known_entities`' output, wide rebuilds it position-correctly, and after Task 4 those are the same set. Convergence is the expected result and is itself the finding to report; it is not independent corroboration. Say so in the probe's output and in the results JSON.

Likewise, the oracle and the fix now share an algorithm, so agreement checks the **plumbing** — the fix runs inside `_load_ingestion_preload_state` with only W in hand, through the real queries, the real `entity_type` loop and the real `ident_to_file` narrowing, while the oracle runs offline with the whole history — not the algorithm. Tasks 1–6 are the non-circular evidence.

- [ ] **Step 1: Add the acceptance mode**

The probe's driver is `sweep()` (line 457), which at each affected position calls the real `_preload_known_entities` and `_preload_known_deps` and diffs them against `position_exact_live_edges` (229). `main()` (752) parses args and prints the report; `build_ts_positions` (65), `invert_ms_to_positions` (163) and `edge_live_at` (179) are the oracle primitives and **stay as they are**.

Add a `--verify-fix` flag threaded from `main()` into `sweep()`. When set, `sweep()` passes the position arguments to both preloads — `ts_positions=build_ts_positions(commit_metadata)`, `watermark_pos=w`, `t_hi_ms=mcp_server._iso_to_epoch_ms(envelopes[w])`, plus `hash_to_pos` as today — instead of the date-only arguments it passes now. The oracle side is unchanged.

Then assert, at every affected position:

- `wrongly_excluded == 0` and `wrongly_included == 0`, in both framings
- `timestamp_collisions == 0` — **without this the comparison is invalid**, because the fix and the oracle resolve collisions in opposite directions and may legitimately disagree
- all four unmappable counters at 0 (`unmappable_valid_from_facts`, `unmappable_valid_to_facts`, `unmappable_module_path_valid_from`, `unmappable_module_path_valid_to`)

`main()` already exits nonzero on nonzero unmappable counters (line ~865) with an "INVALID MEASUREMENT" message — extend that same gate rather than adding a second one.

Report the narrow/wide convergence explicitly in the printed output and in the JSON, as a named key (e.g. `"framings_converged": true`) rather than leaving it for a reader to infer from two equal numbers.

- [ ] **Step 2: Correct the two stale docstrings**

The module docstring's *"the oracle below is NOT a candidate fix"* (lines 11–12) and `position_exact_live_edges`' *"This works only because the entire history is in hand at analysis time… A resuming forward walk has no such thing"* (lines 239–241).

Both were correct against the pre-#246 shape the probe was conceived against, and are false against current master: `build_linearization` and `_git_commits(repo_path, None, branch)` now run above the preload block. Replace with a note saying the inversion **is** the basis of the fix as of #245, and that `edge_live_at`'s ambiguity policy remains deliberately opposite to `_position_of_valid_time`'s and must not be shared.

- [ ] **Step 3: Run the acceptance**

```bash
python evals/at_scale/probe_dep_preload_exposure.py --verify-fix \
  --output evals/at_scale/results/245-dep-preload-exposure-fixed.json
```

Full forward ingestion of ~610 commits — budget for a long run. If any counter is nonzero, **stop and report**; do not adjust the assertion to make it pass.

- [ ] **Step 4: Commit**

```bash
git add evals/at_scale/probe_dep_preload_exposure.py \
        evals/at_scale/results/245-dep-preload-exposure-fixed.json
git commit -m "$(cat <<'EOF'
Add the probe's fixed-preload acceptance mode and record the run

Drives the position-filtered preloads at every affected watermark position and
requires zero wrong exclusions and zero wrong inclusions in both framings,
with zero collisions and zero unplaceable facts.

Recorded as partly self-referential: the oracle and the fix now share an
algorithm, so this checks the plumbing rather than the algorithm, and the
narrow/wide framings converge by construction once the close side is correct.
The unit and resume tests are the non-circular evidence.

Corrects two docstrings asserting the inversion could not be a fix. True
before PR #246 moved the linearization above the preload block; false after.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Record corrections and the follow-up issue

**Files:**
- Modify: `docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md`
- Modify: `mcp_server.py` — `_preload_known_entities` docstring only

- [ ] **Step 1: Add a revision section to the #238/#231 spec**

Append a `## Revision — 2026-08-10` section, following how `2026-07-31-reverse-walk-write-amplification-design.md` records its own corrections (revision section, not an edit in place). It must state:

- The "The close END is not recoverable at all" claim and the `_preload_known_deps and _preload_pinned_commits` / **Unchanged** section are superseded.
- Why: the close position is recoverable by inverting `valid_to`, and full-history `commit_metadata` became available at preload time in that spec's own "Call-order change in `_run_ingestion`" section — the fix was unlocked by the very change that shipped alongside the claim.
- The residual it listed as "deliberately accepted" is now closed.

- [ ] **Step 2: File the value-staleness follow-up issue**

`entity_descriptions` still takes whichever `:description` version was live at date `T_hi(W)`, which can be a version written above W with an inverted author date. The forward walk uses that dict for body-change detection, so a from-the-future description makes a real change at W+1 compare equal and never be recorded.

The issue body must carry: the mechanism, that the exposure is **unmeasured** (the probe measured membership only), why it was scoped out (per-attribute interval inversion over an attribute rewritten on every body edit, against a project rule to measure before believing a mechanism), and a sketch of how to measure it by extending the probe.

Write the body to the scratchpad first, then:

```bash
gh issue create \
  --title "bug: entity preload's :description values are date-bounded, not position-bounded" \
  --body-file /tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/32f595b9-0f88-4780-aba4-63f2aabc0ca9/scratchpad/value-staleness-issue.md
```

Use `Refs #238` and `Refs #222` in the body. **No closing keyword.** Note the issue number it returns — Step 3 needs it.

- [ ] **Step 3: Point the docstring at it, using the number from Step 2**

Add to `_preload_known_entities`' docstring, under the existing residual discussion:

```
    Residual, deliberately scoped out and tracked as #<N>: entity_descriptions
    still carries whichever :description version was live at DATE T_hi(W),
    which can be a version written above W with an inverted author date. The
    forward walk uses this dict for body-change detection, so a
    from-the-future description makes a real change compare equal and go
    unrecorded. Membership is position-exact; values are not. Unmeasured.
```

- [ ] **Step 4: Verify no closing keyword leaked into any commit**

```bash
git log master..HEAD --format='%B' | grep -inE '(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed))[[:space:]]+#[0-9]+' || echo "clean"
```

Expected: `clean`. Any hit must be rewritten before the push — this has bitten the project twice, most recently via a *negated* keyword.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-08-04-position-indexed-preload-design.md mcp_server.py
git commit -m "$(cat <<'EOF'
Record the superseded close-side claim and the value-staleness residual

The #238/#231 spec's "the close END is not recoverable at all" is superseded:
the close position comes from inverting valid_to, and the full-history
commit_metadata that makes it possible arrived in that same spec's own
call-order change.

Membership is now position-exact at all four preload sites. Attribute VALUES
are not -- entity_descriptions still reads at date T_hi(W). Filed separately,
unmeasured, and named in the docstring.

Refs #238
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification before the PR

- [ ] `pytest tests/test_mcp_server.py -q` — full suite green
- [ ] Task 6's ablation re-confirmed on the final tree
- [ ] Task 7's acceptance JSON committed with all counters at zero
- [ ] `git log master..HEAD --format='%B' | grep -inE '(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed))[[:space:]]+#[0-9]+'` returns nothing
- [ ] PR body carries `Closes #238`, `Closes #245`, `Refs #222` — and nothing that closes #222
- [ ] `master` requires an approving review on top of green CI; ask before any `--admin` bypass
