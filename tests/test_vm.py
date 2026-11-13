"""Unit tests for the libvirt VM module (autoinstall_helper.vm).

System boundaries (virsh, virt-install, qemu-img) are mocked at the point of
usage — see AGENTS.md — through a small FakeRunner replacing vm.subprocess.run.
"""

import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from autoinstall_helper import vm
from autoinstall_helper.fsprobe import PathInfo


def completed(stdout="", stderr="", returncode=0):
    """Build a CompletedProcess-like result for the fake runner."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Replacement for vm.subprocess.run: records commands and dispatches canned results."""

    def __init__(self, handler=None):
        self.calls: list[list[str]] = []
        self._handler = handler or (lambda cmd: completed())

    def __call__(self, cmd, check=False, capture_output=False, text=True, **kwargs):
        self.calls.append(list(cmd))
        return self._handler(list(cmd))

    def virsh(self) -> list[list[str]]:
        """Every recorded virsh invocation."""
        return [call for call in self.calls if call and call[0] == "virsh"]

    def virsh_actions(self) -> list[tuple[str, ...]]:
        """The virsh subcommand and its arguments, e.g. ('detach-disk', 'vm', 'sda')."""
        return [tuple(call[3:]) for call in self.virsh()]

    def action(self, name: str) -> tuple[str, ...]:
        """The recorded virsh action with the given subcommand name."""
        return next(action for action in self.virsh_actions() if action[0] == name)

    def find(self, command: str) -> list[str] | None:
        """The first recorded command starting with the given executable name."""
        return next((call for call in self.calls if call[:1] == [command]), None)

    def flat_args(self) -> list[str]:
        """Every argument of every recorded command, flattened."""
        return [arg for call in self.calls for arg in call]


def domain_xml(media="/tmp/pendrive.img", target="/var/lib/libvirt/images/test-vm.qcow2", **options):
    """A trimmed-down but realistic libvirt domain definition."""
    usb = options.get("usb", True)
    nvram = options.get("nvram", True)
    graphics = options.get("graphics", "spice")
    description = options.get("description", True)

    usb_disk = (
        f"""
    <disk type='file' device='disk'>
      <driver name='qemu' type='raw'/>
      <source file='{media}'/>
      <target dev='sda' bus='usb' removable='on'/>
    </disk>"""
        if usb
        else ""
    )
    nvram_element = "<nvram>/var/lib/libvirt/qemu/nvram/test-vm_VARS.fd</nvram>" if nvram else ""
    graphics_element = f"<graphics type='{graphics}' port='5900'/>" if graphics else ""
    description_element = f"<description>{vm.build_marker(Path(media), 'auto')}</description>" if description else ""
    return f"""<domain type='kvm' id='1'>
  <name>test-vm</name>
  {description_element}
  <memory unit='KiB'>4194304</memory>
  <os firmware='efi'>
    {nvram_element}
    <type arch='x86_64'>hvm</type>
  </os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='{target}'/>
      <target dev='vda' bus='virtio'/>
    </disk>{usb_disk}
    {graphics_element}
  </devices>
</domain>"""


def domain_runner(monkeypatch, xml, state="shut off", defined=None, domains="test-vm\n"):
    """Install a FakeRunner serving the standard virsh queries for one domain."""

    def handler(cmd):
        if cmd[:3] == ["virsh", "--connect", vm.SYSTEM_URI]:
            action = cmd[3]
            if action == "list":
                return completed(stdout=domains)
            if action == "dumpxml":
                return completed(stdout=xml)
            if action == "domstate":
                return completed(stdout=f"{state}\n")
            if action == "define" and defined is not None:
                defined["xml"] = Path(cmd[4]).read_text(encoding="utf-8")
                return completed()
        return completed()

    runner = FakeRunner(handler)
    monkeypatch.setattr(vm, "resolve_uri", lambda requested=None: vm.SYSTEM_URI)
    monkeypatch.setattr(vm.subprocess, "run", runner)
    return runner


LISTING_TABLE = " Id   Name      State\n------\n 1    test-vm   running\n -    foreign   shut off\n"


