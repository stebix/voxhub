#!/usr/bin/env bash
# voxhub SSH forced command wrapper — the only entrypoint annotator SSH
# sessions can reach.
#
# Installed at /usr/local/bin/voxhub-forced-command.sh and referenced from
# both sshd_config (Match User voxhub → ForceCommand) and per-key command=
# restrictions in authorized_keys.
#
# Two-branch contract (docs/plans/c-transport-rpc-implementation-plan.md,
# "Forced-command wrapper contract"):
#
#   1. SSH_ORIGINAL_COMMAND starts with 'rsync ' → exec rrsync READ-ONLY,
#      rooted at the staging root (bulk data transfer).
#   2. SSH_ORIGINAL_COMMAND equals 'voxhub-server rpc' or bare 'rpc' →
#      exec voxhub-server rpc.  The JSON-RPC request flows over stdin and
#      the response over stdout, both untouched by this wrapper.
#   3. Anything else → forbidden ServerError-shaped JSON envelope on
#      STDOUT (the client parses stdout for structured errors; stderr is
#      for humans and sshd logs), exit 1.
#
# No tokenization of arguments is ever performed — there are none.  The
# legacy bare-subcommand forms ('list-stores', 'prepare-pull ...',
# 'voxhub-server gc') are deliberately forbidden over SSH; operator
# subcommands run only via a real shell on the server.
#
# Security notes:
#   * rrsync stays read-only (-ro) until push ships (launch plan 4.1-4.3).
#     The future writable flip (launch 4.3) must add symlink defenses
#     first: rsync -a preserves symlinks, so a writable staging root would
#     let a malicious peer upload a symlink pointing at stores_dir or
#     .meta/provenance.jsonl.  Read-only makes that moot for pull.
#   * VOXHUB_ANNOTATOR (injected by sshd from the connecting key's
#     environment= option; PermitUserEnvironment allowlists exactly that
#     one variable) is left untouched: exec preserves the environment, so
#     the server inherits the key-bound identity and treats it as
#     authoritative for provenance.  Do not unset or rewrite it here.

set -euo pipefail

# Server configuration: voxhub-server reads its stores/staging directories
# from the TOML config referenced by VOXHUB_SERVER_CONFIG.  Explicit path —
# do not rely on ~ expansion under non-login SSH sessions, and do not rely
# on the caller to set this.  deploy.sh writes the config here.
export VOXHUB_SERVER_CONFIG="${VOXHUB_SERVER_CONFIG:-/home/voxhub/.config/voxhub/server.toml}"

# Staging root for the rsync branch.  deploy.sh renders the operator's
# staging dir over the placeholder token below at install time.  Rendering
# at install time (rather than parsing server.toml here at run time) keeps
# the wrapper free of TOML-parsing fragility and keeps the installed file
# a pure function of the repo copy + deploy args, so redeploys stay
# byte-comparable and idempotent.  The repo copy ships the raw token, so
# tests (and the launch 1.4 sshd e2e suite) run it unrendered via the
# VOXHUB_STAGING_ROOT env fallback.  Annotators cannot smuggle that
# variable in over SSH: PermitUserEnvironment allowlists only
# VOXHUB_ANNOTATOR.
STAGING_ROOT="${VOXHUB_STAGING_ROOT:-@STAGING_ROOT@}"

# Resolve the exec targets.  PATH lookup first (lets tests stub both
# binaries); absolute fallbacks match where deploy.sh installs them.  The
# VOXHUB_SERVER / VOXHUB_RRSYNC env overrides exist for test harnesses —
# sshd never sets them (see the PermitUserEnvironment allowlist above).
VOXHUB_SERVER="${VOXHUB_SERVER:-$(command -v voxhub-server || echo /usr/local/bin/voxhub-server)}"
RRSYNC="${VOXHUB_RRSYNC:-$(command -v rrsync || echo /usr/local/bin/rrsync)}"

# Emit a ServerError-shaped envelope (voxhub_schema.models.ServerError) on
# STDOUT and fail.  protocol_version tracks voxhub_schema.PROTOCOL_VERSION
# (pinned wire contract v2).  Both arguments must be fixed strings under
# our control, and must not contain double quotes or backslashes —
# SSH_ORIGINAL_COMMAND is attacker-controlled and interpolating it here
# would allow JSON injection into the envelope.
deny() {
    printf '{"protocol_version":2,"error":true,"code":"%s","message":"%s"}\n' "$1" "$2"
    exit 1
}

CMD="${SSH_ORIGINAL_COMMAND:-}"

case "$CMD" in
    'rsync '*)
        # Bulk-transfer branch.  rrsync re-parses SSH_ORIGINAL_COMMAND
        # itself (exec preserves the environment) and confines every path
        # to $STAGING_ROOT.  -ro: read-only until push ships (see header).
        if [[ -z "$STAGING_ROOT" || ! -d "$STAGING_ROOT" ]]; then
            # Unrendered wrapper without the env fallback, or a staging dir
            # that has vanished — refuse loudly rather than handing rrsync
            # a garbage root.
            deny server_misconfigured 'staging root is not configured on this server'
        fi
        exec "$RRSYNC" -ro "$STAGING_ROOT"
        ;;
    'voxhub-server rpc' | 'rpc')
        # RPC branch: single fixed argv, stdin/stdout pass through.
        exec "$VOXHUB_SERVER" rpc
        ;;
esac

deny forbidden "command not allowed: expected 'voxhub-server rpc' or an rsync transfer"
