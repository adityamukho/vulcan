# Marketplace Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish this repo as a GitHub-hosted Claude Code plugin by removing the `cargo`/Rust barrier (auto-download pre-built minigraf binaries), reverting the skill name to `temporal-reasoning`, and renaming the GitHub repo back to `temporal_reasoning`.

**Architecture:** `install.py` gains a `ensure_minigraf()` function that detects platform, downloads the correct pre-built binary from GitHub releases, verifies SHA256, and extracts to `~/.local/bin` (Linux/macOS) or `%LOCALAPPDATA%\Programs\minigraf` (Windows), falling back to `cargo install` only for unsupported platforms. Name references to "Vulcan" in user-facing files are reverted to "Temporal Reasoning"; the Python module name (`vulcan.py`, `from vulcan import`) is unchanged.

**Tech Stack:** Python stdlib only (`urllib.request`, `tarfile`, `zipfile`, `hashlib`, `platform`, `tempfile`). pytest for tests.

---

## File Map

| File | Change |
|---|---|
| `install.py` | Replace `check_minigraf()` with `ensure_minigraf()` + 5 private helpers; update `SKILL_DIRS`; update docstring |
| `tests/test_install.py` | New — tests for all new install.py functions |
| `SKILL.md` | Frontmatter `name`, H1, prose "Vulcan" → "Temporal Reasoning"; Dependencies section |
| `CLAUDE.md` | H1 rename only |
| `AGENTS.md` | H1 rename + minigraf version bump |
| `skill.json` | `requires.minigraf` bump to `>=0.19.0` |
| `ROADMAP.md` | Mark Marketplace Publishing complete |
| `README.md` | Brand rename + overhaul Install section with plugin instructions |

**Not changed:** `vulcan.py`, `report_issue.py`, `tools/*.json` (internal API names — breaking change deferred).

---

## Task 1: GitHub repo rename

**Files:**
- Manual: GitHub repo settings at https://github.com/adityamukho/vulcan/settings
- Run: `git remote set-url`

- [ ] **Step 1: Rename repo on GitHub**

Go to https://github.com/adityamukho/vulcan/settings → Repository name → change to `temporal_reasoning` → click "Rename".

Expected: GitHub redirects `adityamukho/vulcan` → `adityamukho/temporal_reasoning` automatically.

- [ ] **Step 2: Update git remote**

```bash
git remote set-url origin git@github.com:adityamukho/temporal_reasoning.git
```

- [ ] **Step 3: Verify remote**

```bash
git remote -v
```

Expected output:
```
origin  git@github.com:adityamukho/temporal_reasoning.git (fetch)
origin  git@github.com:adityamukho/temporal_reasoning.git (push)
```

---

## Task 2: Revert name in SKILL.md

**Files:**
- Modify: `SKILL.md`

- [ ] **Step 1: Update frontmatter name**

In `SKILL.md`, change line 1–2:
```
---
name: vulcan
```
to:
```
---
name: temporal-reasoning
```

- [ ] **Step 2: Rename H1 and opening prose**

Replace:
```markdown
# Vulcan

Perfect memory. Exact reasoning. Complete history.

Vulcan gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.
```
with:
```markdown
# Temporal Reasoning

Perfect memory. Exact reasoning. Complete history.

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.
```

- [ ] **Step 3: Update Core Idea opening to lead with the user problem**

Replace the current `## The Core Idea` opening paragraph:
```markdown
## The Core Idea

Without memory, every conversation starts from zero. You end up asking the user things they've already answered, writing code that contradicts decisions they've already made, and missing constraints they told you about weeks ago. This skill gives you a persistent store you can write to and query at any time.
```
with:
```markdown
## The Core Idea

Every session starts from zero — you ask questions already answered, write code that contradicts decisions already made, and miss constraints established weeks ago. Temporal Reasoning fixes this: a persistent bi-temporal graph store you write to and query at any time, so context survives across sessions.
```

- [ ] **Step 4: Update Dependencies section**

Replace:
```markdown
## Dependencies

- **Minigraf >= 0.18.0** — `cargo install minigraf`
- **Python 3** — for the wrapper
```
with:
```markdown
## Dependencies

- **Minigraf >= 0.19.0** — run `python install.py` to download the correct pre-built binary for your platform automatically. Falls back to `cargo install minigraf` only on unsupported platforms.
- **Python 3** — for the wrapper
```

