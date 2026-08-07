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


import subprocess as _subprocess

import pytest

from evals.at_scale.probe_dep_preload_exposure import (
    gitlink_event_count,
    position_exact_live_edges,
)


class TestPositionExactLiveEdges:
    """The oracle restricts to edges whose SOURCE MODULE is present in
    file_entities at W, mirroring _preload_known_deps' own ident_to_file
    filter (mcp_server.py:7526-7535, 7558-7560). That narrowing is already
    position-correct after #238, so isolating it out leaves the :depends-on
    bound as the only variable under measurement -- which is exactly #245's
    residual class."""

    def _edges(self):
        return [
            # live from position 1 onward, source module present
            {"src": ":module/a-py", "dep": ":module/b-py", "vf_ms": 1767225600000, "vt_ms": (1 << 63) - 1},
            # source module absent from file_entities -- must be excluded
            {"src": ":module/gone-py", "dep": ":module/b-py", "vf_ms": 1767225600000, "vt_ms": (1 << 63) - 1},
        ]

    def test_excludes_edges_whose_source_module_is_not_a_live_file_entity(self):
        ts_positions = {"2026-01-01T00:00:00Z": [1]}
        live = position_exact_live_edges(
            self._edges(), ts_positions, file_entities={"a.py": []}, w=2
        )
        assert live == {(":module/a-py", ":module/b-py")}

    def test_excludes_edges_introduced_above_the_position(self):
        ts_positions = {"2026-01-01T00:00:00Z": [1]}
        live = position_exact_live_edges(
            self._edges(), ts_positions, file_entities={"a.py": []}, w=0
        )
        assert live == set()


@pytest.fixture
def git_repo(tmp_path):
    """Minimal git repo with two commits (mirrors tests/test_mcp_server.py's fixture)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "auth.py").write_text("def login(): pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add auth"], cwd=repo, check=True, capture_output=True)
    (repo / "models.py").write_text("class User: pass\n")
    _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "add models"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def repo_with_submodule(tmp_path):
    """A repo carrying a real gitlink entry, so gitlink_event_count has
    something to find."""
    inner = tmp_path / "inner"
    inner.mkdir()
    _subprocess.run(["git", "init"], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=inner, check=True, capture_output=True)
    (inner / "x.py").write_text("x = 1\n")
    _subprocess.run(["git", "add", "."], cwd=inner, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "inner"], cwd=inner, check=True, capture_output=True)

    outer = tmp_path / "outer"
    outer.mkdir()
    _subprocess.run(["git", "init"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=outer, check=True, capture_output=True)
    (outer / "main.py").write_text("y = 2\n")
    _subprocess.run(["git", "add", "."], cwd=outer, check=True, capture_output=True)
    _subprocess.run(["git", "commit", "-m", "outer"], cwd=outer, check=True, capture_output=True)
    _subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(inner), "sub"],
        cwd=outer, check=True, capture_output=True,
    )
    _subprocess.run(["git", "commit", "-m", "add sub"], cwd=outer, check=True, capture_output=True)
    return outer


class TestGitlinkEventCount:
    def test_counts_zero_for_a_repo_without_submodules(self, git_repo):
        assert gitlink_event_count(str(git_repo)) == 0

    def test_counts_a_real_gitlink_event(self, repo_with_submodule):
        assert gitlink_event_count(str(repo_with_submodule)) >= 1
