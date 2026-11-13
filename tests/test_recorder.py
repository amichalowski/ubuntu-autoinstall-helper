"""Unit tests for autoinstall_helper.recorder."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from autoinstall_helper import fsprobe, recorder


class TestHelpers:
    """Tests for the module-level helpers."""

    def test_env_diff_ignores_inherited_environment(self):
        assert recorder._env_diff(None) is None
        assert recorder._env_diff(dict(os.environ)) is None

    def test_env_diff_redacts_secrets(self):
        env = dict(os.environ, MY_API_TOKEN="super-secret", PLAIN_VALUE="visible")

        diff = recorder._env_diff(env)

        assert diff == {"MY_API_TOKEN": "<redacted>", "PLAIN_VALUE": "visible"}

    def test_describe_error_for_called_process_error(self):
        exc = subprocess.CalledProcessError(3, ["false"])

        info = recorder._describe_error(exc)

        assert info["type"] == "CalledProcessError"
        assert info["returncode"] == 3

    def test_os_release_returns_a_mapping(self):
        assert isinstance(recorder._os_release(), dict)


class TestRecordingSession:
    """Tests for recording_session() and the Recorder lifecycle."""

    def test_records_subprocess_and_filesystem(self, tmp_path: Path):
        output = tmp_path / "session.recording.json"
        target = tmp_path / "image.img"
        target.write_bytes(b"\x00" * 10)

        with recorder.recording_session(output) as rec:
            result = subprocess.run(
                [sys.executable, "-c", "print('hello')"],
                capture_output=True,
                text=True,
                check=True,
            )
            info = fsprobe.probe(target)
            rec.exit_code = 0

        assert result.stdout == "hello\n"
        assert info.size == 10

        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["metadata"]["schema_version"] == recorder.SCHEMA_VERSION
        assert data["metadata"]["exit_code"] == 0
        assert data["metadata"]["argv"] == sys.argv
        assert "os_release" in data["metadata"]

        subprocess_entry, fs_entry = data["interactions"]
        assert subprocess_entry["type"] == "subprocess"
        assert subprocess_entry["command"][0] == sys.executable
        assert subprocess_entry["result"]["stdout"] == "hello\n"
        assert subprocess_entry["error"] is None
        assert fs_entry["type"] == "fs"
        assert fs_entry["path"] == str(target)
        assert fs_entry["result"]["size"] == 10

    def test_records_uncaptured_streams_as_null(self, tmp_path: Path):
        output = tmp_path / "session.recording.json"

        with recorder.recording_session(output):
            subprocess.run([sys.executable, "-c", "pass"], check=True)

        entry = json.loads(output.read_text(encoding="utf-8"))["interactions"][0]
        assert entry["result"]["stdout"] is None
        assert entry["result"]["stderr"] is None

    def test_records_raised_errors(self, tmp_path: Path):
        output = tmp_path / "session.recording.json"

        with pytest.raises(subprocess.CalledProcessError), recorder.recording_session(output):
            subprocess.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=True)

        entry = json.loads(output.read_text(encoding="utf-8"))["interactions"][0]
        assert entry["result"] is None
        assert entry["error"]["type"] == "CalledProcessError"
        assert entry["error"]["returncode"] == 3

    def test_patches_are_restored_after_the_session(self, tmp_path: Path):
        original_run = subprocess.run
        original_urlopen = urllib.request.urlopen
        original_probe = fsprobe.probe

        with recorder.recording_session(tmp_path / "session.recording.json"):
            assert subprocess.run is not original_run
            assert urllib.request.urlopen is not original_urlopen
            assert fsprobe.probe is not original_probe

        assert subprocess.run is original_run
        assert urllib.request.urlopen is original_urlopen
        assert fsprobe.probe is original_probe