- [ ] **Step 5: Commit**

```bash
git add SKILL.md
git commit -m "feat: revert skill name to temporal-reasoning in SKILL.md"
```

---

## Task 3: Revert name in CLAUDE.md, AGENTS.md, skill.json, ROADMAP.md

**Files:**
- Modify: `CLAUDE.md`, `AGENTS.md`, `skill.json`, `ROADMAP.md`

- [ ] **Step 1: Update CLAUDE.md heading**

Replace line 1:
```markdown
# Vulcan — AI Coding Agent Memory
```
with:
```markdown
# Temporal Reasoning — AI Coding Agent Memory
```

Replace line 3:
```markdown
Vulcan provides persistent bi-temporal graph memory for AI coding agents.
```
with:
```markdown
Temporal Reasoning provides persistent bi-temporal graph memory for AI coding agents.
```

- [ ] **Step 2: Update AGENTS.md heading and minigraf version**

Replace line 1:
```markdown
# Vulcan Repository
```
with:
```markdown
# Temporal Reasoning Repository
```

Replace line 3:
```markdown
Persistent bi-temporal graph memory skill for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.
```
with:
```markdown
Persistent bi-temporal graph memory for AI coding agents. Prevents context drift across long sessions by storing architecture decisions, dependencies, and constraints.
```

Replace in the Dependencies section:
```markdown
- **Minigraf >= 0.18.0** — install via: `cargo install minigraf`
```
with:
```markdown
- **Minigraf >= 0.19.0** — run `python install.py` (downloads pre-built binary automatically)
```

- [ ] **Step 3: Bump minigraf version in skill.json**

In `skill.json`, replace:
```json
"requires": {
    "minigraf": ">=0.18.0"
  },
```
with:
```json
"requires": {
    "minigraf": ">=0.19.0"
  },
```

- [ ] **Step 4: Mark Marketplace Publishing complete in ROADMAP.md**

Replace the `## Marketplace Publishing` section:
```markdown
## Marketplace Publishing

The skill is functionally complete and benchmarked. The blocking dependency for marketplace publication is **minigraf pre-built binaries**: `cargo install minigraf` requires a Rust toolchain, which is too high a barrier for general users. Will be published once minigraf ships binaries for common platforms (Linux x86_64, macOS arm64/x86_64, Windows). At that point, will also reframe the skill description to lead with the user benefit (no lost context between sessions) rather than the mechanism.
```
with:
```markdown
## Marketplace Publishing ✓

Published as a GitHub-hosted Claude Code plugin. Users add the repo to `extraKnownMarketplaces` in `settings.json` — see README for instructions.

Pre-built binary support landed in minigraf v0.19.0 (2026-04-14), removing the `cargo`/Rust installation barrier. `install.py` now downloads the correct binary automatically for Linux x86_64, Linux aarch64, macOS arm64, macOS x86_64, and Windows. Skill description reframed to lead with user benefit.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md AGENTS.md skill.json ROADMAP.md
git commit -m "feat: revert name to temporal-reasoning in CLAUDE.md, AGENTS.md, skill.json, ROADMAP.md"
```

---

## Task 4: Write failing tests for _get_platform_asset() and _verify_checksum()

**Files:**
- Create: `tests/test_install.py`

- [ ] **Step 1: Create test file with imports and platform detection tests**

Create `tests/test_install.py`:

