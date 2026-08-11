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
    diff_descriptions,
    position_correct_descriptions,
)
from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    build_ts_positions,
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
