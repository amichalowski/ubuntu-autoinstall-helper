"""Smoke tests: the built .pyz artifact must validate autoinstall configs.

These tests exercise the real end-to-end path of the release artifact: they
execute the packaged .pyz, which builds the validator Docker image (Snap Store +
GitHub + `docker build`) and runs the validation container. The image-management
checks use that same real image — re-tagged to simulate an outdated digest — and
only the preflight check inspects the host without building. The final test
prunes every validator image, so the run leaves no cached build behind. They
require Docker and network access and are excluded from the default test run;
execute them explicitly:

    uv run build-pyz -o dist/autoinstall-helper.pyz
    uv run pytest -m smoke -v
"""

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parents[2]
WRONG_CONFIG = REPO_ROOT / "wrong-autoinstall" / "autoinstall-wrong-storage.yaml"
BROKEN_COMMANDS_CONFIG = REPO_ROOT / "wrong-autoinstall" / "autoinstall-broken-commands.yaml"
VALIDATOR_REPO = "ubuntu-desktop-bootstrap"

# Commands that do not build or run a container finish quickly.
FAST_TIMEOUT_S = 60

# Digest that never matches the bundled assets, i.e. an image built before they changed.
OUTDATED_DIGEST = "000000000000"


def _output(proc) -> str:
    """Combine stdout and stderr so assertions do not depend on the stream."""
    return proc.stdout + proc.stderr


def _list_validator_images(run_artifact) -> tuple[str, dict[str, str]]:
    """Run `validate images` and map each listed 'repo:tag' to its report line."""
    output = _output(run_artifact("validate", "images", timeout=FAST_TIMEOUT_S))
    rows = {}
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) > 1 and tokens[0] == "[autoinstall-helper]" and tokens[1].startswith(f"{VALIDATOR_REPO}:"):
            rows[tokens[1]] = line
    return output, rows


def _bundled_assets_digest(output: str) -> str:
    """Extract the digest the packaged tool reports for its bundled docker/ assets."""
    match = re.search(r"bundled assets digest: ([0-9a-f]{12})", output)
    assert match, output
    return match.group(1)


def _ensure_validator_image(run_artifact) -> str:
    """Build (or reuse) the real validator image and return its digest-tagged tag."""
    proc = run_artifact("validate", str(WRONG_CONFIG), "--validation", "storage")
    output = _output(proc)
    assert "Building image" in output or "already exists" in output, output

    _, rows = _list_validator_images(run_artifact)
    assert rows, output
    return next(tag for tag, line in rows.items() if line.endswith("current"))


def _remove_images(*tags: str) -> None:
    """Best-effort cleanup for tags created by the test (never removes a real build)."""
    for tag in tags:
        subprocess.run(["docker", "image", "rm", "-f", tag], capture_output=True, check=False)


def test_wrong_autoinstall_config_fails_validation(run_artifact):
    """The artifact must reject the erroneous Gemini-generated storage config."""
    proc = run_artifact("validate", str(WRONG_CONFIG))

    output = _output(proc)
    assert proc.returncode != 0, f"Validation unexpectedly succeeded for {WRONG_CONFIG}:\n{output}"
    # The CLI prints this only after the container ran and rejected the config,
    # which distinguishes a real validation failure from infrastructure errors
    # (Snap Store / Docker Hub outages, Docker build failures, ...).
    assert "Validation failed" in output, f"Unexpected failure (not a validation error):\n{output}"


def test_storage_scope_rejects_wrong_config(run_artifact):
    """--validation storage must run the storage layer and reject the bad config."""
    proc = run_artifact("validate", str(WRONG_CONFIG), "--validation", "storage")

    output = _output(proc)
    assert proc.returncode != 0, f"Storage validation unexpectedly succeeded:\n{output}"
    assert "[storage] Running Curtin schema validation" in output, output
    assert "Validation failed" in output, output


def test_autoinstall_scope_runs_subiquity_only(run_artifact):
    """--validation autoinstall must skip the storage/Curtin layer.

    The known-bad config only fails the storage layer (Subiquity's dry-run
    ignores the invalid ``wipe`` value), so this asserts scope isolation rather
    than the validation verdict.
    """
    proc = run_artifact("validate", str(WRONG_CONFIG), "--validation", "autoinstall")

    output = _output(proc)
    assert "[autoinstall] Running Subiquity dry-run validation" in output, output
    assert "[storage] Running Curtin schema validation" not in output, output