```python
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import zipfile

import pytest
from unittest.mock import patch, MagicMock

# import install.py as a module (main() is guarded by __name__ == "__main__")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install


class TestGetPlatformAsset:
    def test_linux_x86_64(self):
        with patch("sys.platform", "linux"), patch("platform.machine", return_value="x86_64"):
            assert install._get_platform_asset() == "minigraf-x86_64-unknown-linux-gnu.tar.xz"

    def test_linux_amd64_alias(self):
        with patch("sys.platform", "linux"), patch("platform.machine", return_value="amd64"):
            assert install._get_platform_asset() == "minigraf-x86_64-unknown-linux-gnu.tar.xz"

    def test_linux_aarch64(self):
        with patch("sys.platform", "linux"), patch("platform.machine", return_value="aarch64"):
            assert install._get_platform_asset() == "minigraf-aarch64-unknown-linux-gnu.tar.xz"

    def test_macos_arm64(self):
        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="arm64"):
            assert install._get_platform_asset() == "minigraf-aarch64-apple-darwin.tar.xz"

    def test_macos_x86_64(self):
        with patch("sys.platform", "darwin"), patch("platform.machine", return_value="x86_64"):
            assert install._get_platform_asset() == "minigraf-x86_64-apple-darwin.tar.xz"

    def test_windows(self):
        with patch("sys.platform", "win32"):
            assert install._get_platform_asset() == "minigraf-x86_64-pc-windows-msvc.zip"

    def test_unsupported_platform_returns_none(self):
        with patch("sys.platform", "freebsd14"), patch("platform.machine", return_value="x86_64"):
            assert install._get_platform_asset() is None

    def test_unsupported_linux_arch_returns_none(self):
        with patch("sys.platform", "linux"), patch("platform.machine", return_value="riscv64"):
            assert install._get_platform_asset() is None


class TestVerifyChecksum:
    def test_valid_checksum_passes(self, tmp_path):
        data = b"fake minigraf binary content"
        asset = tmp_path / "minigraf.tar.xz"
        asset.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        sha256_file = tmp_path / "minigraf.tar.xz.sha256"
        sha256_file.write_text(f"{digest}  minigraf.tar.xz\n")
        # Should not raise
        install._verify_checksum(str(asset), str(sha256_file))

    def test_invalid_checksum_raises(self, tmp_path):
        data = b"fake minigraf binary content"
        asset = tmp_path / "minigraf.tar.xz"
        asset.write_bytes(data)
        sha256_file = tmp_path / "minigraf.tar.xz.sha256"
        sha256_file.write_text("deadbeef" * 8 + "  minigraf.tar.xz\n")
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            install._verify_checksum(str(asset), str(sha256_file))
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_install.py -v 2>&1 | head -30
```

Expected: `AttributeError: module 'install' has no attribute '_get_platform_asset'` (functions don't exist yet).

---

## Task 5: Implement _get_platform_asset() and _verify_checksum()

**Files:**
- Modify: `install.py`

- [ ] **Step 1: Add import and _get_platform_asset() after existing imports**

After the existing imports at the top of `install.py` (after `from datetime import datetime, timezone`), add:

```python
import platform
import hashlib
```

Then, after the `SKILL_DIRS` constant block, add:

```python
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
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
pytest tests/test_install.py::TestGetPlatformAsset tests/test_install.py::TestVerifyChecksum -v
```

Expected: All 10 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: add _get_platform_asset and _verify_checksum to install.py"
```

---

## Task 6: Write failing tests for _install_binary()

**Files:**
- Modify: `tests/test_install.py`

- [ ] **Step 1: Append _install_binary tests**

Add to `tests/test_install.py`:

```python
class TestInstallBinary:
    def test_extracts_tar_xz_and_sets_executable(self, tmp_path):
        binary_data = b"#!/bin/sh\necho 'minigraf 0.19.0'"
        archive_path = tmp_path / "minigraf-x86_64-unknown-linux-gnu.tar.xz"
        with tarfile.open(str(archive_path), "w:xz") as tar:
            info = tarfile.TarInfo(name="minigraf")
            info.size = len(binary_data)
            tar.addfile(info, io.BytesIO(binary_data))

        install_dir = str(tmp_path / "local" / "bin")
        with patch("sys.platform", "linux"), \
             patch("os.path.expanduser", side_effect=lambda p: install_dir if "local/bin" in p else os.path.expanduser(p)):
            result = install._install_binary(
                str(archive_path), "minigraf-x86_64-unknown-linux-gnu.tar.xz"
            )

        assert result == os.path.join(install_dir, "minigraf")
        assert os.path.exists(result)
        assert os.access(result, os.X_OK)

    def test_extracts_zip_on_windows(self, tmp_path):
        binary_data = b"MZ fake windows exe"
        archive_path = tmp_path / "minigraf-x86_64-pc-windows-msvc.zip"
        with zipfile.ZipFile(str(archive_path), "w") as zf:
            zf.writestr("minigraf.exe", binary_data)

        install_dir = str(tmp_path / "Programs" / "minigraf")
        with patch("sys.platform", "win32"), \
             patch.dict(os.environ, {"LOCALAPPDATA": str(tmp_path)}):
            result = install._install_binary(
                str(archive_path), "minigraf-x86_64-pc-windows-msvc.zip"
            )

        assert result == os.path.join(
            str(tmp_path), "Programs", "minigraf", "minigraf.exe"
        )
        assert os.path.exists(result)

    def test_raises_if_no_binary_in_archive(self, tmp_path):
        archive_path = tmp_path / "minigraf-x86_64-unknown-linux-gnu.tar.xz"
        with tarfile.open(str(archive_path), "w:xz") as tar:
            info = tarfile.TarInfo(name="README.md")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"blah"))

        install_dir = str(tmp_path / "local" / "bin")
        with patch("sys.platform", "linux"), \
             patch("os.path.expanduser", side_effect=lambda p: install_dir if "local/bin" in p else os.path.expanduser(p)):
            with pytest.raises(ValueError, match="No minigraf binary"):
                install._install_binary(
                    str(archive_path), "minigraf-x86_64-unknown-linux-gnu.tar.xz"
                )
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_install.py::TestInstallBinary -v 2>&1 | head -20
```

Expected: `AttributeError: module 'install' has no attribute '_install_binary'`

---

## Task 7: Implement _install_binary()

**Files:**
- Modify: `install.py`

- [ ] **Step 1: Add _install_binary() after _verify_checksum()**

```python
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
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
pytest tests/test_install.py::TestInstallBinary -v
```

Expected: All 3 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: add _install_binary to install.py"
```

