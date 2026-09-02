"""Guards on the portable tool manifest: `tools/*.json` and `skill.json`.

These files are not read by `mcp_server.py` at runtime -- `_TOOLS` is the only
thing an MCP client ever sees. They exist for the portable-skill surface:
`install.py` syncs `skill.json` and the whole `tools/` directory into the
harness skill directory, where a non-MCP harness reads them instead.

Being unreachable from the server is exactly why they rot. Every one of them
had drifted by 2026-09-02:

  * `query.json` still declared an `as_of` integer parameter. `_TOOLS` has not
    accepted one since valid-time landed, and `handle_minigraf_query` takes a
    single `datalog` argument -- so the portable manifest advertised a
    parameter that would have been rejected.
  * `transact.json`'s example was `[[:decision/cache-strategy
    :decision/description "use Redis"]]` -- a namespaced attribute, which the
    live description explicitly warns against.
  * Four tools (`minigraf_rule`, `minigraf_audit`, `minigraf_ingest_git`,
    `minigraf_ingest_status`) had no file at all.
  * `skill.json` pinned `minigraf >=0.19.0` against a real floor of `>=2.0.0`,
    and named version 0.3.1 against a plugin at 0.7.0.

None of that could fail a test, because nothing imported these files. So the
guard has to compare them against the sources of truth directly.
"""

import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tool_basename(name: str) -> str:
    """`minigraf_query` -> `query`; `memory_prepare_turn` is already bare."""
    return name[len("minigraf_"):] if name.startswith("minigraf_") else name


def _load(*parts) -> dict:
    with open(os.path.join(REPO_ROOT, *parts)) as f:
        return json.load(f)


def _live_tools() -> list:
    import mcp_server
    return mcp_server._TOOLS


class TestToolSchemaFiles:
    """Every `_TOOLS` entry has a file, and the file says what `_TOOLS` says."""

    def test_every_tool_has_a_schema_file(self):
        expected = {_tool_basename(t.name) + ".json" for t in _live_tools()}
        actual = {
            f for f in os.listdir(os.path.join(REPO_ROOT, "tools"))
            if f.endswith(".json")
        }
        assert actual == expected, (
            f"tools/ is out of sync with mcp_server._TOOLS.\n"
            f"  missing: {sorted(expected - actual)}\n"
            f"  orphaned: {sorted(actual - expected)}"
        )

    def test_each_schema_file_matches_its_tool(self):
        for tool in _live_tools():
            doc = _load("tools", _tool_basename(tool.name) + ".json")
            assert doc["name"] == tool.name
            assert doc["description"] == tool.description, (
                f"{tool.name}: description in tools/ differs from _TOOLS"
            )
            assert doc["parameters"] == tool.inputSchema, (
                f"{tool.name}: parameters in tools/ differ from _TOOLS.inputSchema"
            )

    def test_every_declared_parameter_is_actually_read(self):
        """The specific shape of the `as_of` rot: a declared parameter that
        `call_tool` never reads, so passing it does nothing.

        Checked against the dispatch rather than the handler signature,
        because the wire name and the Python parameter name are allowed to
        differ -- `minigraf_report_issue` declares `issue_type` and passes it
        positionally into `handle_minigraf_report_issue(category, ...)`. A
        signature comparison would call that a defect; it is not one.
        """
        import inspect
        import mcp_server

        source = inspect.getsource(mcp_server.call_tool)
        # Split the dispatch into per-tool branches. Each is `if name == "x":`
        # at a fixed indent, running until the next one.
        branches = re.split(r'\n\s*if name == "([a-z_]+)":', source)
        read_by_tool = {
            branches[i]: set(re.findall(
                r'arguments(?:\.get\(|\[)"([a-z_]+)"', branches[i + 1]
            ))
            for i in range(1, len(branches) - 1, 2)
        }
        assert read_by_tool, "could not parse call_tool's dispatch"

        for tool in _live_tools():
            declared = set(_load(
                "tools", _tool_basename(tool.name) + ".json"
            )["parameters"].get("properties", {}))
            read = read_by_tool.get(tool.name)
            assert read is not None, f"{tool.name} has no call_tool branch"
            assert declared == read, (
                f"{tool.name}: declares {sorted(declared)} but call_tool reads "
                f"{sorted(read)}"
            )


class TestSkillManifest:
    """`skill.json` mirrors three canonical sources; it is authoritative for none."""

    def test_lists_every_tool_schema_file(self):
        skill = _load("skill.json")
        expected = sorted(
            "tools/" + _tool_basename(t.name) + ".json" for t in _live_tools()
        )
        assert sorted(skill["tools"]) == expected

    def test_version_matches_the_plugin(self):
        """`.claude-plugin/plugin.json` is canonical for the version -- it is
        what `install.py` reads via PLUGIN_VERSION to name the cache dir."""
        assert _load("skill.json")["version"] == _load(
            ".claude-plugin", "plugin.json"
        )["version"]

    def test_minigraf_requirement_matches_pyproject(self):
        """`pyproject.toml` is canonical for the dependency spec. install.py's
        `_MINIGRAF_SPEC` already mirrors it; this is the third copy."""
        with open(os.path.join(REPO_ROOT, "pyproject.toml")) as f:
            spec = re.search(r'"minigraf(>=[^"]+)"', f.read())
        assert spec, "pyproject.toml has no pinned minigraf dependency"
        assert _load("skill.json")["requires"]["minigraf"] == spec.group(1)

    def test_description_matches_the_plugin(self):
        assert _load("skill.json")["description"] == _load(
            ".claude-plugin", "plugin.json"
        )["description"]
