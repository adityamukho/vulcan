#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Installs minigraf and mcp Python packages, syncs skill files, provides next steps.

Usage:
    python install.py          # Full install
    python install.py --check  # Just check dependencies
    python install.py --force  # Force reinstall even if recent
"""

import sys
import subprocess
import os
import importlib.util
from datetime import datetime, timezone

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(REPO_DIR, ".last_update")

FILES_TO_SYNC = ["SKILL.md", "mcp_server.py", "skill.json"]
DIRS_TO_SYNC = ["tools", "hooks"]
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal-reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]


def check_python_version():
    """Check Python version is 3.9+."""
    if sys.version_info < (3, 9):
        print(f"ERROR: Python 3.9+ required, "
              f"found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_minigraf_package():
    """Verify minigraf Python package is installed, installing via pip if absent."""
    try:
        import minigraf  # noqa: F401
        print("✓ minigraf Python package found")
        return True
    except ImportError:
        print("✗ minigraf not found — installing via pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "minigraf>=0.22.0"],
            timeout=120,
        )
        if result.returncode == 0:
            print("✓ minigraf installed")
            return True
        print("✗ pip install minigraf failed")
        return False


def check_mcp_package():
    """Verify mcp Python package is installed, installing via pip if absent."""
    try:
        import mcp  # noqa: F401
        print("✓ mcp Python package found")
        return True
    except ImportError:
        print("✗ mcp not found — installing via pip...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "mcp>=1.27.0"],
            timeout=120,
        )
        if result.returncode == 0:
            print("✓ mcp installed")
            return True
        print("✗ pip install mcp failed")
        return False


def check_mcp_server_importable():
    """Verify mcp_server module can be imported."""
    try:
        try:
            spec = importlib.util.find_spec("mcp_server")
        except (ValueError, ModuleNotFoundError):
            spec = None
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import mcp_server  # noqa: F401
        print("✓ mcp_server module importable")
        return True
    except ImportError as e:
        print(f"✗ Cannot import mcp_server: {e}")
        return False


def should_update():
    """Check if update should run (no more than once a week)."""
    if not os.path.exists(LAST_UPDATE_FILE):
        return True
    try:
        with open(LAST_UPDATE_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                return True
            last_update = datetime.fromisoformat(content)
    except (ValueError, IOError):
        return True
    return (datetime.now(timezone.utc) - last_update).total_seconds() > UPDATE_INTERVAL


def _write_last_update() -> None:
    with open(LAST_UPDATE_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def _sync_files(target_dir: str) -> None:
    import shutil
    for rel_dir in SKILL_DIRS:
        dest_dir = os.path.join(target_dir, rel_dir)
        os.makedirs(dest_dir, exist_ok=True)
        for fname in FILES_TO_SYNC:
            src = os.path.join(REPO_DIR, fname)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest_dir, fname))
        for dname in DIRS_TO_SYNC:
            src_dir = os.path.join(REPO_DIR, dname)
            if os.path.isdir(src_dir):
                shutil.copytree(src_dir, os.path.join(dest_dir, dname), dirs_exist_ok=True)
    synced = ", ".join(FILES_TO_SYNC + DIRS_TO_SYNC)
    dirs = ", ".join(SKILL_DIRS)
    print(f"✓ Synced [{synced}] → [{dirs}]")


def update_skill(target_dir: str) -> bool:
    """Pull from GitHub and sync skill files to target_dir."""
    print("Checking for skill updates...")
    try:
        result = subprocess.run(
            ["git", "pull", "origin", "master"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        _write_last_update()
        if result.stdout.strip() and "Already up to date" not in result.stdout:
            print("Pulling latest from GitHub...")
        _sync_files(target_dir)
        print("✓ Skill up-to-date")
        return True
    except subprocess.CalledProcessError:
        print("ERROR: git pull failed")
        return False
    except FileNotFoundError:
        print("ERROR: git not found")
        return False
    except subprocess.TimeoutExpired:
        print("ERROR: git pull timed out")
        return False


def _get_target_dir() -> str:
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[idx + 1])
    return os.getcwd()


_PLACEHOLDER_KEY = "your-api-key-here"


def setup_mcp_json(target_dir: str) -> bool:
    """Idempotently write the temporal-reasoning MCP server block into .mcp.json.

    - Creates the file if absent.
    - Merges into existing content if present (other servers are preserved).
    - Always updates args and MINIGRAF_GRAPH_PATH to reflect current paths.
    - Preserves VULCAN_EXTRACTION_STRATEGY if already set by the user.
    - Preserves ANTHROPIC_API_KEY if already set to a real value; otherwise
      writes a placeholder and prints a reminder.
    """
    import json

    mcp_json_path = os.path.join(target_dir, ".mcp.json")
    server_script = os.path.join(REPO_DIR, "mcp_server.py")
    graph_path = os.path.join(target_dir, "memory.graph")

    existing: dict = {}
    file_existed = os.path.exists(mcp_json_path)
    if file_existed:
        try:
            with open(mcp_json_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, IOError):
            existing = {}

    prev_env: dict = existing.get("mcpServers", {}).get("temporal-reasoning", {}).get("env", {})

    strategy = prev_env.get("VULCAN_EXTRACTION_STRATEGY", "heuristic")
    prev_key = prev_env.get("ANTHROPIC_API_KEY", "")
    key_is_real = bool(prev_key) and prev_key != _PLACEHOLDER_KEY
    api_key = prev_key if key_is_real else _PLACEHOLDER_KEY

    new_env = {
        "MINIGRAF_GRAPH_PATH": graph_path,
        "VULCAN_EXTRACTION_STRATEGY": strategy,
        "ANTHROPIC_API_KEY": api_key,
    }
    existing.setdefault("mcpServers", {})["temporal-reasoning"] = {
        "type": "stdio",
        "command": "python",
        "args": [server_script],
        "env": new_env,
    }

    try:
        with open(mcp_json_path, "w") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")
    except IOError as e:
        print(f"✗ Could not write .mcp.json: {e}")
        return False

    verb = "Updated" if file_existed else "Created"
    print(f"✓ {verb} {mcp_json_path}")
    print(f"    MINIGRAF_GRAPH_PATH = {graph_path}")
    print(f"    VULCAN_EXTRACTION_STRATEGY = {strategy}")
    if key_is_real:
        print("    ANTHROPIC_API_KEY = (preserved)")
    else:
        print(f"    ANTHROPIC_API_KEY = {_PLACEHOLDER_KEY}  ← replace with your key")
    return True


def main(target_dir: str = "") -> None:
    print("=" * 50)
    print("Temporal Reasoning Skill Setup")
    print("=" * 50)
    print()

    if not target_dir:
        target_dir = _get_target_dir()

    checks = [
        ("Python version", check_python_version),
        ("minigraf package", check_minigraf_package),
        ("mcp package", check_mcp_package),
        ("MCP server", check_mcp_server_importable),
    ]

    results = []
    for name, check_func in checks:
        print(f"Checking {name}...")
        results.append(check_func())
        print()

    print("Configuring .mcp.json...")
    mcp_ok = setup_mcp_json(target_dir)
    print()

    if all(results) and mcp_ok:
        print("=" * 50)
        print("✓ Setup complete!")
        print("=" * 50)
        print()
        print("Claude Code hooks (auto-memory) — add to .claude/settings.local.json:")
        print("  See hooks/claude-code.json for the hooks block template.")
        print("  The hooks also need ANTHROPIC_API_KEY in the session env when")
        print("  VULCAN_EXTRACTION_STRATEGY=llm (see README § Per-Turn Auto-Memory).")
        print()
        print("Other agents:")
        print("    hooks/codex.toml    — Codex CLI")
        print("    hooks/hermes.yaml   — Hermes")
        print("    hooks/opencode.json — OpenCode")
    else:
        print("=" * 50)
        print("✗ Setup incomplete — fix errors above")
        print("=" * 50)
        sys.exit(1)


if __name__ == "__main__":
    target_dir = _get_target_dir()
    force = "--force" in sys.argv
    if target_dir != REPO_DIR:
        print(f"Installing into: {target_dir}")

    if force or should_update():
        update_skill(target_dir)
    else:
        _sync_files(target_dir)

    main(target_dir)
