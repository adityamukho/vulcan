# evals/at_scale/introduced_by_audit.py
"""Detect the two-value `:introduced-by` corruption from #235 (#287).

WHY DETECTION, WITH NO REPAIR. #244 proposed repairing this and was closed on
the standing "rebuild, never migrate" decision (CLAUDE.md; docs/superpowers/
specs/2026-08-14-ident-rule-r3-and-format-version-design.md). The detection is
worth having on its own terms, because the answer decides whether a graph must
be thrown away and rebuilding is not free. Without it a user cannot tell a
healthy graph from a condemned one.

RE-RUNNING INGESTION REPAIRS NOTHING, and that is mechanical, not a policy
choice. A finished run parks `:ingestion/correction-sweep-through` at
frontier-high's `:hi-hash`. On the next run `_correction_sweep_select_position`
computes `ceiling_pos` from that bound and returns None on its FIRST call
(`if pos > ceiling_pos: return None`, mcp_server.py), so
`_correction_sweep_apply` -- which owns the repair -- runs zero times.
Measured during #235: watermark at linearization position 13 of 13, one
`select` call, zero `apply` calls. So an affected graph is unreachable by the
sweep that would have healed it mid-run, and the only remedy is to rebuild
into a FRESH graph path -- never repaired, never re-ingested in place.

IT RIDES THE SCAN IT DOES NOT PAY FOR. `fact_audit._graph_facts` already runs
`[:find ?e ?a ?v]` over the whole graph and builds a Counter of every fact.
Everything below is one pass over that dict, so the marginal cost of this
check is not a second query, a second scan, or a second lease.

IT IS NOT A SECOND-WITNESS CHECK, and is deliberately kept out of
`fact_audit`'s `divergence` for that reason. `divergence` means "the graph and
its index disagree" -- two independent storage engines cross-checking each
other. This is a well-formedness check on the graph side ALONE: both
`:introduced-by` values are faithfully in the index too, so a corrupt graph
diverges by zero. Folding the two into one number would make it mean two
incomparable things.

NARROWED TO `:introduced-by` ON PURPOSE. `:contains`, `:modified-in` and
`:depends-on` are legitimately multi-valued -- a module contains every function
in it. A detector keyed on "this entity has two values for some attribute"
would report every module in every healthy graph.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

INTRODUCED_BY = ":introduced-by"

_SAMPLE_CAP = 5


def introduced_by_duplicates(
    graph_facts: Counter, sample_cap: int = _SAMPLE_CAP
) -> dict[str, Any]:
    """Entities holding more than one live `:introduced-by` value.

    graph_facts is `fact_audit._graph_facts`' Counter, keyed by
    `(entity, attribute, index-text of value)`.

    Returns `{"entities": int, "sample": [[name, [value, ...]], ...]}`.

    `entities` is the number the rebuild decision rests on and is never
    capped; `sample` is. The count is of DISTINCT values, read from the number
    of Counter keys rather than their counts: identical (entity, attribute,
    value) triples collapse into one key with a count above one, and that is a
    different defect with a different answer, not #235.

    Each sampled entity is named by its own `:ident` fact when the scan
    carries one -- every code entity writes a self-referencing `[ident :ident
    "ident"]` (`_build_code_triples`), and it is already in this Counter, so
    naming it costs nothing. An entity without one falls back to the raw
    entity id: a partial write is exactly the state this looks for, and
    dropping such an entity from the sample would hide the worst case.
    """
    values_by_entity: dict[str, set] = defaultdict(set)
    idents: dict[str, str] = {}
    for entity, attribute, value in graph_facts:
        if attribute == INTRODUCED_BY:
            values_by_entity[entity].add(value)
        elif attribute == ":ident":
            idents[entity] = value

    affected = 0
    sample: list[list[Any]] = []
    for entity, values in values_by_entity.items():
        if len(values) < 2:
            continue
        affected += 1
        if len(sample) < sample_cap:
            sample.append([idents.get(entity, entity), sorted(values)])
    return {"entities": affected, "sample": sample}
