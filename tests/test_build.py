"""Unit tests for zipapp building, docker digest caching, and asset extraction."""

import subprocess
import sys
import zipfile
from pathlib import Path

from autoinstall_helper.build import build_zipapp, compute_docker_digest, exclude_pycache
from autoinstall_helper.docker_utils import _get_docker_dir, docker_image_tag, get_docker_digest


class TestBuildZipapp:
    """Tests for build_zipapp and compute_docker_digest."""

    def test_exclude_pycache(self):
        assert exclude_pycache(Path("src/autoinstall_helper/cli.py")) is True
        assert exclude_pycache(Path("src/autoinstall_helper/__pycache__/cli.cpython-312.pyc")) is False
        assert exclude_pycache(Path("something/test.pyc")) is False

    def test_compute_docker_digest(self):
        docker_dir = Path("src/autoinstall_helper/docker")
        digest = compute_docker_digest(docker_dir)
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)

    def test_build_zipapp_creates_valid_executable_archive(self, tmp_path: Path):
        target_pyz = tmp_path / "test-app.pyz"
        result_path = build_zipapp(target=target_pyz)

        assert result_path == target_pyz
        assert target_pyz.is_file()
        assert zipfile.is_zipfile(target_pyz)

        with zipfile.ZipFile(target_pyz) as zf:
            names = zf.namelist()
            assert "__main__.py" in names
            assert "autoinstall_helper/cli.py" in names
            assert "autoinstall_helper/DOCKER_DIGEST" in names
            assert "autoinstall_helper/docker/Dockerfile" in names
            assert not any("__pycache__" in name for name in names)

            # Digest stored outside docker/ matches the computed digest
            stored = zf.read("autoinstall_helper/DOCKER_DIGEST").decode("utf-8").strip()
            expected = compute_docker_digest(Path("src/autoinstall_helper/docker"))
            assert stored == expected

        # Execute the built pyz archive
        proc = subprocess.run(
            [sys.executable, str(target_pyz), "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "autoinstall-helper" in proc.stdout


class TestDockerDigestAndTag:
    """Tests for get_docker_digest and docker_image_tag."""

    def test_get_docker_digest_dev_mode(self):
        digest = get_docker_digest()
        assert len(digest) == 12
        expected = compute_docker_digest(Path("src/autoinstall_helper/docker"))
        assert digest == expected

    def test_docker_image_tag_with_default_digest(self):
        tag = docker_image_tag("24.04")
        expected_digest = get_docker_digest()
        assert tag == f"ubuntu-desktop-bootstrap:24.04-{expected_digest}"

    def test_docker_image_tag_with_explicit_string(self):
        tag = docker_image_tag("24.04", "123456abcdef")
        assert tag == "ubuntu-desktop-bootstrap:24.04-123456abcdef"


class TestGetDockerDir:
    """Tests for _get_docker_dir in development mode."""

    def test_dev_mode_returns_existing_dir(self):
        docker_dir = _get_docker_dir()
        assert docker_dir.is_dir()
        assert (docker_dir / "Dockerfile").is_file()
        assert (docker_dir / "timedatectl.sh").is_file()
        assert (docker_dir / "validate-autoinstall.py").is_file()
