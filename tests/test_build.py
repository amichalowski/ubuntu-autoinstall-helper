"""Unit tests for zipapp building and docker asset extraction."""

import subprocess
import sys
import zipfile
from pathlib import Path

from autoinstall_helper.build import build_zipapp, exclude_pycache
from autoinstall_helper.cli import _get_docker_dir


class TestBuildZipapp:
    """Tests for build_zipapp."""

    def test_exclude_pycache(self):
        assert exclude_pycache(Path("src/autoinstall_helper/cli.py")) is True
        assert exclude_pycache(Path("src/autoinstall_helper/__pycache__/cli.cpython-312.pyc")) is False
        assert exclude_pycache(Path("something/test.pyc")) is False

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
            assert "autoinstall_helper/docker/Dockerfile" in names
            assert not any("__pycache__" in name for name in names)

        # Execute the built pyz archive
        proc = subprocess.run(
            [sys.executable, str(target_pyz), "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "autoinstall-helper" in proc.stdout


class TestGetDockerDir:
    """Tests for _get_docker_dir in development mode."""

    def test_dev_mode_returns_existing_dir(self):
        docker_dir = _get_docker_dir()
        assert docker_dir.is_dir()
        assert (docker_dir / "Dockerfile").is_file()
        assert (docker_dir / "timedatectl.sh").is_file()
        assert (docker_dir / "validate-autoinstall.py").is_file()
