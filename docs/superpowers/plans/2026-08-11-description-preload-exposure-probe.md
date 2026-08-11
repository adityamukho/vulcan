# #257 `:description` Preload Exposure Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether `_preload_known_entities`' date-bounded `:description` seeding ever returns a value a position-bounded query would not, on this repository's real commit history.

**Architecture:** A read-only probe module in `evals/at_scale/` that reuses the #245 probe's pure timestamp-to-position primitives. Stage 1 censuses how many idents carry more than one distinct `:description` value across all time — if zero, the mechanism cannot fire. Stage 2 drives the real, shipped `_preload_known_entities` at each structurally affected watermark position and diffs the `entity_descriptions` it returns against a position-correct oracle. Nothing in `mcp_server.py` changes.

**Tech Stack:** Python 3, `minigraf` (Datalog graph, bi-temporal), pytest, the existing `evals/at_scale/probe_dep_preload_exposure.py` primitives.

**Spec:** `docs/superpowers/specs/2026-08-11-description-preload-exposure-probe-design.md`

## Global Constraints

- **ALWAYS use `.venv/bin/python`.** System python has minigraf 1.1.1 against a `minigraf>=1.2.3` floor; it fakes 122 test failures, runs queries ~7x slower, and has already produced one wrong diagnosis on this project. Every command in this plan uses it.
- **Single-handle invariant.** At most one live `MiniGrafDb` handle per process. Reuse `mcp_server._db` when it is already set; never open a second handle on the same file. minigraf 1.2.3 raises `Database is already open in this process` if you do.
- **Clause order in every `:any-valid-time` query.** `[?e :description ?desc]` MUST be the EAV clause immediately preceding `[?e :db/valid-from ?vf]` / `[?e :db/valid-to ?vt]`. Those pseudo-attributes bind to whichever EAV clause on `?e` most recently precedes them. Putting `[?e :ident ?ident]` between them binds the window to the `:ident` fact instead — wrong, and silently so.
- **No closing keywords for #257 or #222.** Commit messages and any PR body use `Refs #257` / `Refs #222` only. GitHub scans BOTH commit messages and the PR body, and a keyword/`#N` pair spans blank lines. Verify with `gh pr view --json closingIssuesReferences` before any merge.
- **The exit code reflects measurement validity, not the finding.** A nonzero mismatch count is the number #257 asks for and must never fail the run.
- **Entity types, exactly these six, in this order:** `module`, `function`, `class`, `variable`, `field`, `external-dependency`. This mirrors `_preload_known_entities`' own loop (`mcp_server.py:7429-7432`).

## File Structure

