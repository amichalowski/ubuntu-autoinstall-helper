"""Smoke test: the built .pyz artifact must reject the known-bad autoinstall config.

These tests exercise the real end-to-end path of the release artifact: they
execute the packaged .pyz, which builds the validator Docker image and runs
the validation container. They require Docker and network access and are
excluded from the default test run; execute them explicitly:

    uv run build-pyz -o dist/autoinstall-helper.pyz
    uv run pytest -m smoke -v
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parents[2]
WRONG_CONFIG = REPO_ROOT / "wrong-autoinstall" / "autoinstall-wrong-storage.yaml"
ARTIFACT_ENV = "SMOKE_ARTIFACT"
TIMEOUT_S = 900


def _find_artifact() -> Path:
    """Return the .pyz under test: $SMOKE_ARTIFACT or the single dist/*.pyz."""
    override = os.environ.get(ARTIFACT_ENV)
    if override:
        return Path(override)

    candidates = sorted((REPO_ROOT / "dist").glob("*.pyz"))
    if len(candidates) != 1:
        pytest.fail(
            f"Expected exactly one .pyz in {REPO_ROOT / 'dist'}, found: {candidates}. "
            f"Build it with 'uv run build-pyz -o dist/autoinstall-helper.pyz' or set ${ARTIFACT_ENV}."
        )
    return candidates[0]


def test_wrong_autoinstall_config_fails_validation():
    """The artifact must reject the erroneous Gemini-generated storage config."""
    artifact = _find_artifact()
    proc = subprocess.run(
        [sys.executable, str(artifact), "validate", str(WRONG_CONFIG)],
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_S,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"Validation unexpectedly succeeded for {WRONG_CONFIG}:\n{output}"
    # The CLI prints this only after the container ran and rejected the config,
    # which distinguishes a real validation failure from infrastructure errors
    # (Snap Store / Docker Hub outages, Docker build failures, ...).
    assert "Validation failed" in output, f"Unexpected failure (not a validation error):\n{output}"
