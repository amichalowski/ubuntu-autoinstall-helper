#!/usr/bin/env bash
set -euo pipefail

WORKSHOP="dev"
SDK="opencode"
GIT_PLUG="git-config"
SSH_PLUG="ssh-agent"
WORKSHOP_GIT_DIR="/home/workshop/.config/git"

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] [GIT_CONFIG_DIR]

Prepares the '$WORKSHOP' Workshop on the host:
  - launches or refreshes it
  - mounts the host global git config read-only at $WORKSHOP_GIT_DIR
  - connects the host SSH agent
  - verifies the mount and the agent inside the workshop

If GIT_CONFIG_DIR is omitted, the script uses GIT_CONFIG_SRC when set,
otherwise ~/.config/git when it contains a config file, otherwise it
stages ~/.gitconfig in \${XDG_DATA_HOME:-\$HOME/.local/share}/workshop/host-git-config.

Run this on the host from the project checkout, not inside the workshop.
EOF
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

if [ "$#" -gt 1 ]; then
    usage >&2
    exit 2
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")

command -v workshop >/dev/null 2>&1 || die "workshop CLI not found; run this on the host, not inside the workshop"
[ -f "$PROJECT_DIR/.workshop/$WORKSHOP.yaml" ] || die "missing $PROJECT_DIR/.workshop/$WORKSHOP.yaml"

cd "$PROJECT_DIR"

workshop_status() {
    workshop info "$WORKSHOP" 2>/dev/null | awk -F': *' '/^status:/ {print tolower($2); exit}' || true
}

prepare_git_source() {
    if [ -n "${1:-}" ]; then
        [ -d "$1" ] || die "git config source is not a directory: $1"
        printf '%s\n' "$1"
        return
    fi

    if [ -n "${GIT_CONFIG_SRC:-}" ]; then
        [ -d "$GIT_CONFIG_SRC" ] || die "GIT_CONFIG_SRC is not a directory: $GIT_CONFIG_SRC"
        printf '%s\n' "$GIT_CONFIG_SRC"
        return
    fi

    if [ -f "$HOME/.gitconfig" ]; then
        stage="${XDG_DATA_HOME:-$HOME/.local/share}/workshop/host-git-config"
        mkdir -p "$stage"
        real=$(readlink -f -- "$HOME/.gitconfig")
        rm -f "$stage/config"
        if ! ln -f -- "$real" "$stage/config" 2>/dev/null; then
            cp -f -- "$real" "$stage/config"
        fi
        printf '%s\n' "$stage"
        return
    fi

    if [ -f "$HOME/.config/git/config" ]; then
        printf '%s\n' "$HOME/.config/git"
        return
    fi

    mkdir -p "$HOME/.config/git"
    printf 'warning: no global git config found; using empty %s\n' "$HOME/.config/git" >&2
    printf '%s\n' "$HOME/.config/git"
}

GIT_CONFIG_SOURCE=$(prepare_git_source "${1:-}")

status=$(workshop_status)
printf '==> current workshop status: %s\n' "${status:-off}"

case "$status" in
    ""|off)
        printf '==> launching workshop %s\n' "$WORKSHOP"
        workshop launch "$WORKSHOP"
        ;;
    ready)
        printf '==> refreshing workshop %s\n' "$WORKSHOP"
        workshop refresh "$WORKSHOP"
        ;;
    stopped)
        printf '==> starting stopped workshop %s\n' "$WORKSHOP"
        workshop start "$WORKSHOP"
        printf '==> refreshing workshop %s\n' "$WORKSHOP"
        workshop refresh "$WORKSHOP"
        ;;
    *)
        die "workshop '$WORKSHOP' is in '$status' state; resolve it first"
        ;;
esac

if [ "$(workshop_status)" = "ready" ]; then
    printf '==> stopping workshop %s before remount\n' "$WORKSHOP"
    workshop stop "$WORKSHOP"
fi

printf '==> mounting %s read-only at %s\n' "$GIT_CONFIG_SOURCE" "$WORKSHOP_GIT_DIR"
workshop remount "$WORKSHOP/$SDK:$GIT_PLUG" "$GIT_CONFIG_SOURCE"

printf '==> starting workshop %s\n' "$WORKSHOP"
workshop start "$WORKSHOP"

if workshop connections "$WORKSHOP" --no-headers 2>/dev/null | awk -v p="$WORKSHOP/$SDK:$SSH_PLUG" '$2 == p && $3 != "-" { found=1 } END { exit(found ? 0 : 1) }'; then
    printf '==> ssh-agent plug already connected\n'
else
    printf '==> connecting host ssh-agent\n'
    workshop connect "$WORKSHOP/$SDK:$SSH_PLUG"
fi

printf '==> verifying\n'

workshop exec "$WORKSHOP" -- test -r "$WORKSHOP_GIT_DIR" || die "git config mount is not readable at $WORKSHOP_GIT_DIR"

if workshop exec "$WORKSHOP" -- sh -c "touch '$WORKSHOP_GIT_DIR/.workshop-readonly-check' 2>/dev/null"; then
    workshop exec "$WORKSHOP" -- rm -f "$WORKSHOP_GIT_DIR/.workshop-readonly-check" || true
    die "git config mount is writable; expected read-only"
fi
printf 'OK: git config mount is read-only\n'

if workshop exec "$WORKSHOP" -- test -f "$WORKSHOP_GIT_DIR/config"; then
    printf '==> global git config visible in workshop:\n'
    workshop exec "$WORKSHOP" -- git config --global --list --show-origin || true
else
    printf 'warning: no config file at %s/config; git will use defaults\n' "$WORKSHOP_GIT_DIR" >&2
fi

if ! workshop exec "$WORKSHOP" -- sh -c 'test -S "$SSH_AUTH_SOCK"'; then
    die "SSH agent socket is not available in the workshop"
fi
printf 'OK: SSH agent socket available at %s\n' "$(workshop exec "$WORKSHOP" -- printenv SSH_AUTH_SOCK)"

if workshop exec "$WORKSHOP" -- sh -c 'command -v ssh-add >/dev/null 2>&1'; then
    workshop exec "$WORKSHOP" -- ssh-add -l || printf 'warning: SSH agent has no identities loaded\n' >&2
fi

printf '==> done\n'