def listing_runner(monkeypatch, managed_xml, table=LISTING_TABLE):
    """FakeRunner serving a two-domain list where only test-vm carries the marker."""

    def handler(cmd):
        if cmd[3] == "list":
            return completed(stdout=table)
        if len(cmd) > 4 and cmd[4] == "test-vm":
            return completed(stdout=managed_xml)
        return completed(stdout=domain_xml(description=False))

    monkeypatch.setattr(vm, "resolve_uri", lambda requested=None: vm.SYSTEM_URI)
    monkeypatch.setattr(vm.subprocess, "run", FakeRunner(handler))


@pytest.fixture
def isolated(monkeypatch):
    """Neutralize tool checks and implicit virtualization detection."""
    monkeypatch.setattr(vm, "require_tools", lambda *commands: None)
    monkeypatch.setattr(vm, "detect_virt_type", lambda no_kvm: None)


@pytest.fixture
def uri(monkeypatch):
    """Pin the libvirt connection URI."""
    monkeypatch.setattr(vm, "resolve_uri", lambda requested=None: vm.SYSTEM_URI)
    return vm.SYSTEM_URI


@pytest.fixture
def fake_run(monkeypatch):
    """Install a permissive FakeRunner as vm.subprocess.run and hand it back."""
    runner = FakeRunner()
    monkeypatch.setattr(vm.subprocess, "run", runner)
    return runner


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


class TestBuildVirtInstallCommand:
    def test_default_spec_detaches_from_the_terminal(self):
        cmd = vm.build_virt_install_command(vm.VmSpec(name="test-vm", media=Path("/tmp/pendrive.img")))
        assert cmd[0] == "virt-install"
        assert "--noautoconsole" in cmd
        assert cmd[cmd.index("--disk") + 1] == "size=40,bus=virtio"
        assert "path=/tmp/pendrive.img,bus=usb,removable=on" in cmd
        assert cmd[cmd.index("--boot") + 1] == "uefi"
        assert cmd[cmd.index("--network") + 1] == "network=default"
        assert cmd[cmd.index("--graphics") + 1] == "spice"
        assert "model=tpm-tis,version=2.0,backend.type=emulator" in cmd
        assert "managed=auto" in " ".join(cmd)

    def test_attached_console_omits_noautoconsole(self):
        spec = vm.VmSpec(name="test-vm", media=Path("/tmp/pendrive.img"), wait=True)
        assert "--noautoconsole" not in vm.build_virt_install_command(spec)

    def test_existing_disk_is_reused_and_marked_manual(self):
        spec = vm.VmSpec(name="test-vm", media=Path("/tmp/pendrive.img"), disk=Path("/data/vm.qcow2"))
        cmd = vm.build_virt_install_command(spec)
        assert "path=/data/vm.qcow2,bus=virtio" in cmd
        assert "managed=manual" in " ".join(cmd)

    def test_optional_flags_are_forwarded(self):
        spec = vm.VmSpec(
            name="test-vm",
            media=Path("/tmp/pendrive.img"),
            tpm=False,
            network="user",
            graphics="none",
            serial=True,
            firmware="bios",
            virt_type="qemu",
            memory_mb=2048,
            vcpus=2,
        )
        cmd = vm.build_virt_install_command(spec)
        assert cmd[cmd.index("--memory") + 1] == "2048"
        assert cmd[cmd.index("--vcpus") + 1] == "2"
        assert cmd[cmd.index("--boot") + 1] == "bios"
        assert cmd[cmd.index("--network") + 1] == "user"
        assert "--tpm" not in cmd
        assert cmd[cmd.index("--virt-type") + 1] == "qemu"
        assert cmd[cmd.index("--console") + 1] == "pty,target_type=serial"


class TestNetworkArgument:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", "network=default"),
            ("default", "network=default"),
            ("user", "user"),
            ("none", "none"),
            ("bridge=br0", "bridge=br0"),
        ],
    )
    def test_mapping(self, value, expected):
        assert vm.network_argument(value) == expected


class TestBuildUndefineCommand:
    def test_never_removes_all_storage(self):
        cmd = vm.build_undefine_command(vm.SYSTEM_URI, "test-vm", nvram=True)
        assert cmd == ["virsh", "--connect", vm.SYSTEM_URI, "undefine", "test-vm", "--nvram"]
        assert "--remove-all-storage" not in cmd

    def test_bios_definition_skips_nvram(self):
        assert vm.build_undefine_command(vm.SYSTEM_URI, "test-vm", nvram=False)[-1] == "test-vm"


# ---------------------------------------------------------------------------
# Domain markers
# ---------------------------------------------------------------------------


