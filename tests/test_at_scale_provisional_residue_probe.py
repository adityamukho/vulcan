"""Unit tests for the #256 provisional-residue probe's analysis primitives.

M and N themselves are measurements of this repository's history, not
invariants, so they are not asserted. What IS asserted is that the
comparison fires when it should and that the inputs cannot be silently
misread -- the failure class the spec calls out.
"""

import json

import pytest

from evals.at_scale.probe_provisional_residue import (
    breakdown_by_entity_type,
    commit_entity_count,
    main,
    provisional_entity_idents,
    read_sweep_total,
    require_commit_count_matches,
    require_complete_run,
    require_ingested_graph,
    residue_verdict,
)


_TS = "2026-08-16T00:00:00Z"


def _build_graph(path, *, stamp=True, commits=0, provisional=()):
    """Build a graph that looks like a benchmark run's output.

    Stamped (so require_ingested_graph accepts it), carrying `commits`
    :type/commit entities (so require_commit_count_matches has something to
    count), and `provisional` lineage markers.
    """
    import mcp_server

    mcp_server._reset_db_state()
    mcp_server.open_db(str(path))
    try:
        with mcp_server.db_lease() as db:
            if stamp:
                mcp_server._graph_format_version_stamp_if_new(db, _TS)
            for i in range(commits):
                ident = f":commit/c{i}"
                mcp_server._transact(
                    db,
                    f"[[{ident} :entity-type :type/commit] "
                    f'[{ident} :ident "{ident}"] '
                    f'[{ident} :description "planted commit {i}"]]',
                    _TS,
                )
            for ident in provisional:
                mcp_server._lineage_mark_provisional(db, ident, _TS)
    finally:
        mcp_server._reset_db_state()


def _metrics(**overrides):
    """A metrics dict that passes every gate, so each test can break exactly
    one thing and know which gate fired."""
    base = {
        "final_status": "complete",
        "stderr_capture_complete": True,
        "correction_sweep_skipped": 5,
        "correction_sweep_summaries": [5],
        "commits_ingested": 3,
    }
    base.update(overrides)
    return base


class TestReadSweepTotal:
    def test_reads_the_recorded_total(self):
        assert read_sweep_total({"correction_sweep_skipped": 17}) == 17

    def test_zero_is_a_valid_measured_value(self):
        """No summary line means zero, not unmeasured --
        _correction_sweep_log_summary prints only `if skipped_events:`."""
        assert read_sweep_total({"correction_sweep_skipped": 0}) == 0

    def test_a_missing_key_fails_loudly(self):
        """Defaulting to 0 would silently turn `M <= N` into `M == 0` and
        produce a false failure against a healthy graph."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"final_status": "complete"})

    def test_none_value_fails_with_diagnostic(self):
        """A metrics writer emitting null instead of omitting the field."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"correction_sweep_skipped": None})

    def test_non_numeric_string_fails_with_diagnostic(self):
        """Malformed value in the metrics file."""
        with pytest.raises(SystemExit, match="correction_sweep_skipped"):
            read_sweep_total({"correction_sweep_skipped": "not a number"})


class TestRequireCompleteRun:
    def test_accepts_a_complete_run(self):
        require_complete_run({"final_status": "complete"})

    def test_rejects_an_errored_run(self):
        """Residue on an aborted run means nothing."""
        with pytest.raises(SystemExit, match="complete"):
            require_complete_run({"final_status": "error"})

    def test_rejects_a_stopped_run(self):
        with pytest.raises(SystemExit, match="complete"):
            require_complete_run({"final_status": "stopped"})

    def test_rejects_a_run_whose_stderr_capture_was_incomplete(self):
        """#256 review round 5, Blocking 2. N is scanned from the captured
        stderr, so a truncated capture makes N a lower bound of unknown extent
        -- and if the capture died before the sweep summary was emitted, N
        reads 0. With M == 0 the probe would write `ok: true` for a run
        run_ingestion_benchmark._exit_code has already failed as unverifiable.
        """
        with pytest.raises(SystemExit, match="stderr capture did not complete"):
            require_complete_run(
                {"final_status": "complete", "stderr_capture_complete": False}
            )

    def test_rejects_a_run_that_recorded_a_tee_failure(self):
        """The other half of _exit_code's clause. A tee_failure key can be
        present independently, and it means the same thing: the capture N came
        from is not trustworthy."""
        with pytest.raises(SystemExit, match="stderr capture did not complete"):
            require_complete_run(
                {
                    "final_status": "complete",
                    "stderr_capture_complete": True,
                    "tee_failure": "TeeStderrFailure('pump died')",
                }
            )

    def test_an_absent_stderr_capture_key_is_not_a_refusal(self):
        """`is False`, not `not ...`, matching _exit_code: a pre-#256 metrics
        file carries neither key, and read_sweep_total refuses it on its own
        with a better message."""
        require_complete_run({"final_status": "complete"})

    def test_the_two_refusals_are_worded_distinctly(self):
        """They mean different things -- an unfinished RUN versus an
        unfinished CAPTURE -- and a reader must be able to tell which fired."""
        with pytest.raises(SystemExit) as unfinished:
            require_complete_run({"final_status": "error"})
        with pytest.raises(SystemExit) as uncaptured:
            require_complete_run(
                {"final_status": "complete", "stderr_capture_complete": False}
            )
        assert str(unfinished.value) != str(uncaptured.value)
        assert "stderr capture" not in str(unfinished.value)


