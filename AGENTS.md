# Agent Guidelines

Repository guidelines for AI agents working on **autoinstall-helper**.

## Language

- **English only:** All source comments, docstrings, CLI help text, printed/error messages, configuration file comments, and documentation must be written in **English**. Do not add Polish (or any other non-English) text anywhere in the repository.

## Development Philosophy

- **No backwards compatibility baggage:** This is a private project moving fast (vibecoding). Feel free to refactor, simplify, rename, or redesign any internal abstractions or CLI behaviors without worrying about backwards compatibility or legacy shims. Clean, simple, and maintainable code always wins over backwards compatibility.

## Toolchain & Environment

- **Package & environment manager:** Always use [`uv`](https://docs.astral.sh/uv/) (`uv run ...`). Do not use raw `pip` or manage system virtualenvs directly.
- **Workshop isolation:** the `dev` workshop bind-mounts the repository, so host and workshop would otherwise share `.venv` and recreate each other's environment. The in-project SDK `.workshop/uv-env/` exports `UV_PROJECT_ENVIRONMENT=<project>/.venv-workshop` inside the workshop (`.workshop/dev.yaml` → `project-uv-env`). Run `uv run ...` normally on both sides; never copy, delete, or reuse the other side's environment (`.venv` is the host's, `.venv-workshop` is the workshop's).
- **Python target:** `>= 3.10`.
- **Zero runtime dependencies rule:** Production code in `src/autoinstall_helper/` must only rely on the Python Standard Library (and Ubuntu built-ins like `python3-yaml`). **Never** add packages to `[project.dependencies]`. Only dev/test dependencies are allowed in `[dependency-groups]`.

## Architectural Invariants

- **`src/` layout only:** All production Python code lives under `src/autoinstall_helper/`. Do not introduce loose scripts in the repository root.
- **Asset path resolution:** Never assume the current working directory (`cwd`) or repository root when referencing assets in `src/autoinstall_helper/docker/`. Paths must be resolved relative to `__file__` or via extraction helpers to maintain compatibility with standalone packaging (`zipapp`).
- **Container boundary:** `src/autoinstall_helper/docker/validate-autoinstall.py` is executed exclusively inside the validation Docker container. Do not import or execute it directly in the host environment.
- **Privileged actions:** Operations modifying block devices (`flash`, `inject`, `extract`) require root permissions and enforce `_require_root()`.

## Testing Conventions

- Run tests via `uv run pytest`.
- The suite has three tiers: unit (`tests/test_*.py`, the default run), recorded replay (`tests/replay/`, marker `replay`), and smoke (`tests/smoke/`, marker `smoke`). Both replay and smoke are excluded from the default `uv run pytest` run.
- Replay tests are hermetic component tests: they drive a real command flow (`flash`, `cidata`, `validate`, …) but serve every external boundary (`subprocess`, `urllib`, filesystem metadata) from a committed `tests/replay/fixtures/*.recording.json`. Run them with `uv run pytest -m replay` (no Docker, network, or devices required). Recordings are produced with the dev-mode `--record` flag (`autoinstall-helper --record session.json <command>`); see `docs/design/dev-mode-recording.md`. The tier is currently **scaffolded** (machinery + fixture, no committed recordings yet) — add a test together with each real recording following the scenario plan in `tests/replay/README.md`, and re-add the CI `pytest -m replay` step once the first fixture lands.
- Smoke tests live in `tests/smoke/` and carry the `smoke` marker. They execute the built `.pyz` artifact end-to-end and require Docker and network access, so they are excluded from the default `uv run pytest` run—execute them explicitly with `uv run pytest -m smoke` (after building the artifact, see README).
- Mock at the point of **usage** (e.g., `autoinstall_helper.flash.subprocess.run`), not definition. Filesystem metadata is resolved through the `fsprobe.probe()` seam; mock it at the point of usage (`autoinstall_helper.disk.fsprobe.probe`) rather than patching `pathlib` globally.
- Use pytest's built-in `tmp_path` fixture for any file/directory I/O tests.
- Never perform real block device operations, network calls, or Docker invocations during unit tests—mock system boundaries (`subprocess.run`, `urllib.request.urlopen`, `losetup`, `parted`).

## Lint & Formatting

- Run `uv run ruff check .` and `uv run ruff format --check .` before committing. CI (`.github/workflows/ci.yml`, `lint` job) enforces the same commands and fails otherwise.
- Ruff version is pinned via `uv.lock`, so local hooks, developers, and CI always use the identical version.
- Git hooks are generated with `pre-commit install`. The host uses the default `.git/hooks`; the dev workshop sets `core.hooksPath = .githooks-workshop` in `/etc/gitconfig` (the global config is mounted read-only from the host), so each side gets a hook that hardcodes its own interpreter. The generated workshop directory is ignored by git.
- Broad `except Exception` in CLI entry points and the Snap Store API fallback is an accepted pattern (`BLE001` globally ignored). Blocking `subprocess.run` calls must state intent explicitly: `check=True` or `check=False`.

## Commits & Signing

- **Never commit without an SSH signature.** The repository enforces signed commits (`commit.gpgsign = true`, `gpg.format = ssh`). If the signing key is not available (e.g. `ssh-add -l` reports "The agent has no identities" or `git commit` fails with "Couldn't find key in agent"), stop and ask the user to load the key into the ssh-agent, then retry. Do not work around it (no `--no-gpg-sign`, no `-c commit.gpgsign=false`, no changes to the signing configuration).
