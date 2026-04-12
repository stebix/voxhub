#!/usr/bin/env bash
# voxhub SSH forced command wrapper.
#
# Installed at /usr/local/bin/voxhub-forced-command.sh and referenced from
# both sshd_config (Match User voxhub → ForceCommand) and per-key command=
# restrictions in authorized_keys.
#
# Only allows the explicit set of voxhub-server subcommands that annotators
# need.  Everything else is rejected with a structured JSON error on stderr.
#
# Server configuration: ``voxhub-server`` reads its stores directory
# from ``[storage].stores_dir`` in the TOML config referenced by
# VOXHUB_SERVER_CONFIG (or ``~/.config/voxhub/server.toml``).  Clients
# no longer send a path over the wire — the server is authoritative.
# Export VOXHUB_SERVER_CONFIG in the voxhub user's environment (e.g.
# ``/etc/voxhub/server.toml``) before this wrapper executes.

set -euo pipefail

VOXHUB_SERVER="${VOXHUB_SERVER:-/usr/local/bin/voxhub-server}"

ALLOWED_SUBCOMMANDS=(
    list-stores
    prepare-pull
    integrate-annotations
    cleanup
    healthcheck
)

CMD="${SSH_ORIGINAL_COMMAND:-}"

if [[ -z "$CMD" ]]; then
    printf '{"error":true,"code":"no_command","message":"interactive shell access is disabled"}\n' >&2
    exit 1
fi

# Extract the first token (the subcommand).
SUBCMD="${CMD%% *}"

allowed=false
for a in "${ALLOWED_SUBCOMMANDS[@]}"; do
    if [[ "$SUBCMD" == "$a" ]]; then
        allowed=true
        break
    fi
done

if [[ "$allowed" != true ]]; then
    printf '{"error":true,"code":"forbidden","message":"command not allowed: %s"}\n' "$SUBCMD" >&2
    exit 1
fi

# shellcheck disable=SC2086
# Word-splitting on $CMD is intentional — the CLI parser expects individual args.
exec "$VOXHUB_SERVER" $CMD
