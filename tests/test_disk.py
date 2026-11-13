"""Unit tests for autoinstall_helper.disk helpers."""

from pathlib import Path
from types import SimpleNamespace

from autoinstall_helper import disk
from autoinstall_helper.fsprobe import PathInfo


class TestIsBlockDevice:
    """Tests for is_block_device(path)."""

    def test_block_device(self, monkeypatch):
        monkeypatch.setattr(disk.fsprobe, "probe", lambda path: PathInfo(True, False, False, True, 0))
        assert disk.is_block_device(Path("/dev/sda")) is True

    def test_regular_file(self, monkeypatch):
        monkeypatch.setattr(disk.fsprobe, "probe", lambda path: PathInfo(True, True, False, False, 4096))
        assert disk.is_block_device(Path("/tmp/image.img")) is False

    def test_nonexistent_path(self, monkeypatch):
        monkeypatch.setattr(disk.fsprobe, "probe", lambda path: PathInfo(False, False, False, False, 0))
        assert disk.is_block_device(Path("/dev/does-not-exist")) is False


class TestFixGptBackupHeader:
    """Tests for fix_gpt_backup_header(target)."""

    def test_sgdisk_called_when_present(self, monkeypatch):
        calls = []

        def _fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(disk.shutil, "which", lambda cmd: "/usr/bin/sgdisk" if cmd == "sgdisk" else None)
        monkeypatch.setattr(disk.subprocess, "run", _fake_run)

        disk.fix_gpt_backup_header("/dev/sda")
        assert ["sgdisk", "-e", "/dev/sda"] in calls
        assert ["partprobe", "/dev/sda"] in calls

    def test_sgdisk_skipped_when_absent(self, monkeypatch):
        calls = []

        def _fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(disk.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(disk.subprocess, "run", _fake_run)

        disk.fix_gpt_backup_header("/dev/sda")
        assert not any(c[0] == "sgdisk" for c in calls)
        assert ["partprobe", "/dev/sda"] in calls