---

## Task 8: Write failing tests for _get_latest_version(), _download_binary(), _install_via_cargo()

**Files:**
- Modify: `tests/test_install.py`

- [ ] **Step 1: Append network and cargo tests**

Add to `tests/test_install.py`:

```python
class TestGetLatestVersion:
    def test_parses_version_from_redirect_url(self):
        mock_resp = MagicMock()
        mock_resp.url = "https://github.com/adityamukho/minigraf/releases/tag/v0.19.0"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            version = install._get_latest_version()

        assert version == "v0.19.0"

    def test_raises_on_unexpected_redirect(self):
        mock_resp = MagicMock()
        mock_resp.url = "https://github.com/adityamukho/minigraf/releases"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(ValueError, match="Could not determine"):
                install._get_latest_version()


class TestDownloadBinary:
    def test_downloads_asset_and_sha256_sidecar(self, tmp_path):
        downloaded = []

        def fake_urlretrieve(url, out):
            downloaded.append(url)
            with open(out, "wb") as f:
                f.write(b"fake")

        with patch("urllib.request.urlretrieve", side_effect=fake_urlretrieve):
            result = install._download_binary(
                "minigraf-x86_64-unknown-linux-gnu.tar.xz", "v0.19.0", str(tmp_path)
            )

        assert len(downloaded) == 2
        assert any("minigraf-x86_64-unknown-linux-gnu.tar.xz.sha256" in u for u in downloaded)
        assert result == str(tmp_path / "minigraf-x86_64-unknown-linux-gnu.tar.xz")
        assert os.path.exists(result)
        assert os.path.exists(result + ".sha256")


class TestInstallViaCargo:
    def test_returns_true_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert install._install_via_cargo() is True

    def test_returns_false_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            assert install._install_via_cargo() is False

    def test_returns_false_when_cargo_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert install._install_via_cargo() is False
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_install.py::TestGetLatestVersion tests/test_install.py::TestDownloadBinary tests/test_install.py::TestInstallViaCargo -v 2>&1 | head -20
```

Expected: `AttributeError: module 'install' has no attribute '_get_latest_version'`

---

## Task 9: Implement _get_latest_version(), _download_binary(), _install_via_cargo()

**Files:**
- Modify: `install.py`

- [ ] **Step 1: Add the three functions after _install_binary()**

```python
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
```

- [ ] **Step 2: Run tests to confirm they pass**

```bash
pytest tests/test_install.py::TestGetLatestVersion tests/test_install.py::TestDownloadBinary tests/test_install.py::TestInstallViaCargo -v
```

