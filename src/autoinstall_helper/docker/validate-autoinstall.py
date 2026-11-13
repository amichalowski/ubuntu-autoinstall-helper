#!/usr/bin/env python3
"""Run Subiquity autoinstall, Curtin storage, shell command and APT validation."""

import argparse
import asyncio
import copy
import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
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

# autoinstall.apt keys that Subiquity resolves itself and that have no equivalent
# in the apt layer, so it cannot apply or check them.
SUBIQUITY_ONLY_APT_KEYS = ("mirror-selection", "geoip")

# Where apt reads extra repositories and their trusted signing keys from.
APT_SOURCES_DIR = Path("/etc/apt/sources.list.d")
APT_KEYRING_DIR = Path("/etc/apt/trusted.gpg.d")

# The container's own Ubuntu archive sources.  They point at the release the
# image is built on, which is older than the release being installed.
UBUNTU_SOURCES_FILES = (Path("/etc/apt/sources.list.d/ubuntu.sources"), Path("/etc/apt/sources.list"))

# Launchpad exposes one resource per PPA, including its signing key fingerprint;
# the key itself is fetched from the keyserver, as `add-apt-repository` does.
LAUNCHPAD_PPA_URL = "https://api.launchpad.net/1.0/~{owner}/+archive/ubuntu/{name}"
LAUNCHPAD_PPA_SOURCE = "deb https://ppa.launchpadcontent.net/{owner}/{name}/ubuntu {suite} main"
KEYSERVER_KEY_URL = "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x{fingerprint}"
DEFAULT_KEYSERVER = "keyserver.ubuntu.com"
HTTP_TIMEOUT_S = 30

# curtin hands a source matching this pattern to `add-apt-repository` (e.g.
# `ppa:owner/name` or `cloud-archive:name`); kept in sync with ADD_APT_REPO_MATCH
# in curtin/commands/apt_config.py.
ABBREVIATED_SOURCE_PATTERN = re.compile(r"^[\w-]+:\w")

# The only shorthand this layer resolves itself.
_PPA_PATTERN = re.compile(r"^ppa:([^/\s]+)/([^/\s]+)$")

# apt reports a package it cannot find with one of these messages.
_MISSING_PACKAGE_PATTERNS = (
    re.compile(r"Unable to locate package (\S+)"),
    re.compile(r"Package '([^']+)' has no installation candidate"),
)

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


@dataclass(frozen=True)
class AptSource:
    """One `apt.sources` entry to check, plus the path it came from in the config."""

    label: str
    name: str
    source: str | None
    key: str | None
    keyid: str | None
    keyserver: str | None


def abbreviated_source(source: str) -> bool:
    """Whether a source is a shorthand (e.g. ``ppa:owner/name``) needing expansion."""
    return bool(ABBREVIATED_SOURCE_PATTERN.match(source))


def collect_apt_sources(apt: Any) -> tuple[list[AptSource], list[str]]:
    """Collect every `apt.sources` entry to check and any structural errors.

    ``apt`` is the raw value of ``autoinstall.apt`` from the YAML file.  The
    entries are validated here so a malformed one is reported clearly instead of
    being written to the container's apt configuration.
    """
    if apt is None:
        return [], []
    if not isinstance(apt, dict):
        return [], ["autoinstall.apt must be a mapping"]

    sources = apt.get("sources")
    if sources is None:
        return [], []
    if not isinstance(sources, dict):
        return [], ["autoinstall.apt.sources must be a mapping"]

    collected: list[AptSource] = []
    errors: list[str] = []
    for name, entry in sources.items():
        prefix = f"apt.sources[{name!r}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        source = entry.get("source")
        if source is not None and (not isinstance(source, str) or not source.strip()):
            errors.append(f"{prefix}.source must be a non-empty string")
            continue
        if source is None and "key" not in entry and "keyid" not in entry:
            errors.append(f"{prefix} must declare 'source', 'key' or 'keyid'")
            continue
        malformed = False
        for key in ("key", "keyid", "keyserver"):
            value = entry.get(key)
            if value is not None and not isinstance(value, str):
                errors.append(f"{prefix}.{key} must be a string")
                malformed = True
        if malformed:
            # Do not collect an entry whose fields failed their type check, so a
            # later stage can never receive a non-string key or keyserver.
            continue
        collected.append(
            AptSource(
                label=prefix,
                name=str(name),
                source=source,
                key=entry.get("key"),
                keyid=entry.get("keyid"),
                keyserver=entry.get("keyserver"),
            )
        )
    return collected, errors