class TestBreakdownByEntityType:
    def test_groups_by_the_ident_namespace(self):
        idents = [":function/a", ":function/b", ":module/c"]
        assert breakdown_by_entity_type(idents) == {"function": 2, "module": 1}

    def test_breakdown_sums_to_the_total(self):
        idents = [":function/a", ":class/b", ":module/c", ":function/d"]
        assert sum(breakdown_by_entity_type(idents).values()) == len(idents)

    def test_empty_input_yields_an_empty_breakdown(self):
        assert breakdown_by_entity_type([]) == {}

    def test_ident_with_no_slash_buckets_under_full_string(self):
        """Edge case: an ident without a slash."""
        idents = ["identifier_no_slash"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"identifier_no_slash": 1}

    def test_ident_with_no_leading_colon(self):
        """Edge case: an ident without leading colon."""
        idents = ["function/foo"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"function": 1}

    def test_empty_string_ident(self):
        """Edge case: an empty string ident."""
        idents = [""]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"": 1}

    def test_nested_namespace_ident(self):
        """Edge case: a nested namespace with multiple slashes."""
        idents = [":deeply/nested/type/name"]
        result = breakdown_by_entity_type(idents)
        assert sum(result.values()) == len(idents)
        assert result == {"deeply": 1}


class TestResidueVerdict:
    def test_m_below_n_passes(self):
        assert residue_verdict(3, 10)["ok"] is True

    def test_m_equal_to_n_passes(self):
        assert residue_verdict(10, 10)["ok"] is True

    def test_m_above_n_fails(self):
        """Provisional state the sweep never accounted for -- the #251
        signature this probe exists to detect."""
        assert residue_verdict(11, 10)["ok"] is False

    def test_both_raw_numbers_survive_in_the_verdict(self):
        """`M <= N` weakens as N grows, so a future run must be able to
        compare N itself across runs."""
        verdict = residue_verdict(3, 10)
        assert verdict["provisional_entities"] == 3
        assert verdict["sweep_skipped"] == 10


