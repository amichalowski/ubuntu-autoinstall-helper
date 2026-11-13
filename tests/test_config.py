"""Unit tests for autoinstall_helper.config."""

from pathlib import Path

import pytest

from autoinstall_helper.config import (
    extract_installer_channel,
    extract_snaps,
    has_cloud_config_header,
    require_cloud_config_header,
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


class TestRequireCloudConfigHeader:
    """Tests for require_cloud_config_header(config_file)."""

    def test_valid_header_passes(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("#cloud-config\nautoinstall:\n  version: 1\n", encoding="utf-8")
        require_cloud_config_header(cfg)  # must not raise

    def test_missing_header_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("autoinstall:\n  version: 1\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            require_cloud_config_header(cfg)
        assert "#cloud-config" in str(exc_info.value)

    def test_different_comment_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("#not-cloud-config\nautoinstall:\n  version: 1\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            require_cloud_config_header(cfg)
        assert "#cloud-config" in str(exc_info.value)


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
        assert "Field 'autoinstall.refresh-installer.channel' not found" in str(exc_info.value)

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
        assert "Field 'autoinstall.refresh-installer.channel' not found" in str(exc_info.value)

    def test_missing_autoinstall_key_raises(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("other_key: 123\n", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            extract_installer_channel(cfg)
        assert "Field 'autoinstall.refresh-installer.channel' not found" in str(exc_info.value)


class TestExtractSnaps:
    """Tests for extract_snaps(config_file)."""

    def test_returns_the_raw_snaps_list(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text(
            """
autoinstall:
  snaps:
    - name: hello-world
    - name: code
      channel: 24.04/stable
      classic: true
""",
            encoding="utf-8",
        )
        assert extract_snaps(cfg) == [
            {"name": "hello-world"},
            {"name": "code", "channel": "24.04/stable", "classic": True},
        ]

    def test_missing_snaps_section_returns_empty_list(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("autoinstall:\n  version: 1\n", encoding="utf-8")
        assert extract_snaps(cfg) == []

    def test_missing_autoinstall_key_returns_empty_list(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("other_key: 123\n", encoding="utf-8")
        assert extract_snaps(cfg) == []

    def test_non_mapping_snaps_value_is_returned_unvalidated(self, tmp_path: Path):
        cfg = tmp_path / "autoinstall.yaml"
        cfg.write_text("autoinstall:\n  snaps: hello-world\n", encoding="utf-8")
        assert extract_snaps(cfg) == "hello-world"
