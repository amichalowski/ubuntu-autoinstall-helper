"""Tests for docker_utils.py: the Docker preflight check (`validate doctor`)."""

import subprocess

from autoinstall_helper import docker_utils


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
