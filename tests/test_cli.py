"""Tests for the CLI layer (cli.py): command dispatch and root-privilege requirement.

We mock functions at the point of use (autoinstall_helper.cli.*), not at definition —
see AGENTS.md.
"""

import json
import sys
from pathlib import Path

import pytest

from autoinstall_helper import cli


@pytest.fixture
def run(monkeypatch):
    """Call cli.main() with the given argv, replacing sys.argv."""

    def _run(*argv):
        monkeypatch.setattr(sys, "argv", ["autoinstall-helper", *argv])
        return cli.main()

    return _run


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_dispatch(monkeypatch, run, tmp_path):
    config = tmp_path / "autoinstall.yaml"
    config.write_text("#cloud-config\n")
    seen = {}

    monkeypatch.setattr(cli, "validate", lambda cfg, validation: seen.update(cfg=cfg, validation=validation) or 0)

    rc = run("validate", str(config), "--validation", "storage")
    assert rc == 0
    assert seen == {"cfg": config, "validation": "storage"}


def test_validate_dispatch_default(monkeypatch, run, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "validate", lambda cfg, validation: 0)
    assert run("validate") == 0


def test_validate_doctor_dispatch(monkeypatch, run):
    """'validate doctor' runs the Docker preflight instead of the validator."""
    calls = {}
    monkeypatch.setattr(cli, "docker_doctor", lambda: calls.update(doctor=True) or 0)
    monkeypatch.setattr(cli, "validate", lambda cfg, validation: pytest.fail("validate must not run"))

    assert run("validate", "doctor") == 0
    assert calls == {"doctor": True}


def test_validate_doctor_propagates_failure(monkeypatch, run):
    monkeypatch.setattr(cli, "docker_doctor", lambda: 1)

    assert run("validate", "doctor") == 1


# ---------------------------------------------------------------------------
# flash
# ---------------------------------------------------------------------------


def test_flash_on_file_no_root_required(monkeypatch, run, tmp_path):
    target = tmp_path / "out.img"
    target.touch()
    seen = {}

    monkeypatch.setattr(cli, "is_block_device", lambda p: False)
    monkeypatch.setattr(cli, "flash", lambda iso, target, yes: seen.update(iso=iso, target=target, yes=yes) or 0)

    rc = run("flash", "img.iso", str(target), "--yes")
    assert rc == 0
    assert seen == {"iso": Path("img.iso"), "target": target, "yes": True}


def test_flash_on_block_device_requires_root(monkeypatch, run, tmp_path):
    target = tmp_path / "dev"

    monkeypatch.setattr(cli, "is_block_device", lambda p: True)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)

    with pytest.raises(SystemExit, match="root"):
        run("flash", "img.iso", str(target), "--yes")


def test_flash_on_block_device_with_root(monkeypatch, run, tmp_path):
    target = tmp_path / "dev"
    called = {}

    monkeypatch.setattr(cli, "is_block_device", lambda p: True)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "flash", lambda iso, target, yes: called.update(yes=yes) or 0)

    assert run("flash", "img.iso", str(target), "--yes") == 0
    assert called == {"yes": True}


# ---------------------------------------------------------------------------
# inject / import
# ---------------------------------------------------------------------------


def test_inject_requires_root(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="root"):
        run("inject", str(tmp_path))


def test_inject_with_root_dispatch(monkeypatch, run, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "inject", lambda target, config: seen.update(target=target, config=config) or 0)

    assert run("inject", "disk.img", "conf.yaml") == 0
    assert seen == {"target": Path("disk.img"), "config": Path("conf.yaml")}


def test_import_alias_dispatches_to_inject(monkeypatch, run, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "inject", lambda target, config: seen.update(config=config) or 0)

    assert run("import", "disk.img", "conf.yaml") == 0
    assert seen == {"config": Path("conf.yaml")}


# ---------------------------------------------------------------------------
# extract / export
# ---------------------------------------------------------------------------


def test_extract_requires_root(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="root"):
        run("extract", str(tmp_path))


def test_extract_with_root_dispatch(monkeypatch, run, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "extract", lambda target, output: seen.update(target=target, output=output) or 0)

    assert run("extract", "disk.img", "out.yaml") == 0
    assert seen == {"target": Path("disk.img"), "output": Path("out.yaml")}


def test_export_alias_dispatches_to_extract(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "extract", lambda target, output: 0)
    assert run("export", "disk.img") == 0


# ---------------------------------------------------------------------------
# cidata (subcommands)
# ---------------------------------------------------------------------------


def test_cidata_inject_dispatch(monkeypatch, run, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "inject", lambda target, config: seen.update(target=target) or 0)

    assert run("cidata", "inject", "disk.img") == 0
    assert seen == {"target": Path("disk.img")}


def test_cidata_inject_requires_root(monkeypatch, run, tmp_path):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="cidata inject"):
        run("cidata", "inject", "disk.img")


# ---------------------------------------------------------------------------
# vm (libvirt virtual machines)
# ---------------------------------------------------------------------------


