#!/usr/bin/env python3
"""
Installation script for temporal-reasoning skill.
Checks dependencies (downloading minigraf pre-built binary if needed), syncs skill files, provides next steps.

Usage:
    python install.py          # Full install with dependencies
    python install.py --check  # Just check dependencies
    python install.py --force  # Force reinstall even if recent
"""

import sys
import subprocess
import os
from datetime import datetime, timezone
import platform
import hashlib

UPDATE_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_UPDATE_FILE = os.path.join(REPO_DIR, ".last_update")

FILES_TO_SYNC = ["SKILL.md", "vulcan.py", "skill.json"]
DIRS_TO_SYNC = ["tools"]
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal-reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]

MINIGRAF_RELEASES_URL = "https://github.com/adityamukho/minigraf/releases"


def _get_platform_asset() -> str | None:
    """Return the release asset filename for the current platform, or None if unsupported."""
    machine = platform.machine().lower()
    plat = sys.platform

    if plat == "linux":
        if machine in ("x86_64", "amd64"):
            return "minigraf-x86_64-unknown-linux-gnu.tar.xz"
        if machine in ("aarch64", "arm64"):
            return "minigraf-aarch64-unknown-linux-gnu.tar.xz"
    elif plat == "darwin":
        if machine in ("arm64", "aarch64"):
            return "minigraf-aarch64-apple-darwin.tar.xz"
        if machine in ("x86_64", "amd64"):
            return "minigraf-x86_64-apple-darwin.tar.xz"
    elif plat == "win32":
        return "minigraf-x86_64-pc-windows-msvc.zip"

    return None


def _verify_checksum(asset_path: str, sha256_path: str) -> None:
    """Verify SHA256 of asset_path against sha256_path. Raises ValueError on mismatch."""
    h = hashlib.sha256()
    with open(asset_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    actual = h.hexdigest()

    with open(sha256_path) as f:
        expected = f.read().strip().split()[0]

    if actual != expected:
        raise ValueError(
            f"SHA256 mismatch for {os.path.basename(asset_path)}: "
            f"got {actual[:16]}…, expected {expected[:16]}…"
        )


def _install_binary(asset_path: str, asset: str) -> str:
    """Extract minigraf binary from asset archive. Returns path to installed binary."""
    if sys.platform == "win32":
        install_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Programs", "minigraf"
        )
        binary_name = "minigraf.exe"
    else:
        install_dir = os.path.expanduser("~/.local/bin")
        binary_name = "minigraf"

    os.makedirs(install_dir, exist_ok=True)

    if asset.endswith(".tar.xz"):
        import tarfile as _tarfile
        with _tarfile.open(asset_path) as tar:
            members = [m for m in tar.getmembers()
                       if m.name.endswith("minigraf") or m.name.endswith("minigraf.exe")]
            if not members:
                raise ValueError(f"No minigraf binary found in {asset}")
            member = members[0]
            member.name = os.path.basename(member.name)
            tar.extract(member, path=install_dir)
    elif asset.endswith(".zip"):
        import zipfile as _zipfile
        with _zipfile.ZipFile(asset_path) as zf:
            names = [n for n in zf.namelist()
                     if n.endswith("minigraf.exe") or n.endswith("minigraf")]
            if not names:
                raise ValueError(f"No minigraf binary found in {asset}")
            data = zf.read(names[0])
            out = os.path.join(install_dir, os.path.basename(names[0]))
            with open(out, "wb") as f:
                f.write(data)

    binary_path = os.path.join(install_dir, binary_name)
    if sys.platform != "win32":
        os.chmod(binary_path, 0o755)

    return binary_path


def _get_latest_version() -> str:
    """Follow GitHub releases/latest redirect to get the current version tag."""
    import urllib.request
    url = f"{MINIGRAF_RELEASES_URL}/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "temporal-reasoning-install"})
    with urllib.request.urlopen(req) as resp:
        final_url = resp.url
    # final_url is like .../releases/tag/v0.19.0
    tag = final_url.rstrip("/").split("/")[-1]
    if not tag.startswith("v"):
        raise ValueError(f"Could not determine latest version from redirect URL: {final_url}")
    return tag


def _download_binary(asset: str, version: str, dest_dir: str) -> str:
    """Download asset and its .sha256 sidecar to dest_dir. Returns path to asset file."""
    import urllib.request
    base_url = f"{MINIGRAF_RELEASES_URL}/download/{version}"
    asset_path = os.path.join(dest_dir, asset)
    for filename in (asset, asset + ".sha256"):
        url = f"{base_url}/{filename}"
        out = os.path.join(dest_dir, filename)
        print(f"  Downloading {filename}...")
        urllib.request.urlretrieve(url, out)
    return asset_path


def _install_via_cargo() -> bool:
    """Fall back to cargo install minigraf. Returns True on success."""
    try:
        result = subprocess.run(
            ["cargo", "install", "minigraf"],
            timeout=300,
        )
        if result.returncode == 0:
            print("✓ minigraf installed via cargo")
            return True
        print("✗ cargo install minigraf failed")
        return False
    except FileNotFoundError:
        print("✗ cargo not found")
        print()
        print("To install minigraf, either:")
        print("  1. Install Rust (https://rustup.rs), then: cargo install minigraf")
        print("  2. Download manually from: https://github.com/adityamukho/minigraf/releases")
        return False


