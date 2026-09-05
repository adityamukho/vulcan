"""#325: the commit census on a RESUMED graph -- the only at-scale check that
can observe a wrongly-RETAINED frontier interval.

`resume_ok` is the pure half and gets its own tests, mirroring
tests/test_at_scale_commit_census.py's split between the pure comparison and
the real-repo collection. `run_resume_census` drives two real
`_run_ingestion` passes over a real repo into a real graph -- the collection
half cannot be unit-tested against integers, because the property under test
is whether a real resume actually reaches `_frontier_load`'s retention branch,
not whether some hand-picked counts diff to zero.
"""

import subprocess as _subprocess

import pytest

from evals.at_scale import probe_resume_census
from evals.at_scale.probe_resume_census import resume_ok


@pytest.fixture(autouse=True)
def reset_mcp_server_db():
    """This module's own copy of test_mcp_server.py's autouse fixture.

    A fixture defined there is not shared with this file. Without an
    equivalent here, the last test in this module to run leaves the get_db()
    shim's lease open when the process exits, and a `db_lease` generator
    force-closed at interpreter shutdown (after other module globals have
    started tearing down) prints a spurious "Exception ignored while closing
    generator" -- cosmetic when this file runs as part of the full suite
    (test_mcp_server.py's own teardown runs later and cleans up regardless),
    but not when this file runs in isolation, which is worth keeping quiet.
    """
    import mcp_server
    mcp_server._reset_db_state()
    yield
    mcp_server._reset_db_state()


def _repo(tmp_path, n, start=0):
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    for i in range(start, start + n):
        (repo / "auth.py").write_text(f"def login():\n    return {i}\n")
        _subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        _subprocess.run(["git", "commit", "-m", f"c{i}"], cwd=repo, check=True, capture_output=True)
    return repo