def test_vm_create_dispatch(monkeypatch, run, tmp_path):
    media = tmp_path / "pendrive.img"
    seen = {}
    monkeypatch.setattr(cli.vm, "create", lambda spec, **kwargs: seen.update(spec=spec, **kwargs) or 0)

    rc = run("vm", "create", "test-vm", "--media", str(media), "--disk-size", "20", "--no-tpm", "--wait")
    assert rc == 0
    spec = seen["spec"]
    assert (spec.name, spec.media, spec.disk_size_gib, spec.tpm, spec.wait) == ("test-vm", media, 20, False, True)
    assert seen["dry_run"] is False
    assert seen["reinstall"] is False


def test_vm_create_does_not_require_root(monkeypatch, run, tmp_path):
    media = tmp_path / "pendrive.img"
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(cli.vm, "create", lambda spec, **kwargs: 0)
    assert run("vm", "create", "test-vm", "--media", str(media)) == 0


def test_vm_reinstall_dispatch(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.vm, "reinstall_domain", lambda name, **kwargs: seen.update(name=name, **kwargs) or 0)

    assert run("vm", "reinstall", "test-vm", "--reset-disk", "--wait") == 0
    assert seen["name"] == "test-vm"
    assert seen["reset_disk"] is True
    assert seen["wait"] is True


def test_vm_eject_dispatch(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.vm, "eject", lambda name, **kwargs: seen.update(name=name, **kwargs) or 0)

    assert run("vm", "eject", "test-vm", "--live-only", "--reboot") == 0
    assert seen == {"name": "test-vm", "uri": None, "live_only": True, "reboot": True}


def test_vm_delete_dispatch(monkeypatch, run):
    seen = {}
    monkeypatch.setattr(cli.vm, "delete", lambda name, **kwargs: seen.update(name=name, **kwargs) or 0)

    assert run("vm", "delete", "test-vm", "--purge-disk", "-y") == 0
    assert seen == {"name": "test-vm", "uri": None, "purge_disk": True, "yes": True}


def test_vm_list_and_doctor_dispatch(monkeypatch, run):
    calls = {}
    monkeypatch.setattr(cli.vm, "list_domains", lambda **kwargs: calls.update(list=kwargs) or 0)
    monkeypatch.setattr(cli.vm, "doctor", lambda uri: calls.update(doctor=uri) or 0)

    assert run("vm", "list") == 0
    assert calls["list"] == {"uri": None, "show_all": False}
    assert run("vm", "list", "--all") == 0
    assert calls["list"] == {"uri": None, "show_all": True}
    assert run("vm", "doctor", "--uri", "qemu:///session") == 0
    assert calls["doctor"] == "qemu:///session"


def test_vm_without_action_prints_help(run, capsys):
    assert run("vm") == 0
    out = capsys.readouterr().out
    assert "create" in out
    assert "reinstall" in out


# ---------------------------------------------------------------------------
# zipapp entry point
# ---------------------------------------------------------------------------


def test_zipapp_main_exits_with_command_status(monkeypatch):
    """zipapp ignores return values, so the entry point must raise SystemExit."""
    monkeypatch.setattr(cli, "main", lambda: 3)
    with pytest.raises(SystemExit) as excinfo:
        cli.zipapp_main()
    assert excinfo.value.code == 3


def test_cidata_extract_dispatch(monkeypatch, run, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "extract", lambda target, output: seen.update(output=output) or 0)

    assert run("cidata", "extract", "disk.img", "out.yaml") == 0
    assert seen == {"output": Path("out.yaml")}


def test_cidata_without_action_prints_help(monkeypatch, run, capsys):
    assert run("cidata") == 0
    out = capsys.readouterr().out
    assert "inject" in out
    assert "extract" in out


# ---------------------------------------------------------------------------
# No command
# ---------------------------------------------------------------------------


def test_no_subcommand_prints_help(run, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert "validate" in out
    assert "flash" in out
    assert "cidata" in out
    assert "vm" in out


def test_build_parser_has_all_commands():
    parser = cli.build_parser()
    subparsers_action = next(a for a in parser._actions if isinstance(a, cli.argparse._SubParsersAction))
    choices = set(subparsers_action.choices.keys())
    assert {"validate", "flash", "inject", "import", "extract", "export", "cidata", "vm"}.issubset(choices)


def test_main_with_explicit_argv(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "validate", lambda cfg, validation: called.update(called=True) or 0)
    rc = cli.main(["validate", "my_autoinstall.yaml"])
    assert rc == 0
    assert called == {"called": True}


def test_record_flag_wraps_dispatch(monkeypatch, tmp_path):
    """--record must run dispatch inside a recording session and persist metadata."""
    record = tmp_path / "session.json"
    monkeypatch.setattr(cli, "dispatch", lambda args, parser: 0)

    rc = cli.main(["--record", str(record), "validate", "autoinstall.yaml"])

    assert rc == 0
    data = json.loads(record.read_text(encoding="utf-8"))
    assert data["metadata"]["exit_code"] == 0
    assert data["metadata"]["schema_version"] == 1