class TestMarker:
    def test_round_trip(self):
        marker = vm.build_marker(Path("/data/pendrive.img"), "auto")
        assert vm.parse_marker(marker) == {
            "tool": vm.TOOL_NAME,
            "managed": "auto",
            "media": "/data/pendrive.img",
        }

    def test_foreign_description_is_ignored(self):
        assert vm.parse_marker("just a description") == {}
        assert vm.parse_marker(None) == {}
        assert vm.parse_marker("") == {}

    def test_is_managed_requires_the_tool_field(self):
        assert vm.is_managed(vm.parse_marker(vm.build_marker(Path("/data/p.img"), "auto"))) is True
        assert vm.is_managed({}) is False
        assert vm.is_managed({"managed": "auto", "media": "/data/p.img"}) is False


# ---------------------------------------------------------------------------
# Domain XML parsing
# ---------------------------------------------------------------------------


class TestParseDisks:
    def test_reports_target_and_media_disks(self):
        disks = vm.parse_disks(domain_xml())
        assert [(d.target, d.bus, d.source_type) for d in disks] == [
            ("vda", "virtio", "file"),
            ("sda", "usb", "file"),
        ]

    def test_domain_without_media(self):
        assert [d.bus for d in vm.parse_disks(domain_xml(usb=False))] == ["virtio"]


class TestParseDescriptionAndGraphics:
    def test_description_round_trip(self):
        assert vm.parse_marker(vm.parse_description(domain_xml()))["media"] == "/tmp/pendrive.img"

    def test_graphics_type(self):
        assert vm.parse_graphics(domain_xml(graphics="spice")) == "spice"
        assert vm.parse_graphics(domain_xml(graphics="")) is None


class TestHasNvram:
    def test_uefi_domain(self):
        assert vm.has_nvram(domain_xml()) is True

    def test_bios_domain(self):
        assert vm.has_nvram(domain_xml(nvram=False)) is False


class TestParseDomainList:
    def test_table_is_parsed(self):
        output = " Id   Name    State\n--------------------\n 1    test-vm running\n -    other   shut off\n"
        assert vm.parse_domain_list(output) == [("test-vm", "running"), ("other", "shut off")]

    def test_empty_output(self):
        assert vm.parse_domain_list(" Id   Name   State\n--------------------\n") == []


class TestXmlMutation:
    def test_set_description_inserts_after_name(self):
        updated = vm.set_description(domain_xml(description=False), "hello")
        assert vm.parse_description(updated) == "hello"
        assert [child.tag for child in ET.fromstring(updated)][:2] == ["name", "description"]

    def test_add_media_disk_creates_a_usb_device(self):
        disks = vm.parse_disks(vm.add_media_disk(domain_xml(usb=False), Path("/tmp/pendrive.img")))
        assert [d.bus for d in disks] == ["virtio", "usb"]
        assert disks[1].source == "/tmp/pendrive.img"
        assert disks[1].target != disks[0].target

    def test_set_media_path_updates_the_source(self):
        updated = vm.set_media_path(domain_xml(), Path("/data/new.img"))
        assert [d.source for d in vm.parse_disks(updated) if d.bus == vm.USB_BUS] == ["/data/new.img"]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestResolveUri:
    def test_prefers_the_system_connection(self, monkeypatch):
        monkeypatch.setattr(vm, "uri_works", lambda uri: True)
        assert vm.resolve_uri() == vm.SYSTEM_URI

    def test_falls_back_to_the_session(self, monkeypatch):
        monkeypatch.setattr(vm, "uri_works", lambda uri: uri == vm.SESSION_URI)
        assert vm.resolve_uri() == vm.SESSION_URI

    def test_explicit_uri_is_validated(self, monkeypatch):
        monkeypatch.setattr(vm, "uri_works", lambda uri: uri == "qemu+ssh://host/system")
        assert vm.resolve_uri("qemu+ssh://host/system") == "qemu+ssh://host/system"
        with pytest.raises(SystemExit, match="cannot connect"):
            vm.resolve_uri("qemu:///nope")

    def test_unreachable_libvirt_suggests_groups(self, monkeypatch):
        monkeypatch.setattr(vm, "uri_works", lambda uri: False)
        with pytest.raises(SystemExit, match="usermod -aG libvirt,kvm"):
            vm.resolve_uri()