def _census(**overrides):
    """A clean 15-commit census, before overrides -- mirrors
    test_at_scale_commit_census.py's own `_clean` helper."""
    kwargs = dict(
        ref="main", repo_commits=15, walk_claimed=15, graph_commit_entities=15,
        repo_vs_walk=0, walk_vs_graph=0, repo_vs_graph=0,
        distinct_commit_idents=15, ident_collisions=0, final_status="complete",
        proved_nothing=False, ok=True,
        interpretation="repo, walk and graph agree at 15 commits.",
        census_error=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestResumeOk:
    """The probe's own verdict, not collect_commit_census's. See
    probe_resume_census.resume_ok's docstring for why."""

    def test_agreeing_counts_pass(self):
        assert resume_ok(_census()) is True

    def test_a_nonzero_repo_vs_graph_fails(self):
        """The finding this probe exists to catch: the graph is missing
        commits the repo has, however the walk's own bookkeeping reads."""
        assert resume_ok(_census(repo_vs_graph=3)) is False

    def test_walk_vs_graph_alone_does_not_decide_it(self):
        """Counterfactual for using collect_commit_census's own `ok`: a
        nonzero walk_vs_graph with repo_vs_graph still 0 must NOT fail this
        probe's gate -- that is exactly the shape a healthy skip-heavy resume
        produces (walk_claimed undercounts what commit_census's repo_vs_walk
        expects, but the graph is still complete)."""
        census = _census(walk_vs_graph=5, repo_vs_walk=-5, ok=False)
        assert resume_ok(census) is True

    def test_repo_vs_walk_alone_does_not_decide_it(self):
        assert resume_ok(_census(repo_vs_walk=5, ok=False)) is True

    def test_a_census_error_reads_true_here_deliberately(self):
        """Not a gap in this function -- collect_commit_census routes both
        repo_commits and graph_commit_entities to 0 on a failed collection, so
        repo_vs_graph reads 0 regardless. The CLI's unconditional
        census_error check is what keeps this from reaching a green log
        unnoticed; see main()."""
        census = _census(
            repo_commits=0, graph_commit_entities=0, repo_vs_graph=0,
            census_error="CalledProcessError: ...", proved_nothing=False, ok=False,
        )
        assert resume_ok(census) is True


class TestResumeCensus:
    """#325: the census the nightly could not run. A wrongly-retained interval
    means positions are never claimed, and every existing at-scale detector
    reads that clean -- fact_audit's two witnesses agree about a commit
    neither holds, both :introduced-by checks only examine entities that
    exist, stderr_capture has nothing to read, and the fresh-graph
    commit_census cannot reach a retention at all."""

    @pytest.mark.asyncio
    async def test_clean_resume_agrees_on_all_three_counts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        repo = _repo(tmp_path, 12)
        _repo(tmp_path, 3, start=12)
        result = await probe_resume_census.run_resume_census(
            str(repo), "main", str(tmp_path / "memory.graph"), truncate_by=3,
        )
        assert result["ok"] is True, result["interpretation"]
        assert result["repo_commits"] == 15
        assert result["repo_vs_walk"] == 0
        assert result["repo_vs_graph"] == 0
        assert result["graph_commit_entities"] == 15
        assert result["proved_nothing"] is False, (
            "a run whose denominator is zero proves nothing and must say so "
            "rather than reading as a pass"
        )

    @pytest.mark.asyncio
    async def test_prior_ingested_and_processed_this_run_attribute_correctly(
        self, tmp_path, monkeypatch
    ):
        """The specific bug the controller ruling corrected in the brief's
        starter code: a manual `_ingest_progress["processed"] = 0` reset
        before the resume call is a no-op (_run_ingestion immediately
        overwrites it with its own recomputed prior_ingested), so a naive
        `walk_claimed = prior + this_run` double-counts the 12 already-
        ingested commits. Pinned here as its own assertion so a regression
        back to that shape is caught even if the totals above happened to
        still agree by coincidence."""
        repo = _repo(tmp_path, 12)
        _repo(tmp_path, 3, start=12)
        result = await probe_resume_census.run_resume_census(
            str(repo), "main", str(tmp_path / "memory.graph"), truncate_by=3,
        )
        assert result["prior_ingested"] == 12
        assert result["processed_this_run"] == 3
        assert result["walk_claimed"] == 15

    @pytest.mark.asyncio
    async def test_an_empty_repo_proves_nothing_and_does_not_fail(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        result = await probe_resume_census.run_resume_census(
            str(repo), "main", str(tmp_path / "memory.graph"), truncate_by=0,
        )
        assert result["ok"] is True
        assert result["census_error"] is not None or result["proved_nothing"] is True

    @pytest.mark.asyncio
    async def test_ingest_progress_does_not_leak_across_calls(self, tmp_path, monkeypatch):
        """Positive control for the _CLEAN_INGEST_PROGRESS reset. Without it,
        this empty-repo run -- executed AFTER a real 15-commit run in the same
        process -- would inherit walk_claimed=15 from the previous call's
        leftover `_ingest_progress`, and collect_commit_census would report a
        15-commit walk against a 0-commit repo instead of a clean
        proved_nothing/census_error result."""
        monkeypatch.setenv("MINIGRAF_INGEST_STREAM_RATIO", "1:20")
        populated = _repo(tmp_path, 15)
        first = await probe_resume_census.run_resume_census(
            str(populated), "main", str(tmp_path / "first.graph"), truncate_by=3,
        )
        assert first["walk_claimed"] == 15

        empty = tmp_path / "empty"
        empty.mkdir()
        _subprocess.run(["git", "init", "-b", "main"], cwd=empty, check=True, capture_output=True)
        second = await probe_resume_census.run_resume_census(
            str(empty), "main", str(tmp_path / "second.graph"), truncate_by=0,
        )
        assert second["walk_claimed"] == 0
        assert second["prior_ingested"] == 0


class TestCli:
    def test_a_census_error_fails_regardless_of_the_flag(self, monkeypatch, capsys):
        """The unconditional half of main()'s two-axis exit code, mirroring
        probe_ident_collision_new_history.py's measurement_invalid: a failed
        collection must not be indistinguishable from a clean run just
        because nobody passed --fail-on-mismatch."""
        async def fake_run(*args, **kwargs):
            return _census(
                repo_commits=0, graph_commit_entities=0, repo_vs_graph=0,
                census_error="CalledProcessError: bad ref", ok=True,
            )

        monkeypatch.setattr(probe_resume_census, "run_resume_census", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["probe", "--repo", "/r", "--branch", "main", "--graph", "/g", "--truncate-by", "3"],
        )

        assert probe_resume_census.main() == 1
        assert "CENSUS COLLECTION FAILED" in capsys.readouterr().err

    def test_fail_on_mismatch_flips_the_exit_code_on_a_real_mismatch(
        self, monkeypatch, capsys
    ):
        async def fake_run(*args, **kwargs):
            return _census(repo_commits=15, graph_commit_entities=12, repo_vs_graph=3, ok=False)

        monkeypatch.setattr(probe_resume_census, "run_resume_census", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            [
                "probe", "--repo", "/r", "--branch", "main", "--graph", "/g",
                "--truncate-by", "3", "--fail-on-mismatch",
            ],
        )

        assert probe_resume_census.main() == 1

    def test_without_the_flag_a_mismatch_exits_zero(self, monkeypatch, capsys):
        """Counterfactual for the test above: the flag, not the mismatch
        alone, is what changes the exit code -- matching the ident-collision
        probe's default of exit 0 on a finding."""
        async def fake_run(*args, **kwargs):
            return _census(repo_commits=15, graph_commit_entities=12, repo_vs_graph=3, ok=False)

        monkeypatch.setattr(probe_resume_census, "run_resume_census", fake_run)
        monkeypatch.setattr(
            "sys.argv",
            ["probe", "--repo", "/r", "--branch", "main", "--graph", "/g", "--truncate-by", "3"],
        )

        assert probe_resume_census.main() == 0
