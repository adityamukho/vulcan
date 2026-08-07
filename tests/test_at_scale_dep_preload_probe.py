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
    count_unmappable_module_path_facts,
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


class TestSentinelMirrorsMcpServer:
    """The probe duplicates minigraf's forever sentinel rather than importing
    it, so the analysis primitives stay importable without opening a graph.
    That duplication was guarded only by a comment saying "keep them
    identical" -- and the import of VALID_TIME_FOREVER_MS in this module was
    left unused (ruff F401), which is the assertion that was written and then
    lost. If the two ever diverge, every open edge is misread as CLOSED at a
    nonsense position and the whole measurement silently changes value.
    """

    def test_forever_sentinel_matches_the_value_mcp_server_writes(self):
        import mcp_server

        assert VALID_TIME_FOREVER_MS == mcp_server._VALID_TIME_FOREVER_MS


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
    position_correct_file_entities,
    position_exact_live_edges,
)

# Epoch-ms for META's dates, so a fact can be pinned to a known POSITION.
MS = {
    0: 1767225600000,  # 2026-01-01, position 0
    1: 1767571200000,  # 2026-01-05, position 1
    2: 1767312000000,  # 2026-01-02, position 2  <- INVERTED: later pos, earlier date
    3: 1767657600000,  # 2026-01-06, positions 3 AND 4 (collision)
}