class TestRequireTools:
    def test_missing_tool_exits_with_install_hint(self, monkeypatch):
        monkeypatch.setattr(vm.shutil, "which", lambda command: None)
        with pytest.raises(SystemExit, match="missing required command"):
            vm.require_tools("virt-install")

    def test_present_tools_pass(self, monkeypatch):
        monkeypatch.setattr(vm.shutil, "which", lambda command: f"/usr/bin/{command}")
        assert vm.require_tools("virt-install", "virsh") is None


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_dry_run_prints_the_command(self, capsys, tmp_path):
        media = tmp_path / "pendrive.img"
        media.touch()
        assert vm.create(vm.VmSpec(name="test-vm", media=media), dry_run=True) == 0
        out = capsys.readouterr().out
        assert "virt-install" in out
        assert "--noautoconsole" in out

    def test_missing_media_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit, match="does not exist"):
            vm.create(vm.VmSpec(name="test-vm", media=tmp_path / "nope.img"), dry_run=True)

    def test_block_device_is_rejected(self, tmp_path, monkeypatch):
        media = tmp_path / "pendrive.img"
        media.touch()
        monkeypatch.setattr(vm.fsprobe, "probe", lambda path: PathInfo(True, False, False, True, 0))
        with pytest.raises(SystemExit, match="block devices are not supported"):
            vm.create(vm.VmSpec(name="test-vm", media=media), dry_run=True)

    def test_directory_is_rejected(self, tmp_path):
        with pytest.raises(SystemExit, match="not a regular file"):
            vm.create(vm.VmSpec(name="test-vm", media=tmp_path), dry_run=True)

    def test_existing_domain_requires_reinstall(self, isolated, uri, tmp_path, monkeypatch):
        media = tmp_path / "pendrive.img"
        media.touch()
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        with pytest.raises(SystemExit, match="already exists"):
            vm.create(vm.VmSpec(name="test-vm", media=media))

    def test_reinstall_flag_delegates_to_reinstall(self, isolated, uri, tmp_path, monkeypatch):
        media = tmp_path / "pendrive.img"
        media.touch()
        seen = {}
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        monkeypatch.setattr(vm, "reinstall_domain", lambda name, **kwargs: seen.update(name=name, **kwargs) or 0)
        assert vm.create(vm.VmSpec(name="test-vm", media=media), reinstall=True) == 0
        assert seen["name"] == "test-vm"
        assert seen["media"] == media


# ---------------------------------------------------------------------------
# reinstall
# ---------------------------------------------------------------------------


class TestReinstall:
    def test_undefines_with_nvram_then_defines_and_starts(self, isolated, monkeypatch, tmp_path):
        media = tmp_path / "pendrive.img"
        media.touch()
        defined: dict[str, str] = {}
        runner = domain_runner(monkeypatch, domain_xml(media=str(media)), defined=defined)

        assert vm.reinstall_domain("test-vm") == 0
        assert [action[0] for action in runner.virsh_actions()] == [
            "list",
            "dumpxml",
            "domstate",
            "undefine",
            "define",
            "start",
        ]
        assert "--nvram" in runner.action("undefine")
        assert str(media) in defined["xml"]

    def test_media_is_reattached_when_it_was_ejected(self, isolated, monkeypatch, tmp_path):
        media = tmp_path / "pendrive.img"
        media.touch()
        defined: dict[str, str] = {}
        xml = domain_xml(media=str(media), usb=False)
        runner = domain_runner(monkeypatch, xml, defined=defined)

        assert vm.reinstall_domain("test-vm") == 0
        assert [d.source for d in vm.parse_disks(defined["xml"]) if d.bus == vm.USB_BUS] == [str(media)]
        assert "--remove-all-storage" not in runner.flat_args()

    def test_media_can_be_overridden(self, isolated, monkeypatch, tmp_path):
        old_media = tmp_path / "old.img"
        old_media.touch()
        new_media = tmp_path / "new.img"
        new_media.touch()
        defined: dict[str, str] = {}
        domain_runner(monkeypatch, domain_xml(media=str(old_media)), defined=defined)

        assert vm.reinstall_domain("test-vm", media=new_media) == 0
        assert [d.source for d in vm.parse_disks(defined["xml"]) if d.bus == vm.USB_BUS] == [str(new_media)]

    def test_dry_run_changes_nothing(self, isolated, monkeypatch, capsys, tmp_path):
        media = tmp_path / "pendrive.img"
        media.touch()
        runner = domain_runner(monkeypatch, domain_xml(media=str(media)), state="running")

        assert vm.reinstall_domain("test-vm", dry_run=True) == 0
        assert [action[0] for action in runner.virsh_actions()] == ["list", "dumpxml", "domstate"]
        assert "Reinstall plan" in capsys.readouterr().out

    def test_reset_disk_stops_the_domain_before_recreating(self, isolated, monkeypatch, tmp_path):
        media = tmp_path / "pendrive.img"
        media.touch()
        target = tmp_path / "test-vm.qcow2"
        target.write_text("disk")
        recreated = []
        monkeypatch.setattr(vm, "recreate_storage", lambda uri, path: recreated.append(path))
        runner = domain_runner(
            monkeypatch,
            domain_xml(media=str(media), target=str(target)),
            state="running",
            defined={},
        )

        assert vm.reinstall_domain("test-vm", reset_disk=True) == 0
        actions = [action[0] for action in runner.virsh_actions()]
        assert actions == ["list", "dumpxml", "domstate", "destroy", "undefine", "define", "start"]
        assert recreated == [target]

    def test_missing_domain_is_rejected(self, isolated, monkeypatch):
        domain_runner(monkeypatch, domain_xml(), domains="")
        with pytest.raises(SystemExit, match="does not exist"):
            vm.reinstall_domain("test-vm")