def test_commands_scope_rejects_broken_commands(run_artifact):
    """--validation commands must catch shell syntax errors the other layers ignore."""
    proc = run_artifact("validate", str(BROKEN_COMMANDS_CONFIG), "--validation", "commands")

    output = _output(proc)
    assert proc.returncode != 0, output
    assert "[commands] Checking" in output, output
    assert "sh -n found a syntax error" in output, output
    # The payload inside `bash -c '...'` is a quoted word to the outer parse.
    assert "bash -c (depth 1)" in output, output
    # An unterminated here-document only shellcheck reports (sh -n just warns).
    assert "shellcheck (error) reported" in output, output
    assert "[commands] Shell command validation failed." in output, output


def test_commands_scope_accepts_valid_commands(run_artifact, tmp_path):
    """A valid command list must pass the commands scope."""
    config = tmp_path / "valid-commands.yaml"
    config.write_text(
        "#cloud-config\n"
        "autoinstall:\n"
        "  version: 1\n"
        "  refresh-installer:\n"
        "    update: true\n"
        '    channel: "24.04/stable"\n'
        "  late-commands:\n"
        "    - curtin in-target -- true\n"
        "    - bash -c 'echo hi'\n",
        encoding="utf-8",
    )

    proc = run_artifact("validate", str(config), "--validation", "commands")

    output = _output(proc)
    assert proc.returncode == 0, output
    assert "[commands] Shell command syntax is valid." in output, output


def test_validate_reuses_cached_docker_image(run_artifact):
    """A second run must reuse the digest-tagged image instead of rebuilding it."""
    run_artifact("validate", str(WRONG_CONFIG), "--validation", "storage")
    proc = run_artifact("validate", str(WRONG_CONFIG), "--validation", "storage")

    output = _output(proc)
    assert "already exists" in output, f"Expected a cached image, got:\n{output}"


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


def test_validate_doctor_reports_the_docker_status(run_artifact):
    """`validate doctor` must report a status and exit consistently with it.

    The verdict depends on the host (Docker group, root, daemon state), so the
    assertion ties the exit code to the reported verdict instead of hardcoding it.
    """
    proc = run_artifact("validate", "doctor", timeout=FAST_TIMEOUT_S)

    output = _output(proc)
    assert "Docker preflight check" in output, output
    assert "without sudo" in output, output
    assert (proc.returncode == 0) == ("Docker support is ready." in output), output


def test_validate_images_lists_the_built_validator_image(run_artifact):
    """A real image build (Dockerfile + network) must be listed as 'current'."""
    current_tag = _ensure_validator_image(run_artifact)
    digest = current_tag.rsplit("-", 1)[1]

    output, rows = _list_validator_images(run_artifact)

    assert _bundled_assets_digest(output) == digest, output
    assert current_tag in rows, output
    assert rows[current_tag].endswith("current"), output


def test_validate_images_prune_removes_stale_images_only(run_artifact):
    """`--prune -y` must drop outdated images and keep the freshly built one.

    The stale image is the real validator image built by `validate`, re-tagged
    with an outdated digest (what an old build looks like). Any other stale
    validator image on the host is pruned as well — that is what the command
    does — so the assertions only cover the tags created here.
    """
    current_tag = _ensure_validator_image(run_artifact)
    version = current_tag.rsplit("-", 1)[0]
    outdated_tag = f"{version}-{OUTDATED_DIGEST}"
    subprocess.run(["docker", "image", "tag", current_tag, outdated_tag], check=True)

    try:
        proc = run_artifact("validate", "images", "--prune", "-y", timeout=FAST_TIMEOUT_S)

        output = _output(proc)
        assert proc.returncode == 0, output
        assert "Removing" in output, output
        assert outdated_tag in output, output

        remaining, rows = _list_validator_images(run_artifact)
        assert outdated_tag not in rows, remaining
        assert current_tag in rows, remaining
    finally:
        _remove_images(outdated_tag)


def test_validate_images_prune_all_removes_every_image(run_artifact):
    """`--prune --all -y` must wipe every validator image, including the current one.

    Runs last in this module on purpose: it reclaims the disk space used by the
    build (as the release job would want) and leaves the host without a cached
    image, so the next validation run has to build it again.
    """
    _ensure_validator_image(run_artifact)

    proc = run_artifact("validate", "images", "--prune", "--all", "-y", timeout=FAST_TIMEOUT_S)

    output = _output(proc)
    assert proc.returncode == 0, output
    assert "Removing" in output, output
    assert f"{VALIDATOR_REPO}:" in output, output

    remaining, rows = _list_validator_images(run_artifact)
    assert rows == {}, remaining
    assert "none —" in remaining, remaining
