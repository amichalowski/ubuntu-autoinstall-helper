# Recorded-replay component tests (`tests/replay/`)

Status: **scaffolded — no recordings committed yet.** The recording/replay
machinery, the `replayed_session` fixture and the `replay` pytest marker are in
place; real fixtures and their tests are added on a host that can actually run
the flows (Docker, network, devices, libvirt). This file is the scenario plan:
record each one with `--record`, then add the matching test.

## How it works

A recording is a single `tests/replay/fixtures/<name>.recording.json` file
produced by the dev-only `--record` flag (see
[`docs/design/dev-mode-recording.md`](../../docs/design/dev-mode-recording.md)).
It captures every `subprocess`, `urllib` and filesystem-metadata interaction of a
real session. At replay time the `replayed_session` fixture serves those results
back, so a test drives a real command flow with **no Docker, network, devices or
ISOs**.

## Recording recipe

1. **Use a comparable host.** `shutil.which` (`snap`, `sgdisk`, …) is *not*
   recorded, so record and replay on similar systems (same distro and optional
   tools). The `metadata.os_release`/`metadata.kernel` fields document where a
   recording came from.
2. **Record** (the flag must precede the subcommand):

   ```bash
   uv run autoinstall-helper --record tests/replay/fixtures/<name>.recording.json <command...>
   ```

3. **Review the JSON before committing.** Outputs and absolute paths end up in
   the file; env vars whose names look secret (`*TOKEN*`, `*SECRET*`, …) are
   redacted automatically. ISOs, disk images and block devices are never stored —
   only their size/existence metadata.
4. **Add a test** using the indirect-parametrized fixture:

   ```python
   import pytest
   from autoinstall_helper import flash

   pytestmark = pytest.mark.replay


   @pytest.mark.parametrize("replayed_session", ["flash-new-target"], indirect=True)
   def test_flash_new_target(replayed_session, tmp_path):
       # No ISO and no device: metadata comes from the recording.
       assert flash.flash(tmp_path / "respin.iso", tmp_path / "target.img", yes=True) == 0
   ```

5. **Run and commit** `uv run pytest -m replay -v` together with the fixture.

Notes:

- Test file basenames must be unique repo-wide. `tests/replay/__init__.py` makes
  the directory a package, so `tests/replay/test_flash.py` does not collide with
  `tests/test_flash.py`.
- Replay matches by command/URL/path with an ordered fallback; a mismatch (for
  example the `tmp_path` differs from the recorded path) warns loudly instead of
  silently desynchronizing.
- Only metadata is journaled for paths, so a replayed flow needs no ISO; small
  text files (e.g. `autoinstall.yaml`) stay real and must exist.
- Re-add the CI step (`uv run pytest -m replay -v`) once the first recording lands.

## Scenarios to record

| Fixture | Flow | Preconditions | Command to record | Assert in test |
| --- | --- | --- | --- | --- |
| `flash-new-target` | Create the target file, `fallocate`, `dd`, `sync`, GPT fix | A small ISO file; target file **absent** | `--record … flash <iso> <target.img> --yes` | `flash.flash(iso, target, yes=True) == 0` |
| `flash-existing-target` | Existing target, `dd`, `sync`, GPT fix (no `fallocate`) | ISO file; target file ≥ ISO + 128 MiB | `--record … flash <iso> <target.img> --yes` | `flash.flash(iso, target, yes=True) == 0` |
| `flash-block-device` | Block-device branch + `--yes` confirmation | Root; a disposable block device or `losetup` device | `sudo … --record … flash <iso> /dev/sdX --yes` | `flash.flash(iso, dev, yes=True) == 0` |
| `cidata-inject-existing` | `losetup`, `mkfs.vfat`, `mount`, copy | Root/loop devices; image with a cidata partition | `sudo … --record … cidata inject <target.img> <autoinstall.yaml>` | `cidata.inject(target, yaml) == 0` |
| `cidata-inject-new-partition` | Create partition (`parted`, `partprobe`), format, mount | Root/loop devices; image without a cidata partition | `sudo … --record … cidata inject <target.img> <autoinstall.yaml>` | `cidata.inject(target, yaml) == 0` |
| `cidata-extract` | `losetup`, `mount`, read `user-data` | Root/loop devices; image with cidata | `sudo … --record … cidata extract <target.img> <out.yaml>` | `cidata.extract(target, out) == 0` |
| `validate-cached-image` | `snap`/Snap API version, `docker image inspect`, `docker run` | Docker; validator image already built | `--record … validate <autoinstall.yaml>` | `cli.validate(config) == 0` |
| `validate-build` | Snap API `urlopen`, GitHub `urlopen`, `docker build`, `docker run` | Docker; network; image absent | `--record … validate <autoinstall.yaml>` | `cli.validate(config) == 0` |
| `validate-invalid` | Same, but the validator exits non-zero | Docker; a config that fails validation | `--record … validate <wrong-autoinstall.yaml>` | `cli.validate(config) == <rc>` |
| `validate-commands` | Same, scoped to the shell-command layer (`sh -n` + shellcheck in-container) | Docker; a config with broken shell commands | `--record … validate <wrong-autoinstall-broken-commands.yaml> --validation commands` | `cli.validate(config, validation="commands") != 0` |
| `validate-images` | `docker image ls --format` + status from the bundled assets digest | Docker; at least one validator image built | `--record … validate images` | `docker_images() == 0`; every recorded tag is listed (assert on tags, not on `current`/`stale` — the digest follows the `docker/` assets) |
| `vm-create-dry-run` | Media metadata + printed `virt-install` command | None (no libvirt needed) | `--record … vm create <name> --media <media.img> --dry-run` | `vm.create(spec, dry_run=True) == 0` |
| `vm-create` | `virsh`/`virt-install`/`qemu-img` full create | libvirt + KVM; media image | `--record … vm create <name> --media <media.img>` | `vm.create(spec) == 0` |
