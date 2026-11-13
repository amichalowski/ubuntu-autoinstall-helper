"""Unit tests for pure validation and transformation functions in docker/validate-autoinstall.py."""

import copy
import importlib.util
import json
import subprocess
import sys
import types
import urllib.error
from pathlib import Path
from typing import ClassVar

import pytest

# Dynamic load because the filename has a hyphen: validate-autoinstall.py
_VALIDATOR_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "autoinstall_helper" / "docker" / "validate-autoinstall.py"
)
_spec = importlib.util.spec_from_file_location("validate_autoinstall", _VALIDATOR_PATH)
assert _spec and _spec.loader
_validator_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validator_mod)

validate_storage_structure = _validator_mod.validate_storage_structure
storage_only_data = _validator_mod.storage_only_data
curtin_storage_input = _validator_mod.curtin_storage_input
collect_shell_commands = _validator_mod.collect_shell_commands
nested_shell_scripts = _validator_mod.nested_shell_scripts
validate_shell_commands = _validator_mod.validate_shell_commands
AptSource = _validator_mod.AptSource
abbreviated_source = _validator_mod.abbreviated_source
collect_apt_sources = _validator_mod.collect_apt_sources
collect_packages = _validator_mod.collect_packages
skipped_apt_keys = _validator_mod.skipped_apt_keys
target_codename = _validator_mod.target_codename
retarget_default_sources = _validator_mod.retarget_default_sources
resolve_ppa = _validator_mod.resolve_ppa
apply_apt_sources = _validator_mod.apply_apt_sources
validate_apt = _validator_mod.validate_apt


class TestValidateStorageStructure:
    """Tests for validate_storage_structure(ai_data)."""

    def test_no_storage_section(self):
        assert validate_storage_structure({}) == []

    def test_storage_none(self):
        assert validate_storage_structure({"storage": None}) == []

    def test_storage_not_mapping(self):
        errors = validate_storage_structure({"storage": "invalid"})
        assert errors == ["autoinstall.storage must be a mapping"]

    def test_storage_layout_based_without_config(self):
        data = {"storage": {"layout": {"name": "lvm"}}}
        assert validate_storage_structure(data) == []

    def test_storage_config_not_list(self):
        data = {"storage": {"config": "not-a-list"}}
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config must be a list"]

    def test_storage_config_entry_not_dict(self):
        data = {"storage": {"config": ["not-a-dict"]}}
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0] must be a mapping"]

    def test_storage_action_missing_id(self):
        data = {
            "storage": {
                "config": [
                    {"type": "disk", "path": "/dev/sda"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0].id must be a non-empty string"]

    def test_storage_action_empty_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "", "type": "disk"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert errors == ["autoinstall.storage.config[0].id must be a non-empty string"]

    def test_storage_duplicate_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk"},
                    {"id": "part-0", "type": "partition"},
                    {"id": "disk-0", "type": "disk"},
                ]
            }
        }
        errors = validate_storage_structure(data)
        assert len(errors) == 1
        assert "Duplicate storage action id 'disk-0' at autoinstall.storage.config[2]" in errors[0]
        assert "first declared at index 0" in errors[0]

    def test_valid_storage_config(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "path": "/dev/sda"},
                    {"id": "part-0", "type": "partition", "device": "disk-0", "size": 1024},
                ]
            }
        }
        assert validate_storage_structure(data) == []


class TestStorageOnlyData:
    """Tests for storage_only_data(ai_data)."""

    def test_defaults_when_empty(self):
        result = storage_only_data({})
        assert result == {
            "version": 1,
            "interactive-sections": ["identity"],
        }

    def test_preserves_version_and_interactive_sections(self):
        data = {
            "version": 2,
            "interactive-sections": ["network", "storage"],
        }
        result = storage_only_data(data)
        assert result["version"] == 2
        assert result["interactive-sections"] == ["network", "storage"]

    def test_includes_source_and_storage(self):
        data = {
            "version": 1,
            "source": {"id": "ubuntu-desktop"},
            "storage": {"layout": {"name": "direct"}},
            "identity": {"username": "admin"},
            "packages": ["vim", "curl"],
        }
        result = storage_only_data(data)
        assert "source" in result
        assert "storage" in result
        assert "identity" not in result
        assert "packages" not in result


