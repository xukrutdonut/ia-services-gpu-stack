#!/usr/bin/env bash
# Only writes to the dedicated Agent source volume, never to the host Hermes home.
set -euo pipefail
repo=${HERMES_AGENT_REPO:-https://github.com/NousResearch/hermes-agent.git}
ref=${HERMES_AGENT_REF:?Set HERMES_AGENT_REF to a full Agent commit SHA}
target=${HERMES_AGENT_TARGET:-/opt/hermes}
[[ "$ref" =~ ^[0-9a-f]{40}$ ]] || { echo 'HERMES_AGENT_REF must be a full commit SHA' >&2; exit 1; }
# This container starts as root; the persistent checkout belongs to the WebUI UID.
git_agent() { git -c safe.directory="$target" -C "$target" "$@"; }
if [[ ! -d "$target/.git" ]]; then
    git clone --depth=1 --filter=blob:none "$repo" "$target"
fi
if [[ $(git_agent rev-parse HEAD 2>/dev/null || true) != "$ref" ]]; then
    if [[ -n $(git_agent status --porcelain) ]]; then
        echo 'Agent source volume has local changes; refusing to overwrite them' >&2
        exit 1
    fi
    git_agent fetch --depth=1 "$repo" "$ref"
    git_agent checkout --detach "$ref"
fi
# Already populated submodules do not need the network on subsequent starts.
git_agent -c safe.directory='*' submodule update --init --recursive
[[ -f "$target/pyproject.toml" ]] || { echo 'Agent checkout has no pyproject.toml' >&2; exit 1; }
if [[ $(id -u) == 0 ]]; then
    chown -R "${WANTED_UID:-1000}:${WANTED_GID:-1000}" "$target"
fi
printf 'Hermes Agent ready: %s\n' "$(git_agent rev-parse HEAD)"