def collect_packages(ai_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Collect every `packages:` entry to check and any structural errors."""
    entries = ai_data.get("packages")
    if entries is None:
        return [], []
    if not isinstance(entries, list):
        return [], ["autoinstall.packages must be a list"]

    names: list[str] = []
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, str) or not entry.strip():
            errors.append(f"packages[{index}] must be a non-empty string")
            continue
        names.append(entry.strip())
    return names, errors


def skipped_apt_keys(apt: Any) -> list[str]:
    """Return the autoinstall.apt keys the apt layer cannot check itself."""
    if not isinstance(apt, dict):
        return []
    return [key for key in SUBIQUITY_ONLY_APT_KEYS if key in apt]


def target_codename(release: str | None) -> str | None:
    """Map an Ubuntu release number (e.g. ``26.04``) to its codename.

    The validator image is built on the Subiquity snap base, which is older than
    the release being installed (the 26.04 installer runs on a 24.04 base), so
    the target series cannot be read from the container itself.  ``distro-info``
    ships the full release table (python3-distro-info is already in the image).
    """
    if not release:
        return None
    # Lazy import: distro-info only exists inside the validator container.
    from distro_info import UbuntuDistroInfo  # type: ignore[import-untyped]

    info = UbuntuDistroInfo()
    for codename in info.all:
        if (info.version(codename) or "").split()[0] == release:
            return codename
    return None


def _http_get(url: str) -> str:
    """Fetch a URL and decode the response as UTF-8."""
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as response:
        return response.read().decode("utf-8")


def container_codename() -> str | None:
    """Return the codename of the release the validator container itself runs."""
    try:
        content = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("UBUNTU_CODENAME", "VERSION_CODENAME"):
            codename = value.strip().strip('"')
            if codename:
                return codename
    return None


def retarget_default_sources(suite: str) -> list[str]:
    """Point the container's own Ubuntu sources at the release being installed.

    The validator image is built on the Subiquity snap base, which is older than
    the target release (the 26.04 installer runs on a 24.04 base).  Without this
    the default archive would answer for the wrong release, so a package that
    exists only in the target release would be reported as missing.
    """
    container = container_codename()
    if not container or container == suite:
        return []

    errors: list[str] = []
    for path in UBUNTU_SOURCES_FILES:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace(container, suite), encoding="utf-8")
        except OSError as exc:
            errors.append(f"could not retarget {path} to {suite}: {exc}")
    return errors


def _write_signing_key(name: str, key: str) -> None:
    """Write an armored signing key where apt looks for trusted keys."""
    APT_KEYRING_DIR.mkdir(parents=True, exist_ok=True)
    (APT_KEYRING_DIR / f"{name}.asc").write_text(key.rstrip() + "\n", encoding="utf-8")


def _write_source(name: str, source: str) -> None:
    """Write a source to /etc/apt/sources.list.d, picking .sources for deb822."""
    APT_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    extension = ".sources" if "\n" in source.strip() else ".list"
    (APT_SOURCES_DIR / f"{name}{extension}").write_text(source.rstrip() + "\n", encoding="utf-8")


def _expand_release(source: str, suite: str) -> str:
    """Replace the ``$RELEASE`` template variable curtin renders in a source."""
    return source.replace("${RELEASE}", suite).replace("$RELEASE", suite)


def _fetch_key_by_id(keyid: str, keyserver: str | None) -> str:
    """Download an armored key from a keyserver, as curtin does for ``keyid``."""
    server = keyserver or DEFAULT_KEYSERVER
    return _http_get(f"https://{server}/pks/lookup?op=get&search=0x{keyid}")


def resolve_ppa(source: str, suite: str) -> tuple[str, str]:
    """Resolve a ``ppa:owner/name`` source to a deb line and its signing key.

    Raises :class:`ValueError` with a readable message when the PPA does not
    exist on Launchpad, which is exactly the mistake this layer exists to catch.
    """
    match = _PPA_PATTERN.match(source)
    if match is None:
        raise ValueError(f"{source!r} is not a valid 'ppa:owner/name' source")
    owner, name = match.groups()

    try:
        payload = json.loads(_http_get(LAUNCHPAD_PPA_URL.format(owner=owner, name=name)))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"PPA {source!r} does not exist on Launchpad") from exc
        raise ValueError(f"Launchpad could not be queried for {source!r} (HTTP {exc.code})") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Launchpad could not be queried for {source!r} ({exc})") from exc

    fingerprint = payload.get("signing_key_fingerprint") if isinstance(payload, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError(f"Launchpad reported no signing key for {source!r}")

    try:
        key = _http_get(KEYSERVER_KEY_URL.format(fingerprint=fingerprint))
    except (urllib.error.HTTPError, OSError) as exc:
        raise ValueError(f"the signing key of {source!r} could not be fetched ({exc})") from exc

    return LAUNCHPAD_PPA_SOURCE.format(owner=owner, name=name, suite=suite), key


def apply_apt_source(source: AptSource, suite: str) -> None:
    """Write one extra source and its signing key into the container's apt config."""
    if source.key is not None:
        if "-----BEGIN PGP PUBLIC KEY BLOCK-----" not in source.key:
            raise ValueError("only armored PGP keys ('-----BEGIN PGP PUBLIC KEY BLOCK-----') are supported")
        _write_signing_key(source.name, source.key)
    elif source.keyid is not None:
        _write_signing_key(source.name, _fetch_key_by_id(source.keyid, source.keyserver))

    if source.source is None:
        return
    if source.source.startswith("ppa:"):
        line, key = resolve_ppa(source.source, suite)
        _write_source(source.name, line)
        _write_signing_key(source.name, key)
        return
    if abbreviated_source(source.source):
        raise ValueError(f"{source.source!r} is a repository shorthand this layer cannot resolve")
    _write_source(source.name, _expand_release(source.source, suite))


def apply_apt_sources(sources: list[AptSource], suite: str) -> list[str]:
    """Apply every extra source; return one error message per source that failed."""
    errors: list[str] = []
    for source in sources:
        try:
            apply_apt_source(source, suite)
        except (OSError, ValueError) as exc:
            errors.append(f"{source.label}: {exc}")
    return errors


def update_package_lists() -> subprocess.CompletedProcess:
    """Run ``apt-get update`` to verify the repositories and refresh the indexes."""
    print("[apt] Running apt-get update to verify the repositories...")
    return subprocess.run(
        ["apt-get", "update"],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _missing_packages(output: str) -> list[str]:
    """Return the package names apt reported as unavailable."""
    missing: set[str] = set()
    for pattern in _MISSING_PACKAGE_PATTERNS:
        missing.update(pattern.findall(output))
    return sorted(missing)


def check_packages(packages: list[str]) -> tuple[bool, list[str]]:
    """Check that every package is installable from the configured repositories.

    ``apt-get install --simulate`` resolves the dependency graph without
    changing the container, and apt reports every package it cannot find at
    once, so the full list of missing names is available in a single run.
    """
    if not packages:
        return True, []
    command = ["apt-get", "install", "--simulate", "--no-install-recommends", "--assume-yes", *packages]
    print(f"[apt] Simulating the install of {len(packages)} package(s)...")
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if result.returncode == 0:
        return True, []

    missing = _missing_packages(result.stdout)
    if missing:
        return False, [f"not available in the configured repositories: {', '.join(missing)}"]
    _print_diagnostics(result.stdout)
    return False, [f"apt-get install --simulate failed ({result.returncode}); see the apt output above."]


def validate_apt(ai_data: dict[str, Any], release: str | None = None) -> bool:
    """Check APT repositories, their signing keys and the declared packages.

    Every ``apt.sources`` entry is written into the container's apt configuration
    for the release being installed (the validator image itself runs an older
    release, so its default archive sources are retargeted first), then
    ``apt-get update`` proves that each repository exists and that its Release
    signature matches its signing key.  The declared ``packages:`` are checked
    against the resulting indexes with ``apt-get install --simulate``.
    """
    apt = ai_data.get("apt")
    sources, source_errors = collect_apt_sources(apt)
    packages, package_errors = collect_packages(ai_data)
    errors = source_errors + package_errors
    for error in errors:
        print(f"[apt] Structure error: {error}", file=sys.stderr)
    for key in skipped_apt_keys(apt):
        print(f"[apt] 'apt.{key}' is resolved by Subiquity itself; it is not checked here.", file=sys.stderr)

    # A structural error is fatal and needs no repository work, so stop before
    # resolving the release or touching the container's apt configuration.
    if errors:
        print("[apt] APT validation failed.")
        return False

    if not sources and not packages:
        print("[apt] No apt sources or packages declared; nothing to check.")
        return True

    suite = target_codename(release) or ""
    if not suite:
        print(
            f"[apt] Could not map the target release {release!r} to an Ubuntu codename, so the "
            f"sources cannot be resolved. Rebuild the validator image (distro-info-data).",
            file=sys.stderr,
        )
        errors.append("unknown target release")
    else:
        retarget_errors = retarget_default_sources(suite)
        for error in retarget_errors:
            print(f"[apt] {error}", file=sys.stderr)
        errors.extend(retarget_errors)

    if errors:
        print("[apt] APT validation failed.")
        return False

    repositories_ok = True
    if sources:
        print(f"[apt] Writing {len(sources)} source(s) for {suite}...")
        apply_errors = apply_apt_sources(sources, suite)
        for error in apply_errors:
            print(f"[apt] {error}", file=sys.stderr)
        repositories_ok = not apply_errors

    if repositories_ok:
        update = update_package_lists()
        if update.stdout:
            print(update.stdout, end="")
        if update.returncode != 0:
            print(
                f"[apt] apt-get update failed ({update.returncode}): a repository is unreachable "
                f"or its signature does not match its signing key.",
                file=sys.stderr,
            )
            repositories_ok = False

    packages_ok = True
    if packages and repositories_ok:
        packages_ok, problems = check_packages(packages)
        for problem in problems:
            print(f"[apt] {problem}", file=sys.stderr)

    was_clean = repositories_ok and packages_ok
    print("[apt] APT repositories and packages are valid." if was_clean else "[apt] APT validation failed.")
    return was_clean


async def validate(
    ai_data: dict[str, Any],
    mode: str,
    verbosity: int,
    validator,
    *,
    use_shellcheck: bool = True,
    shellcheck_severity: str = "error",
    release: str | None = None,
) -> int:
    check_storage = mode in ("storage", "full")
    check_commands = mode in ("commands", "full")
    check_apt = mode in ("apt", "full")

    # The command and apt layers are independent of Subiquity/Curtin, so they are
    # the only checks run for `--validation commands` / `--validation apt`.
    if mode == "commands":
        clean = validate_shell_commands(ai_data, use_shellcheck=use_shellcheck, severity=shellcheck_severity)
        return 0 if clean else 1
    if mode == "apt":
        clean = validate_apt(ai_data, release)
        return 0 if clean else 1

    commands_clean = (
        validate_shell_commands(ai_data, use_shellcheck=use_shellcheck, severity=shellcheck_severity)
        if check_commands
        else True
    )
    apt_clean = validate_apt(ai_data, release) if check_apt else True

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
        if not apt_clean:
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
        choices=("autoinstall", "storage", "commands", "apt", "full"),
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
    parser.add_argument(
        "--release",
        default=None,
        help="Ubuntu release targeted by the installer (e.g. 26.04), used to resolve $RELEASE and PPAs",
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
                release=args.release,
            )
        )
    except Exception as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
