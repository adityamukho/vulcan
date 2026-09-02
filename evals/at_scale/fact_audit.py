# evals/at_scale/fact_audit.py
"""Cross-check a graph against its fact index (#302).

WHY THIS EXISTS. `stderr_capture.py` is the at-scale tier's corruption
detector, and it can only see corruption that PRINTS. #302 measured a fact
page garbled to `0xff` costing ~11% of a graph's facts with zero bytes on
stderr and zero `error_signals`: the run reported clean. Every other gate the
harness has is blind to it too -- `_run_ingestion` isolates per-commit
failures so `skipped_commits` stays empty, and `processed`/`final_status`
count commits, not facts.

THE SECOND WITNESS. `<graph>.fts.sqlite3` is written from the same triples in
the same transaction boundary as the graph itself, but by a different storage
engine (SQLite). It therefore knows what was written independently of whether
the graph can still produce it. Every current index row the graph cannot
produce is a candidate loss; the reverse -- a graph fact absent from the index
-- catches fabrication (a garbled page can invent facts, measured) and index
write failure.

THE CLEAN DIVERGENCE IS ZERO, EXACTLY, and that is what makes this gate-able
without a tolerance. Getting there took two normalizations, both measured
(evals/at_scale/probe_graph_index_divergence.py, results/302-graph-index-
divergence.json):

  * ENTITY SPACE. The graph reports UUIDs; the index stores the resolved
    `:ident` (`_resolved_facts_triples`, #194). Index entities are mapped
    FORWARD into UUID space the way minigraf itself does, rather than mapping
    graph UUIDs back via the graph's own `:ident` facts -- only memory-prefixed
    entities get a self-referencing `:ident` (`_ensure_memory_idents`), so the
    backward map leaves every ingestion entity unmapped and reports 136 facts
    missing from EACH side on a 100-commit graph, a pure artifact.
  * VALUE TYPE. minigraf returns a typed value (`:version 1` comes back as an
    int); the index stores the datalog text it was transacted from ('1'). Left
    alone that is a divergence of exactly the number of non-string-valued
    facts. `_index_text` renders the graph side back into that text -- `str()`
    for everything except a bool, which Python spells `True` and EDN spells
    `true`. The bool case tests the Python TYPE, never the text, so a fact
    whose value is genuinely the STRING "True" keeps its capitalization on
    both sides and stays auditable.

THERE IS NO LONGER AN EXCLUDED CLASS OF FACT, and the history matters because
it says what this gate is worth. `_FACTS_TRIPLE_PATTERN` (mcp_server.py) used
to accept a quoted string, a keyword, a number or a #uuid/#inst literal as a
triple's value -- but not a bare `true`/`false`. So `[:function/f :static
true]` was transacted into the graph and never reached the index: 83 facts on
the 822-commit at-scale graph, every one of them `:static`. This audit found
them, reported them as `unindexed_boolean_facts` outside `divergence` (the
alternative being a permanently red gate that said nothing), and #303 fixed
the pattern. The key is DELETED rather than left reporting zero -- a key that
always says 0 reads like a covered case while covering nothing.

What the exclusion cost while it stood is the reason not to reintroduce one:
a `:static` fact the graph had genuinely LOST and a `:static` fact the index
could never hold produced the same number. `TestBooleanFactsAreAudited::
test_a_lost_boolean_fact_is_now_reported_as_divergence` is the test that could
not be written before.

WHAT IT DOES NOT COVER. A fact absent from BOTH witnesses is invisible here --
if a write never reached either, or if both were damaged the same way. This
detects divergence between two witnesses, which is strictly more than the
stderr scanner could see, not a proof of integrity. Upstream page checksums
(#302's option 3) are the only thing that would be.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections import Counter
from typing import Any, Optional

from evals.at_scale.introduced_by_audit import (
    entities_without_introduced_by,
    introduced_by_duplicates,
)

# Every fact the graph can currently produce. Wildcard on all three positions:
# a per-attribute scan would be blind to loss in any attribute nobody thought
# to list.
SCAN_QUERY = "[:find ?e ?a ?v :where [?e ?a ?v]]"

_SAMPLE_CAP = 5


def entity_uuid(entity: str) -> str:
    """The entity id the graph reports for an index row's entity.

    minigraf derives a keyword entity's id as
    `Uuid::new_v5(&Uuid::NAMESPACE_OID, k.as_bytes())` with the leading colon
    still attached (`edn_to_entity_id`, minigraf src/query/datalog/matcher.rs:
    733). Anything that does not start with a colon is already a UUID string
    and is returned unchanged -- the same branch that function takes first.
    """
    if not entity.startswith(":"):
        return entity
    return str(uuid.uuid5(uuid.NAMESPACE_OID, entity))


def _index_text(value: Any) -> str:
    """Render a graph value as the datalog text the index stores.

    `str()` for everything except bools, which Python spells `True` and EDN
    spells `true`. The test is on the Python TYPE, never on the text: a fact
    whose value is genuinely the STRING "True" keeps its capitalization on
    both sides, and a blanket `.lower()` would invent a divergence there
    (#303).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _graph_facts(query_fn: Any) -> tuple[Counter, int, Optional[str]]:
    """(counter of (entity, attribute, index-text of value), distinct
    entities, error).

    A scan that raises is RECORDED, not propagated: a graph too damaged to
    query is the loudest possible result of this audit, and an exception here
    would destroy the metrics of the run that found it.
    """
    try:
        res = query_fn(SCAN_QUERY)
    except Exception as e:  # noqa: BLE001 -- recorded, see docstring
        return Counter(), 0, f"{type(e).__name__}: {e}"
    if not res.get("ok"):
        return Counter(), 0, f"query not ok: {res!r}"
    rows = res.get("results") or []
    facts = Counter()
    entities = set()
    for e, a, v in rows:
        facts[(e, a, _index_text(v))] += 1
        entities.add(e)
    return facts, len(entities), None


def audit_graph_against_index(
    index_path: str,
    query_fn: Any = None,
    sample_cap: int = _SAMPLE_CAP,
    expected_graph_path: Optional[str] = None,
) -> dict[str, Any]:
    """Compare every current fact index row against what the graph produces.

    query_fn defaults to mcp_server.handle_minigraf_query, which takes a lease
    rather than opening its own handle -- the single-handle invariant applies
    here like everywhere else. That also means the graph side of this
    comparison is whatever mcp_server is CURRENTLY BOUND TO, which is not
    visible in the arguments: pass expected_graph_path and a mismatch is
    recorded as an audit_error instead of being reported as a divergence of
    every fact in two unrelated graphs.

    The index side is STREAMED and decremented against the graph's counter, so
    only one full copy of the fact set is ever in memory. On an at-scale graph
    that is the difference between one large dict and two.

    Returns a dict that always carries `divergence`; `audit_error` is None on
    a clean audit and a string when the graph could not be scanned at all
    (which a consumer must treat as a failure, not as "no divergence found").
    """
    import mcp_server

    if query_fn is None:
        query_fn = mcp_server.handle_minigraf_query

    t0 = time.perf_counter()
    bound = mcp_server._graph_path_current()
    if expected_graph_path is not None and str(bound) != str(expected_graph_path):
        return {
            "graph_facts": 0, "graph_distinct_entities": 0,
            "index_total_rows": 0, "index_current_rows": 0,
            "missing_from_graph": 0, "missing_from_index": 0, "divergence": 0,
            "missing_from_graph_sample": [], "missing_from_index_sample": [],
            # None, never a zero dict: nothing was scanned, so "no affected
            # entities" would be a claim about a graph never read. Same
            # reasoning as audit_error itself.
            "introduced_by_duplicates": None,
            "entities_without_introduced_by": None,
            "audit_seconds": time.perf_counter() - t0,
            "audit_error": (
                f"bound to {bound!r}, expected {expected_graph_path!r} -- "
                f"refusing to audit one graph against another's index"
            ),
        }

    graph_facts, distinct_entities, scan_error = _graph_facts(query_fn)
    graph_total = sum(graph_facts.values())

    # #287, riding this scan rather than paying for its own. Kept OUT of
    # `divergence` deliberately: a two-value :introduced-by is faithfully in
    # the index too, so the two witnesses agree perfectly about a graph that
    # is wrong -- see introduced_by_audit.py. None on a failed scan, because
    # an empty fact set trivially has no duplicates.
    duplicates = (
        None if scan_error else introduced_by_duplicates(graph_facts, sample_cap)
    )

    # #316, the opposite defect, riding the same pass. Kept OUT of
    # `divergence` for the same reason and then some: the index is missing
    # exactly the facts the graph is missing, so the two witnesses agree
    # perfectly about an entity that has no lineage at all. None on a failed
    # scan -- an empty fact set trivially has no orphans, and would report a
    # `code_entities_scanned` of 0 that reads like a graph with no code in it
    # rather than a graph never read.
    orphans = (
        None if scan_error else entities_without_introduced_by(graph_facts, sample_cap)
    )

    remaining = Counter(graph_facts)
    index_current = 0
    index_total = 0
    missing_from_graph = 0
    missing_samples: list[list[str]] = []
    index_error = None
    try:
        con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            index_total = con.execute("SELECT count(*) FROM facts_dedup").fetchone()[0]
            # facts_dedup, not facts_fts: the two hold the same rows by
            # construction (shared rowids -- fact_index.insert_facts) and this
            # one is an ordinary B-tree table rather than an FTS5 virtual one.
            # valid_to is stored as '' for a current fact, never NULL.
            for entity, attribute, value in con.execute(
                "SELECT entity, attribute, value FROM facts_dedup WHERE valid_to = ''"
            ):
                index_current += 1
                key = (entity_uuid(entity), attribute, value)
                if remaining.get(key, 0) > 0:
                    remaining[key] -= 1
                else:
                    missing_from_graph += 1
                    if len(missing_samples) < sample_cap:
                        missing_samples.append(list(key))
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 -- recorded like the scan error
        index_error = f"{type(e).__name__}: {e}"

    missing_from_index = 0
    extra_samples: list[list[str]] = []
    for key, n in remaining.items():
        if n <= 0:
            continue
        missing_from_index += n
        if len(extra_samples) < sample_cap:
            extra_samples.append(list(key))

    errors = [e for e in (scan_error, index_error) if e]
    return {
        "graph_facts": graph_total,
        "graph_distinct_entities": distinct_entities,
        "index_total_rows": index_total,
        "index_current_rows": index_current,
        # Index witnessed it, graph cannot produce it: the #302 signal.
        "missing_from_graph": missing_from_graph,
        # Graph produces it, index never witnessed it: fabrication by a
        # garbled page (measured -- one target invented 33 facts) or a failed
        # index write.
        "missing_from_index": missing_from_index,
        "divergence": missing_from_graph + missing_from_index,
        "missing_from_graph_sample": missing_samples,
        "missing_from_index_sample": extra_samples,
        # #287. An affected graph must be REBUILT into a fresh graph path --
        # never repaired, never re-ingested in place.
        "introduced_by_duplicates": duplicates,
        # #316. Same remedy, opposite defect: live code entities with NO
        # lineage. Carries its own `code_entities_scanned` denominator, which
        # is the positive control -- a check that matched nothing would also
        # report 0 entities.
        "entities_without_introduced_by": orphans,
        "audit_seconds": time.perf_counter() - t0,
        "audit_error": "; ".join(errors) if errors else None,
    }
