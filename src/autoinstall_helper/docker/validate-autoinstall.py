#!/usr/bin/env python3
"""Run Subiquity autoinstall, Curtin storage and shell command validation."""

import argparse
import asyncio
import copy
import importlib.util
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CURTIN_ROOT = ROOT / "curtin"
SUBIQUITY_VALIDATOR_PATH = Path(__file__).with_name("validate-autoinstall-user-data.py")

# Autoinstall keys holding shell commands, in execution order.
COMMAND_SECTIONS = ("early-commands", "late-commands", "error-commands")

# Subiquity executes a string command as `["sh", "-c", command]`
# (subiquity/server/controllers/cmdlist.py) and a list command directly as
# argv, without a shell.  Only the string form is parsed by a shell, so that is
# the dialect the syntax check must use.
COMMAND_INTERPRETER = "sh"

# Kept in sync with SHELLCHECK_SEVERITIES in autoinstall_helper/docker_utils.py.
SHELLCHECK_SEVERITIES = ("error", "warning", "info", "style")

# Interpreters whose `-c` argument is a shell script we can check on its own.
_SHELL_INTERPRETERS = ("bash", "sh")

# Depth limit for the recursive `-c` extraction; real configurations never nest
# deeply, and the bound keeps a pathological input from looping forever.
_NESTED_MAX_DEPTH = 4