class TestPositionCorrectFileEntities:
    """The WIDE measurement's entity set, and the reason it exists.

    Final whole-branch review, CRITICAL finding: _preload_known_entities is
    position-correct on the INTRODUCTION side only (its position clause keys
    on the introducing commit's hash). Its CLOSE side is still valid_at =
    T_hi(W), a date bound carrying the identical author-date inversion the
    probe exists to measure. A module whose close DATE falls below ts(W) but
    whose close POSITION sits above W therefore vanishes from file_entities
    at W -- taking its :depends-on edges out of BOTH sides of the diff before
    the diff is computed, and understating the measurement.

    The first test below is that exact case, and it is the one that was
    invisible: on this repository, four modules deleted by df6b8be at
    position 124 and 30 misclassified edges.
    """

    def _ts_positions(self):
        return build_ts_positions(META)

    def test_close_date_below_the_watermark_but_close_position_above_it_stays_live(self):
        # vulcan.py is closed by the commit at POSITION 2, whose date
        # (2026-01-02) is EARLIER than position 1's own (2026-01-05).
        #
        # A date bound at W=1 sees vt = 01-02 <= ts(W) = 01-05 and calls the
        # module closed -- that is _preload_known_entities' close side, and it
        # is wrong. Position-correctly, the close is at position 2 > 1, so the
        # module is still live at W=1.
        facts = [{"path": "vulcan.py", "vf_ms": MS[0], "vt_ms": MS[2]}]

        assert "vulcan.py" in position_correct_file_entities(facts, self._ts_positions(), w=1)
        # And it is correctly GONE from position 2 onward, where the close
        # genuinely lands.
        assert position_correct_file_entities(facts, self._ts_positions(), w=2) == {}

    def test_open_module_is_live_from_its_introduction_onward(self):
        facts = [{"path": "a.py", "vf_ms": MS[1], "vt_ms": VALID_TIME_FOREVER_MS}]
        ts_positions = self._ts_positions()

        assert position_correct_file_entities(facts, ts_positions, w=0) == {}
        assert set(position_correct_file_entities(facts, ts_positions, w=1)) == {"a.py"}
        assert set(position_correct_file_entities(facts, ts_positions, w=4)) == {"a.py"}

    def test_shape_matches_the_file_entities_dict_its_consumers_expect(self):
        # _preload_known_deps and position_exact_live_edges both read only the
        # KEYS; the values must still be lists so the dict is drop-in.
        facts = [{"path": "a.py", "vf_ms": MS[0], "vt_ms": VALID_TIME_FOREVER_MS}]
        result = position_correct_file_entities(facts, self._ts_positions(), w=0)
        assert result == {"a.py": []}

    def test_a_rename_keeps_each_path_live_only_over_its_own_window(self):
        # Two distinct (path, vf, vt) rows, as load_module_path_facts returns
        # for a renamed module: old closed at position 2, new opened there.
        facts = [
            {"path": "old.py", "vf_ms": MS[0], "vt_ms": MS[2]},
            {"path": "new.py", "vf_ms": MS[2], "vt_ms": VALID_TIME_FOREVER_MS},
        ]
        ts_positions = self._ts_positions()

        assert set(position_correct_file_entities(facts, ts_positions, w=0)) == {"old.py"}
        assert set(position_correct_file_entities(facts, ts_positions, w=2)) == {"new.py"}

    def test_ambiguous_close_takes_the_latest_colliding_position(self):
        # MS[3] collides across positions 3 and 4. edge_live_at resolves an
        # ambiguous close to the LATEST -- the direction that cannot
        # understate exposure -- so the module survives w=3 and dies at w=4.
        facts = [{"path": "a.py", "vf_ms": MS[0], "vt_ms": MS[3]}]
        ts_positions = self._ts_positions()

        assert set(position_correct_file_entities(facts, ts_positions, w=3)) == {"a.py"}
        assert position_correct_file_entities(facts, ts_positions, w=4) == {}

    def test_an_unmappable_introduction_is_not_live_anywhere(self):
        facts = [{"path": "a.py", "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS}]
        ts_positions = self._ts_positions()

        assert all(
            position_correct_file_entities(facts, ts_positions, w=w) == {}
            for w in range(len(META))
        )


class TestCountUnmappableModulePathFacts:
    """WIDE's own unmappable-fact diagnostic. WIDE is rebuilt entirely from
    module :path facts, so a :path vf/vt that fails to invert to a position
    breaks WIDE at every affected W silently -- understating it (unmappable
    vf drops the module) or overstating it (unmappable vt reads it as never
    closed) -- unless counted, exactly as unmappable_valid_from_facts /
    unmappable_valid_to_facts already do for :depends-on edges.
    """

    def _ts_positions(self):
        return build_ts_positions(META)

    def test_a_fully_mappable_population_counts_zero_both_ways(self):
        facts = [
            {"path": "a.py", "vf_ms": MS[0], "vt_ms": VALID_TIME_FOREVER_MS},
            {"path": "b.py", "vf_ms": MS[1], "vt_ms": MS[2]},
        ]
        assert count_unmappable_module_path_facts(facts, self._ts_positions()) == (0, 0)

    def test_an_unmappable_valid_from_is_counted(self):
        facts = [{"path": "a.py", "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS}]
        unmappable_vf, unmappable_vt = count_unmappable_module_path_facts(
            facts, self._ts_positions()
        )
        assert (unmappable_vf, unmappable_vt) == (1, 0)

    def test_an_unmappable_valid_to_is_counted(self):
        facts = [{"path": "a.py", "vf_ms": MS[0], "vt_ms": 1}]
        unmappable_vf, unmappable_vt = count_unmappable_module_path_facts(
            facts, self._ts_positions()
        )
        assert (unmappable_vf, unmappable_vt) == (0, 1)

    def test_the_open_sentinel_is_never_counted_as_an_unmappable_valid_to(self):
        # VALID_TIME_FOREVER_MS does not correspond to any commit's date, so
        # inverting it directly would always fail -- the sentinel must be
        # excluded from the valid-to check the same way sweep()'s own
        # unmappable_vt does for :depends-on edges.
        facts = [{"path": "a.py", "vf_ms": MS[0], "vt_ms": VALID_TIME_FOREVER_MS}]
        assert count_unmappable_module_path_facts(facts, self._ts_positions()) == (0, 0)

    def test_each_unmappable_fact_is_counted_independently(self):
        facts = [
            {"path": "a.py", "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS},
            {"path": "b.py", "vf_ms": 1, "vt_ms": VALID_TIME_FOREVER_MS},
            {"path": "c.py", "vf_ms": MS[0], "vt_ms": 1},
        ]
        assert count_unmappable_module_path_facts(facts, self._ts_positions()) == (2, 1)


class TestPositionExactLiveEdges:
    """The oracle restricts to edges whose SOURCE MODULE is present in
    file_entities at W, mirroring _preload_known_deps' own ident_to_file
    filter (mcp_server.py:7526-7535, 7558-7560).

    WHICH file_entities the caller passes selects the NARROW measurement
    (_preload_known_entities' own output, position-correct on the
    introduction side only) or the WIDE one
    (position_correct_file_entities', correct at both ends). An earlier
    version of this docstring asserted the narrowing was position-correct
    outright and that holding it fixed isolated the :depends-on bound. It
    does not -- see TestPositionCorrectFileEntities."""

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

    def test_a_closed_edge_is_live_before_its_close_and_excluded_at_and_after_it(self):
        # Neither of the two tests above ever gives vt_ms < VALID_TIME_FOREVER_MS,
        # so nothing exercises that comparison's False branch (the actual
        # invert_ms_to_positions(vt_ms, ...) call) without this. #245 review
        # round, Important finding 3.
        ts_positions = {
            "2026-01-01T00:00:00Z": [1],  # introduction
            "2026-01-03T00:00:00Z": [3],  # close
        }
        edges = [
            {"src": ":module/a-py", "dep": ":module/b-py", "vf_ms": 1767225600000, "vt_ms": 1767398400000},
        ]
        file_entities = {"a.py": []}

        assert position_exact_live_edges(edges, ts_positions, file_entities, w=2) == {
            (":module/a-py", ":module/b-py")
        }
        assert position_exact_live_edges(edges, ts_positions, file_entities, w=3) == set()


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


from evals.at_scale.probe_dep_preload_exposure import _ingest_into


class TestIngestIntoSurfacesStatus:
    """#245 review round, CRITICAL finding: mcp_server._run_ingestion swallows
    every exception internally (mcp_server.py:10212-10220), sets
    _ingest_progress["status"] to "error" (or "stopped" on a short-circuited
    run, mcp_server.py:10205-10208), and returns NORMALLY -- it never raises.
    Without surfacing that status, main() has no way to tell a failed or
    partial ingestion from a complete one, and its own `_db is not None`
    fallback would silently sweep whatever partial graph is left behind."""

    @pytest.mark.asyncio
    async def test_surfaces_a_non_complete_status_without_raising(self, tmp_path, monkeypatch):
        import mcp_server

        async def fake_run_ingestion(_repo_path, _branch):
            # Mirrors _run_ingestion's own swallowed-exception terminal state:
            # returns normally, having set status to something other than
            # "complete".
            mcp_server._ingest_progress["status"] = "error"
            mcp_server._ingest_progress["error"] = "simulated failure"
            mcp_server._db = None

        monkeypatch.setattr(mcp_server, "_run_ingestion", fake_run_ingestion)

        graph_path = tmp_path / "probe.graph"
        branch, status = await _ingest_into(str(tmp_path), "main", graph_path)

        assert branch == "main"
        assert status == "error"

    @pytest.mark.asyncio
    async def test_surfaces_complete_status_on_a_real_successful_ingestion(self, git_repo, tmp_path):
        graph_path = tmp_path / "probe.graph"
        branch, status = await _ingest_into(str(git_repo), "HEAD", graph_path)

        assert branch == "HEAD"
        assert status == "complete"


class TestLoadModulePathFacts:
    """Against a REAL ingested graph, because the failure mode this guards is
    silent: a wrong clause order or a mistyped attribute returns zero rows
    without raising, which would make position_correct_file_entities empty at
    every position and the WIDE figure a confident, meaningless zero."""

    @pytest.mark.asyncio
    async def test_returns_a_windowed_fact_for_every_ingested_module(self, git_repo, tmp_path):
        from evals.at_scale.probe_dep_preload_exposure import load_module_path_facts
        import mcp_server

        graph_path = tmp_path / "probe.graph"
        _branch, status = await _ingest_into(str(git_repo), "HEAD", graph_path)
        assert status == "complete"

        # Same handle recovery main() does: _run_ingestion may leave _db None.
        db = mcp_server._db or mcp_server.open_db(str(graph_path))
        facts = load_module_path_facts(db)

        assert {f["path"] for f in facts} == {"auth.py", "models.py"}
        for f in facts:
            assert isinstance(f["vf_ms"], int)
            assert isinstance(f["vt_ms"], int)
            # Neither module is ever deleted in this fixture, so both must
            # still carry the open sentinel -- a stray finite vt here would
            # mean the pseudo-attributes bound to the wrong EAV clause.
            assert f["vt_ms"] == VALID_TIME_FOREVER_MS