- **Create `evals/at_scale/probe_description_preload_exposure.py`** — the whole probe. Pure analysis primitives at the top (importable without opening a graph, matching the sibling probe's contract), then the DB loader, then `sweep()`, then `main()`. Single module because every piece exists to produce one report; splitting it would separate functions that only ever run together.
- **Create `tests/test_at_scale_description_preload_probe.py`** — unit tests for the pure primitives plus one real-backend test pinning the clause-order rule.
- **Generated, committed in Task 8:** `evals/at_scale/results/257-description-preload-exposure.json`.

Shared fact-record shape used by every function below:

```python
{"entity_type": "module", "ident": ":module/foo-py", "desc": "foo.py", "vf_ms": 1735689600000, "vt_ms": 1735776000000}
```

---

### Task 1: Census primitive — `census_distinct_values`

**Files:**
- Create: `evals/at_scale/probe_description_preload_exposure.py`
- Test: `tests/test_at_scale_description_preload_probe.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ENTITY_TYPES: Tuple[str, ...]` and `census_distinct_values(facts: Sequence[Dict]) -> Dict[str, Dict]`. The returned dict is keyed by entity type; each value has keys `idents_total: int`, `idents_with_multiple_values: int`, `offending_idents: Dict[str, List[str]]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_at_scale_description_preload_probe.py`:

```python
# tests/test_at_scale_description_preload_probe.py
"""Unit tests for the #257 :description exposure probe's analysis primitives.

The probe's headline numbers cannot be asserted -- they are what the probe
exists to discover. What CAN be asserted are the components that could
silently produce a WRONG number: the census's distinct-VALUE (not
distinct-interval) counting, the position-correct oracle, the diff's
separation of value exposure from membership disagreement, and the
unmappable-fact diagnostics.

Every test that pins a position-versus-date distinction is ablation-proven:
its docstring names the date-bounded answer, and that answer differs from the
asserted one. A test whose date-bounded and position-bounded answers agree
proves nothing about #257.
"""

from evals.at_scale.probe_description_preload_exposure import (
    ENTITY_TYPES,
    census_distinct_values,
)


class TestCensusDistinctValues:
    def test_two_intervals_carrying_one_value_are_not_exposure(self):
        """An entity deleted and re-added has two :description intervals and
        one value. That is the modal case on any real history and must not be
        counted -- counting intervals instead of values would report the whole
        repository as exposed.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "a.py",
             "vf_ms": 100, "vt_ms": 200},
            {"entity_type": "module", "ident": ":module/a", "desc": "a.py",
             "vf_ms": 300, "vt_ms": 400},
        ]
        report = census_distinct_values(facts)
        assert report["module"]["idents_total"] == 1
        assert report["module"]["idents_with_multiple_values"] == 0
        assert report["module"]["offending_idents"] == {}

    def test_one_ident_with_two_values_is_counted_and_named(self):
        """The offending values are reported verbatim, not just counted: a
        nonzero census escalates this work to a full sweep, and the escalation
        needs to start from which idents fired, not from an integer.
        """
        facts = [
            {"entity_type": "external-dependency", "ident": ":module/sub",
             "desc": "old-name", "vf_ms": 100, "vt_ms": 200},
            {"entity_type": "external-dependency", "ident": ":module/sub",
             "desc": "new-name", "vf_ms": 200, "vt_ms": 300},
        ]
        report = census_distinct_values(facts)
        assert report["external-dependency"]["idents_with_multiple_values"] == 1
        assert report["external-dependency"]["offending_idents"] == {
            ":module/sub": ["new-name", "old-name"]
        }

    def test_every_entity_type_appears_even_with_no_facts(self):
        """A type missing from the report and a type with zero exposure are
        different claims. Absent types would let a query failure for one type
        (_collect swallows those) read as a clean zero.
        """
        report = census_distinct_values([])
        assert set(report) == set(ENTITY_TYPES)
        assert all(report[t]["idents_total"] == 0 for t in ENTITY_TYPES)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evals.at_scale.probe_description_preload_exposure'`

- [ ] **Step 3: Write minimal implementation**

Create `evals/at_scale/probe_description_preload_exposure.py`:

```python
# evals/at_scale/probe_description_preload_exposure.py
"""#257 exposure probe: does the entity preload's DATE-bounded :description
seeding ever return a value a POSITION-bounded query would not?

#238 and #245 (PR #258) made _preload_known_entities' MEMBERSHIP decision
position-exact in both directions. The VALUES it seeds were left behind:
entity_descriptions[ident] still comes from whichever :description version was
live at :valid-at T_hi(W), a date query. T_hi(W) = max(ts[0..W]) is the
monotone envelope of author dates at or below the watermark, and author dates
are not monotonic in topological order, so a version written ABOVE the
watermark carrying an EARLIER author date can be the version that query
returns.

This module measures how often that happens on real history. The #245 probe
(probe_dep_preload_exposure.py) compared membership only and says nothing
about it.

TWO STAGES, and the first one may make the second moot:

  Stage 1 -- CENSUS. How many idents carry more than one DISTINCT :description
    value across all time? If none do, a date-bounded and a position-bounded
    query return the same string for every ident and the mechanism cannot
    fire, whatever the dates do. Counted per entity type, over distinct
    VALUES and not distinct intervals: an entity deleted and re-added has two
    intervals and one value, which is not exposure.

  Stage 2 -- VALUE DIFF. Drive the real, shipped _preload_known_entities at
    each structurally affected watermark and diff the entity_descriptions it
    returns against a position-correct oracle. This answers #257 on its own
    terms rather than resting on Stage 1's structural argument.

Stage 2 is not redundant with Stage 1. Stage 1's zero implies Stage 2's zero
only through an argument about how :description is written; Stage 2 checks the
shipped query's actual output and needs no such argument.

ONE MODE, unlike the #245 probe's date-only/--verify-fix pair. #257 is the
residual in the SHIPPED code, so _preload_known_entities is always driven with
its full post-#238/#245 argument set. There is no pre-fix leg to measure.

WHAT THE PREDICTION WAS, fixed in the design spec before any data existed:
census zero, mismatches zero. The spec records the argument for it (see its
"The issue's stated mechanism does not match the code" section). A nonzero
census is the falsifier and escalates to the per-position sweep Stage 2
already implements.
"""

from __future__ import annotations

from typing import Dict, List, Sequence

# The entity types _preload_known_entities loads, in its own order
# (mcp_server.py:7429-7432). Mirrored rather than imported so these analysis
# primitives stay importable without opening a graph, matching
# probe_dep_preload_exposure's contract. If the two ever diverge, this probe
# silently measures a different population than the function under test.
ENTITY_TYPES = (
    "module", "function", "class", "variable", "field", "external-dependency",
)


def census_distinct_values(facts: Sequence[Dict]) -> Dict[str, Dict]:
    """Per entity type: how many idents carry more than one DISTINCT
    :description value across all time.

    Distinct VALUES, not distinct intervals. An entity that was deleted and
    re-added carries two :description intervals and one value; counting
    intervals would report essentially every long-lived entity as exposed and
    drown the real signal.

    Every type in ENTITY_TYPES appears in the output even with no facts. A
    type missing from the report and a type with zero exposure are different
    claims, and _collect's per-type `except Exception: pass`
    (mcp_server.py:7491-7492) makes the difference load-bearing: a swallowed
    query failure for one type must not read as a clean zero.

    offending_idents carries the values verbatim, sorted, because a nonzero
    census escalates this work to a full sweep and the escalation starts from
    which idents fired, not from an integer.
    """
    by_type: Dict[str, Dict[str, set]] = {t: {} for t in ENTITY_TYPES}
    for fact in facts:
        entity_type = fact["entity_type"]
        if entity_type not in by_type:
            continue
        by_type[entity_type].setdefault(fact["ident"], set()).add(fact["desc"])

    report: Dict[str, Dict] = {}
    for entity_type in ENTITY_TYPES:
        idents = by_type[entity_type]
        offenders = {
            ident: sorted(values)
            for ident, values in idents.items()
            if len(values) > 1
        }
        report[entity_type] = {
            "idents_total": len(idents),
            "idents_with_multiple_values": len(offenders),
            "offending_idents": offenders,
        }
    return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py tests/test_at_scale_description_preload_probe.py
git commit -m "Add the #257 probe's distinct-value census

Counts distinct :description VALUES per ident, not distinct intervals: a
deleted-and-re-added entity has two intervals and one value, which is not
exposure. Every entity type appears even with no facts so a swallowed
per-type query failure cannot read as a clean zero.

Refs #257"
```

---

### Task 2: The position-correct oracle — `position_correct_descriptions`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`
- Test: `tests/test_at_scale_description_preload_probe.py`

**Interfaces:**
- Consumes: `ENTITY_TYPES` (Task 1). From `probe_dep_preload_exposure`: `VALID_TIME_FOREVER_MS`, `invert_ms_to_positions(ms, ts_positions) -> List[int]`, `edge_live_at(vf_positions, vt_positions, w) -> bool`, `build_ts_positions(commit_metadata) -> Dict[str, List[int]]`.
- Produces: `position_correct_descriptions(facts: Sequence[Dict], ts_positions: Dict[str, List[int]], w: int) -> Dict[str, set]` — ident to the set of distinct `:description` values live at position `w`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_description_preload_probe.py`, and extend the existing import block at the top of the file to also import `position_correct_descriptions`:

```python
from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    build_ts_positions,
)

# Position 2 is INVERTED: it sits above position 1 but carries an earlier
# author date (Jan 2 against Jan 5). That inversion is the whole of #257, and
# it is the same fixture shape tests/test_at_scale_dep_preload_probe.py uses.
#
#   pos 0: 2026-01-01   T_hi(0) = 2026-01-01
#   pos 1: 2026-01-05   T_hi(1) = 2026-01-05
#   pos 2: 2026-01-02   T_hi(2) = 2026-01-05
META = [
    ("h0", "2026-01-01T00:00:00Z", "a@b.com", "s0"),
    ("h1", "2026-01-05T00:00:00Z", "a@b.com", "s1"),
    ("h2", "2026-01-02T00:00:00Z", "a@b.com", "s2"),
]

MS_JAN_01 = 1767225600000  # 2026-01-01T00:00:00Z
MS_JAN_02 = 1767312000000  # 2026-01-02T00:00:00Z
MS_JAN_05 = 1767571200000  # 2026-01-05T00:00:00Z


