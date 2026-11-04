"""Unit tests for pure helpers in autoinstall_helper.cidata."""

from types import SimpleNamespace

from autoinstall_helper import cidata
from autoinstall_helper.cidata import _parse_pairs


class TestParsePairs:
    """Tests for _parse_pairs(line) - lsblk / udev format parsing."""

    def test_typical_lsblk_output(self):
        line = 'PATH="/dev/sda" LABEL="cidata" PARTLABEL=""'
        expected = {
            "PATH": "/dev/sda",
            "LABEL": "cidata",
            "PARTLABEL": "",
        }
        assert _parse_pairs(line) == expected

    def test_empty_line(self):
        assert _parse_pairs("") == {}

    def test_line_without_quotes(self):
        # Format KEY="VALUE" expects double quotes
        assert _parse_pairs("KEY=VALUE ANOTHER=123") == {}

    def test_whitespace_and_special_chars(self):
        line = 'NAME="loop0p1" FSTYPE="vfat" MOUNTPOINT="/media/my mount"'
        result = _parse_pairs(line)
        assert result["NAME"] == "loop0p1"
        assert result["FSTYPE"] == "vfat"
        assert result["MOUNTPOINT"] == "/media/my mount"


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
