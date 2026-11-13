"""Tests for docker_utils.py: the Docker preflight check (`validate doctor`) and
validator image management (`validate images`)."""

import subprocess

import pytest

from autoinstall_helper import docker_utils

CURRENT_DIGEST = "c94a31ab745d"


class DockerStub:
    """Minimal stand-in for the Docker CLI, recording every invocation."""

    def __init__(self, ls_output="", *, ls_rc=0, ls_stderr="", rm_rc=0, rm_stderr=""):
        self.ls_output = ls_output
        self.ls_rc = ls_rc
        self.ls_stderr = ls_stderr
        self.rm_rc = rm_rc
        self.rm_stderr = rm_stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "image", "ls"]:
            return subprocess.CompletedProcess(cmd, self.ls_rc, self.ls_output, self.ls_stderr)
        return subprocess.CompletedProcess(cmd, self.rm_rc, "", self.rm_stderr)

    @property
    def removed(self) -> list[str]:
        return [cmd[-1] for cmd in self.calls if cmd[:3] == ["docker", "image", "rm"]]


def image_row(tag, *, size="1.2GB", created="2 weeks ago"):
    """Build one line of `docker image ls --format` output."""
    return f"ubuntu-desktop-bootstrap\t{tag}\t{tag}\t{created}\t{size}\n"


def install_stub(monkeypatch, stub):
    """Route both the digest lookup and the Docker CLI through the stub."""
    monkeypatch.setattr(docker_utils, "get_docker_digest", lambda: CURRENT_DIGEST)
    monkeypatch.setattr(docker_utils.subprocess, "run", stub)
    return stub


class TestDockerDoctor:
    """`validate doctor` reports Docker availability and non-sudo access."""

    def test_missing_docker_binary(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda command: None)

        assert docker_utils.docker_doctor() == 1
        out = capsys.readouterr().out
        assert "MISSING (required)" in out
        assert "apt install docker.io" in out
        assert "Docker support is NOT ready." in out

    def test_unreachable_daemon_as_regular_user_mentions_docker_group(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda command: f"/usr/bin/{command}")
        monkeypatch.setattr(docker_utils, "_docker_server_version", lambda: (None, "permission denied"))
        monkeypatch.setattr(docker_utils, "_docker_group_membership", lambda: False)
        monkeypatch.setattr(docker_utils.os, "geteuid", lambda: 1000)

        assert docker_utils.docker_doctor() == 1
        out = capsys.readouterr().out
        assert "unreachable" in out
        assert "permission denied" in out
        assert "systemctl enable --now docker" in out
        assert "usermod -aG docker" in out
        assert "without sudo  no" in out

    def test_ready_host_as_regular_user(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda command: f"/usr/bin/{command}")
        monkeypatch.setattr(docker_utils, "_docker_server_version", lambda: ("28.0.4", ""))
        monkeypatch.setattr(docker_utils.os, "geteuid", lambda: 1000)

        assert docker_utils.docker_doctor() == 0
        out = capsys.readouterr().out
        assert "daemon        reachable (server 28.0.4)" in out
        assert "without sudo  ok" in out
        assert "Docker support is ready." in out

    def test_root_cannot_verify_sudo_free_access(self, monkeypatch, capsys):
        monkeypatch.setattr(docker_utils.shutil, "which", lambda command: f"/usr/bin/{command}")
        monkeypatch.setattr(docker_utils, "_docker_server_version", lambda: ("28.0.4", ""))
        monkeypatch.setattr(docker_utils.os, "geteuid", lambda: 0)

        assert docker_utils.docker_doctor() == 0
        out = capsys.readouterr().out
        assert "unknown (running as root)" in out
        assert "normal user" in out


class TestDockerServerVersion:
    """`docker version` probing used by the preflight check."""

    def test_returns_server_version(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="28.0.4\n", stderr="")

        monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

        assert docker_utils._docker_server_version() == ("28.0.4", "")

    def test_reports_last_error_line(self, monkeypatch):
        def fake_run(*args, **kwargs):
            stderr = "Cannot connect to the Docker daemon.\npermission denied while trying to connect\n"
            return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr=stderr)

        monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

        version, error = docker_utils._docker_server_version()
        assert version is None
        assert error == "permission denied while trying to connect"

    def test_blank_stderr_falls_back_to_generic_message(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="\n")

        monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

        assert docker_utils._docker_server_version() == (None, "the Docker daemon is not reachable")


class TestDockerGroupMembership:
    """Group lookup via stdlib (no subprocess, no `id`)."""

    def test_missing_group_returns_none(self, monkeypatch):
        def fake_getgrnam(name):
            raise KeyError(name)

        monkeypatch.setattr(docker_utils.grp, "getgrnam", fake_getgrnam)

        assert docker_utils._docker_group_membership() is None

    def test_member_and_non_member(self, monkeypatch):
        class Group:
            gr_gid = 999

        monkeypatch.setattr(docker_utils.grp, "getgrnam", lambda name: Group())
        monkeypatch.setattr(docker_utils.os, "getgroups", lambda: [999, 1000])
        assert docker_utils._docker_group_membership() is True

        monkeypatch.setattr(docker_utils.os, "getgroups", lambda: [1000])
        assert docker_utils._docker_group_membership() is False