class TestProvisionalEntityIdents:
    """M is counted from a graph with a KNOWN number of planted markers, so a
    query that silently counts the wrong thing is visible."""

    def test_counts_exactly_the_planted_markers(self, tmp_path):
        import mcp_server

        graph = str(tmp_path / "residue.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                for ident in (":function/alpha", ":function/beta", ":module/gamma"):
                    mcp_server._lineage_mark_provisional(
                        db, ident, "2026-08-16T00:00:00Z"
                    )
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert sorted(found) == [":function/alpha", ":function/beta", ":module/gamma"]

    def test_a_confirmed_entity_leaves_no_residue(self, tmp_path):
        """_lineage_confirm retracts the marker, so a confirmed entity must
        stop counting toward M."""
        import mcp_server

        graph = str(tmp_path / "confirmed.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                mcp_server._lineage_mark_provisional(
                    db, ":function/alpha", "2026-08-16T00:00:00Z"
                )
                mcp_server._lineage_mark_provisional(
                    db, ":function/beta", "2026-08-16T00:00:00Z"
                )
                mcp_server._lineage_confirm(db, ":function/alpha")
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert found == [":function/beta"]

    def test_an_empty_graph_yields_no_residue(self, tmp_path):
        import mcp_server

        graph = str(tmp_path / "empty.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                assert provisional_entity_idents(db) == []
        finally:
            mcp_server._reset_db_state()

    def test_an_unmarked_entity_with_ordinary_facts_is_not_counted(self, tmp_path):
        """A query that accidentally matched on entity presence rather than
        marker presence would sweep this in too. Neither the confirmed-entity
        test nor the empty-graph test can catch that: this needs an entity
        that was simply never marked, sitting right next to one that was."""
        import mcp_server

        graph = str(tmp_path / "mixed.graph")
        mcp_server._reset_db_state()
        mcp_server.open_db(graph)
        try:
            with mcp_server.db_lease() as db:
                mcp_server._transact(
                    db,
                    '[[:module/untouched :entity-type :type/module] '
                    '[:module/untouched :ident ":module/untouched"] '
                    '[:module/untouched :description "untouched.py"]]',
                    "2026-08-16T00:00:00Z",
                )
                mcp_server._lineage_mark_provisional(
                    db, ":function/alpha", "2026-08-16T00:00:00Z"
                )
                found = provisional_entity_idents(db)
        finally:
            mcp_server._reset_db_state()

        assert found == [":function/alpha"]


class TestRequireIngestedGraph:
    """The guard that closes the probe's fail-open (#256 review round 5,
    Blocking 1). minigraf's open() CREATES a missing file, so before this
    guard a typo'd --graph-path produced provisional_entities: 0, an empty
    breakdown, ok: true and exit 0 -- the exact shape of a genuine clean
    result, with nothing to tell the two apart post hoc.
    """

    def test_accepts_a_graph_stamped_at_the_current_version(self, tmp_path):
        import mcp_server

        graph = tmp_path / "stamped.graph"
        _build_graph(graph)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                require_ingested_graph(db, str(graph))
        finally:
            mcp_server._reset_db_state()

    def test_refuses_a_graph_minigraf_just_created(self, tmp_path):
        """An unstamped graph is the fail-open case itself: opening a path
        that did not exist yields exactly this."""
        import mcp_server

        graph = tmp_path / "never-ingested.graph"
        assert not graph.exists()
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                with pytest.raises(SystemExit, match="no format-version stamp"):
                    require_ingested_graph(db, str(graph))
        finally:
            mcp_server._reset_db_state()

    def test_refuses_a_graph_stamped_at_another_version(self, tmp_path):
        """_graph_format_version_verify would accept neither, but it WOULD
        accept the case above; this one confirms the stricter read-based check
        still catches the version drift the verify half was written for."""
        import mcp_server

        graph = tmp_path / "old-rule.graph"
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                ident = mcp_server._FORMAT_VERSION_IDENT
                mcp_server._transact(
                    db,
                    f"[[{ident} :entity-type :type/ingestion] "
                    f'[{ident} :ident "{ident}"] '
                    f'[{ident} :description "graph format version"] '
                    f"[{ident} :version 999]]",
                    _TS,
                )
                with pytest.raises(SystemExit, match="format version 999"):
                    require_ingested_graph(db, str(graph))
        finally:
            mcp_server._reset_db_state()


class TestCommitEntityCount:
    def test_counts_the_planted_commits(self, tmp_path):
        import mcp_server

        graph = tmp_path / "commits.graph"
        _build_graph(graph, commits=4)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                assert commit_entity_count(db) == 4
        finally:
            mcp_server._reset_db_state()

    def test_an_empty_graph_counts_zero(self, tmp_path):
        import mcp_server

        graph = tmp_path / "no-commits.graph"
        _build_graph(graph, commits=0)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                assert commit_entity_count(db) == 0
        finally:
            mcp_server._reset_db_state()

    def test_uses_the_benchmarks_own_status_query(self, tmp_path, monkeypatch):
        """Imported, not re-typed: the cross-check is only evidence if it asks
        the SAME question the benchmark answered. Proven by redirecting the
        benchmark's constant at a different entity type and watching the count
        follow -- a re-typed copy of the query would ignore this entirely.
        """
        import mcp_server

        from evals.at_scale import run_ingestion_benchmark

        graph = tmp_path / "redirected.graph"
        _build_graph(graph, commits=4)
        monkeypatch.setattr(
            run_ingestion_benchmark,
            "_STATUS_QUERY",
            "[:find (count ?e) :where [?e :entity-type :type/ingestion]]",
        )
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                # 1 = the format-version stamp entity, not the 4 commits.
                assert commit_entity_count(db) == 1
        finally:
            mcp_server._reset_db_state()


class TestRequireCommitCountMatches:
    def test_accepts_a_graph_whose_commit_count_matches(self, tmp_path):
        import mcp_server

        graph = tmp_path / "paired.graph"
        _build_graph(graph, commits=3)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                assert require_commit_count_matches(db, _metrics()) == 3
        finally:
            mcp_server._reset_db_state()

    def test_refuses_a_graph_from_a_different_run(self, tmp_path):
        """The stamp proves "ingested by this build", not "ingested by THIS
        run" -- every persisted graph is stamped identically."""
        import mcp_server

        graph = tmp_path / "other-run.graph"
        _build_graph(graph, commits=7)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                with pytest.raises(SystemExit, match="7 :type/commit"):
                    require_commit_count_matches(db, _metrics(commits_ingested=3))
        finally:
            mcp_server._reset_db_state()

    def test_a_missing_commits_ingested_key_fails_loudly(self, tmp_path):
        import mcp_server

        graph = tmp_path / "old-metrics.graph"
        _build_graph(graph, commits=3)
        metrics = _metrics()
        del metrics["commits_ingested"]
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                with pytest.raises(SystemExit, match="commits_ingested"):
                    require_commit_count_matches(db, metrics)
        finally:
            mcp_server._reset_db_state()

    def test_a_non_numeric_commits_ingested_fails_with_diagnostic(self, tmp_path):
        import mcp_server

        graph = tmp_path / "corrupt-metrics.graph"
        _build_graph(graph, commits=3)
        mcp_server._reset_db_state()
        mcp_server.open_db(str(graph))
        try:
            with mcp_server.db_lease() as db:
                with pytest.raises(SystemExit, match="commits_ingested"):
                    require_commit_count_matches(
                        db, _metrics(commits_ingested="lots")
                    )
        finally:
            mcp_server._reset_db_state()


class TestMain:
    """End-to-end coverage of the CLI. Nothing exercised main() before #256
    review round 5, which is precisely why the fail-open below survived four
    rounds of review of the same failure shape elsewhere on the branch.
    """

    @staticmethod
    def _run(graph, metrics, out, extra=()):
        metrics_path = out.parent / "metrics.json"
        metrics_path.write_text(json.dumps(metrics))
        return main(
            [
                "--graph-path", str(graph),
                "--metrics-json", str(metrics_path),
                "--json-out", str(out),
                # Never the committed evals/at_scale/benchmark.md: main()
                # appends unconditionally, so the default would make every
                # test in this class edit a tracked file.
                "--report-path", str(out.parent / "benchmark.md"),
                *extra,
            ]
        )

    def test_a_nonexistent_graph_path_is_refused(self, tmp_path):
        """THE finding. minigraf's open() creates the file, so this used to
        print provisional_entities: 0, ok: true, exit 0 -- indistinguishable
        from the committed artifact of a real, passing run.
        """
        out = tmp_path / "verdict.json"
        with pytest.raises(SystemExit) as exc:
            self._run(tmp_path / "typo-in-this-name.graph", _metrics(), out)
        assert "format-version stamp" in str(exc.value)
        assert not out.exists(), "a refused probe must not write a verdict"

    def test_a_nonexistent_graph_path_never_reports_ok(self, tmp_path):
        """Stated as the property rather than the mechanism: whatever the
        probe does with a graph it cannot verify, it must not be `ok: true`.
        """
        out = tmp_path / "verdict.json"
        try:
            code = self._run(tmp_path / "also-a-typo.graph", _metrics(), out)
        except SystemExit as exc:
            assert exc.code != 0
        else:
            assert code != 0
        assert not out.exists()

    def test_a_paired_graph_and_metrics_produce_a_verdict(self, tmp_path):
        graph = tmp_path / "paired.graph"
        _build_graph(
            graph, commits=3, provisional=[":function/alpha", ":module/beta"]
        )
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(), out) == 0

        result = json.loads(out.read_text())
        assert result["ok"] is True
        assert result["provisional_entities"] == 2
        assert result["sweep_skipped"] == 5
        assert result["commits_in_graph"] == 3
        assert result["breakdown_by_entity_type"] == {"function": 1, "module": 1}
        assert result["graph_path"] == str(graph.resolve())

    def test_m_above_n_exits_non_zero(self, tmp_path):
        graph = tmp_path / "residue.graph"
        _build_graph(
            graph,
            commits=3,
            provisional=[":function/a", ":function/b", ":function/c"],
        )
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(correction_sweep_skipped=1), out) == 1
        assert json.loads(out.read_text())["ok"] is False

    def test_a_graph_from_a_different_run_is_refused(self, tmp_path):
        graph = tmp_path / "wrong-run.graph"
        _build_graph(graph, commits=9)
        out = tmp_path / "verdict.json"

        with pytest.raises(SystemExit, match="different runs"):
            self._run(graph, _metrics(commits_ingested=3), out)
        assert not out.exists()

    def test_an_incomplete_stderr_capture_is_refused(self, tmp_path):
        graph = tmp_path / "untrusted.graph"
        _build_graph(graph, commits=3)
        out = tmp_path / "verdict.json"

        with pytest.raises(SystemExit, match="stderr capture did not complete"):
            self._run(graph, _metrics(stderr_capture_complete=False), out)
        assert not out.exists()

    def test_json_out_is_honoured_so_a_rerun_cannot_clobber_the_artifact(
        self, tmp_path
    ):
        from evals.at_scale.probe_provisional_residue import _DEFAULT_JSON_OUT

        graph = tmp_path / "elsewhere.graph"
        _build_graph(graph, commits=3)
        out = tmp_path / "somewhere-else.json"

        assert self._run(graph, _metrics(), out) == 0
        assert out.exists()
        assert out.resolve() != _DEFAULT_JSON_OUT.resolve()

    def test_a_moved_graph_warns_but_still_reports(self, tmp_path, capsys):
        """A persisted graph legitimately moves; the commit-count cross-check
        is the real evidence of pairing, so this warns rather than fails."""
        graph = tmp_path / "moved.graph"
        _build_graph(graph, commits=3)
        out = tmp_path / "verdict.json"

        assert self._run(
            graph, _metrics(graph_path="/somewhere/that/is/not/here.graph"), out
        ) == 0
        assert "WARNING" in capsys.readouterr().err

    def test_a_matching_recorded_graph_path_is_silent(self, tmp_path, capsys):
        graph = tmp_path / "in-place.graph"
        _build_graph(graph, commits=3)
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(graph_path=str(graph.resolve())), out) == 0
        assert "WARNING" not in capsys.readouterr().err

    def test_a_verdict_is_appended_to_the_report(self, tmp_path):
        """#276: the verdict has to reach the durable human record, not only
        the results JSON that nothing in benchmark.md names."""
        graph = tmp_path / "reported.graph"
        _build_graph(graph, commits=3, provisional=[":function/alpha"])
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(), out) == 0

        text = (tmp_path / "benchmark.md").read_text()
        assert "## Provisional Residue" in text
        assert "| Verdict (#256) | OK -- M <= N" in text
        assert "| Provisional entities (M) | 1 |" in text
        assert "| Commits in graph | 3 |" in text
        assert "verdict.json" in text

    def test_a_failing_verdict_is_appended_too(self, tmp_path):
        """The case the record most needs. An M > N run must not be the one
        that quietly leaves no trace."""
        graph = tmp_path / "residue.graph"
        _build_graph(graph, commits=3,
                     provisional=[":function/a", ":function/b", ":function/c"])
        out = tmp_path / "verdict.json"

        assert self._run(graph, _metrics(correction_sweep_skipped=1), out) == 1
        assert "**FAILED**" in (tmp_path / "benchmark.md").read_text()

    def test_a_refused_run_appends_nothing(self, tmp_path):
        """The guards raise before any measurement exists. A section rendered
        from a refusal would be a verdict about a graph the probe declined to
        read."""
        out = tmp_path / "verdict.json"
        with pytest.raises(SystemExit):
            self._run(tmp_path / "typo-in-this-name.graph", _metrics(), out)
        assert not (tmp_path / "benchmark.md").exists()

    def test_the_default_report_path_is_the_committed_benchmark_md(self, tmp_path):
        """--report-path exists for the tests; the default must still be the
        real record, or a genuine run would write its verdict nowhere."""
        from evals.at_scale.probe_provisional_residue import _DEFAULT_REPORT_PATH

        assert _DEFAULT_REPORT_PATH.name == "benchmark.md"
        assert _DEFAULT_REPORT_PATH.parent.name == "at_scale"