# ---------------------------------------------------------------------------
# eject
# ---------------------------------------------------------------------------


class TestEject:
    def test_detaches_persistently(self, isolated, monkeypatch):
        runner = domain_runner(monkeypatch, domain_xml(), state="running")
        assert vm.eject("test-vm") == 0
        assert runner.action("detach-disk") == ("detach-disk", "test-vm", "sda", "--live", "--config")

    def test_live_only(self, isolated, monkeypatch):
        runner = domain_runner(monkeypatch, domain_xml(), state="running")
        assert vm.eject("test-vm", live_only=True) == 0
        assert runner.action("detach-disk") == ("detach-disk", "test-vm", "sda", "--live")

    def test_stopped_domain_updates_the_config_only(self, isolated, monkeypatch):
        runner = domain_runner(monkeypatch, domain_xml(), state="shut off")
        assert vm.eject("test-vm") == 0
        assert runner.action("detach-disk") == ("detach-disk", "test-vm", "sda", "--config")

    def test_reboot_after_eject(self, isolated, monkeypatch):
        runner = domain_runner(monkeypatch, domain_xml(), state="running")
        assert vm.eject("test-vm", reboot=True) == 0
        assert runner.action("reboot") == ("reboot", "test-vm")

    def test_without_media_is_rejected(self, isolated, monkeypatch):
        domain_runner(monkeypatch, domain_xml(usb=False))
        with pytest.raises(SystemExit, match="no installation media"):
            vm.eject("test-vm")

    def test_reboot_requires_a_running_domain(self, isolated, monkeypatch):
        domain_runner(monkeypatch, domain_xml(), state="shut off")
        with pytest.raises(SystemExit, match="--reboot requires a running domain"):
            vm.eject("test-vm", reboot=True)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_definition_is_removed_without_touching_the_media(self, isolated, monkeypatch, tmp_path):
        target = tmp_path / "test-vm.qcow2"
        target.write_text("disk")
        media = tmp_path / "pendrive.img"
        media.write_text("media")
        runner = domain_runner(monkeypatch, domain_xml(media=str(media), target=str(target)), state="running")

        assert vm.delete("test-vm") == 0
        assert media.exists()
        assert target.exists()
        assert [action[0] for action in runner.virsh_actions()] == [
            "list",
            "dumpxml",
            "domstate",
            "destroy",
            "undefine",
        ]
        assert "--nvram" in runner.action("undefine")
        assert "--remove-all-storage" not in runner.flat_args()

    def test_purge_disk_removes_only_the_target(self, isolated, monkeypatch, tmp_path):
        target = tmp_path / "test-vm.qcow2"
        target.write_text("disk")
        media = tmp_path / "pendrive.img"
        media.write_text("media")
        runner = domain_runner(monkeypatch, domain_xml(media=str(media), target=str(target)), state="shut off")

        assert vm.delete("test-vm", purge_disk=True, yes=True) == 0
        assert not target.exists()
        assert media.exists()
        assert "--remove-all-storage" not in runner.flat_args()

    def test_purge_disk_in_non_interactive_mode_requires_yes(self, isolated, monkeypatch, tmp_path):
        target = tmp_path / "test-vm.qcow2"
        target.write_text("disk")
        media = tmp_path / "pendrive.img"
        media.write_text("media")
        domain_runner(monkeypatch, domain_xml(media=str(media), target=str(target)), state="shut off")
        monkeypatch.setattr(vm.sys.stdin, "isatty", lambda: False)

        with pytest.raises(SystemExit, match="requires -y"):
            vm.delete("test-vm", purge_disk=True)
        assert target.exists()


