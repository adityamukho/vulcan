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


def experiment_1(tmpdir, sizes=SIZES, reps=300):
    """E1: per-call cost for all three queries, HIT and MISS, across a 50x
    filler range.

    HIT pool (:e/hit{i}) carries :ident + :introduced-by, and for the control
    a lineage marker. MISS pool is the filler entities (:e/n{i}) -- they EXIST
    but carry none of those attributes, which is the realistic miss shape
    during ingestion.
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


# PROVENANCE: bench_lineage_query_cost.py originally recorded
# _lineage_is_provisional at 0.046-0.053 ms/call across 100k/1M/5M filler, on
# 2026-08-04. That figure does NOT reproduce: a fresh reference run of that
# same, unmodified bench under .venv on 2026-08-11 gave 0.1819 ms/call at
# 100k filler (vs. 1.2132 ms/call under system python's stale minigraf 1.1.1).
# Different hardware or a different minigraf version is the likely cause --
# we cannot tell which from here. Because the absolute figure is
# machine-dependent and has already gone stale once, it is not usable as a
# pass/fail band. What the control actually needs to validate is FLATNESS --
# that per-call cost does not grow with graph size -- which is
# machine-independent and is the property the rest of this bench's verdict
# depends on. So there is only a ceiling (a misconfiguration tripwire, not a
# calibration) and a growth-ratio check (the real self-test); there is no
# lower bound, because a control that comes back faster than expected is not
# evidence of a broken methodology.
CONTROL_CEILING_MS = 5.0


def validate_control(e1):
    """Returns (ok, messages). The control is this bench's self-test.

    Two checks, not equally sound:

    - Ceiling (CONTROL_CEILING_MS): a misconfiguration tripwire. If the
      control is wildly slower than any healthy run, something about the
      environment is wrong -- check `which python` (must be .venv/bin/python)
      and `pip show minigraf` against the floor in pyproject.toml (>=1.2.3).
      This is NOT a calibrated performance band; see the PROVENANCE comment
      above CONTROL_CEILING_MS for why an absolute band was dropped.
    - Growth ratio (>=2.0x across the size range): the real self-test. It
      is machine-independent and asks whether the control reproduces
      FLATNESS, the property this bench's E1/E3 verdicts actually rely on.

    A failure on either check means the run's methodology is suspect and its
    verdict must be WITHHELD -- E1/E3 numbers from a run that fails
    validate_control should be reported as an invalid measurement, not
    published as a result.
    """
    messages = []
    ok = True
    for n, row in e1.items():
        for key in ("control_hit", "control_miss"):
            v = row[key]
            if v > CONTROL_CEILING_MS:
                ok = False
                messages.append(
                    f"control {key} at {n} filler = {v:.4f} ms/call, above the "
                    f"{CONTROL_CEILING_MS} ms misconfiguration ceiling -- check "
                    "`which python` (must be .venv/bin/python) and `pip show "
                    "minigraf` against the >=1.2.3 floor in pyproject.toml. "
                    "Verdict must be withheld, not published."
                )
    ratio = max(r["control_hit"] for r in e1.values()) / min(
        r["control_hit"] for r in e1.values()
    )
    if ratio >= 2.0:
        ok = False
        messages.append(
            f"control grew {ratio:.2f}x across the size range; it must be "
            "FLAT (this is the actual self-test, machine-independent) -- "
            "this bench's methodology disagrees with itself across graph "
            "sizes. Verdict must be withheld, not published."
        )
    return ok, messages
