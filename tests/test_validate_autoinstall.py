"""Unit tests for pure validation and transformation functions in docker/validate-autoinstall.py."""

import copy
import importlib.util
import subprocess
from pathlib import Path

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
