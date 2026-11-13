"""Unit tests for autoinstall_helper.replayer."""

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from autoinstall_helper.fsprobe import PathInfo
from autoinstall_helper.replayer import Replayer, ReplayError


def _write_recording(path: Path, interactions: list[dict]) -> Path:
    data = {
        "metadata": {"schema_version": 1, "os_release": {"VERSION_ID": "24.04"}},
        "interactions": interactions,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _subprocess_entry(seq: int, command: list[str], *, returncode: int = 0, stdout=None, stderr=None, error=None):
    return {
        "seq": seq,
        "type": "subprocess",
        "command": command,
        "result": None if error else {"returncode": returncode, "stdout": stdout, "stderr": stderr},
        "error": error,
    }


class TestSubprocessReplay:
    """Tests for next_subprocess()."""

    def test_returns_text_when_requested(self, tmp_path: Path):
        path = _write_recording(
            tmp_path / "r.json",
            [_subprocess_entry(0, ["echo", "hi"], stdout="hi\n", stderr="")],
        )
        replayer = Replayer(path)

        result = replayer.next_subprocess(["echo", "hi"], capture_output=True, text=True)

        assert result.returncode == 0
        assert result.stdout == "hi\n"
        assert result.stderr == ""
        replayer.assert_exhausted()

    def test_reencodes_bytes_when_text_not_requested(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [_subprocess_entry(0, ["echo"], stdout="hi\n")])
        replayer = Replayer(path)

        result = replayer.next_subprocess(["echo"], capture_output=True)

        assert result.stdout == b"hi\n"

    def test_check_true_raises_on_nonzero_returncode(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [_subprocess_entry(0, ["false"], returncode=1)])
        replayer = Replayer(path)

        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            replayer.next_subprocess(["false"], check=True)

        assert excinfo.value.returncode == 1

    def test_recorded_error_is_reraised(self, tmp_path: Path):
        path = _write_recording(
            tmp_path / "r.json",
            [
                _subprocess_entry(
                    0,
                    ["missing"],
                    error={"type": "FileNotFoundError", "message": "No such file"},
                )
            ],
        )
        replayer = Replayer(path)

        with pytest.raises(FileNotFoundError, match="No such file"):
            replayer.next_subprocess(["missing"])

    def test_ordered_fallback_warns_on_mismatch(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [_subprocess_entry(0, ["other"], stdout="x")])
        replayer = Replayer(path)

        with pytest.warns(UserWarning, match="Replay mismatch"):
            result = replayer.next_subprocess(["expected"], text=True)

        assert result.stdout == "x"

    def test_missing_interaction_raises(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [])
        replayer = Replayer(path)

        with pytest.raises(ReplayError, match="No recorded subprocess"):
            replayer.next_subprocess(["anything"])


class TestUrlopenReplay:
    """Tests for next_urlopen()."""

    def test_returns_fake_response(self, tmp_path: Path):
        path = _write_recording(
            tmp_path / "r.json",
            [
                {
                    "seq": 0,
                    "type": "urlopen",
                    "url": "https://example.com",
                    "result": {"status": 200, "body": "body", "response_headers": {"X": "1"}},
                    "error": None,
                }
            ],
        )
        replayer = Replayer(path)

        response = replayer.next_urlopen("https://example.com")

        assert response.status == 200
        assert response.read() == b"body"
        assert response.headers == {"X": "1"}

    def test_recorded_http_error_is_reraised(self, tmp_path: Path):
        path = _write_recording(
            tmp_path / "r.json",
            [
                {
                    "seq": 0,
                    "type": "urlopen",
                    "url": "https://example.com/missing",
                    "result": None,
                    "error": {"type": "HTTPError", "message": "Not Found", "code": 404, "headers": {}},
                }
            ],
        )
        replayer = Replayer(path)

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            replayer.next_urlopen("https://example.com/missing")

        assert excinfo.value.code == 404


class TestProbeReplay:
    """Tests for next_probe()."""

    def test_returns_recorded_metadata(self, tmp_path: Path):
        path = _write_recording(
            tmp_path / "r.json",
            [
                {
                    "seq": 0,
                    "type": "fs",
                    "path": "/data/image.iso",
                    "result": {
                        "exists": True,
                        "is_file": True,
                        "is_dir": False,
                        "is_block": False,
                        "size": 42,
                    },
                }
            ],
        )
        replayer = Replayer(path)

        assert replayer.next_probe(Path("/data/image.iso")) == PathInfo(True, True, False, False, 42)


class TestAssertExhausted:
    """Tests for assert_exhausted()."""

    def test_reports_unreplayed_interactions(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [_subprocess_entry(0, ["echo"])])
        replayer = Replayer(path)

        with pytest.raises(ReplayError, match="1 recorded interactions were not replayed"):
            replayer.assert_exhausted()

    def test_metadata_is_exposed(self, tmp_path: Path):
        path = _write_recording(tmp_path / "r.json", [])
        replayer = Replayer(path)

        assert replayer.metadata["os_release"]["VERSION_ID"] == "24.04"
