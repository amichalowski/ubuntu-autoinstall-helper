"""Unit tests for autoinstall_helper.config."""

import pytest
from pathlib import Path

from autoinstall_helper.config import (
    extract_installer_channel,
    has_cloud_config_header,
)


class TestHasCloudConfigHeader:
    """Tests for has_cloud_config_header(config_file)."""

    def test_valid_cloud_config_header(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("#cloud-config\nautoinstall:\n  version: 1\n", encoding="utf-8")
        assert has_cloud_config_header(cfg) is True

    def test_header_with_trailing_whitespace(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("#cloud-config   \r\nautoinstall:\n  version: 1\n", encoding="utf-8")
        assert has_cloud_config_header(cfg) is True

    def test_missing_header(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("autoinstall:\n  version: 1\n", encoding="utf-8")
        assert has_cloud_config_header(cfg) is False

    def test_different_comment(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("#not-cloud-config\nautoinstall:\n  version: 1\n", encoding="utf-8")
        assert has_cloud_config_header(cfg) is False

    def test_nonexistent_file(self, tmp_path: Path):
        cfg = tmp_path / "nonexistent.yaml"
        assert has_cloud_config_header(cfg) is False


class TestExtractInstallerChannel:
    """Tests for extract_installer_channel(config_file)."""

    def test_valid_channel(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text(
            """
autoinstall:
  version: 1
  refresh-installer:
    update: true
    channel: 24.04/stable
""",
            encoding="utf-8",
        )
        assert extract_installer_channel(cfg) == "24.04/stable"

    def test_channel_stripped(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text(
            """
autoinstall:
  refresh-installer:
    channel: "  latest/edge  "
""",
            encoding="utf-8",
        )
        assert extract_installer_channel(cfg) == "latest/edge"

    def test_missing_channel_field_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text(
            """
autoinstall:
  refresh-installer:
    update: true
""",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc_info:
            extract_installer_channel(cfg)
        assert "Nie znaleziono pola 'autoinstall.refresh-installer.channel'" in str(exc_info.value)

    def test_missing_refresh_installer_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text(
            """
autoinstall:
  version: 1
""",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc_info:
            extract_installer_channel(cfg)
        assert "Nie znaleziono pola 'autoinstall.refresh-installer.channel'" in str(exc_info.value)

    def test_missing_autoinstall_key_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("other_key: 123\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            extract_installer_channel(cfg)
        assert "Nie znaleziono pola 'autoinstall.refresh-installer.channel'" in str(exc_info.value)