class TestCurtinStorageInput:
    """Tests for curtin_storage_input(ai_data)."""

    def test_no_storage_returns_none(self):
        assert curtin_storage_input({}) is None

    def test_layout_only_storage_returns_none(self):
        assert curtin_storage_input({"storage": {"layout": {"name": "lvm"}}}) is None

    def test_non_list_config_returns_none(self):
        assert curtin_storage_input({"storage": {"config": "not-list"}}) is None

    def test_disk_match_removed_and_placeholder_inserted(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "match": {"size": "largest"}},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert "match" not in cfg
        assert cfg["path"] == "/dev/autoinstall-match"

    def test_disk_with_path_and_match_keeps_existing_path(self):
        data = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "path": "/dev/nvme0n1", "match": {"ssd": True}},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert "match" not in cfg
        assert cfg["path"] == "/dev/nvme0n1"

    @pytest.mark.parametrize("size_value", [-1, "100%", "50%"])
    def test_partition_size_placeholder(self, size_value):
        data = {
            "storage": {
                "config": [
                    {"id": "part-1", "type": "partition", "size": size_value},
                    {"id": "lvm-1", "type": "lvm_partition", "size": size_value},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        assert result["storage"]["config"][0]["size"] == "1B"
        assert result["storage"]["config"][1]["size"] == "1B"

    def test_partition_explicit_size_untouched(self):
        data = {
            "storage": {
                "config": [
                    {"id": "part-1", "type": "partition", "size": 1073741824},
                    {"id": "part-2", "type": "partition", "size": "10GB"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        assert result["storage"]["config"][0]["size"] == 1073741824
        assert result["storage"]["config"][1]["size"] == "10GB"

    def test_dm_crypt_defaults_dm_name_to_id(self):
        data = {
            "storage": {
                "config": [
                    {"id": "crypt-root", "type": "dm_crypt", "volume": "part-root"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert cfg["dm_name"] == "crypt-root"

    def test_dm_crypt_preserves_explicit_dm_name(self):
        data = {
            "storage": {
                "config": [
                    {"id": "crypt-root", "type": "dm_crypt", "dm_name": "custom-name"},
                ]
            }
        }
        result = curtin_storage_input(data)
        assert result is not None
        cfg = result["storage"]["config"][0]
        assert cfg["dm_name"] == "custom-name"

    def test_does_not_mutate_original_input(self):
        original = {
            "storage": {
                "config": [
                    {"id": "disk-0", "type": "disk", "match": {"size": "largest"}},
                    {"id": "part-0", "type": "partition", "size": -1},
                ]
            }
        }
        copied = copy.deepcopy(original)
        _ = curtin_storage_input(original)
        assert original == copied


class TestNestedShellScripts:
    """Extraction of `bash -c '...'` payloads that neither `-n` nor shellcheck sees."""

    def test_single_quoted_payload(self):
        assert nested_shell_scripts("curtin in-target -- bash -c 'echo hi'") == [("bash", "echo hi", 1)]

    def test_double_quoted_payload(self):
        assert nested_shell_scripts('sh -c "echo hi"') == [("sh", "echo hi", 1)]

    def test_double_quoted_payload_handles_escapes(self):
        assert nested_shell_scripts(r'sh -c "echo \"hi\""') == [("sh", 'echo "hi"', 1)]

    def test_adjacent_quoted_fragments_are_one_argument(self):
        # The shell concatenates quoted/unquoted fragments, so `bash -c 'a '@b' 'c'`
        # passes a single argument.  Stopping at the first quote would truncate it.
        script = "bash -c '\nsetup '@a' '/b'\n'"
        assert nested_shell_scripts(script) == [("bash", "\nsetup @a /b\n", 1)]

    def test_no_wrapper_returns_empty(self):
        assert nested_shell_scripts("curtin in-target -- apt-get update") == []

    def test_absolute_path_and_env_wrappers(self):
        assert nested_shell_scripts("/bin/bash -c 'true'") == [("bash", "true", 1)]
        assert nested_shell_scripts("/usr/bin/env sh -c 'true'") == [("sh", "true", 1)]

    def test_does_not_match_a_longer_word(self):
        assert nested_shell_scripts("myflush -c 'true'") == []

    def test_recursive_extraction_records_depth(self):
        script = "bash -c 'bash -c \"echo hi\"'"
        assert nested_shell_scripts(script) == [("bash", 'bash -c "echo hi"', 1), ("bash", "echo hi", 2)]

    def test_unterminated_quote_is_skipped(self):
        assert nested_shell_scripts("bash -c 'echo hi") == []

    def test_c_and_script_inside_a_larger_command(self):
        script = "true && curtin in-target -- bash -c 'echo hi' || false"
        assert nested_shell_scripts(script) == [("bash", "echo hi", 1)]


class TestCollectShellCommands:
    """Collection of checkable snippets and structural errors."""

    def test_string_entries_are_checked_as_sh(self):
        data = {"late-commands": ["apt-get update", "curtin in-target -- true"]}
        commands, errors = collect_shell_commands(data)
        assert errors == []
        assert [(c.label, c.interpreter, c.script) for c in commands] == [
            ("late-commands[0]", "sh", "apt-get update"),
            ("late-commands[1]", "sh", "curtin in-target -- true"),
        ]

    def test_all_sections_are_covered(self):
        data = {
            "early-commands": ["a"],
            "late-commands": ["b"],
            "error-commands": ["c"],
        }
        commands, errors = collect_shell_commands(data)
        assert errors == []
        assert [c.label for c in commands] == ["early-commands[0]", "late-commands[0]", "error-commands[0]"]

    def test_argv_entry_is_not_parsed_by_a_shell(self):
        data = {"late-commands": [["curtin", "in-target", "--", "true"]]}
        commands, errors = collect_shell_commands(data)
        assert commands == []
        assert errors == []

    def test_argv_entry_with_non_strings_is_reported(self):
        data = {"late-commands": [["curtin", 42]]}
        commands, errors = collect_shell_commands(data)
        assert commands == []
        assert errors == ["late-commands[0] argv entry must contain only strings (non-string items: [1])"]

    def test_non_string_entry_is_reported(self):
        data = {"late-commands": [{"not": "a command"}]}
        commands, errors = collect_shell_commands(data)
        assert commands == []
        assert errors == ["late-commands[0] must be a string or a list of strings"]

    def test_section_must_be_a_list(self):
        commands, errors = collect_shell_commands({"late-commands": "apt-get update"})
        assert commands == []
        assert errors == ["autoinstall.late-commands must be a list"]

    def test_absent_sections_are_ignored(self):
        assert collect_shell_commands({}) == ([], [])

    def test_nested_payload_gets_a_label_with_depth(self):
        data = {"late-commands": ["bash -c 'echo hi'"]}
        commands, errors = collect_shell_commands(data)
        assert errors == []
        assert [(c.label, c.interpreter, c.script) for c in commands] == [
            ("late-commands[0]", "sh", "bash -c 'echo hi'"),
            ("late-commands[0] → bash -c (depth 1)", "bash", "echo hi"),
        ]


class TestParseArgs:
    """The new scope and shellcheck flags of the in-container CLI."""

    def _parse(self, monkeypatch, tmp_path, *extra):
        config = tmp_path / "autoinstall.yaml"
        config.write_text("#cloud-config\n", encoding="utf-8")
        monkeypatch.setattr(_validator_mod.sys, "argv", ["validate-autoinstall.py", str(config), *extra])
        return _validator_mod.parse_args()

    def test_defaults(self, monkeypatch, tmp_path):
        args = self._parse(monkeypatch, tmp_path)
        assert args.validation == "full"
        assert args.shellcheck is True
        assert args.shellcheck_severity == "error"

    def test_commands_scope_and_disabled_shellcheck(self, monkeypatch, tmp_path):
        args = self._parse(
            monkeypatch,
            tmp_path,
            "--validation",
            "commands",
            "--no-shellcheck",
            "--shellcheck-severity",
            "warning",
        )
        assert args.validation == "commands"
        assert args.shellcheck is False
        assert args.shellcheck_severity == "warning"

    def test_unknown_severity_is_rejected(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit):
            self._parse(monkeypatch, tmp_path, "--shellcheck-severity", "bogus")


class TestValidateShellCommands:
    """End-to-end assembly of the command-syntax layer (subprocess mocked)."""

    def _install(self, monkeypatch, *, syntax_rc=0, syntax_err="", shellcheck_rc=0, shellcheck_out="", available=True):
        calls: list[tuple[list[str], str]] = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs["input"]))
            if cmd[0] == "shellcheck":
                return subprocess.CompletedProcess(cmd, shellcheck_rc, shellcheck_out, "")
            return subprocess.CompletedProcess(cmd, syntax_rc, "", syntax_err)

        monkeypatch.setattr(_validator_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            _validator_mod.shutil,
            "which",
            lambda name: "/usr/bin/shellcheck" if available else None,
        )
        return calls

    def test_clean_commands_pass(self, monkeypatch, capsys):
        calls = self._install(monkeypatch)
        assert validate_shell_commands({"late-commands": ["apt-get update"]}) is True
        assert "Shell command syntax is valid." in capsys.readouterr().out
        assert [cmd for cmd, _ in calls] == [
            ["sh", "-n"],
            ["shellcheck", "--shell", "sh", "--severity", "error", "--format", "gcc", "-"],
        ]

    def test_syntax_error_fails_and_prints_diagnostics(self, monkeypatch, capsys):
        self._install(monkeypatch, syntax_rc=2, syntax_err="sh: 3: Syntax error: end of file unexpected\n")
        assert validate_shell_commands({"late-commands": ["for i in 1; do"]}) is False
        err = capsys.readouterr().err
        assert "[commands] late-commands[0]: sh -n found a syntax error" in err
        assert "end of file unexpected" in err

    def test_nested_payload_is_checked_with_its_own_interpreter(self, monkeypatch):
        calls = self._install(monkeypatch)
        validate_shell_commands({"late-commands": ["bash -c 'echo hi'"]})
        assert [cmd for cmd, _ in calls] == [
            ["sh", "-n"],
            ["shellcheck", "--shell", "sh", "--severity", "error", "--format", "gcc", "-"],
            ["bash", "-n"],
            ["shellcheck", "--shell", "bash", "--severity", "error", "--format", "gcc", "-"],
        ]
        assert calls[2][1] == "echo hi"

    def test_shellcheck_can_be_disabled(self, monkeypatch, capsys):
        calls = self._install(monkeypatch)
        assert validate_shell_commands({"late-commands": ["apt-get update"]}, use_shellcheck=False) is True
        assert [cmd for cmd, _ in calls] == [["sh", "-n"]]
        assert "shellcheck" not in capsys.readouterr().err

    def test_missing_shellcheck_warns_and_degrades(self, monkeypatch, capsys):
        calls = self._install(monkeypatch, available=False)
        assert validate_shell_commands({"late-commands": ["apt-get update"]}) is True
        assert [cmd for cmd, _ in calls] == [["sh", "-n"]]
        assert "shellcheck not found" in capsys.readouterr().err

    def test_shellcheck_findings_fail_validation(self, monkeypatch, capsys):
        self._install(
            monkeypatch, shellcheck_rc=1, shellcheck_out="-:1:1: error: unterminated here document [SC1044]\n"
        )
        assert validate_shell_commands({"late-commands": ["cat << EOF"]}) is False
        assert "shellcheck (error) reported" in capsys.readouterr().err

    def test_severity_is_forwarded(self, monkeypatch):
        calls = self._install(monkeypatch)
        validate_shell_commands({"late-commands": ["true"]}, severity="warning")
        assert "--severity" in calls[1][0]
        assert calls[1][0][calls[1][0].index("--severity") + 1] == "warning"

    def test_structural_errors_fail_even_without_commands(self, monkeypatch, capsys):
        self._install(monkeypatch)
        assert validate_shell_commands({"late-commands": [42]}) is False
        assert "Structure error" in capsys.readouterr().err


class TestCollectAptSources:
    """Structural validation of autoinstall.apt.sources."""

    def test_no_apt_section(self):
        assert collect_apt_sources(None) == ([], [])

    def test_apt_not_a_mapping(self):
        sources, errors = collect_apt_sources("ppa:owner/name")
        assert sources == []
        assert errors == ["autoinstall.apt must be a mapping"]

    def test_sources_not_a_mapping(self):
        sources, errors = collect_apt_sources({"sources": ["deb https://example.invalid/ubuntu noble main"]})
        assert sources == []
        assert errors == ["autoinstall.apt.sources must be a mapping"]

    def test_entry_not_a_mapping(self):
        sources, errors = collect_apt_sources({"sources": {"x": "deb https://example.invalid/ubuntu noble main"}})
        assert sources == []
        assert errors == ["apt.sources['x'] must be a mapping"]

    def test_empty_source_is_rejected(self):
        sources, errors = collect_apt_sources({"sources": {"x": {"source": "   "}}})
        assert sources == []
        assert errors == ["apt.sources['x'].source must be a non-empty string"]

    def test_entry_without_source_or_key_is_rejected(self):
        sources, errors = collect_apt_sources({"sources": {"x": {}}})
        assert sources == []
        assert errors == ["apt.sources['x'] must declare 'source', 'key' or 'keyid'"]

    def test_key_must_be_a_string(self):
        sources, errors = collect_apt_sources({"sources": {"x": {"key": 42}}})
        assert sources == []
        assert errors == ["apt.sources['x'].key must be a string"]

    def test_ppa_source_is_marked_abbreviated(self):
        sources, errors = collect_apt_sources({"sources": {"k": {"source": "ppa:phoerious/keepassxc"}}})
        assert errors == []
        assert sources == [AptSource("apt.sources['k']", "k", "ppa:phoerious/keepassxc", None, None, None)]
        assert abbreviated_source(sources[0].source) is True

    def test_deb822_source_is_not_abbreviated(self):
        stanza = "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: $RELEASE\n"
        sources, errors = collect_apt_sources({"sources": {"docker": {"source": stanza}}})
        assert errors == []
        assert abbreviated_source(sources[0].source) is False

    def test_key_and_keyserver_are_collected(self):
        entry = {
            "source": "deb https://example.invalid/ubuntu noble main",
            "keyid": "DEADBEEF",
            "keyserver": "keys.example",
        }
        sources, errors = collect_apt_sources({"sources": {"k": entry}})
        assert errors == []
        assert sources == [AptSource("apt.sources['k']", "k", entry["source"], None, "DEADBEEF", "keys.example")]


class TestCollectPackages:
    """Structural validation of autoinstall.packages."""

    def test_no_packages(self):
        assert collect_packages({}) == ([], [])

    def test_packages_not_a_list(self):
        assert collect_packages({"packages": "git"}) == ([], ["autoinstall.packages must be a list"])

    def test_non_string_entry_is_reported(self):
        assert collect_packages({"packages": ["git", 42]}) == (["git"], ["packages[1] must be a non-empty string"])

    def test_names_are_stripped(self):
        assert collect_packages({"packages": [" git ", "curl"]}) == (["git", "curl"], [])


class _FakeHttpResponse:
    """Minimal context-manager response for urllib.request.urlopen."""

    def __init__(self, payload: str):
        self._data = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _install_http(monkeypatch, responses: dict[str, str]) -> None:
    """Serve urlopen responses by URL substring; unknown URLs raise HTTP 404."""

    def fake_urlopen(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        for fragment, payload in responses.items():
            if fragment in url:
                return _FakeHttpResponse(payload)
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(_validator_mod.urllib.request, "urlopen", fake_urlopen)


class _FakeUbuntuDistroInfo:
    """Stand-in for distro_info.UbuntuDistroInfo (not installed on the host)."""

    _versions: ClassVar[dict[str, str]] = {"noble": "24.04 LTS", "resolute": "26.04 LTS", "stonking": "26.10"}

    @property
    def all(self):
        return list(self._versions)

    def version(self, codename):
        return self._versions.get(codename)


def _install_distro_info(monkeypatch) -> None:
    module = types.ModuleType("distro_info")
    module.UbuntuDistroInfo = _FakeUbuntuDistroInfo
    monkeypatch.setitem(sys.modules, "distro_info", module)


class TestTargetCodename:
    """Mapping of an Ubuntu release number to its codename via distro-info."""

    def test_maps_a_known_release(self, monkeypatch):
        _install_distro_info(monkeypatch)
        assert target_codename("24.04") == "noble"
        assert target_codename("26.04") == "resolute"

    def test_unknown_release_returns_none(self, monkeypatch):
        _install_distro_info(monkeypatch)
        assert target_codename("99.04") is None

    def test_missing_release_returns_none(self):
        assert target_codename(None) is None


class TestRetargetDefaultSources:
    """Pointing the container's own archive sources at the target release."""

    def _install(self, monkeypatch, tmp_path, *, container="noble"):
        source = tmp_path / "ubuntu.sources"
        source.write_text("Suites: noble noble-updates noble-security\n", encoding="utf-8")
        monkeypatch.setattr(_validator_mod, "UBUNTU_SOURCES_FILES", (source,))
        monkeypatch.setattr(_validator_mod, "container_codename", lambda: container)
        return source

    def test_container_release_is_rewritten(self, monkeypatch, tmp_path):
        source = self._install(monkeypatch, tmp_path)

        assert retarget_default_sources("resolute") == []

        assert source.read_text(encoding="utf-8") == "Suites: resolute resolute-updates resolute-security\n"

    def test_same_release_is_left_alone(self, monkeypatch, tmp_path):
        source = self._install(monkeypatch, tmp_path)

        assert retarget_default_sources("noble") == []

        assert source.read_text(encoding="utf-8") == "Suites: noble noble-updates noble-security\n"

    def test_unknown_container_release_is_a_noop(self, monkeypatch, tmp_path):
        source = self._install(monkeypatch, tmp_path, container=None)

        assert retarget_default_sources("resolute") == []

        assert source.read_text(encoding="utf-8") == "Suites: noble noble-updates noble-security\n"

    def test_missing_file_is_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_validator_mod, "UBUNTU_SOURCES_FILES", (tmp_path / "absent",))
        monkeypatch.setattr(_validator_mod, "container_codename", lambda: "noble")

        assert retarget_default_sources("resolute") == []


class TestResolvePpa:
    """Expansion of a `ppa:owner/name` source through Launchpad and the keyserver."""

    def test_existing_ppa_returns_line_and_key(self, monkeypatch):
        _install_http(
            monkeypatch,
            {
                "api.launchpad.net": json.dumps({"signing_key_fingerprint": "ABCDEF"}),
                "keyserver.ubuntu.com": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nkey\n-----END PGP PUBLIC KEY BLOCK-----\n",
            },
        )

        line, key = resolve_ppa("ppa:phoerious/keepassxc", "resolute")

        assert line == "deb https://ppa.launchpadcontent.net/phoerious/keepassxc/ubuntu resolute main"
        assert key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")

    def test_missing_ppa_raises(self, monkeypatch):
        _install_http(monkeypatch, {})
        with pytest.raises(ValueError, match="does not exist on Launchpad"):
            resolve_ppa("ppa:nobody/nothing", "resolute")

    def test_invalid_shorthand_raises(self):
        with pytest.raises(ValueError, match="not a valid"):
            resolve_ppa("ppa:onlyowner", "resolute")

    def test_missing_fingerprint_raises(self, monkeypatch):
        _install_http(monkeypatch, {"api.launchpad.net": "{}"})
        with pytest.raises(ValueError, match="no signing key"):
            resolve_ppa("ppa:owner/name", "resolute")


class TestApplyAptSources:
    """Writing sources and signing keys into the container's apt configuration."""

    def _dirs(self, monkeypatch, tmp_path):
        sources_dir = tmp_path / "sources.list.d"
        keyring_dir = tmp_path / "trusted.gpg.d"
        monkeypatch.setattr(_validator_mod, "APT_SOURCES_DIR", sources_dir)
        monkeypatch.setattr(_validator_mod, "APT_KEYRING_DIR", keyring_dir)
        return sources_dir, keyring_dir

    def test_deb_line_is_written_and_release_expanded(self, monkeypatch, tmp_path):
        sources_dir, _ = self._dirs(monkeypatch, tmp_path)
        entry = AptSource("apt.sources['d']", "d", "deb https://example.invalid/ubuntu $RELEASE main", None, None, None)

        assert apply_apt_sources([entry], "resolute") == []

        assert (sources_dir / "d.list").read_text() == "deb https://example.invalid/ubuntu resolute main\n"

    def test_deb822_stanza_uses_the_sources_extension(self, monkeypatch, tmp_path):
        sources_dir, _ = self._dirs(monkeypatch, tmp_path)
        stanza = "Types: deb\nURIs: https://example.invalid/ubuntu\nSuites: $RELEASE\n"
        entry = AptSource("apt.sources['d']", "d", stanza, None, None, None)

        assert apply_apt_sources([entry], "resolute") == []

        assert (sources_dir / "d.sources").read_text() == stanza.replace("$RELEASE", "resolute")

    def test_armored_key_is_written_next_to_the_source(self, monkeypatch, tmp_path):
        _, keyring_dir = self._dirs(monkeypatch, tmp_path)
        key = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----"
        entry = AptSource("apt.sources['d']", "d", "deb https://example.invalid/ubuntu noble main", key, None, None)

        assert apply_apt_sources([entry], "noble") == []

        assert (keyring_dir / "d.asc").read_text() == key + "\n"

    def test_non_armored_key_is_rejected(self, monkeypatch, tmp_path):
        self._dirs(monkeypatch, tmp_path)
        entry = AptSource("apt.sources['d']", "d", None, "not-a-key", None, None)

        errors = apply_apt_sources([entry], "noble")

        assert errors == [
            "apt.sources['d']: only armored PGP keys ('-----BEGIN PGP PUBLIC KEY BLOCK-----') are supported"
        ]

    def test_unsupported_shorthand_is_reported(self, monkeypatch, tmp_path):
        self._dirs(monkeypatch, tmp_path)
        entry = AptSource("apt.sources['c']", "c", "cloud-archive:victoria", None, None, None)

        errors = apply_apt_sources([entry], "noble")

        assert errors == [
            "apt.sources['c']: 'cloud-archive:victoria' is a repository shorthand this layer cannot resolve"
        ]

    def test_key_only_entry_writes_no_source(self, monkeypatch, tmp_path):
        sources_dir, keyring_dir = self._dirs(monkeypatch, tmp_path)
        key = "-----BEGIN PGP PUBLIC KEY BLOCK-----\nabc\n-----END PGP PUBLIC KEY BLOCK-----"
        entry = AptSource("apt.sources['k']", "k", None, key, None, None)

        assert apply_apt_sources([entry], "noble") == []

        assert (keyring_dir / "k.asc").exists()
        assert list(sources_dir.glob("*")) == []


class TestSkippedAptKeys:
    """Reporting of the autoinstall.apt keys the layer cannot check."""

    def test_reports_present_subiquity_keys(self):
        assert skipped_apt_keys({"geoip": True, "sources": {}}) == ["geoip"]

    def test_absent_keys_or_invalid_apt(self):
        assert skipped_apt_keys(None) == []
        assert skipped_apt_keys({"sources": {}}) == []


class TestValidateApt:
    """End-to-end assembly of the apt layer (filesystem and subprocess mocked)."""

    def _install(self, monkeypatch, tmp_path, *, update_rc=0, update_out="", install_rc=0, install_out=""):
        monkeypatch.setattr(_validator_mod, "APT_SOURCES_DIR", tmp_path / "sources.list.d")
        monkeypatch.setattr(_validator_mod, "APT_KEYRING_DIR", tmp_path / "trusted.gpg.d")
        monkeypatch.setattr(_validator_mod, "target_codename", lambda release: "resolute" if release else None)
        monkeypatch.setattr(_validator_mod, "retarget_default_sources", lambda suite: [])
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["apt-get", "update"]:
                return subprocess.CompletedProcess(cmd, update_rc, update_out, "")
            return subprocess.CompletedProcess(cmd, install_rc, install_out, "")

        monkeypatch.setattr(_validator_mod.subprocess, "run", fake_run)
        return calls

    def test_no_apt_and_no_packages_is_clean(self, monkeypatch, tmp_path, capsys):
        calls = self._install(monkeypatch, tmp_path)
        assert validate_apt({}, "26.04") is True
        assert calls == []
        assert "nothing to check" in capsys.readouterr().out

    def test_packages_are_checked_after_apt_update(self, monkeypatch, tmp_path):
        calls = self._install(monkeypatch, tmp_path)

        assert validate_apt({"packages": ["git", "curl"]}, "26.04") is True

        assert calls[0][:2] == ["apt-get", "update"]
        assert calls[1][:2] == ["apt-get", "install"]
        assert calls[1][-2:] == ["git", "curl"]

    def test_sources_are_written_before_the_update(self, monkeypatch, tmp_path, capsys):
        calls = self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(
            _validator_mod, "resolve_ppa", lambda source, suite: ("deb https://ppa.example/x/y resolute main", "KEY")
        )

        assert validate_apt({"apt": {"sources": {"k": {"source": "ppa:owner/name"}}}}, "26.04") is True

        assert (tmp_path / "sources.list.d" / "k.list").read_text() == "deb https://ppa.example/x/y resolute main\n"
        assert calls[0][:2] == ["apt-get", "update"]
        assert "APT repositories and packages are valid." in capsys.readouterr().out

    def test_unknown_release_fails_without_touching_apt(self, monkeypatch, tmp_path, capsys):
        calls = self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(_validator_mod, "target_codename", lambda release: None)
        data = {"apt": {"sources": {"k": {"source": "deb https://example.invalid/ubuntu $RELEASE main"}}}}

        assert validate_apt(data, None) is False

        assert calls == []
        assert "Could not map the target release" in capsys.readouterr().err

    def test_failing_update_fails_validation(self, monkeypatch, tmp_path, capsys):
        self._install(monkeypatch, tmp_path, update_rc=100)

        assert validate_apt({"packages": ["git"]}, "26.04") is False

        assert "apt-get update failed" in capsys.readouterr().err

    def test_missing_package_fails_and_is_named(self, monkeypatch, tmp_path, capsys):
        self._install(monkeypatch, tmp_path, install_rc=100, install_out="E: Unable to locate package nope\n")

        assert validate_apt({"packages": ["nope"]}, "26.04") is False

        assert "not available in the configured repositories: nope" in capsys.readouterr().err

    def test_package_without_a_candidate_is_reported(self, monkeypatch, tmp_path, capsys):
        self._install(
            monkeypatch, tmp_path, install_rc=100, install_out="E: Package 'foo' has no installation candidate\n"
        )

        assert validate_apt({"packages": ["foo"]}, "26.04") is False

        assert "not available in the configured repositories: foo" in capsys.readouterr().err

    def test_structural_error_skips_everything(self, monkeypatch, tmp_path, capsys):
        calls = self._install(monkeypatch, tmp_path)

        assert validate_apt({"apt": {"sources": {"k": {}}}}, "26.04") is False

        assert calls == []
        captured = capsys.readouterr()
        assert "Structure error" in captured.err
        assert "[apt] APT validation failed." in captured.out

    def test_subiquity_only_keys_are_announced(self, monkeypatch, tmp_path, capsys):
        self._install(monkeypatch, tmp_path)
        monkeypatch.setattr(
            _validator_mod, "resolve_ppa", lambda source, suite: ("deb https://ppa.example/x/y resolute main", "KEY")
        )

        validate_apt({"apt": {"geoip": True, "sources": {"k": {"source": "ppa:a/b"}}}}, "26.04")

        assert "'apt.geoip' is resolved by Subiquity itself" in capsys.readouterr().err