class TestListValidatorImages:
    """Parsing of `docker image ls --format` output."""

    def test_parses_version_and_digest_from_tag(self, monkeypatch):
        stub = install_stub(monkeypatch, DockerStub(image_row("26.04-c94a31ab745d")))

        images = docker_utils.list_validator_images()

        assert images == [
            docker_utils.ValidatorImage(
                tag="ubuntu-desktop-bootstrap:26.04-c94a31ab745d",
                image_id="26.04-c94a31ab745d",
                created="2 weeks ago",
                size="1.2GB",
                version="26.04",
                digest="c94a31ab745d",
            )
        ]
        assert stub.calls[0] == [
            "docker",
            "image",
            "ls",
            "ubuntu-desktop-bootstrap",
            "--format",
            "\t".join("{{." + field + "}}" for field in docker_utils.DOCKER_IMAGE_FIELDS),
        ]

    def test_unrecognized_tag_has_no_version_or_digest(self, monkeypatch):
        install_stub(monkeypatch, DockerStub(image_row("manual")))

        image = docker_utils.list_validator_images()[0]

        assert image.version is None
        assert image.digest is None
        assert image.status(CURRENT_DIGEST) == "unrecognized"

    def test_docker_failure_is_reported_with_a_doctor_hint(self, monkeypatch):
        install_stub(
            monkeypatch,
            DockerStub(ls_rc=1, ls_stderr="Cannot connect to the Docker daemon.\npermission denied\n"),
        )

        with pytest.raises(SystemExit) as excinfo:
            docker_utils.list_validator_images()

        message = str(excinfo.value)
        assert "permission denied" in message
        assert "validate doctor" in message


class TestDockerImages:
    """`validate images` listing and pruning."""

    def test_lists_current_and_stale_images(self, monkeypatch, capsys):
        stub = install_stub(
            monkeypatch,
            DockerStub(image_row(f"26.04-{CURRENT_DIGEST}") + image_row("24.04-deadbeef1234")),
        )

        assert docker_utils.docker_images() == 0

        out = capsys.readouterr().out
        assert f"bundled assets digest: {CURRENT_DIGEST}" in out
        assert f"ubuntu-desktop-bootstrap:26.04-{CURRENT_DIGEST}" in out
        assert "current" in out
        assert "stale" in out
        assert stub.removed == []

    def test_no_images(self, monkeypatch, capsys):
        install_stub(monkeypatch, DockerStub())

        assert docker_utils.docker_images() == 0
        assert "none — 'validate <config>' builds one on demand" in capsys.readouterr().out

    def test_prune_removes_only_stale_images(self, monkeypatch, capsys):
        stub = install_stub(
            monkeypatch,
            DockerStub(image_row(f"26.04-{CURRENT_DIGEST}") + image_row("24.04-deadbeef1234")),
        )

        assert docker_utils.docker_images(prune=True, yes=True) == 0

        assert stub.removed == ["ubuntu-desktop-bootstrap:24.04-deadbeef1234"]
        assert "Removed 1 of 1 image(s)" in capsys.readouterr().out

    def test_prune_all_removes_every_image(self, monkeypatch):
        stub = install_stub(
            monkeypatch,
            DockerStub(image_row(f"26.04-{CURRENT_DIGEST}") + image_row("24.04-deadbeef1234")),
        )

        assert docker_utils.docker_images(prune=True, remove_all=True, yes=True) == 0

        assert stub.removed == [
            f"ubuntu-desktop-bootstrap:26.04-{CURRENT_DIGEST}",
            "ubuntu-desktop-bootstrap:24.04-deadbeef1234",
        ]

    def test_nothing_to_prune(self, monkeypatch, capsys):
        stub = install_stub(monkeypatch, DockerStub(image_row(f"26.04-{CURRENT_DIGEST}")))

        assert docker_utils.docker_images(prune=True, yes=True) == 0

        assert stub.removed == []
        assert "Nothing to prune" in capsys.readouterr().out

    def test_dry_run_does_not_remove_anything(self, monkeypatch, capsys):
        stub = install_stub(monkeypatch, DockerStub(image_row("24.04-deadbeef1234")))

        assert docker_utils.docker_images(prune=True, dry_run=True) == 0

        assert stub.removed == []
        assert "Would remove 1 validator image(s)" in capsys.readouterr().out

    def test_non_interactive_prune_requires_yes(self, monkeypatch):
        stub = install_stub(monkeypatch, DockerStub(image_row("24.04-deadbeef1234")))
        monkeypatch.setattr(docker_utils.sys.stdin, "isatty", lambda: False)

        with pytest.raises(SystemExit) as excinfo:
            docker_utils.docker_images(prune=True)

        assert "-y / --yes" in str(excinfo.value)
        assert stub.removed == []

    def test_cancelled_by_user(self, monkeypatch, capsys):
        stub = install_stub(monkeypatch, DockerStub(image_row("24.04-deadbeef1234")))
        monkeypatch.setattr(docker_utils.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")

        assert docker_utils.docker_images(prune=True) == 1

        assert stub.removed == []
        assert "Cancelled by user" in capsys.readouterr().out

    def test_removal_failure_is_reported_and_returns_exit_code(self, monkeypatch, capsys):
        install_stub(
            monkeypatch,
            DockerStub(image_row("24.04-deadbeef1234"), rm_rc=1, rm_stderr="conflict: image is in use\n"),
        )

        assert docker_utils.docker_images(prune=True, yes=True) == 1

        captured = capsys.readouterr()
        assert "FAILED ubuntu-desktop-bootstrap:24.04-deadbeef1234: conflict: image is in use" in captured.err
        assert "1 failed" in captured.out