# ---------------------------------------------------------------------------
# storage helpers
# ---------------------------------------------------------------------------


class TestStorage:
    def test_remove_storage_falls_back_to_unlink_for_plain_files(self, isolated, monkeypatch, tmp_path):
        monkeypatch.setattr(vm, "pool_for_path", lambda uri, path: None)
        target = tmp_path / "disk.qcow2"
        target.write_text("data")
        assert vm.remove_storage(vm.SYSTEM_URI, target) == f"file {target}"
        assert not target.exists()

    def test_remove_storage_prefers_the_libvirt_api(self, isolated, monkeypatch, tmp_path, fake_run):
        monkeypatch.setattr(vm, "pool_for_path", lambda uri, path: "default")
        target = tmp_path / "disk.qcow2"
        target.write_text("data")
        assert vm.remove_storage(vm.SYSTEM_URI, target) == "libvirt volume default/disk.qcow2"
        assert target.exists()
        assert fake_run.virsh_actions() == [("vol-delete", "disk.qcow2", "--pool", "default")]

    def test_recreate_storage_uses_qemu_img_for_plain_files(self, isolated, monkeypatch, tmp_path):
        monkeypatch.setattr(vm, "pool_for_path", lambda uri, path: None)
        target = tmp_path / "disk.qcow2"
        target.write_text("data")

        def handler(cmd):
            if cmd[:2] == ["qemu-img", "info"]:
                return completed(stdout=json.dumps({"format": "qcow2", "virtual-size": 42949672960}))
            return completed()

        runner = FakeRunner(handler)
        monkeypatch.setattr(vm.subprocess, "run", runner)

        vm.recreate_storage(vm.SYSTEM_URI, target)
        create = next(call for call in runner.calls if call[:2] == ["qemu-img", "create"])
        assert create[2:4] == ["-f", "qcow2"]
        assert create[-1] == "42949672960"

    def test_recreate_storage_prefers_the_libvirt_api(self, isolated, monkeypatch, tmp_path):
        monkeypatch.setattr(vm, "pool_for_path", lambda uri, path: "default")
        volume_xml = (
            "<volume><capacity unit='bytes'>42949672960</capacity><target><format type='qcow2'/></target></volume>"
        )

        def handler(cmd):
            if cmd[:3] == ["virsh", "--connect", vm.SYSTEM_URI] and cmd[3] == "vol-dumpxml":
                return completed(stdout=volume_xml)
            return completed()

        runner = FakeRunner(handler)
        monkeypatch.setattr(vm.subprocess, "run", runner)

        vm.recreate_storage(vm.SYSTEM_URI, tmp_path / "disk.qcow2")
        assert runner.action("vol-delete") == ("vol-delete", "disk.qcow2", "--pool", "default")
        assert runner.action("vol-create-as") == (
            "vol-create-as",
            "default",
            "disk.qcow2",
            "42949672960",
            "--format",
            "qcow2",
        )


# ---------------------------------------------------------------------------
# console / status / list
# ---------------------------------------------------------------------------


class TestConsole:
    def test_graphical_console_requires_virt_viewer(self, isolated, uri, monkeypatch):
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        monkeypatch.setattr(vm, "dumpxml", lambda uri_, name: domain_xml())
        monkeypatch.setattr(vm.shutil, "which", lambda command: None)
        with pytest.raises(SystemExit, match="virt-viewer is required"):
            vm.console("test-vm")

    def test_serial_console_uses_virsh(self, isolated, uri, monkeypatch, fake_run):
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        monkeypatch.setattr(vm, "dumpxml", lambda uri_, name: domain_xml())
        monkeypatch.setattr(vm.shutil, "which", lambda command: None)
        assert vm.console("test-vm", serial=True) == 0
        assert ("console", "test-vm") in fake_run.virsh_actions()

    def test_graphics_none_falls_back_to_serial(self, isolated, uri, monkeypatch, fake_run):
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        monkeypatch.setattr(vm, "dumpxml", lambda uri_, name: domain_xml(graphics=""))
        monkeypatch.setattr(vm.shutil, "which", lambda command: None)
        assert vm.console("test-vm") == 0
        assert ("console", "test-vm") in fake_run.virsh_actions()