def load_subiquity_validator():
    spec = importlib.util.spec_from_file_location("subiquity_user_data_validator", SUBIQUITY_VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load Subiquity's autoinstall validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_storage_structure(ai_data: dict[str, Any]) -> list[str]:
    """Check structural invariants that tools cannot catch gracefully.

    Validates only things that are:
    - tool-agnostic (no hardcoded Subiquity/Curtin version-specific lists), and
    - not checked cleanly by either subiquity dry-run or curtin schema-validate.

    Specifically, duplicate or missing action ids cause both tools to either
    crash with an opaque KeyError or silently produce wrong results, so we
    catch those early with a clear message.
    """
    storage = ai_data.get("storage")
    if storage is None:
        return []
    if not isinstance(storage, dict):
        return ["autoinstall.storage must be a mapping"]

    actions = storage.get("config")
    if actions is None:
        # layout-based or empty storage — subiquity handles it
        return []
    if not isinstance(actions, list):
        return ["autoinstall.storage.config must be a list"]

    errors = []
    seen: dict[str, int] = {}
    for index, action in enumerate(actions):
        prefix = f"autoinstall.storage.config[{index}]"
        if not isinstance(action, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id:
            errors.append(f"{prefix}.id must be a non-empty string")
        elif action_id in seen:
            errors.append(
                f"Duplicate storage action id {action_id!r} at {prefix} (first declared at index {seen[action_id]})"
            )
        else:
            seen[action_id] = index

    return errors


def storage_only_data(ai_data: dict[str, Any]) -> dict[str, Any]:
    """Keep the context Subiquity needs while avoiding unrelated sections."""
    data = {
        "version": ai_data.get("version", 1),
        "interactive-sections": ai_data.get("interactive-sections", ["identity"]),
    }
    for key in ("source", "storage"):
        if key in ai_data:
            data[key] = ai_data[key]
    return data


def curtin_storage_input(ai_data: dict[str, Any]) -> dict[str, Any] | None:
    """Project explicit autoinstall actions into Curtin's static schema shape.

    Subiquity extends the Curtin storage config format with a few constructs
    that Curtin does not understand.  This function strips or normalises those
    extensions so the result can be passed directly to `curtin schema-validate`.

    Transformations applied:
    - disk.match is removed; if no concrete identity remains, a placeholder
      path is inserted so Curtin's required-field check still passes.
    - partition/lvm_partition size=-1 and size=N% are replaced with a valid
      literal size ("1B") because Curtin's schema requires size >= 1.
    - dm_crypt.dm_name is filled in from the action id when absent, matching
      Curtin's own defaulting behaviour.
    """
    storage = ai_data.get("storage")
    if not isinstance(storage, dict) or "config" not in storage:
        return None

    actions = copy.deepcopy(storage["config"])
    if not isinstance(actions, list):
        return None

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if action_type == "disk":
            action.pop("match", None)
            if not any(key in action for key in ("path", "serial", "wwn")):
                # Disk identity is resolved by Subiquity against installer probes.
                action["path"] = "/dev/autoinstall-match"
        if action_type in ("partition", "lvm_partition"):
            size = action.get("size")
            if size == -1 or (isinstance(size, str) and size.endswith("%")):
                # These Subiquity sizes need disk geometry; use a schema-only value.
                action["size"] = "1B"
        if action_type == "dm_crypt" and "dm_name" not in action:
            action["dm_name"] = action.get("id")

    return {"storage": {"version": 1, "config": actions}}


@dataclass(frozen=True)
class ShellCommand:
    """One shell snippet to check, plus the path it came from in the config."""

    label: str
    interpreter: str
    script: str


def _shell_words(command: str) -> list[str]:
    """Split a command the way the outer POSIX shell would (quote removal included).

    ``shlex`` models exactly what matters here: adjacent quoted and unquoted
    fragments concatenate into a single argument, and a failing split (unbalanced
    quotes) is reported to the caller.
    """
    lexer = shlex.shlex(command, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def nested_shell_scripts(script: str, depth: int = 1) -> list[tuple[str, str, int]]:
    """Return the ``(interpreter, script, depth)`` payloads embedded via ``-c``.

    ``sh -n`` and shellcheck only parse the outer command; the script passed to
    ``bash -c '...'`` is a single argument to them, so a syntax error inside it
    stays invisible unless it is extracted and checked separately.  The argument
    is taken from a real POSIX word split (not a quote-boundary heuristic), so a
    payload assembled from several quoted fragments is reassembled correctly.
    """
    found: list[tuple[str, str, int]] = []
    try:
        words = _shell_words(script)
    except ValueError:
        # Unbalanced quotes are the outer `sh -n` / `bash -n` check's job.
        return found
    for index, word in enumerate(words):
        if word != "-c" or index == 0 or index + 1 >= len(words):
            continue
        interpreter = words[index - 1].rpartition("/")[2]
        if interpreter not in _SHELL_INTERPRETERS:
            continue
        payload = words[index + 1]
        found.append((interpreter, payload, depth))
        if depth < _NESTED_MAX_DEPTH:
            found.extend(nested_shell_scripts(payload, depth + 1))
    return found


def collect_shell_commands(ai_data: dict[str, Any]) -> tuple[list[ShellCommand], list[str]]:
    """Collect every shell snippet to check and any structural errors.

    String entries are executed as ``sh -c`` and get both the outer snippet and
    every nested ``-c`` payload.  List entries are executed directly as argv
    (no shell), so only the element types are validated.
    """
    commands: list[ShellCommand] = []
    errors: list[str] = []
    for section in COMMAND_SECTIONS:
        entries = ai_data.get(section)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append(f"autoinstall.{section} must be a list")
            continue
        for index, entry in enumerate(entries):
            prefix = f"{section}[{index}]"
            if isinstance(entry, list):
                invalid = [i for i, item in enumerate(entry) if not isinstance(item, str)]
                if invalid:
                    errors.append(f"{prefix} argv entry must contain only strings (non-string items: {invalid})")
                continue
            if not isinstance(entry, str):
                errors.append(f"{prefix} must be a string or a list of strings")
                continue
            commands.append(ShellCommand(prefix, COMMAND_INTERPRETER, entry))
            for interpreter, payload, depth in nested_shell_scripts(entry):
                commands.append(ShellCommand(f"{prefix} → {interpreter} -c (depth {depth})", interpreter, payload))
    return commands, errors


def _print_diagnostics(text: str) -> None:
    """Print a tool's output indented under the command it belongs to."""
    for line in text.splitlines():
        if line.strip():
            print(f"    {line}", file=sys.stderr)


def _run_shell(command: ShellCommand, argv: list[str]) -> subprocess.CompletedProcess:
    """Run one checker on the snippet, feeding the script on stdin."""
    return subprocess.run(argv, input=command.script, text=True, capture_output=True, check=False)


def check_shell_syntax(command: ShellCommand) -> bool:
    """Run ``sh -n`` / ``bash -n`` on one snippet; report diagnostics on failure."""
    result = _run_shell(command, [command.interpreter, "-n"])
    if result.returncode == 0:
        return True
    print(f"[commands] {command.label}: {command.interpreter} -n found a syntax error", file=sys.stderr)
    _print_diagnostics(result.stderr)
    return False


def run_shellcheck(command: ShellCommand, severity: str) -> bool:
    """Run shellcheck on one snippet; report findings at or above ``severity``."""
    result = _run_shell(
        command,
        ["shellcheck", "--shell", command.interpreter, "--severity", severity, "--format", "gcc", "-"],
    )
    if result.returncode == 0:
        return True
    print(f"[commands] {command.label}: shellcheck ({severity}) reported:", file=sys.stderr)
    _print_diagnostics(result.stdout)
    return False


def validate_shell_commands(ai_data: dict[str, Any], *, use_shellcheck: bool = True, severity: str = "error") -> bool:
    """Check the syntax of every early/late/error command; return True when clean.

    This is a pure syntax check: commands are never executed and their runtime
    context (``curtin in-target``, installed binaries, ``/target``) is not
    simulated, so only parsing mistakes are reported.
    """
    commands, errors = collect_shell_commands(ai_data)
    for error in errors:
        print(f"[commands] Structure error: {error}", file=sys.stderr)

    shellcheck_available = shutil.which("shellcheck") is not None
    if use_shellcheck and not shellcheck_available:
        print("[commands] shellcheck not found; running syntax checks only.", file=sys.stderr)

    print(f"[commands] Checking {len(commands)} command(s) in {', '.join(COMMAND_SECTIONS)}...")
    was_clean = not errors
    for command in commands:
        if not check_shell_syntax(command):
            was_clean = False
        if use_shellcheck and shellcheck_available and not run_shellcheck(command, severity):
            was_clean = False

    print("[commands] Shell command syntax is valid." if was_clean else "[commands] Shell command validation failed.")
    return was_clean


async def validate(
    ai_data: dict[str, Any],
    mode: str,
    verbosity: int,
    validator,
    *,
    use_shellcheck: bool = True,
    shellcheck_severity: str = "error",
) -> int:
    check_storage = mode in ("storage", "full")
    check_commands = mode in ("commands", "full")

    # The command layer is independent of Subiquity/Curtin, so it is the only
    # check run for `--validation commands`.
    if mode == "commands":
        clean = validate_shell_commands(ai_data, use_shellcheck=use_shellcheck, severity=shellcheck_severity)
        return 0 if clean else 1

    commands_clean = (
        validate_shell_commands(ai_data, use_shellcheck=use_shellcheck, severity=shellcheck_severity)
        if check_commands
        else True
    )

    structural_errors = validate_storage_structure(ai_data) if check_storage else []

    for error in structural_errors:
        print(f"Storage structure error: {error}", file=sys.stderr)

    app = await validator.make_app()
    validation_data = storage_only_data(ai_data) if mode == "storage" else ai_data
    with tempfile.TemporaryDirectory() as td:
        config_path = Path(td) / "autoinstall.yaml"
        config_path.write_text(yaml.safe_dump(validation_data), encoding="utf-8")
        print(f"[{mode}] Running Subiquity dry-run validation...")
        subiquity_result = await validator.verify_autoinstall(
            app=app,
            cfg_path=str(config_path),
            verbosity=verbosity,
        )

        if subiquity_result != 0:
            return 1
        if not commands_clean:
            return 1
        if not check_storage:
            return 0
        if structural_errors:
            print("[storage] Curtin validation skipped due to structural errors.")
            return 1

        rendered = curtin_storage_input(ai_data)
        if rendered is None:
            if isinstance(ai_data.get("storage"), dict) and "layout" in ai_data["storage"]:
                reason = "layout-based storage is expanded by Subiquity at install time"
            else:
                reason = "there is no explicit storage.config action list"
            print(f"[storage] Curtin schema validation skipped: {reason}.")
            return 0

        curtin_config_path = Path(td) / "curtin-storage.yaml"
        curtin_config_path.write_text(yaml.safe_dump(rendered), encoding="utf-8")
        command = [
            str(CURTIN_ROOT / "bin" / "curtin"),
            "schema-validate",
            "--storage",
            "--config",
            str(curtin_config_path),
        ]
        print("[storage] Running Curtin schema validation...")
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            print(f"[storage] Curtin schema validation failed ({result.returncode}).")
            return 1

    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=argparse.FileType("r"), default="-")
    parser.add_argument(
        "--validation",
        choices=("autoinstall", "storage", "commands", "full"),
        default="full",
        help="Validation scope (default: full)",
    )
    parser.add_argument(
        "--no-shellcheck",
        dest="shellcheck",
        action="store_false",
        default=True,
        help="Skip shellcheck (the sh -n / bash -n syntax check always runs)",
    )
    parser.add_argument(
        "--shellcheck-severity",
        choices=SHELLCHECK_SEVERITIES,
        default="error",
        help="Minimum shellcheck severity that fails validation (default: error)",
    )
    parser.add_argument(
        "--no-expect-cloudconfig",
        dest="expect_cloudconfig",
        action="store_false",
        default=True,
        help="Assume the data is not wrapped in cloud-config.",
    )
    parser.add_argument("-v", "--verbosity", action="count", default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validator = load_subiquity_validator()
    try:
        ai_data = validator.parse_autoinstall(args.input.read(), args.expect_cloudconfig)
        if not isinstance(ai_data, dict):
            raise TypeError("autoinstall data must be a mapping")
    except Exception as exc:
        print(f"FAILURE: {exc}", file=sys.stderr)
        return 1

    try:
        # Note: asyncio.run is required because Subiquity's validator API
        # (make_app and verify_autoinstall) consists of async coroutines.
        return asyncio.run(
            validate(
                ai_data,
                args.validation,
                args.verbosity,
                validator,
                use_shellcheck=args.shellcheck,
                shellcheck_severity=args.shellcheck_severity,
            )
        )
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