Expected: All 6 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: add _get_latest_version, _download_binary, _install_via_cargo to install.py"
```

---

## Task 10: Write failing tests for ensure_minigraf()

**Files:**
- Modify: `tests/test_install.py`

- [ ] **Step 1: Append ensure_minigraf tests**

Add to `tests/test_install.py`:

```python
class TestEnsureMinigraf:
    def test_returns_true_if_already_on_path(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert install.ensure_minigraf() is True
        mock_run.assert_called_once_with(
            ["minigraf"], input="", capture_output=True, text=True, timeout=10, check=True
        )

    def test_downloads_binary_when_not_found(self, tmp_path):
        binary_path = str(tmp_path / "bin" / "minigraf")
        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("install._get_platform_asset", return_value="minigraf-x86_64-unknown-linux-gnu.tar.xz"), \
             patch("install._get_latest_version", return_value="v0.19.0"), \
             patch("install._download_binary", return_value=str(tmp_path / "asset.tar.xz")), \
             patch("install._verify_checksum"), \
             patch("install._install_binary", return_value=binary_path):
            assert install.ensure_minigraf() is True

    def test_falls_back_to_cargo_on_unsupported_platform(self):
        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("install._get_platform_asset", return_value=None), \
             patch("install._install_via_cargo", return_value=True) as mock_cargo:
            assert install.ensure_minigraf() is True
        mock_cargo.assert_called_once()

    def test_falls_back_to_cargo_on_download_failure(self):
        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("install._get_platform_asset", return_value="minigraf-x86_64-unknown-linux-gnu.tar.xz"), \
             patch("install._get_latest_version", side_effect=Exception("network error")), \
             patch("install._install_via_cargo", return_value=False) as mock_cargo:
            assert install.ensure_minigraf() is False
        mock_cargo.assert_called_once()

    def test_falls_back_to_cargo_on_checksum_failure(self, tmp_path):
        with patch("subprocess.run", side_effect=FileNotFoundError), \
             patch("install._get_platform_asset", return_value="minigraf-x86_64-unknown-linux-gnu.tar.xz"), \
             patch("install._get_latest_version", return_value="v0.19.0"), \
             patch("install._download_binary", return_value=str(tmp_path / "asset.tar.xz")), \
             patch("install._verify_checksum", side_effect=ValueError("SHA256 mismatch")), \
             patch("install._install_via_cargo", return_value=True) as mock_cargo:
            assert install.ensure_minigraf() is True
        mock_cargo.assert_called_once()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_install.py::TestEnsureMinigraf -v 2>&1 | head -20
```

Expected: `AttributeError: module 'install' has no attribute 'ensure_minigraf'`

---

## Task 11: Implement ensure_minigraf() and wire it into install.py

**Files:**
- Modify: `install.py`

- [ ] **Step 1: Add ensure_minigraf() after _install_via_cargo()**

```python
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
```

- [ ] **Step 2: Replace check_minigraf() call in main() with ensure_minigraf()**

In the `main()` function, replace:
```python
    checks = [
        ("Python version", check_python_version),
        ("minigraf CLI", check_minigraf),
        ("Module import", check_tool_import),
    ]
```
with:
```python
    checks = [
        ("Python version", check_python_version),
        ("minigraf CLI", ensure_minigraf),
        ("Module import", check_tool_import),
    ]
```

- [ ] **Step 3: Update SKILL_DIRS to use temporal-reasoning**

Replace:
```python
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "vulcan"),
    os.path.join("skills", "vulcan"),
]
```
with:
```python
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal-reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]
```

- [ ] **Step 4: Update module docstring**

Replace the top-of-file docstring:
```python
"""
Installation script for vulcan skill.
Checks dependencies, syncs skill files, provides next steps.
```
with:
```python
"""
Installation script for temporal-reasoning skill.
Checks dependencies (downloading minigraf pre-built binary if needed), syncs skill files, provides next steps.
```

- [ ] **Step 5: Run all install tests**

```bash
pytest tests/test_install.py -v
```

Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: replace check_minigraf with ensure_minigraf binary download in install.py"
```

---

## Task 12: Overhaul README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rename H1 and opening prose**

