"""Unit tests for autoinstall_helper.disk helpers."""

import stat
from pathlib import Path
from types import SimpleNamespace

from autoinstall_helper import disk


class TestIsBlockDevice:
    """Tests for is_block_device(path)."""

    def test_block_device(self, monkeypatch):
        def _fake_stat(self):
            return SimpleNamespace(st_mode=stat.S_IFBLK | 0o660)

        monkeypatch.setattr(Path, "stat", _fake_stat)
        assert disk.is_block_device(Path("/dev/sda")) is True

    def test_regular_file(self, monkeypatch):
        def _fake_stat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

        monkeypatch.setattr(Path, "stat", _fake_stat)
        assert disk.is_block_device(Path("/tmp/image.img")) is False

    def test_nonexistent_path(self, monkeypatch):
        def _fake_stat(self):
            raise OSError("No such file or directory")

        monkeypatch.setattr(Path, "stat", _fake_stat)
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
