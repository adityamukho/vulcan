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
