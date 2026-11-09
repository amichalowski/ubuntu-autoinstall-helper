"""Unit tests for pure helpers in autoinstall_helper.cidata."""

import json
from types import SimpleNamespace

from autoinstall_helper import cidata


class TestLsblkDevices:
    """Tests for _lsblk_devices(*args) - lsblk JSON output parsing."""

    def _patch_lsblk(self, monkeypatch, stdout: str):
        monkeypatch.setattr(
            cidata.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=stdout, returncode=0),
        )

    def test_parses_json(self, monkeypatch):
        stdout = json.dumps({"blockdevices": [{"path": "/dev/sda", "type": "disk"}]})
        self._patch_lsblk(monkeypatch, stdout)

        assert cidata._lsblk_devices("-p", "-o", "PATH,TYPE", "/dev/sda") == [
            {"path": "/dev/sda", "type": "disk"},
        ]

    def test_missing_blockdevices_key(self, monkeypatch):
        self._patch_lsblk(monkeypatch, "{}")
        assert cidata._lsblk_devices() == []

    def test_invalid_json_returns_empty(self, monkeypatch):
        self._patch_lsblk(monkeypatch, "not-json")
        assert cidata._lsblk_devices() == []


class TestIterDevices:
    """Tests for _iter_devices(devices) - recursive blockdevice traversal."""

    def test_flattens_nested_children(self):
        devices = [
            {
                "path": "/dev/sda",
                "children": [
                    {"path": "/dev/sda1", "children": [{"path": "/dev/sda1p1"}]},
                    {"path": "/dev/sda2"},
                ],
            }
        ]
        assert [dev["path"] for dev in cidata._iter_devices(devices)] == [
            "/dev/sda",
            "/dev/sda1",
            "/dev/sda1p1",
            "/dev/sda2",
        ]

    def test_empty(self):
        assert list(cidata._iter_devices([])) == []


class TestGetPartitions:
    """Tests for _get_partitions(disk) / _find_last_partition(disk) - lsblk JSON."""

    def _patch_lsblk(self, monkeypatch, blockdevices: list):
        stdout = json.dumps({"blockdevices": blockdevices})
        monkeypatch.setattr(
            cidata.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=stdout, returncode=0),
        )

    def test_returns_partition_paths(self, monkeypatch):
        self._patch_lsblk(
            monkeypatch,
            [
                {
                    "path": "/dev/sda",
                    "type": "disk",
                    "children": [
                        {"path": "/dev/sda1", "type": "part"},
                        {"path": "/dev/sda2", "type": "part"},
                    ],
                }
            ],
        )
        assert cidata._get_partitions("/dev/sda") == ["/dev/sda1", "/dev/sda2"]

    def test_ignores_non_partition_devices(self, monkeypatch):
        self._patch_lsblk(
            monkeypatch,
            [
                {"path": "/dev/sda", "type": "disk"},
                {"path": "/dev/sdb", "type": "loop"},
            ],
        )
        assert cidata._get_partitions("/dev/sda") == []

    def test_find_last_partition(self, monkeypatch):
        self._patch_lsblk(
            monkeypatch,
            [
                {
                    "path": "/dev/sda",
                    "type": "disk",
                    "children": [
                        {"path": "/dev/sda1", "type": "part"},
                        {"path": "/dev/sda2", "type": "part"},
                    ],
                }
            ],
        )
        assert cidata._find_last_partition("/dev/sda") == "/dev/sda2"

    def test_find_last_partition_none(self, monkeypatch):
        self._patch_lsblk(monkeypatch, [{"path": "/dev/sda", "type": "disk"}])
        assert cidata._find_last_partition("/dev/sda") is None


