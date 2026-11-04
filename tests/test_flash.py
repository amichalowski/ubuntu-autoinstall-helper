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


class TestApplyNocow:
    """Tests for _apply_nocow(target)."""

    def _record_run(self, monkeypatch):
        calls: list[tuple] = []

        def _fake_run(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(flash.subprocess, "run", _fake_run)
        return calls

    def test_non_btrfs_is_noop(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "image.img"
        monkeypatch.setattr(flash, "_is_btrfs", lambda path: False)
        calls = self._record_run(monkeypatch)

        flash._apply_nocow(target)

        assert calls == []
        assert not target.exists()

    def test_new_file_on_btrfs_touched_and_tagged(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "image.img"
        monkeypatch.setattr(flash, "_is_btrfs", lambda path: True)
        calls = self._record_run(monkeypatch)

        flash._apply_nocow(target)

        assert target.exists()
        assert target.stat().st_size == 0
        assert calls == [(["chattr", "+C", str(target)],)]

    def test_empty_existing_file_on_btrfs_tagged(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "image.img"
        target.touch()
        monkeypatch.setattr(flash, "_is_btrfs", lambda path: True)
        calls = self._record_run(monkeypatch)

        flash._apply_nocow(target)

        assert calls == [(["chattr", "+C", str(target)],)]

    def test_non_empty_file_on_btrfs_untouched(self, monkeypatch, tmp_path: Path):
        target = tmp_path / "image.img"
        target.write_bytes(b"\x00" * 16)
        monkeypatch.setattr(flash, "_is_btrfs", lambda path: True)
        calls = self._record_run(monkeypatch)

        flash._apply_nocow(target)

        assert calls == []