def ensure_minigraf() -> bool:
    """Ensure minigraf is available. Downloads pre-built binary if not on PATH."""
    try:
        subprocess.run(
            ["minigraf"],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        print("✓ minigraf CLI: found")
        return True
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass

    print("✗ minigraf CLI not found — downloading pre-built binary...")

    asset = _get_platform_asset()
    if asset is None:
        print("  No pre-built binary for this platform — falling back to cargo install...")
        return _install_via_cargo()

    try:
        import tempfile
        version = _get_latest_version()
        with tempfile.TemporaryDirectory() as tmp:
            asset_path = _download_binary(asset, version, tmp)
            _verify_checksum(asset_path, asset_path + ".sha256")
            binary_path = _install_binary(asset_path, asset)

        install_dir = os.path.dirname(binary_path)
        print(f"✓ minigraf {version} installed to {binary_path}")

        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        if install_dir not in path_dirs:
            print(f"  Note: add {install_dir} to your PATH to use minigraf from any directory.")

        return True
    except Exception as e:
        print(f"  Binary download failed ({e}) — falling back to cargo install...")
        return _install_via_cargo()


def _get_target_dir() -> str:
    """Return install target: --target arg if provided, else cwd."""
    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            return os.path.abspath(sys.argv[idx + 1])
    return os.getcwd()


def check_python_version():
    """Check Python version is 3.8+."""
    if sys.version_info < (3, 8):
        print(f"ERROR: Python 3.8+ required, "
              f"found {sys.version_info.major}.{sys.version_info.minor}")
        return False
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return True


def check_tool_import():
    """Verify vulcan module can be imported."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("vulcan")
        if spec is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            sys.path.insert(0, script_dir)
        import vulcan
        print("✓ vulcan module can be imported")
        return True
    except ImportError as e:
        print(f"✗ Cannot import vulcan: {e}")
        return False


def main():
    print("=" * 50)
    print("Vulcan Skill Setup")
    print("=" * 50)
    print()

    checks = [
        ("Python version", check_python_version),
        ("minigraf CLI", ensure_minigraf),
        ("Module import", check_tool_import),
    ]

    results = []
    for name, check_func in checks:
        print(f"Checking {name}...")
        results.append(check_func())
        print()

    if all(results):
        print("=" * 50)
        print("✓ Setup complete!")
        print("=" * 50)
        print()
        print("Usage:")
        print("  # As Python module:")
        msg = "from vulcan import query, transact; "
        msg += "print(query('[:find ?e :where [?e :test/name]]'))"
        print(f"  python -c \"{msg}\"")
        print()
        print("  # As CLI:")
        print("  python vulcan.py query '[:find ?e :where [?e :test/name]]'")
        print("  python vulcan.py transact '[[:test :person/name \\\"Alice\\\"]]'")
        print()
        print("  # Import and use in code:")
        print("  from vulcan import query, transact")
        tx_msg = "transact('[[:decision :arch/cache-strategy \"Redis\"]]', "
        tx_msg += "reason='fast in-memory caching')"
        print(f"  {tx_msg}")
        q_msg = "result = query('[:find ?s :where [_ :arch/cache-strategy ?s]]')"
        print(f"  {q_msg}")
    else:
        print("=" * 50)
        print("✗ Setup incomplete - fix errors above")
        print("=" * 50)
        sys.exit(1)


def should_update():
    """Check if update should run (no more than once a week)."""
    if not os.path.exists(LAST_UPDATE_FILE):
        return True

    try:
        with open(LAST_UPDATE_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                return True
            last_update = datetime.fromisoformat(content)
    except ValueError:
        # Legacy float epoch or corrupt file — treat as expired and let the
        # next successful update_skill() write a fresh ISO 8601 timestamp.
        return True
    except IOError:
        return True

    return (datetime.now(timezone.utc) - last_update).total_seconds() > UPDATE_INTERVAL


def _write_last_update() -> None:
    """Write the current UTC time as ISO 8601 to the last-update file."""
    with open(LAST_UPDATE_FILE, 'w') as f:
        f.write(datetime.now(timezone.utc).isoformat())


def _sync_files(target_dir: str) -> None:
    """Copy skill files and directories into each agent skill folder under target_dir."""
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
            check=True
        )
        # Always record the check time so the weekly throttle resets,
        # regardless of whether git pull fetched new commits.
        _write_last_update()

        if result.stdout.strip() and "Already up to date" not in result.stdout:
            print("Pulling latest from GitHub...")
            _sync_files(target_dir)
            print("✓ Skill updated!")
        else:
            _sync_files(target_dir)
            print("✓ Skill already up-to-date")
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


if __name__ == "__main__":
    target_dir = _get_target_dir()
    force = "--force" in sys.argv
    if target_dir != REPO_DIR:
        print(f"Installing into: {target_dir}")

    # Pull from GitHub when forced or when weekly interval has elapsed
    if force or should_update():
        update_skill(target_dir)
    else:
        # Still sync files even if we skip git pull (e.g. fresh project install)
        _sync_files(target_dir)

    main()
