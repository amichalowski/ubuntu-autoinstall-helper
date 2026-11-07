"""Smoke tests for the packaged CLI contract (no Docker required).

These execute the built .pyz but exercise argument parsing, error handling and
the exit-code propagation of the zipapp entry point. None of them reaches
Docker, so they are fast; they still carry the smoke marker because they run
against the release artifact.
"""

import os

import pytest

pytestmark = pytest.mark.smoke

# These commands never touch Docker, so a short timeout is enough.
FAST_TIMEOUT_S = 60


def test_help_exits_zero(run_artifact):
    proc = run_artifact("--help", timeout=FAST_TIMEOUT_S)

    assert proc.returncode == 0, proc.stderr
    assert "validate" in proc.stdout
    assert "flash" in proc.stdout


def test_no_subcommand_prints_help(run_artifact):
    proc = run_artifact(timeout=FAST_TIMEOUT_S)

    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout


def test_unknown_command_exits_with_usage_error(run_artifact):
    proc = run_artifact("definitely-not-a-command", timeout=FAST_TIMEOUT_S)

    assert proc.returncode == 2  # argparse usage error
    assert "invalid choice" in proc.stderr


def test_missing_config_file_fails_with_clear_error(run_artifact, tmp_path):
    missing = tmp_path / "does-not-exist.yaml"
    proc = run_artifact("validate", str(missing), timeout=FAST_TIMEOUT_S)

    assert proc.returncode != 0
    assert "ERROR: File does not exist" in proc.stdout + proc.stderr


def test_non_docker_command_does_not_extract_assets(run_artifact, tmp_path):
    """Non-Docker commands must not unpack the bundled docker/ assets into $TMPDIR."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env = {**os.environ, "TMPDIR": str(tmpdir)}

    proc = run_artifact(
        "flash",
        str(tmp_path / "missing.iso"),
        str(tmp_path / "target.img"),
        env=env,
        timeout=FAST_TIMEOUT_S,
    )

    assert proc.returncode != 0
    assert "ISO file does not exist" in proc.stdout + proc.stderr
    assert list(tmpdir.iterdir()) == [], "docker/ assets were extracted by a non-Docker command"
