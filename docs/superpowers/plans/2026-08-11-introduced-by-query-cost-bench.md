# `:introduced-by` Query Cost Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the bench that answers whether `_entity_introduced_by_query` and `_entity_ident_is_live` per-call latency is constant in graph size — the one number that decides #239's fix.

**Architecture:** A single standalone script, `evals/at_scale/bench_introduced_by_query_cost.py`, closely modelled on the existing `bench_lineage_query_cost.py`: real on-disk `MiniGrafDb` fixtures, `db.checkpoint()` before any timed query, warmup-with-assertion before each timed loop, printed tables plus a JSON result file. Four experiments and a stated-in-advance FLAT/NOT-FLAT verdict.

**Tech Stack:** Python 3, `minigraf` (real backend, no mocks), `mcp_server` imported directly.

**Spec:** `docs/superpowers/specs/2026-08-11-introduced-by-query-cost-bench-design.md`

## Global Constraints

- **Real minigraf backend only.** No mocks anywhere. Import `mcp_server` and call its real functions.
- **`db.checkpoint()` after every fixture write, before any timed query.** `bench_lineage_query_cost.py`'s methodology note records that an uncheckpointed graph answers the point query and the join query *differently* once enough facts accumulate — which would silently invert this bench's verdict.
- **One live `MiniGrafDb` handle per process.** `del db` before opening the next fixture. Two handles on one file each cache their own `page_count` and corrupt each other (`CLAUDE.md`'s single-handle invariant, #251/#253, project-minigraf/minigraf#304). minigraf ≥1.2.2 raises `Database is already open in this process` rather than corrupting, so a violation fails loudly — but do not rely on that.
- **Every timed loop is preceded by one untimed warmup call whose result is asserted** against what the fixture should produce. This is the fixture-visibility guard; without it a MISS-cost number can be reported as if it were a HIT.
- **The FLAT threshold is fixed by the spec and must not be adjusted to fit the data:** per-call latency grows by **less than 2x** across the 50x filler range AND less than 2x across the 50x ident-population range, for both HIT and MISS.
- **`_lineage_is_provisional` is the control.** Its known answer is flat 0.046–0.053 ms/call at 100k/1M/5M. If the bench does not reproduce that, it must print `INVALID MEASUREMENT` and withhold the verdict.
- **This plan changes no production code.** `mcp_server.py` is read-only here. If you believe a change is required, stop and report.
- **Commit messages use `Refs #239` / `Refs #245` / `Refs #222`.** **Never** `Closes`/`Fixes`/`Resolves` + an issue number — this bench does not fix #239, and #222 must stay open. GitHub scans commit messages and PR bodies, and a *negated* keyword has auto-closed an issue on this project.
- **Branch:** `bench-239-introduced-by-query-cost`, already created off `master` (`463a922`).

## The three queries under test

All three are subject-literal point queries, verified on `master`:

| function | line | query it issues |
| --- | --- | --- |
| `_entity_ident_is_live` | 5648 | `[:find ?i :where [<ident> :ident ?i]]` |
| `_entity_introduced_by_query` | 5718 → `_entity_introduced_by_values_query` 5674 | `[:find ?c :where [<ident> :introduced-by ?c]]` |
| `_lineage_is_provisional` (control) | 5480 | `[:find ?e :where [<marker-ident> :entity ?e]]` |

Time the **public wrappers**, not raw Datalog — `_entity_introduced_by_query` carries ambiguity detection on top of the values query, and the wrapper is what any fix replaces.

## File Structure

| File | Responsibility |
| --- | --- |
| `evals/at_scale/bench_introduced_by_query_cost.py` | The whole bench. One file, matching how every sibling bench in this directory is structured. |
| `evals/at_scale/results/239-introduced-by-query-cost.json` | Output. Written by the bench, committed with the final run. |
| `evals/at_scale/benchmark.md` | Gains one entry recording the verdict. |

## A note on the batch form, already settled

There is **no set-membership predicate in minigraf's Datalog** — `contains?` is a *string* predicate alongside `ends-with?` and `matches?` (`query/datalog/parser.rs:1175`). A query cannot be restricted to N specific idents server-side. The realistic batch form is therefore **whole-relation query + Python-side narrowing**, exactly as `_preload_known_deps` narrows via `ident_to_file`. E3 measures that shape. Do not spend time looking for a scoped-query form; it does not exist.

The whole-relation query must bind the **ident**, not the subject variable: `[?e :ident ?i] [?e :introduced-by ?c]` finding `?i ?c`. Binding `?e` in `:find` returns minigraf's internal UUID, not the keyword ident — this has bitten this codebase repeatedly (#133, #141), and Task 1 pins it.

---

### Task 1: Scaffold, fixtures, timed helpers, and the UUID-binding pin

**Files:**
- Create: `evals/at_scale/bench_introduced_by_query_cost.py`

**Interfaces:**
- Consumes: `mcp_server._entity_ident_is_live`, `._entity_introduced_by_query`, `._lineage_is_provisional`, `._lineage_marker_ident`
- Produces:
  - `fresh_db(tmpdir, name) -> (db, path)`
  - `populate_filler(db, n, chunk=1000) -> None`
  - `populate_lineage_entities(db, idents, chunk=1000) -> None`
  - `add_lineage_markers(db, idents, chunk=1000) -> None`
  - `timed(fn, db, idents, reps, expected) -> float` (ms/call)
  - `timed_whole_relation(db, reps, expected_len) -> (float, int)`
  - `CANDIDATES_PER_COMMIT = 1265`, `TS = "2026-01-01T00:00:00Z"`, `SIZES = (100_000, 1_000_000, 5_000_000)`

- [ ] **Step 1: Write the scaffold and fixtures**

```python
#!/usr/bin/env python3
"""#239 discriminator: does per-ident point-query cost grow with graph size?

#239 proposes replacing per-ident point queries with a set-at-a-time form.
That is correct IF AND ONLY IF per-call cost is constant in graph size. If it
grows, batching only reduces how often we pay a growing cost and the run stays
superlinear.

Sibling of bench_lineage_query_cost.py, which asked the identical question of
_lineage_is_provisional and REFUTED the scan hypothesis there: a MISS came out
10-12% CHEAPER than a HIT, and neither scaled across a 50x graph-size range
(0.046-0.053 ms/call at 100k/1M/5M). That result is reused here as a CONTROL --
if this bench does not reproduce it, the methodology is broken and the other
numbers must be discarded rather than believed.

The competing prior: #152 and #236 each found a per-item operation degrading to
a full scan because minigraf had no B-tree on the queried column, and both were
fixed with a companion table, not with batching. #236 measured batching buying
nothing there -- 200 triples cost the same as 200 calls, 20 calls, or 1.

VERDICT THRESHOLD, fixed before any data exists so it cannot be chosen to fit
the result: FLAT = under 2x growth across 50x of filler AND under 2x across 50x
of ident population, for both HIT and MISS.

METHODOLOGY NOTE: fixture writes are checkpointed (db.checkpoint()) before any
timed query. Skipping this is a trap -- an uncheckpointed graph answers the
point query and the whole-relation query differently once enough facts
accumulate, which would silently invert the verdict.

SINGLE-HANDLE INVARIANT: at most one live MiniGrafDb per process (CLAUDE.md).
`del db` before opening the next fixture.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

REPO = "/home/aditya/Work/AMC/Minigraf/temporal_reasoning"
sys.path.insert(0, REPO)

from minigraf import MiniGrafDb
import mcp_server

TS = "2026-01-01T00:00:00Z"

# The reverse stream's working figure for candidate idents per commit, carried
# over from bench_lineage_query_cost.py so the two benches' crossover numbers
# are directly comparable.
CANDIDATES_PER_COMMIT = 1265

SIZES = (100_000, 1_000_000, 5_000_000)
IDENT_POPULATIONS = (1_000, 10_000, 50_000)


def fresh_db(tmpdir, name):
    """A real on-disk MiniGrafDb, like the other at-scale bench scripts."""
    path = os.path.join(tmpdir, f"{name}.graph")
    for suffix in ("", ".wal", ".lock"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    return MiniGrafDb.open(path), path


def populate_filler(db, n, chunk=1000):
    """Bulk unrelated facts on distinct entities, to set total graph size.

    These entities double as realistic MISS targets: they EXIST in the graph
    (as a real candidate ident would) but carry neither :ident nor
    :introduced-by, which is exactly the shape of a miss during ingestion.
    """
    for start in range(0, n, chunk):
        batch = [f'[:e/n{i} :attr "v{i}"]' for i in range(start, min(start + chunk, n))]
        db.execute(f'(transact {{:valid-from "{TS}"}} [' + " ".join(batch) + "])")
    db.checkpoint()  # see module docstring's METHODOLOGY NOTE


def populate_lineage_entities(db, idents, chunk=1000):
    """Write the :ident + :introduced-by pair a real code entity carries, so
    both _entity_ident_is_live and _entity_introduced_by_query HIT on these.

    Mirrors what _build_code_triples emits for an introduction: the entity's
    own :ident string and an :introduced-by edge to a commit entity.
    """
    facts = []
    for ident in idents:
        facts.append(f'[{ident} :ident "{ident}"]')
        facts.append(f"[{ident} :introduced-by :commit/c0]")
    for start in range(0, len(facts), chunk):
        db.execute(
            f'(transact {{:valid-from "{TS}"}} [' + " ".join(facts[start:start + chunk]) + "])"
        )
    db.checkpoint()  # see module docstring's METHODOLOGY NOTE


def add_lineage_markers(db, idents, chunk=1000):
    """The 3-fact marker pattern _lineage_confirm_batch writes, via
    mcp_server._lineage_marker_ident so the ident shape matches production
    byte-for-byte. Only the CONTROL query needs these.
    """
    facts = []
    for ident in idents:
        mident = mcp_server._lineage_marker_ident(ident)
        facts.append(f"[{mident} :entity-type :type/lineage-marker]")
        facts.append(f"[{mident} :entity {ident}]")
        facts.append(f"[{mident} :status :provisional]")
    for start in range(0, len(facts), chunk):
        db.execute(
            f'(transact {{:valid-from "{TS}"}} [' + " ".join(facts[start:start + chunk]) + "])"
        )
    db.checkpoint()  # see module docstring's METHODOLOGY NOTE


def timed(fn, db, idents, reps, expected):
    """One untimed warmup call asserted against `expected`, then `reps` timed
    calls cycling through idents. Returns ms/call.

    The assertion is the fixture-visibility guard: without it a MISS-cost
    number can be silently reported as if it were a HIT (see the module
    docstring's METHODOLOGY NOTE).
    """
    warmup = fn(db, idents[0])
    got = bool(warmup) if not isinstance(warmup, bool) else warmup
    assert got == expected, (
        f"{fn.__name__}({idents[0]!r}) -> {warmup!r}, expected truthiness "
        f"{expected} -- fixture not visible to queries yet (METHODOLOGY NOTE)"
    )
    t0 = time.perf_counter()
    for i in range(reps):
        fn(db, idents[i % len(idents)])
    return (time.perf_counter() - t0) / reps * 1000


WHOLE_RELATION_QUERY = (
    "(query [:find ?i ?c :where [?e :ident ?i] [?e :introduced-by ?c]])"
)


def whole_relation(db):
    """The set-at-a-time form a batch fix would use: the entire
    ident -> introduced-by relation in one query, narrowed in Python.

    Binds ?i (the :ident OBJECT), never the ?e subject variable -- binding a
    subject in :find returns minigraf's internal UUID rather than the keyword
    ident, which has bitten this codebase repeatedly (#133, #141).

    There is no server-side way to restrict this to N specific idents:
    minigraf's `contains?` is a STRING predicate, not set membership
    (query/datalog/parser.rs:1175). Whole-relation-plus-Python-narrowing is
    the realistic shape, and it is what _preload_known_deps already does via
    ident_to_file.
    """
    raw = mcp_server._db_execute(db, WHOLE_RELATION_QUERY)
    return {row[0]: row[1] for row in json.loads(raw).get("results", [])}


def timed_whole_relation(db, reps, expected_len):
    """One untimed warmup asserted against expected_len, then `reps` timed
    calls. Returns (ms/call, result_len)."""
    result = whole_relation(db)
    assert len(result) == expected_len, (
        f"whole_relation returned {len(result)} rows, expected {expected_len} "
        "-- fixture not visible to queries yet (METHODOLOGY NOTE)"
    )
    t0 = time.perf_counter()
    for _ in range(reps):
        result = whole_relation(db)
    return (time.perf_counter() - t0) / reps * 1000, len(result)
```

- [ ] **Step 2: Add the UUID-binding pin and a smoke check**

Append:

```python
def pin_whole_relation_binds_idents(tmpdir):
    """Pin that the whole-relation query returns KEYWORD IDENTS, not UUIDs.

    Not a style check. If ?i ever came back as a UUID, every batch-form number
    in E3 would be measuring a query whose output the fix cannot actually use,
    and the crossover would be meaningless. Cheap to assert, expensive to miss.
    """
    db, _ = fresh_db(tmpdir, "pin")
    idents = [":module/pin-a", ":module/pin-b"]
    populate_lineage_entities(db, idents)
    rel = whole_relation(db)
    assert set(rel.keys()) == set(idents), (
        f"whole_relation keys are {sorted(rel.keys())}, expected {sorted(idents)} "
        "-- if these look like UUIDs, the query is binding ?e instead of ?i"
    )
    assert all(v == ":commit/c0" for v in rel.values()), rel
    del db
    print("pin: whole-relation query binds keyword idents, not UUIDs -- OK")
```

- [ ] **Step 3: Run the pin**

```bash
python - <<'EOF'
import tempfile, sys
sys.path.insert(0, "/home/aditya/Work/AMC/Minigraf/temporal_reasoning")
from evals.at_scale.bench_introduced_by_query_cost import pin_whole_relation_binds_idents
with tempfile.TemporaryDirectory() as d:
    pin_whole_relation_binds_idents(d)
EOF
```

Expected: `pin: whole-relation query binds keyword idents, not UUIDs -- OK`

**If the idents come back as UUIDs, STOP and report** — the batch form needs redesigning before E3 means anything.

- [ ] **Step 4: Commit**

```bash
git add evals/at_scale/bench_introduced_by_query_cost.py
git commit -m "$(cat <<'EOF'
Add the #239 cost bench's fixtures and whole-relation pin

Sibling of bench_lineage_query_cost.py, reusing its fixture shape, its
checkpoint-before-timing methodology and its warmup-with-assertion guard.

Pins that the whole-relation ident -> introduced-by query binds the :ident
object rather than the ?e subject: a subject variable in :find returns
minigraf's internal UUID, which has bitten this codebase twice (#133, #141),
and every batch-form number would otherwise measure a query the fix cannot use.

Refs #239
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: E1 — per-call cost vs graph size, HIT and MISS, with the control

**Files:**
- Modify: `evals/at_scale/bench_introduced_by_query_cost.py`

**Interfaces:**
- Consumes: everything from Task 1
- Produces: `experiment_1(tmpdir, sizes, reps) -> dict` keyed by filler size, each value a dict with keys `is_live_hit`, `is_live_miss`, `intro_by_hit`, `intro_by_miss`, `control_hit`, `control_miss`

- [ ] **Step 1: Write E1**

```python
def experiment_1(tmpdir, sizes=SIZES, reps=300):
    """E1: per-call cost for all three queries, HIT and MISS, across a 50x
    filler range.

    HIT pool (:e/hit{i}) carries :ident + :introduced-by, and for the control
    a lineage marker. MISS pool is the filler entities (:e/n{i}) -- they EXIST
    but carry none of those attributes, which is the realistic miss shape
    during ingestion.

    reps=300 because these are sub-ms/call and need volume for a stable mean;
    the same figure bench_lineage_query_cost.py uses for its point query.
    """
    results = {}
    hit_idents = [f":e/hit{i}" for i in range(CANDIDATES_PER_COMMIT)]

    print("=== E1: point-query cost, HIT vs MISS, vary graph size ===")
    print(f"({reps} reps/cell, 1 asserted warmup before each timed loop)")
    header = (f"{'filler':>10} {'is_live H':>10} {'is_live M':>10} "
              f"{'intro H':>10} {'intro M':>10} {'ctrl H':>10} {'ctrl M':>10}")
    print(header)

    for n in sizes:
        db, _ = fresh_db(tmpdir, f"e1_{n}")
        populate_filler(db, n)
        populate_lineage_entities(db, hit_idents)
        add_lineage_markers(db, hit_idents)
        miss_idents = [f":e/n{i}" for i in range(0, n, max(1, n // reps))]

        row = {
            "is_live_hit": timed(mcp_server._entity_ident_is_live, db, hit_idents, reps, True),
            "is_live_miss": timed(mcp_server._entity_ident_is_live, db, miss_idents, reps, False),
            "intro_by_hit": timed(mcp_server._entity_introduced_by_query, db, hit_idents, reps, True),
            "intro_by_miss": timed(mcp_server._entity_introduced_by_query, db, miss_idents, reps, False),
            "control_hit": timed(mcp_server._lineage_is_provisional, db, hit_idents, reps, True),
            "control_miss": timed(mcp_server._lineage_is_provisional, db, miss_idents, reps, False),
        }
        results[n] = row
        print(f"{n:>10} {row['is_live_hit']:>10.4f} {row['is_live_miss']:>10.4f} "
              f"{row['intro_by_hit']:>10.4f} {row['intro_by_miss']:>10.4f} "
              f"{row['control_hit']:>10.4f} {row['control_miss']:>10.4f}")
        del db

    return results
```

- [ ] **Step 2: Add the control validation**

```python
# bench_lineage_query_cost.py measured _lineage_is_provisional at
# 0.046-0.053 ms/call across 100k/1M/5M. A generous band around that: if this
# bench's control lands outside it, something about THIS bench's methodology
# differs from the one whose result we are relying on, and every other number
# here is suspect.
CONTROL_BAND_MS = (0.02, 0.20)


def validate_control(e1):
    """Returns (ok, messages). The control is this bench's self-test."""
    messages = []
    ok = True
    for n, row in e1.items():
        for key in ("control_hit", "control_miss"):
            v = row[key]
            if not (CONTROL_BAND_MS[0] <= v <= CONTROL_BAND_MS[1]):
                ok = False
                messages.append(
                    f"control {key} at {n} filler = {v:.4f} ms/call, outside the "
                    f"{CONTROL_BAND_MS[0]}-{CONTROL_BAND_MS[1]} ms band that "
                    "bench_lineage_query_cost.py established"
                )
    ratio = max(r["control_hit"] for r in e1.values()) / min(
        r["control_hit"] for r in e1.values()
    )
    if ratio >= 2.0:
        ok = False
        messages.append(
            f"control grew {ratio:.2f}x across the size range; it was measured "
            "FLAT (0.046-0.053 ms) -- this bench's methodology disagrees with "
            "the one it is calibrated against"
        )
    return ok, messages
```

- [ ] **Step 3: Smoke-run E1 at tiny sizes**

```bash
python - <<'EOF'
import tempfile, sys
sys.path.insert(0, "/home/aditya/Work/AMC/Minigraf/temporal_reasoning")
from evals.at_scale.bench_introduced_by_query_cost import experiment_1
with tempfile.TemporaryDirectory() as d:
    print(experiment_1(d, sizes=(5_000, 20_000), reps=30))
EOF
```

Expected: a two-row table, all six numbers positive and sub-millisecond, no assertion failures. This only proves the code runs — the real sizes come in Task 5.

- [ ] **Step 4: Commit**

```bash
git add evals/at_scale/bench_introduced_by_query_cost.py
git commit -m "$(cat <<'EOF'
Add E1: point-query cost vs graph size, HIT and MISS

Measures _entity_ident_is_live and _entity_introduced_by_query across a 50x
filler range, with _lineage_is_provisional as a control whose known-flat result
(0.046-0.053 ms, bench_lineage_query_cost.py) validates the methodology.

MISS is measured separately because the #235 result was counter-intuitive: a
MISS came out cheaper than a HIT, which is what refuted the scan hypothesis
there. A MISS that grows while a HIT stays flat is the companion-index
signature.

Refs #239
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: E2 — the second size axis (ident population)

**Files:**
- Modify: `evals/at_scale/bench_introduced_by_query_cost.py`

**Interfaces:**
- Consumes: Task 1 helpers
- Produces: `experiment_2(tmpdir, populations=IDENT_POPULATIONS, filler=1_000_000, reps=300) -> dict` keyed by ident population, values shaped like E1's rows minus the control

**Why this task is separate from E1, and not optional:** total facts and ident population are different variables. `_entity_ident_is_live` queries `:ident`, so its cost may track the number of *entities* rather than graph size. Filler facts inflate the graph without adding idents, so E1 alone could report "flat" and hide a real cliff at high entity counts. `bench_lineage_query_cost.py` splits these axes for the same reason (its E2b varies marker population).

- [ ] **Step 1: Write E2**

```python
def experiment_2(tmpdir, populations=IDENT_POPULATIONS, filler=1_000_000, reps=300):
    """E2: per-call cost as a function of IDENT POPULATION, graph size fixed.

    Distinct from E1's axis: filler facts grow the graph without adding a
    single :ident or :introduced-by fact, so E1 varies "how much unrelated
    data is there" while E2 varies "how many entities of the kind we query
    are there". A query answered by a scan over :ident facts would look flat
    in E1 and grow here.
    """
    results = {}
    print("\n=== E2: point-query cost, vary IDENT POPULATION "
          f"(filler fixed at {filler:,}) ===")
    print(f"({reps} reps/cell, 1 asserted warmup before each timed loop)")
    print(f"{'idents':>10} {'is_live H':>10} {'is_live M':>10} "
          f"{'intro H':>10} {'intro M':>10}")

    for pop in populations:
        db, _ = fresh_db(tmpdir, f"e2_{pop}")
        populate_filler(db, filler)
        pop_idents = [f":e/hit{i}" for i in range(pop)]
        populate_lineage_entities(db, pop_idents)
        miss_idents = [f":e/n{i}" for i in range(0, filler, max(1, filler // reps))]

        row = {
            "is_live_hit": timed(mcp_server._entity_ident_is_live, db, pop_idents, reps, True),
            "is_live_miss": timed(mcp_server._entity_ident_is_live, db, miss_idents, reps, False),
            "intro_by_hit": timed(mcp_server._entity_introduced_by_query, db, pop_idents, reps, True),
            "intro_by_miss": timed(mcp_server._entity_introduced_by_query, db, miss_idents, reps, False),
            "population": pop,
        }
        results[pop] = row
        print(f"{pop:>10} {row['is_live_hit']:>10.4f} {row['is_live_miss']:>10.4f} "
              f"{row['intro_by_hit']:>10.4f} {row['intro_by_miss']:>10.4f}")
        del db

    return results
```

- [ ] **Step 2: Smoke-run E2**

```bash
python - <<'EOF'
import tempfile, sys
sys.path.insert(0, "/home/aditya/Work/AMC/Minigraf/temporal_reasoning")
from evals.at_scale.bench_introduced_by_query_cost import experiment_2
with tempfile.TemporaryDirectory() as d:
    print(experiment_2(d, populations=(100, 500), filler=5_000, reps=30))
EOF
```

Expected: a two-row table, four positive sub-millisecond numbers per row, no assertion failures.

- [ ] **Step 3: Commit**

```bash
git add evals/at_scale/bench_introduced_by_query_cost.py
git commit -m "$(cat <<'EOF'
Add E2: point-query cost vs ident population, graph size fixed

Total facts and ident population are different variables. _entity_ident_is_live
queries :ident, so its cost may track entity count rather than graph size, and
filler facts grow the graph without adding a single :ident. Varying only filler
could report flat and hide a cliff at high entity counts.

Refs #239
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: E3 — the batch form and the crossover

**Files:**
- Modify: `evals/at_scale/bench_introduced_by_query_cost.py`

**Interfaces:**
- Consumes: `whole_relation`, `timed_whole_relation`, E1's results
- Produces: `experiment_3(tmpdir, e1, sizes=SIZES, reps=20) -> dict` keyed by filler size, values with keys `batch_ms`, `batch_len`, `n_point_ms`, `crossover_n`

- [ ] **Step 1: Write E3**

```python
def experiment_3(tmpdir, e1, sizes=SIZES, reps=20):
    """E3: whole-relation batch cost, and the crossover against N point
    queries at CANDIDATES_PER_COMMIT.

    The batch form is whole-relation-plus-Python-narrowing, because minigraf
    has no set-membership predicate (`contains?` is a STRING predicate,
    query/datalog/parser.rs:1175) and so a query cannot be scoped to N specific
    idents server-side. _preload_known_deps already uses exactly this shape.

    reps=20 because the batch call is milliseconds, not microseconds, and is
    stable well before 20 -- the same figure bench_lineage_query_cost.py uses
    for its preload.

    crossover_n is the number of point queries whose combined cost equals ONE
    batch call. Below it, point queries win; above it, the batch does. Compare
    against CANDIDATES_PER_COMMIT to decide whether a per-commit batch is
    worth building.
    """
    results = {}
    hit_idents = [f":e/hit{i}" for i in range(CANDIDATES_PER_COMMIT)]

    print("\n=== E3: whole-relation batch vs N point queries ===")
    print(f"({reps} reps/cell, 1 asserted warmup before each timed loop; "
          f"N = {CANDIDATES_PER_COMMIT} candidates/commit)")
    print(f"{'filler':>10} {'batch ms':>10} {'rows':>8} "
          f"{'N x point':>11} {'crossover N':>12} {'winner':>10}")

    for n in sizes:
        db, _ = fresh_db(tmpdir, f"e3_{n}")
        populate_filler(db, n)
        populate_lineage_entities(db, hit_idents)

        batch_ms, batch_len = timed_whole_relation(
            db, reps, expected_len=CANDIDATES_PER_COMMIT
        )
        point_ms = e1[n]["intro_by_hit"]
        n_point_ms = point_ms * CANDIDATES_PER_COMMIT
        crossover_n = batch_ms / point_ms if point_ms else float("inf")
        winner = "batch" if n_point_ms > batch_ms else "point"

        results[n] = {
            "batch_ms": batch_ms,
            "batch_len": batch_len,
            "point_ms": point_ms,
            "n_point_ms": n_point_ms,
            "crossover_n": crossover_n,
        }
        print(f"{n:>10} {batch_ms:>10.3f} {batch_len:>8} {n_point_ms:>11.1f} "
              f"{crossover_n:>12.0f} {winner:>10}")
        del db

    return results
```

- [ ] **Step 2: Smoke-run E3**

```bash
python - <<'EOF'
import tempfile, sys
sys.path.insert(0, "/home/aditya/Work/AMC/Minigraf/temporal_reasoning")
from evals.at_scale.bench_introduced_by_query_cost import experiment_1, experiment_3
with tempfile.TemporaryDirectory() as d:
    e1 = experiment_1(d, sizes=(5_000,), reps=30)
    print(experiment_3(d, e1, sizes=(5_000,), reps=5))
EOF
```

Expected: one row, `rows` equal to 1265, a finite `crossover N`, no assertion failures.

- [ ] **Step 3: Commit**

```bash
git add evals/at_scale/bench_introduced_by_query_cost.py
git commit -m "$(cat <<'EOF'
Add E3: whole-relation batch cost and the per-commit crossover

The batch form is whole-relation plus Python narrowing, because minigraf has no
set-membership predicate -- contains? is a string predicate -- so a query cannot
be scoped to N specific idents server-side. _preload_known_deps already uses
this shape.

Reports the crossover N: the number of point queries costing one batch call.
Compared against 1265 candidates/commit, that decides whether a per-commit
batch is worth building at all.

Refs #239
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: E4, the verdict, JSON output, CLI, and the real run

**Files:**
- Modify: `evals/at_scale/bench_introduced_by_query_cost.py`
- Create: `evals/at_scale/results/239-introduced-by-query-cost.json`
- Modify: `evals/at_scale/benchmark.md`

**Interfaces:**
- Consumes: everything above
- Produces: `compute_verdict(e1, e2) -> dict`, `main() -> int`

- [ ] **Step 1: Write E4, the verdict, and main**

```python
def experiment_4(tmpdir, reps=300):
    """E4: one point at roughly the scale the killed #245 acceptance run
    reached -- ~247 commits of this repo, a ~35 MB graph.

    Exists to answer a specific question: is the 52x per-commit gap between
    #239's measured 2.74 s/commit and the killed run's 142.5 s/commit explained
    by graph size, or does it remain unaccounted for? A single data point, not
    an ingestion.

    250k facts approximates that graph's fact count. It is an estimate and is
    reported as one.
    """
    n = 250_000
    db, path = fresh_db(tmpdir, "e4")
    populate_filler(db, n)
    hit_idents = [f":e/hit{i}" for i in range(CANDIDATES_PER_COMMIT)]
    populate_lineage_entities(db, hit_idents)
    miss_idents = [f":e/n{i}" for i in range(0, n, max(1, n // reps))]

    row = {
        "filler": n,
        "graph_bytes": os.path.getsize(path),
        "is_live_hit": timed(mcp_server._entity_ident_is_live, db, hit_idents, reps, True),
        "is_live_miss": timed(mcp_server._entity_ident_is_live, db, miss_idents, reps, False),
        "intro_by_hit": timed(mcp_server._entity_introduced_by_query, db, hit_idents, reps, True),
        "intro_by_miss": timed(mcp_server._entity_introduced_by_query, db, miss_idents, reps, False),
    }
    del db

    print("\n=== E4: one point at the killed run's approximate scale ===")
    print(f"filler={n:,}  graph={row['graph_bytes'] / 1e6:.1f} MB")
    print(f"  _entity_ident_is_live       HIT {row['is_live_hit']:.4f}  "
          f"MISS {row['is_live_miss']:.4f} ms/call")
    print(f"  _entity_introduced_by_query HIT {row['intro_by_hit']:.4f}  "
          f"MISS {row['intro_by_miss']:.4f} ms/call")
    return row


# Fixed by the spec BEFORE any data existed, so it cannot be chosen to fit the
# result. The control's own spread across 50x is ~1.15x, so 2x sits far outside
# measured noise while leaving no room to argue a genuine cliff into "flat".
FLAT_THRESHOLD = 2.0

MEASURED_KEYS = ("is_live_hit", "is_live_miss", "intro_by_hit", "intro_by_miss")


def compute_verdict(e1, e2):
    """FLAT iff every measured series grows by less than FLAT_THRESHOLD across
    BOTH size axes. Returns a dict carrying the ratios that produced it."""
    ratios = {}
    for label, table in (("filler", e1), ("ident_population", e2)):
        for key in MEASURED_KEYS:
            series = [table[k][key] for k in sorted(table)]
            lo, hi = min(series), max(series)
            ratios[f"{label}:{key}"] = hi / lo if lo else float("inf")
    worst_name = max(ratios, key=ratios.get)
    worst = ratios[worst_name]
    return {
        "flat": worst < FLAT_THRESHOLD,
        "threshold": FLAT_THRESHOLD,
        "worst_ratio": worst,
        "worst_series": worst_name,
        "ratios": ratios,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", default=os.path.join(
        REPO, "evals/at_scale/results/239-introduced-by-query-cost.json"))
    ap.add_argument("--quick", action="store_true",
                    help="tiny sizes for a smoke run; NOT a valid measurement")
    args = ap.parse_args()

    sizes = (5_000, 20_000, 50_000) if args.quick else SIZES
    populations = (100, 500, 1_000) if args.quick else IDENT_POPULATIONS
    filler = 20_000 if args.quick else 1_000_000
    reps = 30 if args.quick else 300
    batch_reps = 5 if args.quick else 20

    with tempfile.TemporaryDirectory() as tmpdir:
        pin_whole_relation_binds_idents(tmpdir)
        e1 = experiment_1(tmpdir, sizes=sizes, reps=reps)
        e2 = experiment_2(tmpdir, populations=populations, filler=filler, reps=reps)
        e3 = experiment_3(tmpdir, e1, sizes=sizes, reps=batch_reps)
        e4 = experiment_4(tmpdir, reps=reps) if not args.quick else None

    control_ok, control_messages = validate_control(e1)
    verdict = compute_verdict(e1, e2)

    report = {
        "quick": args.quick,
        "control_ok": control_ok,
        "control_messages": control_messages,
        "e1_filler_axis": {str(k): v for k, v in e1.items()},
        "e2_ident_population_axis": {str(k): v for k, v in e2.items()},
        "e3_batch_crossover": {str(k): v for k, v in e3.items()},
        "e4_real_scale_point": e4,
        "verdict": verdict,
        "candidates_per_commit": CANDIDATES_PER_COMMIT,
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"\nwrote {args.output}")

    print("\n=== VERDICT ===")
    if args.quick:
        print("QUICK RUN -- smoke only, NOT a valid measurement.")
        return 0
    if not control_ok:
        print("INVALID MEASUREMENT: the control did not reproduce.")
        for m in control_messages:
            print(f"  - {m}")
        print("The verdict is withheld. bench_lineage_query_cost.py measured")
        print("_lineage_is_provisional FLAT at 0.046-0.053 ms/call; this bench")
        print("disagrees, so its other numbers cannot be trusted either.")
        return 2
    print(f"control reproduced ({CONTROL_BAND_MS[0]}-{CONTROL_BAND_MS[1]} ms band): OK")
    print(f"worst growth: {verdict['worst_ratio']:.2f}x on {verdict['worst_series']} "
          f"(threshold {FLAT_THRESHOLD}x)")
    if verdict["flat"]:
        print("FLAT -- per-call cost is constant in graph size.")
        print("#239's premise holds: the cost is CALL COUNT. Fix is a per-commit")
        print("batch or a write-through cache; E3's crossover picks between them.")
    else:
        print("NOT FLAT -- per-call cost grows.")
        print("Batching only reduces how often we pay a growing cost. The fix is")
        print("a companion index -- the #152/#236 pattern a third time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-run the whole thing**

```bash
python evals/at_scale/bench_introduced_by_query_cost.py --quick \
  --output /tmp/claude-1000/-home-aditya-Work-AMC-Minigraf-temporal-reasoning/32f595b9-0f88-4780-aba4-63f2aabc0ca9/scratchpad/quick.json
```

Expected: every table prints, the JSON is written, and the verdict section says `QUICK RUN -- smoke only`. Exit 0.

- [ ] **Step 3: The real run**

```bash
python evals/at_scale/bench_introduced_by_query_cost.py
```

This builds a 5M-fact fixture, so expect **several minutes**, not seconds. Run it in the FOREGROUND with a generous timeout (600000 ms). If it exceeds that, re-run in the background and poll with `ps -p <PID>` in an `until` loop — **never `pgrep -f`**, which matches the polling shell itself and never goes false.

**Report the verdict verbatim, whatever it is.** Do not adjust `FLAT_THRESHOLD`, `CONTROL_BAND_MS`, or any size to change the outcome. A NOT FLAT result is a finding, not a failure — it redirects #239 from batching to a companion index, which is the entire reason this bench exists. An `INVALID MEASUREMENT` is also a real outcome: report it and stop rather than tuning until the control passes.

- [ ] **Step 4: Add the benchmark.md entry**

`evals/at_scale/benchmark.md` uses `## <Kind> — <identifier>` headers followed by a bullet line and a metrics table (see `## Ingestion Run — 20260719T074053Z`); it already carries non-run entries too, e.g. `## No Ingestion Run — fix-235-two-value-introduced-by`. Append this, filling every `<…>` from the JSON you just wrote:

```markdown
## Point-Query Cost Bench — 239-introduced-by-query-cost

- Repo: `.` @ `bench-239-introduced-by-query-cost`
- Script: `evals/at_scale/bench_introduced_by_query_cost.py`
- Raw: `evals/at_scale/results/239-introduced-by-query-cost.json`
- Question: is per-ident point-query cost constant in graph size? #239's fix
  direction depends on the answer.

| Metric | Value |
|---|---|
| Verdict | <FLAT or NOT FLAT> |
| Threshold (fixed in the spec before any data) | 2.0x |
| Worst growth | <ratio>x on <series name> |
| Control reproduced | <yes/no> (`_lineage_is_provisional`, expected 0.046-0.053 ms/call) |
| `_entity_ident_is_live` HIT, 100k → 5M | <a> → <b> ms/call |
| `_entity_ident_is_live` MISS, 100k → 5M | <a> → <b> ms/call |
| `_entity_introduced_by_query` HIT, 100k → 5M | <a> → <b> ms/call |
| `_entity_introduced_by_query` MISS, 100k → 5M | <a> → <b> ms/call |
| Ident-population axis, worst growth | <ratio>x |
| Batch crossover N (vs 1265 candidates/commit) | <n> |
| E4 real-scale point (~35 MB graph) | `_entity_introduced_by_query` HIT <x> ms/call |

**What this means for #239:** <one line — FLAT: the cost is call count, so a
per-commit batch or write-through cache; NOT FLAT: batching only reduces how
often we pay a growing cost, so a companion index, the #152/#236 pattern a
third time.>
```

- [ ] **Step 5: Commit**

```bash
git add evals/at_scale/bench_introduced_by_query_cost.py \
        evals/at_scale/results/239-introduced-by-query-cost.json \
        evals/at_scale/benchmark.md
git commit -m "$(cat <<'EOF'
Add E4, the verdict, and record the #239 cost measurement

Answers the question #239's fix depends on: whether per-ident point-query cost
is constant in graph size. The FLAT threshold and the control band were both
fixed in the design spec before any data existed.

The control (_lineage_is_provisional, known flat at 0.046-0.053 ms/call from
bench_lineage_query_cost.py) is the bench's self-test: a run whose control does
not reproduce reports INVALID MEASUREMENT and withholds the verdict rather than
publishing numbers it cannot stand behind.

Refs #239
Refs #245
Refs #222

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] `python evals/at_scale/bench_introduced_by_query_cost.py --quick` exits 0
- [ ] The committed JSON has `"quick": false` and `"control_ok": true`
- [ ] `git log master..HEAD --format='%B' | grep -inE '(clos(e|es|ed)|fix(es|ed)?|resolv(e|es|ed))[[:space:]]+#[0-9]+'` returns nothing
- [ ] `mcp_server.py` is untouched: `git diff master..HEAD --stat -- mcp_server.py` is empty
