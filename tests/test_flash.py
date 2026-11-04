"""Unit tests for autoinstall_helper.flash helpers."""

from pathlib import Path
from types import SimpleNamespace

from autoinstall_helper import flash


class TestIsBtrfs:
    """Tests for _is_btrfs(path)."""

    def _patch_run(self, monkeypatch, returncode: int, stdout: str):
        monkeypatch.setattr(
            flash.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
        )

    def test_btrfs_filesystem(self, monkeypatch):
        self._patch_run(monkeypatch, returncode=0, stdout="btrfs\n")
        assert flash._is_btrfs(Path("/tmp")) is True

    def test_non_btrfs_filesystem(self, monkeypatch):
        self._patch_run(monkeypatch, returncode=0, stdout="ext4\n")
        assert flash._is_btrfs(Path("/tmp")) is False

    def test_nonzero_returncode(self, monkeypatch):
        self._patch_run(monkeypatch, returncode=1, stdout="btrfs\n")
        assert flash._is_btrfs(Path("/tmp")) is False
