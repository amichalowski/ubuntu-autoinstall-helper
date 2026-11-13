"""Unit tests for zipapp building, docker digest caching, and asset extraction."""

import subprocess
import sys
import zipfile
from pathlib import Path

from autoinstall_helper.build import (
    archive_filter,
    build_zipapp,
    compute_docker_digest,
    exclude_dev_only,
    exclude_pycache,
)
from autoinstall_helper.docker_utils import docker_image_tag, get_docker_digest, get_docker_dir


class TestBuildZipapp:
    """Tests for build_zipapp and compute_docker_digest."""

    def test_exclude_pycache(self):
        assert exclude_pycache(Path("src/autoinstall_helper/cli.py")) is True
        assert exclude_pycache(Path("src/autoinstall_helper/__pycache__/cli.cpython-312.pyc")) is False
        assert exclude_pycache(Path("something/test.pyc")) is False

    def test_exclude_dev_only(self):
        assert exclude_dev_only(Path("src/autoinstall_helper/build.py")) is False
        assert exclude_dev_only(Path("src/autoinstall_helper/recorder.py")) is False
        assert exclude_dev_only(Path("src/autoinstall_helper/replayer.py")) is False
        assert exclude_dev_only(Path("src/autoinstall_helper/interactions.py")) is False
        assert exclude_dev_only(Path("src/autoinstall_helper/cli.py")) is True
        assert exclude_dev_only(Path("src/autoinstall_helper/build.pyc")) is True

    def test_archive_filter_combines_rules(self):
        assert archive_filter(Path("src/autoinstall_helper/cli.py")) is True
        assert archive_filter(Path("src/autoinstall_helper/build.py")) is False
        assert archive_filter(Path("src/autoinstall_helper/__pycache__/cli.pyc")) is False

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
            # Dev-only modules are stripped from the release artifact
            for module in ("build.py", "recorder.py", "replayer.py", "interactions.py"):
                assert f"autoinstall_helper/{module}" not in names
            assert "autoinstall_helper/fsprobe.py" in names  # production seam stays

            # Digest stored outside docker/ matches the computed digest
            stored = zf.read("autoinstall_helper/DOCKER_DIGEST").decode("utf-8").strip()
            expected = compute_docker_digest(Path("src/autoinstall_helper/docker"))
            assert stored == expected

        # Execute the built pyz archive
        proc = subprocess.run(
            [sys.executable, str(target_pyz), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "autoinstall-helper" in proc.stdout
        # Dev-only flags must not leak into the release artifact
        assert "--record" not in proc.stdout
        assert "--record" not in proc.stderr

    def test_packaged_artifact_rejects_record_flag(self, tmp_path: Path):
        """The .pyz must not expose the dev-only --record option at all."""
        target_pyz = tmp_path / "no-record.pyz"
        build_zipapp(target=target_pyz)

        proc = subprocess.run(
            [sys.executable, str(target_pyz), "--record", str(tmp_path / "session.json"), "validate"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == 2  # argparse usage error
        assert "error:" in proc.stderr
        assert not (tmp_path / "session.json").exists()

    def test_build_zipapp_propagates_exit_code(self, tmp_path: Path):
        """The generated __main__.py must exit with the entry point's status code."""
        src = tmp_path / "src"
        package = src / "autoinstall_helper"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "cli.py").write_text(
            "import sys\n\n\ndef main():\n    return 3\n\n\ndef zipapp_main():\n    sys.exit(main())\n",
            encoding="utf-8",
        )

        target_pyz = tmp_path / "exit-code.pyz"
        build_zipapp(target=target_pyz, src_dir=src)

        proc = subprocess.run(
            [sys.executable, str(target_pyz)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 3, proc.stderr


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

    @staticmethod
    def _write_assets(directory: Path, dockerfile_content: str = "FROM ubuntu:24.04\n") -> Path:
        (directory / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")
        (directory / "timedatectl.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (directory / "validate-autoinstall.py").write_text("print('ok')\n", encoding="utf-8")
        return directory / "Dockerfile"

    def test_docker_image_tag_path_branch_is_deterministic(self, tmp_path: Path):
        dockerfile = self._write_assets(tmp_path)

        tag_one = docker_image_tag("24.04", dockerfile)
        tag_two = docker_image_tag("24.04", dockerfile)

        assert tag_one == tag_two
        prefix = "ubuntu-desktop-bootstrap:24.04-"
        assert tag_one.startswith(prefix)
        digest = tag_one[len(prefix) :]
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)

    def test_docker_image_tag_path_branch_changes_with_content(self, tmp_path: Path):
        dockerfile = self._write_assets(tmp_path)
        tag_before = docker_image_tag("24.04", dockerfile)

        dockerfile.write_text("FROM ubuntu:24.04\nRUN echo changed\n", encoding="utf-8")
        tag_after = docker_image_tag("24.04", dockerfile)

        assert tag_before != tag_after


class TestGetDockerDir:
    """Tests for get_docker_dir in development mode."""

    def test_dev_mode_returns_existing_dir(self):
        docker_dir = get_docker_dir()
        assert docker_dir.is_dir()
        assert (docker_dir / "Dockerfile").is_file()
        assert (docker_dir / "timedatectl.sh").is_file()
        assert (docker_dir / "validate-autoinstall.py").is_file()
