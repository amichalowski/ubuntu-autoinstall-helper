# Workshop git hooks: `` `pre-commit` not found ``

Incident note: the commit hook stopped working inside the `dev` workshop, why it
happened, and how it was repaired.

## It did not work

A commit inside the workshop aborted before the hooks printed anything:

```text
`pre-commit` not found.  Did you forget to activate your virtualenv?
```

No `ruff check`, no `ruff format`, no conventional-commit validation — the hook
script exited 1 without running a single check. Retrying the commit reproduced
it. Commits made in the same container a few minutes earlier had worked.

## Why it did not work

The generated hook hardcodes the interpreter of the environment that created it:

```bash
# .githooks-workshop/pre-commit
INSTALL_PYTHON=/project/.venv/bin/python
...
if [ -x "$INSTALL_PYTHON" ]; then
    exec "$INSTALL_PYTHON" -mpre_commit "${ARGS[@]}"
elif command -v pre-commit > /dev/null; then
    exec pre-commit "${ARGS[@]}"
else
    echo '`pre-commit` not found.  Did you forget to activate your virtualenv?' 1>&2
    exit 1
fi
```

The hook was pinned to `/project/.venv/bin/python`, but `.venv` belongs to the
**host** (it is the host's uv environment, visible in the container only because
the project directory is bind-mounted). Its interpreter is a symlink to the
host's uv-managed CPython:

```text
.venv/bin/python -> /home/belgarion/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12
.venv/pyvenv.cfg: home = /home/belgarion/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin
```

That path does not exist inside the container, so the hook broke in three steps:

1. `[ -x "$INSTALL_PYTHON" ]` was false — the symlink is dangling there;
2. `command -v pre-commit` failed — pre-commit is installed in
   `.venv-workshop/bin`, which is not on the hook's `PATH`;
3. the script printed the "did you forget to activate your virtualenv?" message
   and exited 1.

The message never mentions `.venv`, which is why the failure looks unrelated to
the environment split.

Timeline of the evidence:

| Time | Event |
| --- | --- |
| 08:52 | `.venv-workshop` created (`home = /usr/bin`, uv `0.13.7`) — the workshop's own environment |
| 09:58 | `.githooks-workshop/` created with `INSTALL_PYTHON=/project/.venv/bin/python`; it worked while `.venv` still resolved inside the container |
| 11:26, 11:30, 11:33 | commits succeed — the hooks ran |
| 11:34:28 | the host runs `uv sync` (uv `0.11.8`): `.venv/pyvenv.cfg` and `.venv/bin/python` are rewritten to the host path |
| 11:52 | the next commit fails with the message above |

So the hook had been silently relying on a host environment that the host was
free to rewrite — and did. The isolated environment for the workshop
(`.venv-workshop`) existed the whole time but the hook was not pointing at it.

## How I repaired it

Regenerated the workshop hooks with the in-project SDK script, which writes the
hook scripts into `.githooks-workshop` using the interpreter of the current
(workshop) environment:

```bash
bash .workshop/uv-env/hooks/setup-project
grep -n INSTALL_PYTHON= .githooks-workshop/pre-commit .githooks-workshop/commit-msg
# .githooks-workshop/pre-commit:6:INSTALL_PYTHON=/project/.venv-workshop/bin/python
# .githooks-workshop/commit-msg:6:INSTALL_PYTHON=/project/.venv-workshop/bin/python
```

`.venv-workshop/bin/python` points to `/usr/bin/python3`, which exists in the
container, so the hook resolves again. The retried commit ran every hook and
succeeded (`b9d124e`, hooks passed: `ruff check`, `ruff format`,
`conventional commit`).

`uv run pre-commit install` cannot be used for this: with `core.hooksPath` set,
pre-commit refuses to run ("Cowardly refusing to install hooks with
`core.hooksPath` set"), which is why the SDK script generates the scripts in a
temporary `git_dir` and copies them into `.githooks-workshop`.

The repair lives in `.githooks-workshop/`, which is git-ignored: it fixed this
container only, and the repository itself was not modified.

## Why it came back after a refresh

The manual repair produced working hooks but did not fix the generator. The
in-project SDK hook `.workshop/uv-env/hooks/setup-project` still depended on
`UV_PROJECT_ENVIRONMENT` **already being exported** in its own environment.

The script writes the export to `~/.profile` for future login shells — and then,
in the same process, calls `uv run`. A shell never re-reads `~/.profile`
mid-process, so whenever the export was absent at that moment (a fresh
container, a refresh that recreated `~/.profile`, a non-login shell running the
hook), `uv run` resolved the **shared** `.venv` instead:

```text
warning: Ignoring existing virtual environment linked to non-existent Python interpreter: `.venv/bin/python` -> `/home/belgarion/.local/share/uv/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12`
Using CPython 3.12.3 interpreter at: /usr/bin/python3
Removed virtual environment at: .venv
Creating virtual environment at: .venv
```

Two things happened at once: the workshop destroyed and recreated the host's
`.venv` (the very fight the SDK exists to prevent), and the regenerated hooks
were pinned to `/project/.venv/bin/python`. That pin only works until the host
runs `uv sync` again — exactly the incident above. So the cycle was: refresh,
broken commit, manual repair, refresh, broken commit.

## The fix

The generator exports `UV_PROJECT_ENVIRONMENT` for itself before calling
`uv run`, so every invocation targets `.venv-workshop` no matter what the login
shell had:

```bash
export UV_PROJECT_ENVIRONMENT="$environment"
```

It also verifies the freshly generated hooks **before** installing them, so a
regression fails loudly and leaves the previous, working hooks in place instead
of silently arming hooks that die at the next host `uv sync`:

```bash
python_pin="$(sed -n 's/^INSTALL_PYTHON=//p' "$tmp_dir/hooks/$hook")"
case "$python_pin" in
    "${environment}"/bin/python*) ;;
    *) printf 'error: ...' >&2; exit 1 ;;
esac
```

Verified by running the generator with `env -u UV_PROJECT_ENVIRONMENT bash
.workshop/uv-env/hooks/setup-project`: the hooks stayed pinned to
`.venv-workshop/bin/python` and `.venv` was not touched.

Note: while reproducing this, the workshop did rebuild `.venv` for its own
interpreter — the symptom itself, not a separate problem. The host's next
`uv run`/`uv sync` restores its environment.
