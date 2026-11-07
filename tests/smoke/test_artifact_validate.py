"""Smoke tests: the built .pyz artifact must validate autoinstall configs.

These tests exercise the real end-to-end path of the release artifact: they
execute the packaged .pyz, which builds the validator Docker image and runs the
validation container. They require Docker and network access and are excluded
from the default test run; execute them explicitly:

    uv run build-pyz -o dist/autoinstall-helper.pyz
    uv run pytest -m smoke -v
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parents[2]
WRONG_CONFIG = REPO_ROOT / "wrong-autoinstall" / "autoinstall-wrong-storage.yaml"


def _output(proc) -> str:
    """Combine stdout and stderr so assertions do not depend on the stream."""
    return proc.stdout + proc.stderr


def test_wrong_autoinstall_config_fails_validation(run_artifact):
    """The artifact must reject the erroneous Gemini-generated storage config."""
    proc = run_artifact("validate", str(WRONG_CONFIG))

    output = _output(proc)
    assert proc.returncode != 0, f"Validation unexpectedly succeeded for {WRONG_CONFIG}:\n{output}"
    # The CLI prints this only after the container ran and rejected the config,
    # which distinguishes a real validation failure from infrastructure errors
    # (Snap Store / Docker Hub outages, Docker build failures, ...).
    assert "Validation failed" in output, f"Unexpected failure (not a validation error):\n{output}"


def test_config_without_cloud_config_header_is_rejected(run_artifact, tmp_path):
    """A config without the '#cloud-config' header must be rejected on the host.

    The CLI requires the cloud-config header, so this fails fast with a clear
    message before any Docker image is built or container is started.
    """
    headerless = tmp_path / "no-header.yaml"
    body = WRONG_CONFIG.read_text(encoding="utf-8").splitlines()[1:]
    headerless.write_text("\n".join(body) + "\n", encoding="utf-8")

    proc = run_artifact("validate", str(headerless))

    output = _output(proc)
    assert proc.returncode != 0, output
    assert "#cloud-config" in output, output
    assert "in container" not in output, f"Config should be rejected before Docker:\n{output}"