class TestGetDiskSizeAndLastEnd:
    """Tests for _get_disk_size_and_last_end(disk) - parted print parsing."""

    def _patch_parteds(self, monkeypatch, stdout: str):
        monkeypatch.setattr(
            cidata.subprocess,
            "run",
            lambda *args, **kwargs: SimpleNamespace(stdout=stdout, returncode=0),
        )

    def test_multiple_partitions(self, monkeypatch):
        stdout = (
            "Disk /dev/sda: 16000 MB\n"
            "Sector size (logical/physical): 512B/512B\n"
            "Partition Table: gpt\n"
            "Number  Start   End     Size    File system  Name   Flags\n"
            " 1      1.00MB  513MB   512MB   fat32        boot   boot, esp\n"
            " 2      513MB   8192MB  7679MB  ext4         root\n"
        )
        self._patch_parteds(monkeypatch, stdout)

        assert cidata._get_disk_size_and_last_end("/dev/sda") == (16000.0, 8192.0)

    def test_no_partitions(self, monkeypatch):
        stdout = "Disk /dev/sda: 16000 MB\nPartition Table: gpt\nNumber  Start  End  Size  File system  Name  Flags\n"
        self._patch_parteds(monkeypatch, stdout)

        disk_size, last_end = cidata._get_disk_size_and_last_end("/dev/sda")
        assert disk_size == 16000.0
        assert last_end is None

    def test_missing_disk_size_line(self, monkeypatch):
        stdout = (
            "Partition Table: gpt\n"
            "Number  Start   End     Size    File system  Name  Flags\n"
            " 1      1.00MB  513MB   512MB   fat32        boot\n"
        )
        self._patch_parteds(monkeypatch, stdout)

        disk_size, last_end = cidata._get_disk_size_and_last_end("/dev/sda")
        assert disk_size is None
        assert last_end == 513.0

    def test_malformed_numbers_do_not_crash(self, monkeypatch):
        stdout = "Disk /dev/sda: not-a-number MB\n 1      1.00MB  MB      MB      fat32        boot\n"
        self._patch_parteds(monkeypatch, stdout)

        disk_size, last_end = cidata._get_disk_size_and_last_end("/dev/sda")
        assert disk_size is None
        assert last_end is None


class TestGetLastPartitionEnd:
    """Tests for _get_last_partition_end(disk)."""

    def test_returns_formatted_end(self, monkeypatch):
        monkeypatch.setattr(cidata, "_get_disk_size_and_last_end", lambda disk: (16000.0, 8192.0))
        assert cidata._get_last_partition_end("/dev/sda") == "8192MB"

    def test_returns_float_formatted_end(self, monkeypatch):
        monkeypatch.setattr(cidata, "_get_disk_size_and_last_end", lambda disk: (16000.0, 513.5))
        assert cidata._get_last_partition_end("/dev/sda") == "513.5MB"

    def test_raises_when_no_partition_end(self, monkeypatch):
        import pytest

        monkeypatch.setattr(cidata, "_get_disk_size_and_last_end", lambda disk: (16000.0, None))
        with pytest.raises(SystemExit, match="Could not determine the end of the last partition"):
            cidata._get_last_partition_end("/dev/sda")


class TestFindCidataPartition:
    """Tests for _find_cidata_partition(disk)."""

    def test_find_by_lsblk_label(self, monkeypatch):
        monkeypatch.setattr(
            cidata,
            "_lsblk_devices",
            lambda *args: [{"path": "/dev/sda4", "label": "cidata", "partlabel": ""}],
        )
        assert cidata._find_cidata_partition("/dev/sda") == "/dev/sda4"

    def test_trailing_partition_heuristic_ignores_ext4(self, monkeypatch):
        monkeypatch.setattr(cidata, "_lsblk_devices", lambda *args: [])
        monkeypatch.setattr(
            cidata, "_get_partitions", lambda disk: ["/dev/sda1", "/dev/sda2", "/dev/sda3", "/dev/sda4"]
        )
        monkeypatch.setattr(cidata, "_get_disk_size_and_last_end", lambda disk: (16000.0, 15995.0))

        def _fake_run(cmd, *args, **kwargs):
            if cmd[0] == "blkid":
                return SimpleNamespace(stdout="TYPE=ext4\nLABEL=rootfs\n")
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(cidata.subprocess, "run", _fake_run)
        assert cidata._find_cidata_partition("/dev/sda") is None

    def test_trailing_partition_heuristic_accepts_vfat(self, monkeypatch):
        monkeypatch.setattr(cidata, "_lsblk_devices", lambda *args: [])
        monkeypatch.setattr(
            cidata, "_get_partitions", lambda disk: ["/dev/sda1", "/dev/sda2", "/dev/sda3", "/dev/sda4"]
        )
        monkeypatch.setattr(cidata, "_get_disk_size_and_last_end", lambda disk: (16000.0, 15995.0))

        def _fake_run(cmd, *args, **kwargs):
            if cmd[0] == "blkid":
                return SimpleNamespace(stdout="TYPE=vfat\n")
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(cidata.subprocess, "run", _fake_run)
        assert cidata._find_cidata_partition("/dev/sda") == "/dev/sda4"
