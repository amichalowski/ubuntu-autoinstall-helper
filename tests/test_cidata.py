"""Unit tests for pure helpers in autoinstall_helper.cidata."""

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
