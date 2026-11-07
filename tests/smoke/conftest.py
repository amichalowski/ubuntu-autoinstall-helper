"""Shared fixtures for the end-to-end smoke tests.

The smoke tests execute the built .pyz artifact (see tests/smoke/). They require
Docker and network access and are excluded from the default test run; execute
them explicitly:

    uv run build-pyz -o dist/autoinstall-helper.pyz
    uv run pytest -m smoke -v
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ENV = "SMOKE_ARTIFACT"

# A cold Docker build plus the validation container can take several minutes.
DOCKER_TIMEOUT_S = 900


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


@pytest.fixture(scope="session")
def artifact() -> Path:
    """Path to the built .pyz artifact under test."""
    return _find_artifact()


@pytest.fixture(scope="session")
def run_artifact(artifact):
    """Return a callable that runs the .pyz with the given CLI arguments.

    ``env`` and ``timeout`` can be overridden per call; by default the process
    environment is inherited and the timeout allows for a cold Docker build.
    """

    def _run(
        *args: str,
        env: dict[str, str] | None = None,
        timeout: int = DOCKER_TIMEOUT_S,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(artifact), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=env,
        )

    return _run
