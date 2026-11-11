"""Smoke tests for the packaged 'vm' command (libvirt virtual machines).

These tests exercise the built .pyz artifact end to end. They do not require a
working libvirt installation: 'vm create --dry-run' only builds and prints the
virt-install command line, and 'vm doctor' degrades gracefully to a clear
"not ready" report.

They carry the 'smoke' marker and are excluded from the default test run; run
them explicitly with:

    uv run build-pyz -o dist/autoinstall-helper.pyz
    uv run pytest -m smoke -v
"""

import pytest

pytestmark = pytest.mark.smoke


def test_vm_help_lists_subcommands(run_artifact):
    result = run_artifact("vm", "--help")
    assert result.returncode == 0
    for action in ("create", "eject", "reinstall", "delete", "console", "doctor"):
        assert action in result.stdout


def test_vm_create_dry_run_builds_the_virt_install_command(run_artifact, tmp_path):
    media = tmp_path / "pendrive.img"
    media.write_bytes(b"\0" * 4096)

    result = run_artifact("vm", "create", "smoke-vm", "--media", str(media), "--dry-run")

    assert result.returncode == 0
    assert "virt-install" in result.stdout
    assert f"path={media},bus=usb,removable=on" in result.stdout
    assert "--noautoconsole" in result.stdout
    assert "tool=autoinstall-helper" in result.stdout


def test_vm_create_dry_run_rejects_a_missing_media(run_artifact, tmp_path):
    result = run_artifact("vm", "create", "smoke-vm", "--media", str(tmp_path / "missing.img"), "--dry-run")
    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_vm_doctor_always_reports_a_status(run_artifact):
    result = run_artifact("vm", "doctor", timeout=60)
    assert result.returncode in (0, 1)
    assert "VM preflight check" in result.stdout
