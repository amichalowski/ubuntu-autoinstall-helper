"""Unit tests for autoinstall_helper.disk helpers."""

import stat
from pathlib import Path
from types import SimpleNamespace

from autoinstall_helper import disk


class TestIsBlockDevice:
    """Tests for _is_block_device(path)."""

    def test_block_device(self, monkeypatch):
        def _fake_stat(self):
            return SimpleNamespace(st_mode=stat.S_IFBLK | 0o660)

        monkeypatch.setattr(Path, "stat", _fake_stat)
        assert disk._is_block_device(Path("/dev/sda")) is True

    def test_regular_file(self, monkeypatch):
        def _fake_stat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

        monkeypatch.setattr(Path, "stat", _fake_stat)
        assert disk._is_block_device(Path("/tmp/image.img")) is False

    def test_nonexistent_path(self, monkeypatch):
        def _fake_stat(self):
            raise OSError("No such file or directory")

        monkeypatch.setattr(Path, "stat", _fake_stat)
        assert disk._is_block_device(Path("/dev/does-not-exist")) is False