class TestStatusAndList:
    def test_status_prints_the_media(self, isolated, uri, monkeypatch, capsys):
        monkeypatch.setattr(vm, "domain_exists", lambda uri_, name: True)
        monkeypatch.setattr(vm, "dumpxml", lambda uri_, name: domain_xml())
        monkeypatch.setattr(vm, "domain_state", lambda uri_, name: "running")
        assert vm.status("test-vm") == 0
        out = capsys.readouterr().out
        assert "/tmp/pendrive.img" in out
        assert "running" in out

    def test_list_shows_only_managed_vms_by_default(self, isolated, monkeypatch, capsys):
        listing_runner(monkeypatch, managed_xml=domain_xml())
        assert vm.list_domains() == 0
        out = capsys.readouterr().out
        assert "test-vm" in out
        assert "foreign" not in out
        assert f"  [{vm.TOOL_NAME}]" not in out

    def test_list_all_marks_managed_and_shows_foreign_domains(self, isolated, monkeypatch, capsys):
        listing_runner(monkeypatch, managed_xml=domain_xml())
        assert vm.list_domains(show_all=True) == 0
        out = capsys.readouterr().out
        assert "test-vm" in out
        assert "foreign" in out
        assert f"  [{vm.TOOL_NAME}]" in out

    def test_list_without_managed_vms_explains_the_all_flag(self, isolated, monkeypatch, capsys):
        listing_runner(monkeypatch, managed_xml=domain_xml(description=False))
        assert vm.list_domains() == 0
        assert "No VMs managed by autoinstall-helper" in capsys.readouterr().out

    def test_list_all_without_any_domain(self, isolated, monkeypatch, capsys):
        listing_runner(monkeypatch, table=" Id   Name   State\n----\n", managed_xml=domain_xml())
        assert vm.list_domains(show_all=True) == 0
        assert "No domains" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# doctor / virtualization detection
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_reports_missing_tools_and_unreachable_libvirt(self, monkeypatch, capsys):
        monkeypatch.setattr(vm.shutil, "which", lambda command: None)
        monkeypatch.setattr(vm, "uri_works", lambda uri: False)
        monkeypatch.setattr(vm, "find_uefi_firmware", lambda: None)
        assert vm.doctor() == 1
        out = capsys.readouterr().out
        assert "MISSING (required)" in out
        assert "NOT ready" in out

    def test_ready_host(self, monkeypatch, capsys):
        monkeypatch.setattr(vm.shutil, "which", lambda command: f"/usr/bin/{command}")
        monkeypatch.setattr(vm, "uri_works", lambda uri: True)
        monkeypatch.setattr(vm, "find_uefi_firmware", lambda: "/usr/share/OVMF/OVMF_CODE.fd")
        monkeypatch.setattr(vm.Path, "exists", lambda self: True)
        assert vm.doctor() == 0
        assert "VM support is ready" in capsys.readouterr().out


class TestDetectVirtType:
    def test_explicit_no_kvm_forces_qemu(self):
        assert vm.detect_virt_type(True) == "qemu"

    def test_missing_kvm_warns_and_falls_back(self, monkeypatch, capsys):
        monkeypatch.setattr(vm.Path, "exists", lambda self: False)
        assert vm.detect_virt_type(False) == "qemu"
        assert "software emulation" in capsys.readouterr().out

    def test_kvm_available_uses_autodetection(self, monkeypatch):
        monkeypatch.setattr(vm.Path, "exists", lambda self: True)
        assert vm.detect_virt_type(False) is None


def test_vm_spec_defaults_match_the_documented_values():
    spec = vm.VmSpec(name="vm", media=Path("media.img"))
    assert (spec.disk_size_gib, spec.memory_mb, spec.vcpus) == (40, 4096, 4)
    assert spec.firmware == "uefi"
    assert spec.graphics == "spice"
    assert spec.tpm is True
    assert spec.wait is False