Replace lines 1–5:
```markdown
# Vulcan

**Perfect memory. Exact reasoning. Complete history.**

Vulcan gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.
```
with:
```markdown
# Temporal Reasoning

**Perfect memory. Exact reasoning. Complete history.**

Temporal Reasoning gives AI coding agents bi-temporal graph memory: query any past state, traverse live dependency graphs, and correlate architectural decisions with structural change — all with deterministic Datalog, no fuzzy retrieval.
```

- [ ] **Step 2: Rename section headers and remaining prose references**

Apply these replacements throughout `README.md`:

| Find | Replace |
|---|---|
| `## Questions Only Vulcan Can Answer` | `## Questions Only Temporal Reasoning Can Answer` |
| `Vulcan is the only tool where` | `This is the only tool where` |
| `## Why Vulcan?` | `## Why Temporal Reasoning?` |
| `Vulcan answers a harder question:` | `Temporal Reasoning answers a harder question:` |
| `(Vulcan's storage engine)` | `(the storage engine)` |

- [ ] **Step 3: Replace Install section (lines 95–120)**

Replace the entire `## Install` section:
```markdown
## Install

```bash
# Install minigraf (requires Rust)
cargo install minigraf

# Run setup
python install.py
```

### Install In Agent Environments

Claude Code / Codex:
- Install the local skill from this repository as `vulcan`.
- Use [SKILL.md](/SKILL.md) and [skill.json](/skill.json) as the primary skill files.

OpenCode:
- Run `python install.py` from the repository root.
- This syncs the skill into `.opencode/skills/vulcan`.

If manual installation is required, include:
- [SKILL.md](/SKILL.md)
- [skill.json](/skill.json)
- [tools/query.json](/tools/query.json)
- [tools/transact.json](/tools/transact.json)
- [tools/report_issue.json](/tools/report_issue.json)
```

with:

```markdown
## Install

### Claude Code (plugin — recommended)

Add to your Claude Code `settings.json`:

```json
"extraKnownMarketplaces": {
  "temporal-reasoning": {
    "source": {
      "source": "git",
      "url": "https://github.com/adityamukho/temporal_reasoning"
    }
  }
}
```

Then enable the `temporal-reasoning` plugin in Claude Code. Once enabled, run once to download the minigraf binary:

```bash
python install.py
```

`install.py` auto-detects your platform and downloads the correct pre-built binary (Linux x86_64/aarch64, macOS arm64/x86_64, Windows). Falls back to `cargo install minigraf` on unsupported platforms.

### Manual install

```bash
git clone https://github.com/adityamukho/temporal_reasoning
cd temporal_reasoning
python install.py
```

### OpenCode

```bash
python install.py
```

This syncs the skill into `.opencode/skills/temporal-reasoning`.
```

- [ ] **Step 4: Update Minigraf version reference in architecture diagram**

Replace:
```
│              Minigraf CLI (>= 0.18.0)                   │
```
with:
```
│              Minigraf CLI (>= 0.19.0)                   │
```

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "feat: rename Vulcan to Temporal Reasoning in README, add plugin install instructions"
```

---

## Task 13: Run full test suite and verify

**Files:**
- Read-only verification

- [ ] **Step 1: Run all tests**

```bash
pytest tests/ -v
```

Expected: All tests PASS with no failures or errors.

- [ ] **Step 2: Verify no stray "Vulcan" brand references remain in user-facing files**

```bash
grep -rn "Vulcan" SKILL.md CLAUDE.md AGENTS.md README.md ROADMAP.md skill.json
```

Expected: No output (zero matches). Note: `vulcan.py`, `tools/*.json`, `docs/` and `tests/` may still contain `vulcan` references — those are intentional (internal API or history).

- [ ] **Step 3: Verify install.py SKILL_DIRS use temporal-reasoning**

```bash
grep "SKILL_DIRS" install.py
```

Expected:
```
SKILL_DIRS = [
    os.path.join(".opencode", "skills", "temporal-reasoning"),
    os.path.join("skills", "temporal-reasoning"),
]
```

- [ ] **Step 4: Verify skill.json version**

```bash
python -c "import json; d=json.load(open('skill.json')); print(d['requires']['minigraf'])"
```

Expected: `>=0.19.0`

- [ ] **Step 5: Final commit if any fixups were needed; otherwise done**

```bash
git status
```

If clean: done. If there are uncommitted fixups, stage and commit them with an appropriate message.