class TestPositionCorrectDescriptions:
    def test_a_version_written_above_w_with_an_earlier_date_is_not_live(self):
        """THE #257 SHAPE, and the ablation that proves this test.

        Entity :module/a carries "old" from position 0, superseded at position
        2 by "new". Position 2 is above the watermark W=1 but dated Jan 2,
        earlier than T_hi(1) = Jan 5.

        A DATE-bounded query at T_hi(1) = Jan 5 answers {"new"}: "old" closed
        at Jan 2 <= Jan 5 so it reads as already dead, and "new" opened at
        Jan 2 <= Jan 5 so it reads as already live. Both readings are wrong at
        position 1.

        The position-correct answer is {"old"}. The two differ, which is what
        makes this test evidence about #257 rather than a tautology.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "old",
             "vf_ms": MS_JAN_01, "vt_ms": MS_JAN_02},
            {"entity_type": "module", "ident": ":module/a", "desc": "new",
             "vf_ms": MS_JAN_02, "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        live = position_correct_descriptions(facts, build_ts_positions(META), 1)
        assert live == {":module/a": {"old"}}

    def test_the_same_facts_at_the_higher_position_select_the_new_value(self):
        """At W=2 the superseding version IS live. Without this the previous
        test would also pass against an oracle that simply never advances.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "old",
             "vf_ms": MS_JAN_01, "vt_ms": MS_JAN_02},
            {"entity_type": "module", "ident": ":module/a", "desc": "new",
             "vf_ms": MS_JAN_02, "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        live = position_correct_descriptions(facts, build_ts_positions(META), 2)
        assert live == {":module/a": {"new"}}

    def test_an_unmappable_introduction_is_never_live(self):
        """edge_live_at treats an empty vf_positions as not live. Asserted here
        so the oracle's behaviour on a broken inversion is pinned rather than
        inherited silently -- the count of these is a validity gate, and a
        fact that is both uncounted and silently live would corrupt the
        finding.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/ghost", "desc": "g",
             "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        assert position_correct_descriptions(facts, build_ts_positions(META), 2) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: FAIL with `ImportError: cannot import name 'position_correct_descriptions'`

- [ ] **Step 3: Write minimal implementation**

Add to the import block at the top of `evals/at_scale/probe_description_preload_exposure.py`:

```python
from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    edge_live_at,
    invert_ms_to_positions,
)
```

Then append:

```python
def position_correct_descriptions(
    facts: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
    w: int,
) -> Dict[str, set]:
    """Ident -> the set of DISTINCT :description values genuinely live at
    position w.

    Position-correct at BOTH ends. Each fact's own :db/valid-from and
    :db/valid-to are inverted to positions and tested with edge_live_at; no
    date bound appears anywhere. That second end is what separates this from
    the shipped query: a version superseded at a position ABOVE w by a commit
    whose author date falls BELOW T_hi(w) reads as already dead to any date
    bound, and the superseding version reads as already live. Both are wrong
    at w, and their combination is exactly #257.

    Returns a SET per ident, not a single value, and never picks a winner
    among several. edge_live_at's asymmetric collision policy exists to avoid
    UNDERSTATING a membership answer; applied to a value it would fabricate
    one. diff_descriptions counts a multi-member set as ambiguous instead.

    Like everything else here this is a measurement device and NOT a candidate
    fix -- it needs the whole history in hand, which a resuming forward walk
    does not have.
    """
    live: Dict[str, set] = {}
    for fact in facts:
        vf_positions = invert_ms_to_positions(fact["vf_ms"], ts_positions)
        vt_positions = (
            None if fact["vt_ms"] >= VALID_TIME_FOREVER_MS
            else invert_ms_to_positions(fact["vt_ms"], ts_positions)
        )
        if edge_live_at(vf_positions, vt_positions, w):
            live.setdefault(fact["ident"], set()).add(fact["desc"])
    return live
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Verify the ablation is real**

The first test's docstring claims a date-bounded oracle answers `{"new"}` at W=1. Confirm that rather than trusting it. Run:

```bash
.venv/bin/python -c "
MS_JAN_01, MS_JAN_02, MS_JAN_05 = 1767225600000, 1767312000000, 1767571200000
facts = [('old', MS_JAN_01, MS_JAN_02), ('new', MS_JAN_02, (1<<63)-1)]
t_hi = MS_JAN_05
print(sorted(d for d, vf, vt in facts if vf <= t_hi < vt))
"
```

Expected output: `['new']` — different from the asserted `{'old'}`, so the test discriminates position from date. If this prints `['old']` the fixture is wrong and the test proves nothing; fix the fixture before continuing.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py tests/test_at_scale_description_preload_probe.py
git commit -m "Add the #257 probe's position-correct :description oracle

Inverts each :description fact's own valid-from and valid-to to positions,
with no date bound anywhere. Returns a SET per ident and never picks a
winner among several: edge_live_at's asymmetric collision policy avoids
understating a MEMBERSHIP answer and would fabricate a VALUE.

The oracle test is ablation-proven -- a date bound at T_hi(1) answers
{new} where the position-correct answer is {old}.

Refs #257"
```

---

### Task 3: The diff — `diff_descriptions`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`
- Test: `tests/test_at_scale_description_preload_probe.py`

**Interfaces:**
- Consumes: `position_correct_descriptions`' return shape (Task 2) — `Dict[str, set]`.
- Produces: `diff_descriptions(actual: Dict[str, str], oracle: Dict[str, set]) -> Dict` with keys `value_mismatches: Dict[str, Dict]` (ident -> `{"preloaded": str, "live": List[str]}`), `ambiguous_idents: List[str]`, `preloaded_not_live: List[str]`, `live_not_preloaded: List[str]`. All list values sorted for stable JSON.

- [ ] **Step 1: Write the failing test**

Add `diff_descriptions` to the probe import block at the top of the test file, then append:

```python
class TestDiffDescriptions:
    def test_a_preloaded_value_absent_from_the_live_set_is_a_mismatch(self):
        """The finding itself. Both the preloaded value and the live set are
        recorded, because a bare count would not say which direction the
        preload erred in.
        """
        result = diff_descriptions({":module/a": "new"}, {":module/a": {"old"}})
        assert result["value_mismatches"] == {
            ":module/a": {"preloaded": "new", "live": ["old"]}
        }
        assert result["ambiguous_idents"] == []

    def test_a_matching_value_is_not_a_mismatch(self):
        result = diff_descriptions({":module/a": "old"}, {":module/a": {"old"}})
        assert result["value_mismatches"] == {}

    def test_an_ambiguous_live_set_is_counted_and_never_compared(self):
        """When the oracle cannot name a single correct value there is nothing
        to be wrong against. Letting such an ident fall through to the
        membership test would score it as a match whenever the preloaded value
        happened to be one of several -- inventing agreement out of ambiguity.
        """
        result = diff_descriptions(
            {":module/a": "x"}, {":module/a": {"x", "y"}}
        )
        assert result["ambiguous_idents"] == [":module/a"]
        assert result["value_mismatches"] == {}

    def test_membership_disagreements_are_diagnostics_not_findings(self):
        """#238 and #245 measured and fixed membership. Folding a membership
        disagreement into #257's number would re-measure a closed issue and
        inflate this one.
        """
        result = diff_descriptions(
            {":module/only-preloaded": "p"}, {":module/only-live": {"l"}}
        )
        assert result["preloaded_not_live"] == [":module/only-preloaded"]
        assert result["live_not_preloaded"] == [":module/only-live"]
        assert result["value_mismatches"] == {}
        assert result["ambiguous_idents"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: FAIL with `ImportError: cannot import name 'diff_descriptions'`

- [ ] **Step 3: Write minimal implementation**

Append to `evals/at_scale/probe_description_preload_exposure.py`:

```python
def diff_descriptions(
    actual: Dict[str, str],
    oracle: Dict[str, set],
) -> Dict:
    """Compare the shipped preload's entity_descriptions against the
    position-correct oracle at one position.

    THREE quantities are separated deliberately, and only the first is #257's
    finding:

      value_mismatches -- the preloaded value is not among the values live at
        this position. This is the exposure #257 asks about.

      ambiguous_idents -- the oracle's live set has more than one member, so
        no single correct value exists to compare against. Counted and
        skipped, never resolved. Letting these fall through would score an
        ident as matching whenever the preloaded value happened to be one of
        several, inventing agreement out of ambiguity.

      preloaded_not_live / live_not_preloaded -- MEMBERSHIP disagreements.
        #238 and #245 measured and fixed membership; folding these into #257's
        number would re-measure a closed issue and inflate this one. They are
        reported because a large count here means the two sides are talking
        about different entity populations and the value comparison covers
        less than it appears to -- but they are not the finding.

    Ordering matters: ambiguity is tested BEFORE the value comparison, so an
    ambiguous ident contributes to neither the mismatch count nor the
    membership counters.
    """
    value_mismatches: Dict[str, Dict] = {}
    ambiguous: List[str] = []
    preloaded_not_live: List[str] = []

    for ident, preloaded_value in actual.items():
        live_values = oracle.get(ident)
        if not live_values:
            preloaded_not_live.append(ident)
            continue
        if len(live_values) > 1:
            ambiguous.append(ident)
            continue
        if preloaded_value not in live_values:
            value_mismatches[ident] = {
                "preloaded": preloaded_value,
                "live": sorted(live_values),
            }

    live_not_preloaded = [ident for ident in oracle if ident not in actual]

    return {
        "value_mismatches": value_mismatches,
        "ambiguous_idents": sorted(ambiguous),
        "preloaded_not_live": sorted(preloaded_not_live),
        "live_not_preloaded": sorted(live_not_preloaded),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py tests/test_at_scale_description_preload_probe.py
git commit -m "Add the #257 probe's value diff, with membership kept out of it

Separates three quantities and counts only the first as the finding: a
preloaded value absent from the position-live set; an ambiguous live set,
counted and skipped rather than resolved; and membership disagreements,
which are #238/#245's already-fixed territory and would inflate #257 if
folded in.

Ambiguity is tested before the value comparison so an ambiguous ident
contributes to no counter.

Refs #257"
```

---

### Task 4: Unmappable-fact diagnostics — `count_unmappable_description_facts`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`
- Test: `tests/test_at_scale_description_preload_probe.py`

**Interfaces:**
- Consumes: `invert_ms_to_positions`, `VALID_TIME_FOREVER_MS` (already imported in Task 2).
- Produces: `count_unmappable_description_facts(facts: Sequence[Dict], ts_positions: Dict[str, List[int]]) -> Tuple[int, int]` returning `(unmappable_valid_from, unmappable_valid_to)`.

- [ ] **Step 1: Write the failing test**

Add `count_unmappable_description_facts` to the probe import block, then append:

```python
class TestCountUnmappableDescriptionFacts:
    def test_an_unmappable_valid_from_is_counted(self):
        """An unmappable introduction silently drops the fact from the oracle
        at every W, understating the finding. It has to fail the run rather
        than shrink it.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "a",
             "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        assert count_unmappable_description_facts(
            facts, build_ts_positions(META)
        ) == (1, 0)

    def test_an_unmappable_non_sentinel_valid_to_is_counted(self):
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "a",
             "vf_ms": MS_JAN_01, "vt_ms": 7},
        ]
        assert count_unmappable_description_facts(
            facts, build_ts_positions(META)
        ) == (0, 1)

    def test_the_forever_sentinel_is_not_an_unmappable_close(self):
        """The sentinel is an open fact, not a broken inversion. Counting it
        would fail every run on every graph, since most facts are open.
        """
        facts = [
            {"entity_type": "module", "ident": ":module/a", "desc": "a",
             "vf_ms": MS_JAN_01, "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        assert count_unmappable_description_facts(
            facts, build_ts_positions(META)
        ) == (0, 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: FAIL with `ImportError: cannot import name 'count_unmappable_description_facts'`

- [ ] **Step 3: Write minimal implementation**

Append to `evals/at_scale/probe_description_preload_exposure.py`:

```python
def count_unmappable_description_facts(
    facts: Sequence[Dict],
    ts_positions: Dict[str, List[int]],
) -> tuple:
    """How many :description facts carry a vf_ms/vt_ms whose instant matches no
    commit -- this probe's validity diagnostic.

    Mirrors probe_dep_preload_exposure.count_unmappable_module_path_facts,
    applied to :description. An unmappable vf silently drops the fact from the
    oracle at every W (understating the finding); an unmappable non-sentinel vt
    silently reads it as never-superseded (overstating it). Either way the
    timestamp-to-position inversion the whole oracle rests on is broken for at
    least one fact, so a nonzero count INVALIDATES the measurement rather than
    adjusting it -- see main()'s exit gate.

    The forever sentinel is an open fact, not a broken inversion, and is
    excluded from the vt count. Most facts on a live graph are open; counting
    them would fail every run.

    Returns (unmappable_valid_from, unmappable_valid_to).
    """
    unmappable_vf = sum(
        1 for f in facts
        if not invert_ms_to_positions(f["vf_ms"], ts_positions)
    )
    unmappable_vt = sum(
        1 for f in facts
        if f["vt_ms"] < VALID_TIME_FOREVER_MS
        and not invert_ms_to_positions(f["vt_ms"], ts_positions)
    )
    return unmappable_vf, unmappable_vt
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 13 tests

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py tests/test_at_scale_description_preload_probe.py
git commit -m "Add the #257 probe's unmappable-fact diagnostics

An unmappable valid-from drops a fact from the oracle at every W and an
unmappable non-sentinel valid-to reads it as never-superseded, so a nonzero
count invalidates the measurement rather than adjusting it. The forever
sentinel is an open fact, not a broken inversion, and is excluded.

Refs #257"
```

---

### Task 5: The DB loader — `load_description_facts`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`
- Test: `tests/test_at_scale_description_preload_probe.py`

**Interfaces:**
- Consumes: `ENTITY_TYPES` (Task 1).
- Produces: `load_description_facts(db) -> List[Dict]` returning the shared fact-record shape, deduplicated on `(entity_type, ident, desc, vf_ms, vt_ms)`.

This task's test uses a REAL minigraf backend, following `docs/testing-conventions.md` Pattern 1. That is deliberate: the clause-order rule is the one silent-failure mode in this module, and no pure-function test can reach it — only a real query can show `?vf` binding to the wrong fact's window.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_at_scale_description_preload_probe.py`:

```python
import pytest


@pytest.fixture
def real_db(monkeypatch, tmp_path):
    """A real in-memory MiniGrafDb, per docs/testing-conventions.md Pattern 1.

    Not a MagicMock: a fake never parses the Datalog string, and the clause
    ordering this task exists to pin is only observable when minigraf actually
    parses and executes the query.
    """
    from minigraf import MiniGrafDb

    real_open_in_memory = MiniGrafDb.open_in_memory
    monkeypatch.setattr(
        MiniGrafDb, "open", staticmethod(lambda path: real_open_in_memory())
    )
    import mcp_server

    mcp_server.open_db(str(tmp_path / "t.graph"))
    yield mcp_server.get_db()


class TestLoadDescriptionFacts:
    def test_an_open_description_carries_the_forever_sentinel(self, real_db):
        import mcp_server

        mcp_server._transact(
            real_db,
            '[[:module/a :entity-type :type/module] '
            '[:module/a :ident ":module/a"] '
            '[:module/a :description "a.py"]]',
            "2026-01-01T00:00:00Z",
        )
        facts = load_description_facts(real_db)
        assert len(facts) == 1
        assert facts[0]["entity_type"] == "module"
        assert facts[0]["ident"] == ":module/a"
        assert facts[0]["desc"] == "a.py"
        assert facts[0]["vt_ms"] >= VALID_TIME_FOREVER_MS

    def test_the_window_belongs_to_the_description_fact_not_the_ident_fact(
        self, real_db
    ):
        """THE CLAUSE-ORDER ABLATION.

        :db/valid-from and :db/valid-to bind to whichever EAV clause on ?e most
        recently precedes them. Here :description is closed while :ident stays
        open. The correct query returns the CLOSED window; a query with
        [?e :ident ?ident] moved between :description and the pseudo-attributes
        returns the ident fact's OPEN window instead -- the forever sentinel --
        and every superseded description would silently read as still live,
        making the oracle's live set wrong at every position.
        """
        import mcp_server

        mcp_server._transact(
            real_db,
            '[[:module/a :entity-type :type/module] '
            '[:module/a :ident ":module/a"] '
            '[:module/a :description "a.py"]]',
            "2026-01-01T00:00:00Z",
        )
        mcp_server._ingest_close(
            real_db,
            ['[:module/a :description "a.py"]'],
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "test close",
        )
        facts = load_description_facts(real_db)
        closed = [f for f in facts if f["vt_ms"] < VALID_TIME_FOREVER_MS]
        assert closed, (
            "no closed :description window found -- the query is binding "
            "?vt to the still-open :ident fact, which is the clause-order bug"
        )
        assert closed[0]["vt_ms"] == mcp_server._iso_to_epoch_ms(
            "2026-01-02T00:00:00Z"
        )

    def test_rows_are_deduplicated(self, real_db):
        """An entity carrying several :entity-type or :ident versions across
        time multiplies each :description row under :any-valid-time without
        changing any answer. load_dep_edges dedupes for the same reason.
        """
        import mcp_server

        mcp_server._transact(
            real_db,
            '[[:module/a :entity-type :type/module] '
            '[:module/a :ident ":module/a"] '
            '[:module/a :description "a.py"]]',
            "2026-01-01T00:00:00Z",
        )
        facts = load_description_facts(real_db)
        keys = {(f["entity_type"], f["ident"], f["desc"], f["vf_ms"], f["vt_ms"])
                for f in facts}
        assert len(keys) == len(facts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: FAIL with `ImportError: cannot import name 'load_description_facts'`

- [ ] **Step 3: Write minimal implementation**

Append to `evals/at_scale/probe_description_preload_exposure.py`:

```python
def load_description_facts(db) -> List[Dict]:
    """Every :description fact on a preloaded entity type, current and
    historical, with its validity window.

    The raw material for BOTH stages. One query per entity type, matching
    _preload_known_entities' own per-type loop, so the population measured is
    the population that function actually loads.

    CLAUSE ORDER IS LOAD-BEARING. [?e :description ?desc] must be the EAV
    clause immediately preceding the :db/valid-from / :db/valid-to
    pseudo-attributes, because those bind to whichever EAV clause on ?e most
    recently precedes them. [?e :ident ?ident] therefore goes BEFORE
    :description, never between: moving it would bind ?vf/?vt to the ident
    fact's window instead -- and since :ident outlives every :description
    version it supersedes, every closed description would read as still live
    and the oracle would be wrong at every position. Silently. This is
    load_module_path_facts' documented rule applied to :description, and
    test_the_window_belongs_to_the_description_fact_not_the_ident_fact pins it
    against a real backend.

    Deduplicated on (entity_type, ident, desc, vf, vt). An entity carrying more
    than one :entity-type or :ident version across time otherwise multiplies
    each :description row under :any-valid-time without changing any answer --
    the same reason load_dep_edges dedupes. Two DISTINCT windows on the same
    (ident, desc) pair are legitimately kept: that is a delete-and-re-add, and
    the census must see both intervals to report them as one value.

    A per-type query failure raises rather than being swallowed. That is a
    deliberate difference from _preload_known_entities' own `except Exception:
    pass` (mcp_server.py:7491-7492): this function is a measurement device, and
    a silently empty population here would read as a clean zero exposure.
    """
    import json

    import mcp_server

    seen = set()
    facts: List[Dict] = []
    for entity_type in ENTITY_TYPES:
        raw = mcp_server._db_execute(
            db,
            "(query [:find ?ident ?desc ?vf ?vt "
            ":any-valid-time "
            f":where [?e :entity-type :type/{entity_type}] "
            "[?e :ident ?ident] "
            "[?e :description ?desc] "
            "[?e :db/valid-from ?vf] "
            "[?e :db/valid-to ?vt]])",
        )
        for ident, desc, vf, vt in json.loads(raw).get("results", []):
            key = (entity_type, ident, desc, int(vf), int(vt))
            if key in seen:
                continue
            seen.add(key)
            facts.append({
                "entity_type": entity_type,
                "ident": ident,
                "desc": desc,
                "vf_ms": int(vf),
                "vt_ms": int(vt),
            })
    return facts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Prove the clause-order test actually discriminates**

Temporarily move `[?e :ident ?ident]` to sit between `[?e :description ?desc]` and `[?e :db/valid-from ?vf]` in `load_description_facts`, then run:

`.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py::TestLoadDescriptionFacts -v`

Expected: `test_the_window_belongs_to_the_description_fact_not_the_ident_fact` FAILS with the "no closed :description window found" message. **Then revert the reordering** and re-run to confirm PASS. If the test passes in both orders it does not pin the rule and must be strengthened before continuing.

- [ ] **Step 6: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py tests/test_at_scale_description_preload_probe.py
git commit -m "Add the #257 probe's :description fact loader

One :any-valid-time query per entity type, matching the population
_preload_known_entities actually loads. Clause order is load-bearing --
:ident goes before :description, never between it and the valid-time
pseudo-attributes, or every closed description reads as still live.
Ablation-proven against a real backend.

Raises on a per-type query failure rather than swallowing it the way
_preload_known_entities does: a silently empty population here would read
as clean zero exposure.

Refs #257"
```

---

### Task 6: The sweep driver — `sweep`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5, plus from `probe_dep_preload_exposure`: `affected_positions(commit_metadata) -> List[int]`, `build_ts_positions`, `resume_envelopes(commit_metadata) -> List[str]`, `gitlink_event_count(repo_path) -> int`. From `mcp_server`: `_preload_known_entities`, `_iso_to_epoch_ms`.
- Produces: `sweep(db, repo_path, linearization, commit_metadata, branch=None) -> Dict` — the full report dict consumed by `main()`.

- [ ] **Step 1: Write the implementation**

Add `affected_positions`, `build_ts_positions`, `resume_envelopes`, `gitlink_event_count` to the `probe_dep_preload_exposure` import block, then append:

```python
def sweep(
    db,
    repo_path: str,
    linearization: List[str],
    commit_metadata: Sequence,
    branch: str = None,
) -> Dict:
    """Run Stage 1's census and Stage 2's per-position value diff, and assemble
    the report.

    ONE MODE. _preload_known_entities is always driven with its full
    post-#238/#245 argument set -- valid_at, hash_to_pos, watermark_pos,
    ts_positions, t_hi_ms -- because #257 is the residual in the SHIPPED code.
    Passing a subset would silently turn position_mode off inside the function
    (its gate is watermark_pos AND ts_positions AND t_hi_ms) and this sweep
    would measure the pre-fix path while believing it measured the shipped one.

    Calls the function under test rather than a restatement of what we believe
    it does. On the #238 branch a reviewer and an implementer both simulated
    the counterfactual with a date bound instead of the real position-filtered
    one, which made an inadequate test look adequate and cost two fix rounds.

    STAGE 1 MAY MAKE STAGE 2 MOOT, but Stage 2 runs regardless. A zero census
    implies zero mismatches only through an argument about how :description is
    written; Stage 2 checks the shipped query's actual output and rests on no
    such argument. Two independent lines of evidence for the price of one
    ingest.

    provenance: repo_path alone was not enough to reproduce the first #245
    artifact -- it named a scratch directory that no longer existed, with no
    branch and no head SHA. branch and head_commit are recorded here so a
    future run carries its own.
    """
    import subprocess

    import mcp_server

    if len(commit_metadata) != len(linearization):
        raise ValueError(
            f"commit_metadata has {len(commit_metadata)} entries but "
            f"linearization has {len(linearization)}; a misaligned pair "
            "mis-filters the entire sweep"
        )
    for i, ((meta_hash, _ts, _a, _s), lin_hash) in enumerate(
        zip(commit_metadata, linearization)
    ):
        if meta_hash != lin_hash:
            raise ValueError(
                f"commit_metadata[{i}] is {meta_hash} but linearization[{i}] "
                f"is {lin_hash}; the two must be positionally aligned"
            )

    hash_to_pos = {h: i for i, h in enumerate(linearization)}
    ts_positions = build_ts_positions(commit_metadata)
    envelopes = resume_envelopes(commit_metadata)
    collisions = {ts: pos for ts, pos in ts_positions.items() if len(pos) > 1}

    facts = load_description_facts(db)
    census = census_distinct_values(facts)
    unmappable_vf, unmappable_vt = count_unmappable_description_facts(
        facts, ts_positions
    )

    positions = affected_positions(commit_metadata)
    per_position = []
    mismatch_distinct: set = set()
    mismatch_weighted = 0
    ambiguous_total = 0
    preloaded_not_live_total = 0
    live_not_preloaded_total = 0
    preload_sizes = []

    for w in positions:
        (
            _entity_valid_from, entity_descriptions, _entity_introduced_by,
            _file_entities, _submodule_paths,
        ) = mcp_server._preload_known_entities(
            db, repo_path,
            valid_at=envelopes[w],
            hash_to_pos=hash_to_pos,
            watermark_pos=w,
            ts_positions=ts_positions,
            t_hi_ms=mcp_server._iso_to_epoch_ms(envelopes[w]),
        )
        oracle = position_correct_descriptions(facts, ts_positions, w)
        result = diff_descriptions(entity_descriptions, oracle)

        preload_sizes.append(len(entity_descriptions))
        mismatch_weighted += len(result["value_mismatches"])
        mismatch_distinct.update(result["value_mismatches"])
        ambiguous_total += len(result["ambiguous_idents"])
        preloaded_not_live_total += len(result["preloaded_not_live"])
        live_not_preloaded_total += len(result["live_not_preloaded"])

        per_position.append({
            "position": w,
            "envelope": envelopes[w],
            "preloaded_idents": len(entity_descriptions),
            "oracle_idents": len(oracle),
            "value_mismatches": result["value_mismatches"],
            "ambiguous_idents": result["ambiguous_idents"],
            "preloaded_not_live": len(result["preloaded_not_live"]),
            "live_not_preloaded": len(result["live_not_preloaded"]),
        })

    head_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout.strip()

    return {
        "repo_path": repo_path,
        "branch": branch,
        "head_commit": head_commit,
        "commits": len(commit_metadata),
        "affected_positions": positions,
        "misclassifying_positions": [
            p["position"] for p in per_position if p["value_mismatches"]
        ],
        "description_facts_total": len(facts),
        "census": census,
        "census_idents_with_multiple_values": sum(
            census[t]["idents_with_multiple_values"] for t in ENTITY_TYPES
        ),
        "value_mismatch_total_position_weighted": mismatch_weighted,
        "value_mismatch_distinct_idents": len(mismatch_distinct),
        "ambiguous_idents_total": ambiguous_total,
        "preloaded_not_live_total": preloaded_not_live_total,
        "live_not_preloaded_total": live_not_preloaded_total,
        "unmappable_description_valid_from": unmappable_vf,
        "unmappable_description_valid_to": unmappable_vt,
        "timestamp_collisions": len(collisions),
        "preload_descriptions_empty_everywhere": (
            bool(preload_sizes) and all(n == 0 for n in preload_sizes)
        ),
        "gitlink_events": gitlink_event_count(repo_path),
        "per_position": per_position,
    }
```

- [ ] **Step 2: Verify the module imports and the existing tests still pass**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 16 tests — `sweep` has no unit test of its own (it needs a real ingested graph; Task 7's smoke run exercises it), but a syntax or import error here fails collection.

- [ ] **Step 3: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py
git commit -m "Add the #257 probe's sweep driver

Drives the real _preload_known_entities with its full post-#238/#245
argument set at every structurally affected position and diffs its
entity_descriptions against the position-correct oracle. One mode only:
#257 is the residual in the shipped code, and passing a subset of the
position arguments would silently measure the pre-fix path instead.

Stage 2 runs even when Stage 1's census is zero. The census implies zero
mismatches only through an argument about how :description is written;
the sweep checks the shipped query's output and needs no such argument.

Refs #257"
```

---

### Task 7: CLI and exit gate — `main`

**Files:**
- Modify: `evals/at_scale/probe_description_preload_exposure.py`

**Interfaces:**
- Consumes: `sweep` (Task 6). From `probe_dep_preload_exposure`: `_ingest_into(repo_path, branch, graph_path) -> Tuple[str, str]` (async). From `frontier_registry`: `build_linearization(repo_path, branch) -> List[str]`. From `mcp_server`: `_git_commits`, `_default_git_branch`, `open_db`, `_db`.
- Produces: `main() -> int`, and `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 1: Write the implementation**

Append to `evals/at_scale/probe_description_preload_exposure.py`:

```python
def main() -> int:
    import argparse
    import asyncio
    import json
    import sys
    from pathlib import Path

    repo_root = Path(__file__).parent.parent.parent.resolve()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    parser = argparse.ArgumentParser(
        description=(
            "Measure #257's :description preload exposure against real history."
        )
    )
    parser.add_argument("--repo-path", default=".")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--json-out", "--output", dest="json_out", default=None)
    parser.add_argument(
        "--graph-path", default=None,
        help=(
            "Sweep an EXISTING graph instead of ingesting into a scratch one. "
            "The graph's commit count must match the linearization length or "
            "the run is refused -- a partial graph swept silently would "
            "understate exposure. Exists because a ~30-minute ingest per "
            "iteration is otherwise the entire cost of this measurement."
        ),
    )
    args = parser.parse_args()

    import frontier_registry
    import mcp_server
    from evals.at_scale.probe_dep_preload_exposure import _ingest_into

    def _run_sweep(branch, ingest_status):
        linearization = frontier_registry.build_linearization(
            args.repo_path, branch
        )
        commit_metadata = mcp_server._git_commits(args.repo_path, None, branch)
        # Single-handle invariant (CLAUDE.md): reuse the live handle when one
        # exists. Opening a second on the same file raises as of minigraf
        # 1.2.3, and used to corrupt the page table silently (#251/#253).
        db = (
            mcp_server._db if mcp_server._db is not None
            else mcp_server.open_db(str(graph_path))
        )
        report = sweep(
            db, args.repo_path, linearization, commit_metadata, branch=branch,
        )
        report["ingest_status"] = ingest_status
        return report

    if args.graph_path:
        graph_path = Path(args.graph_path)
        if not graph_path.exists():
            print(f"No graph at {graph_path}. Refusing to sweep nothing.")
            return 1
        mcp_server._db = None
        mcp_server._graph_path = None
        mcp_server.open_db(str(graph_path))
        branch = args.branch or mcp_server._default_git_branch(args.repo_path)
        linearization = frontier_registry.build_linearization(
            args.repo_path, branch
        )
        raw = mcp_server._db_execute(
            mcp_server._db,
            "(query [:find (count ?c) :where [?c :entity-type :type/commit]])",
        )
        rows = json.loads(raw).get("results", [])
        ingested = int(rows[0][0]) if rows else 0
        if ingested != len(linearization):
            print(
                f"Graph holds {ingested} commits but the linearization has "
                f"{len(linearization)}. Refusing to sweep a partial graph -- "
                "an incomplete ingestion understates exposure everywhere."
            )
            return 1
        report = _run_sweep(branch, "pre-existing")
    else:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="minigraf-257-probe-") as tmpdir:
            graph_path = Path(tmpdir) / "probe.graph"
            branch, ingest_status = asyncio.run(
                _ingest_into(args.repo_path, args.branch, graph_path)
            )
            # _run_ingestion never raises on failure -- it wraps its whole body
            # in `except Exception`, sets status and returns normally. This
            # status is the only signal that the graph underneath is whole.
            if ingest_status != "complete":
                error = mcp_server._ingest_progress.get("error")
                print(
                    f"Ingestion did not complete (status={ingest_status!r}"
                    + (f", error={error!r}" if error else "")
                    + "). Refusing to sweep a partial or failed graph."
                )
                return 1
            report = _run_sweep(branch, ingest_status)

    print(json.dumps(report, indent=2))
    print()
    print(f"repo:                              {report['repo_path']} @ {report['branch']}")
    print(f"ingested head:                     {report['head_commit']}")
    print(f"commits:                           {report['commits']}")
    print(f":description facts (deduped):      {report['description_facts_total']}")
    print(f"structurally affected W:           {len(report['affected_positions'])}")
    print(f"W actually mismatching:            {len(report['misclassifying_positions'])}")
    print()
    print("STAGE 1 -- census (idents carrying >1 DISTINCT :description value)")
    for entity_type in ENTITY_TYPES:
        row = report["census"][entity_type]
        print(
            f"  {entity_type:<22} {row['idents_with_multiple_values']:>6} "
            f"of {row['idents_total']}"
        )
    print(f"  TOTAL                  {report['census_idents_with_multiple_values']:>6}")
    print()
    print("STAGE 2 -- value diff against the position-correct oracle")
    print(f"  mismatches, position-weighted:   {report['value_mismatch_total_position_weighted']}")
    print(f"  mismatches, distinct idents:     {report['value_mismatch_distinct_idents']}")
    print()
    print("  (counted separately, NOT the finding:)")
    print(f"  ambiguous idents:                {report['ambiguous_idents_total']}")
    print(f"  preloaded but not live:          {report['preloaded_not_live_total']}")
    print(f"  live but not preloaded:          {report['live_not_preloaded_total']}")
    print()
    print(f"preload empty at every W:          {report['preload_descriptions_empty_everywhere']}")
    print(f"timestamp collisions:              {report['timestamp_collisions']}")
    print(f"unmappable :description vf:        {report['unmappable_description_valid_from']}")
    print(f"unmappable :description vt:        {report['unmappable_description_valid_to']}")
    print(f"gitlink events:                    {report['gitlink_events']}")

    # The exit code reflects measurement VALIDITY, not the finding. A nonzero
    # mismatch count is the number #257 asks for and must never fail the run.
    measurement_invalid = (
        report["unmappable_description_valid_from"] > 0
        or report["unmappable_description_valid_to"] > 0
        or report["timestamp_collisions"] > 0
    )
    if measurement_invalid:
        print()
        print(
            "INVALID MEASUREMENT. Either a :description fact carries a\n"
            "valid-from/valid-to matching no commit -- so the timestamp-to-\n"
            "position inversion the oracle rests on is broken -- or the history\n"
            "has colliding timestamps. Collisions invalidate rather than widen:\n"
            "the shipped _position_of_valid_time and this probe's edge_live_at\n"
            "resolve a collision in OPPOSITE directions, so the comparison is\n"
            "no longer meaningful. Do not adjust this gate to make a run pass."
        )

    if report["preload_descriptions_empty_everywhere"]:
        print()
        print(
            "NOTE: _preload_known_entities returned zero descriptions at every\n"
            "affected position. That may be a genuinely empty population, or it\n"
            "may be _collect's per-type `except Exception: pass`\n"
            "(mcp_server.py:7491-7492) swallowing a real query failure -- the\n"
            "two are indistinguishable from the mismatch count alone. Check\n"
            ":description facts above: nonzero there with zero here is the\n"
            "signature of the latter."
        )

    if report["gitlink_events"] == 0:
        print()
        print(
            "NOTE: zero gitlink events. Submodule external-dependency entities\n"
            "are the ONE preloaded type whose :description is not a function of\n"
            "its ident -- it is `name or path` read from .gitmodules, and the\n"
            "name can change while the path, and so the ident, stays fixed. This\n"
            "history produces none, so that arm is UNMEASURABLE here. That is\n"
            "not the same as zero risk, and any close written for #257 has to\n"
            "name it. #245 recorded :pinned-commit the same way."
        )

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.json_out}")
    return 1 if measurement_invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify `--help` works and the module is importable**

Run: `.venv/bin/python -m evals.at_scale.probe_description_preload_exposure --help`
Expected: argparse usage text listing `--repo-path`, `--branch`, `--json-out`, `--graph-path`. No traceback.

- [ ] **Step 3: Verify the partial-graph refusal fires**

Run: `.venv/bin/python -m evals.at_scale.probe_description_preload_exposure --graph-path /nonexistent/x.graph`
Expected: prints `No graph at /nonexistent/x.graph. Refusing to sweep nothing.` and exits 1. Confirm with `echo $?`.

- [ ] **Step 4: Run the full unit suite**

Run: `.venv/bin/python -m pytest tests/test_at_scale_description_preload_probe.py -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/probe_description_preload_exposure.py
git commit -m "Add the #257 probe's CLI, with validity-only exit gate

The exit code reflects measurement validity, never the finding: nonzero
mismatches are the number #257 asks for. Unmappable facts and timestamp
collisions fail the run -- collisions invalidate rather than widen, since
the shipped _position_of_valid_time and this probe's edge_live_at resolve
one in opposite directions.

--graph-path sweeps an existing graph, gated on its commit count matching
the linearization, so a ~30-minute ingest is not the cost of every
iteration. Zero gitlink events is reported as UNMEASURABLE for the
submodule arm, not as zero, following #245's :pinned-commit wording.

Refs #257"
```

---

### Task 8: Run it, record the artifact, report the verdict

**Files:**
- Create: `evals/at_scale/results/257-description-preload-exposure.json`
- Modify: `evals/at_scale/benchmark.md`

- [ ] **Step 1: Run the probe against this repository**

Run (expect roughly 30-40 minutes, dominated by ingestion):

```bash
.venv/bin/python -m evals.at_scale.probe_description_preload_exposure \
  --repo-path . \
  --json-out evals/at_scale/results/257-description-preload-exposure.json
```

- [ ] **Step 2: Check the validity gate before reading any finding**

Confirm the exit code is 0 (`echo $?`). If it is 1, the measurement is invalid — unmappable facts or timestamp collisions — and the finding must NOT be reported. Investigate the cause; do not adjust the gate.

- [ ] **Step 3: Read the result against the prediction**

The design spec predicted **census zero, mismatches zero**, fixed before any data existed.

- **If both are zero:** the prediction holds. The mechanism cannot fire on the five ident-determined types, and the submodule arm is unmeasurable here. Proceed to Step 4.
- **If the census is nonzero:** the falsifier fired. Do not proceed to Step 4's close-oriented wording. Report the offending idents and their values, and open the question of whether the per-attribute interval-inversion fix #257 sketches is now justified. This is a real finding, not noise.
- **If the census is zero but mismatches are nonzero:** the two stages disagree, which means one of them is wrong. Stop and investigate before reporting anything — a census-zero graph cannot produce a value mismatch, so this outcome indicates a bug in the probe itself.

- [ ] **Step 4: Add a benchmark.md entry**

Append an entry to `evals/at_scale/benchmark.md` following the format of the existing `239-introduced-by-query-cost` entry: date, what was measured, the prediction fixed in advance, the result, and the verdict. State the submodule arm as UNMEASURABLE explicitly.

- [ ] **Step 5: Commit the artifact**

```bash
git add evals/at_scale/results/257-description-preload-exposure.json evals/at_scale/benchmark.md
git commit -m "Record the #257 :description preload exposure measurement

Refs #257"
```

- [ ] **Step 6: Report to the user before touching GitHub**

Summarise: the census per entity type, the Stage 2 mismatch counts, the validity diagnostics, and whether the pre-registered prediction held. Then ask whether to post the #257 comment.

**Do not post to GitHub without asking.** The comment carries two parts and the user should approve both: the measurement verdict, and the correction to #257's body — that `:description` is written once per entity lifetime rather than on every body edit, and that no code path reads `entity_descriptions` for body-change detection (every read feeds `_build_close_triples`' retract value). The real consequence, if the mechanism ever fires, is a retract value that fails to match and leaves the fact live past its window — a stale-fact bug, not a lost body edit.

**The comment must not contain a closing keyword.** `Refs #257` only. Whether a zero result closes #257 is the user's judgement call, and the unmeasurable submodule arm has to be named in whatever close is written.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: architecture and graph acquisition → Tasks 6-7; Stage 1 census → Task 1 (primitive) and Task 5 (loader, incl. the clause-order rule); Stage 2 oracle and diff → Tasks 2-3; report fields → Task 6; exit code → Task 7; the five listed tests → Tasks 1-5, each ablation-proven where it pins a position-versus-date distinction (explicit ablation steps in Tasks 2 and 5); deliverables → Task 8. The spec's "does NOT do" list is honoured: no `mcp_server.py` change appears in any task, no interval-inversion fix is built, and Task 8 Step 6 explicitly declines to close #257.

**Placeholder scan.** No TBD/TODO, no "add error handling", no "similar to Task N". Every code step carries the actual code. Task 8's Step 4 describes a prose entry rather than showing it, because its content is the measurement result, which does not exist until Step 1 runs; the step names the format to follow and the required elements.

**Type consistency.** The fact-record shape (`entity_type`, `ident`, `desc`, `vf_ms`, `vt_ms`) is fixed in the File Structure section and used identically in Tasks 1, 2, 4, 5, 6. `position_correct_descriptions` returns `Dict[str, set]` in Task 2 and is consumed as such by `diff_descriptions` in Task 3 and `sweep` in Task 6. `diff_descriptions`' four output keys are defined in Task 3 and read by the same names in Task 6. `ENTITY_TYPES` is defined in Task 1 and used in Tasks 5, 6, 7.
