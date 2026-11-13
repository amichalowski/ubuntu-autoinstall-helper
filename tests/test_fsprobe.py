"""Unit tests for autoinstall_helper.fsprobe."""

from pathlib import Path

from autoinstall_helper import fsprobe


class TestProbe:
    """Tests for probe(path) - metadata only, never contents."""

    def test_regular_file(self, tmp_path: Path):
        path = tmp_path / "image.iso"
        path.write_bytes(b"\x00" * 1234)

        info = fsprobe.probe(path)

        assert info == fsprobe.PathInfo(exists=True, is_file=True, is_dir=False, is_block=False, size=1234)

    def test_directory(self, tmp_path: Path):
        info = fsprobe.probe(tmp_path)

        assert info.exists is True
        assert info.is_dir is True
        assert info.is_file is False
        assert info.is_block is False

    def test_missing_path(self, tmp_path: Path):
        info = fsprobe.probe(tmp_path / "missing")

        assert info == fsprobe.PathInfo(exists=False, is_file=False, is_dir=False, is_block=False, size=0)
