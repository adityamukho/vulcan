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
