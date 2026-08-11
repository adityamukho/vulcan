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

from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    affected_positions,
    build_ts_positions,
    edge_live_at,
    gitlink_event_count,
    invert_ms_to_positions,
    resume_envelopes,
)

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
