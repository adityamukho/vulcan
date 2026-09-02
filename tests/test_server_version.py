"""Guards on the version this server reports to MCP clients.

`Server(name)` takes an optional `version`, and when it is omitted the SDK
substitutes *its own* package version (`mcp/server/lowlevel/server.py`:
`server_version=self.version if self.version else pkg_version("mcp")`). So
the published 0.7.0 told every client it was version `1.29.1` -- the resolved
`mcp` -- and that number moved whenever a user's resolver picked a different
`mcp` within `>=1.27.0,<2.0.0`, with no release here (#312).

The version reports users paste into bug reports are the one thing that value
is for, so the assertions below pin all three ways of getting it wrong: the
SDK's version, the `0.0.0` placeholder that `pyproject.toml` carries until
`release.yml` stamps it, and nothing at all.
"""

import importlib.metadata
import json
import os

import pytest

import mcp_server

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin_json_version() -> str:
    """The canonical version, read straight from the file that owns it."""
    with open(os.path.join(REPO_ROOT, ".claude-plugin", "plugin.json")) as f:
        return json.load(f)["version"]


def _installed_version():
    """What `importlib.metadata` reports for this distribution, or None."""
    try:
        return importlib.metadata.version("temporal-reasoning")
    except importlib.metadata.PackageNotFoundError:
        return None


def _reported_version() -> str:
    """The version an MCP client is actually told during initialization."""
    return mcp_server.server.create_initialization_options().server_version


def test_server_is_constructed_with_a_version():
    assert mcp_server.server.version is not None


def test_reported_version_is_not_the_mcp_sdk_version():
    assert _reported_version() != importlib.metadata.version("mcp")


def test_reported_version_is_not_the_pyproject_placeholder():
    # A fix that falls through to `0.0.0` looks identical to a working one
    # unless this is asserted separately: it is not the mcp version, and it
    # is not None, so every other assertion here would pass.
    assert _reported_version() != "0.0.0"


def test_reported_version_is_the_plugin_json_version_in_a_checkout():
    """A dev install has no stamped version, so plugin.json is the source.

    `pip install -e .` (what CI runs, and what `install.py` produces) installs
    the `0.0.0` placeholder, because only `release.yml` stamps the real
    version into `pyproject.toml` at build time.
    """
    if _installed_version() not in (None, "0.0.0"):
        pytest.skip("a release-stamped distribution is installed; it wins by design")
    assert _reported_version() == _plugin_json_version()


def test_installed_distribution_version_wins_over_plugin_json(monkeypatch):
    """In a wheel there is no plugin.json, and the stamped metadata is right."""
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    assert mcp_server._package_version() == "9.9.9"


def test_placeholder_metadata_falls_back_to_plugin_json(monkeypatch):
    """`0.0.0` is installed metadata that carries no information.

    A dev install writes a real `temporal_reasoning-0.0.0.dist-info`, so this
    is the ordinary developer case, not an exotic one -- an implementation
    that only handles `PackageNotFoundError` reports `0.0.0` on every
    developer machine.
    """
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.0.0")
    assert mcp_server._package_version() == _plugin_json_version()


def test_missing_distribution_falls_back_to_plugin_json(monkeypatch):
    """Running from a checkout with nothing installed at all."""

    def _absent(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _absent)
    assert mcp_server._package_version() == _plugin_json_version()


def test_no_source_at_all_reports_unknown_not_the_placeholder(monkeypatch):
    """Neither source available: say so, rather than inventing a number."""

    def _absent(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _absent)
    monkeypatch.setattr(mcp_server, "_plugin_json_version", lambda: None)
    assert mcp_server._package_version() == "unknown"
