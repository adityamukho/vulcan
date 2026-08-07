# tests/test_at_scale_dep_preload_probe.py
"""Unit tests for the #245 exposure probe's analysis primitives.

The probe's headline number cannot be asserted -- it is what the probe exists
to discover. What CAN be asserted are the two components that could silently
produce a WRONG number: the timestamp-to-position inversion and the
affected-position derivation. That is the exact error class that cost two fix
rounds on #238, where a date-bound counterfactual made an inadequate test look
adequate.
"""

from evals.at_scale.probe_dep_preload_exposure import (
    VALID_TIME_FOREVER_MS,
    affected_positions,
    build_ts_positions,
    edge_live_at,
    invert_ms_to_positions,
    resume_envelopes,
)

# Position 2 is inverted: it sits above position 1 but carries an earlier date.
# Positions 3 and 4 share a timestamp, so inversion of that instant is
# ambiguous and must yield both.
META = [
    ("h0", "2026-01-01T00:00:00Z", "a@b.com", "s0"),
    ("h1", "2026-01-05T00:00:00Z", "a@b.com", "s1"),
    ("h2", "2026-01-02T00:00:00Z", "a@b.com", "s2"),
    ("h3", "2026-01-06T00:00:00Z", "a@b.com", "s3"),
    ("h4", "2026-01-06T00:00:00Z", "a@b.com", "s4"),
]


class TestBuildTsPositions:
    def test_maps_each_timestamp_to_its_positions(self):
        assert build_ts_positions(META)["2026-01-01T00:00:00Z"] == [0]

    def test_collision_yields_every_colliding_position(self):
        assert build_ts_positions(META)["2026-01-06T00:00:00Z"] == [3, 4]


class TestResumeEnvelopes:
    def test_envelope_is_the_running_maximum(self):
        assert resume_envelopes(META) == [
            "2026-01-01T00:00:00Z",
            "2026-01-05T00:00:00Z",
            "2026-01-05T00:00:00Z",
            "2026-01-06T00:00:00Z",
            "2026-01-06T00:00:00Z",
        ]


class TestAffectedPositions:
    def test_selects_exactly_the_structurally_exposed_positions(self):
        # W=0: T_hi == ts, and no later position carries a date <= 01-01. Clean.
        # W=1: position 2 is above it with an earlier date -> wrong inclusion.
        # W=2: T_hi (01-05) > ts (01-02)             -> wrong exclusion.
        # W=3: position 4 is above it and ties its date -> wrong inclusion,
        #      because the bound is half-open containment [vf, vt) ∋ ts(W)
        #      and vf <= ts(W) admits an exact tie.
        # W=4: last position; T_hi == ts and nothing above. Clean.
        assert affected_positions(META) == [1, 2, 3]

    def test_a_strictly_monotonic_history_exposes_nothing(self):
        monotonic = [
            ("h0", "2026-01-01T00:00:00Z", "a@b.com", "s0"),
            ("h1", "2026-01-02T00:00:00Z", "a@b.com", "s1"),
            ("h2", "2026-01-03T00:00:00Z", "a@b.com", "s2"),
        ]
        assert affected_positions(monotonic) == []

    def test_empty_history_is_handled(self):
        assert affected_positions([]) == []


class TestInvertMsToPositions:
    def test_inverts_a_unique_timestamp(self):
        ts_positions = build_ts_positions(META)
        # 2026-01-02T00:00:00Z
        assert invert_ms_to_positions(1767312000000, ts_positions) == [2]

    def test_inverts_a_collided_timestamp_to_both_positions(self):
        ts_positions = build_ts_positions(META)
        # 2026-01-06T00:00:00Z
        assert invert_ms_to_positions(1767657600000, ts_positions) == [3, 4]

    def test_unknown_timestamp_yields_no_positions(self):
        assert invert_ms_to_positions(1, build_ts_positions(META)) == []


class TestEdgeLiveAt:
    def test_open_edge_is_live_at_and_after_its_introduction(self):
        assert edge_live_at([1], None, 1) is True
        assert edge_live_at([1], None, 4) is True

    def test_open_edge_is_not_live_below_its_introduction(self):
        assert edge_live_at([1], None, 0) is False

    def test_closed_edge_is_not_live_at_or_after_its_close(self):
        assert edge_live_at([1], [3], 3) is False
        assert edge_live_at([1], [3], 2) is True

    def test_ambiguous_introduction_uses_the_earliest_colliding_position(self):
        # A collided vf could be either position; the earliest is the only
        # choice that cannot understate exposure.
        assert edge_live_at([3, 4], None, 3) is True

    def test_ambiguous_close_uses_the_latest_colliding_position(self):
        # Symmetrically, the latest close cannot understate exposure.
        assert edge_live_at([1], [3, 4], 3) is True
        assert edge_live_at([1], [3, 4], 4) is False

    def test_unmappable_introduction_is_not_live_anywhere(self):
        assert edge_live_at([], None, 0) is False
