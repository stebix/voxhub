# Move `zarr_root` from client-supplied to server-owned, rename to `stores_dir`

## Context

Today, every server subcommand (`list-stores`, `prepare-pull`, `integrate-annotations`, `validate-attributes`, `healthcheck`) takes `zarr_root` as a positional CLI argument supplied by the client. The client parses it out of an `SshTarget` string (`user@host:/data/zarr`) and ships it verbatim through the SSH forced-command wrapper into `voxhub-server`. This has two problems:

1. **Server implementation detail leaks into the protocol.** The client has no business knowing where the operator chose to put their zarr stores. Clients want store `ct001`, not `/srv/voxhub/zarr/ct001.zarr`.
2. **Path-injection surface.** The forced-command wrapper (`scripts/deploy/voxhub-forced-command.sh:48`) whitelists only the subcommand token, then `exec`s `voxhub-server $SSH_ORIGINAL_COMMAND`. Any client can point the server at any filesystem path the `voxhub` user can read or write.

On top of that, `zarr_root` is a poor name: it collides with zarr's own "root group" concept (`zarr.open_group(path)` returns the root group of a *single* store), so reading the code you can't tell at a glance whether `zarr_root` is a directory of stores or a group inside one store.

**Outcome.** The server owns the path as operator config; the client only speaks in store names. The name becomes `stores_dir` throughout.

## Recommended approach

Server reads `stores_dir` from its settings (TOML, already loaded from `VOXHUB_SERVER_CONFIG`). Client drops `zarr_root` from the target string and the wire protocol. Audit metadata (`server_stores_dir`) is populated by the server and echoed back in the `prepare-pull` response, then recorded by the client in its pull manifest.

Ship as **two PRs**:

### PR 1 — Server reads `stores_dir` from settings (non-breaking plumbing)

Internal change. Client, schema, wrapper untouched. Positional CLI arg stays as a temporary override so nothing breaks.

Critical files:

- `packages/voxhub-core/src/voxhub_core/server/settings.py`
  - Add `StorageSettings(stores_dir: Path)` attrs class.
  - Extend `ServerSettings` with `storage: StorageSettings`.
  - Add `_parse_storage()`; wire into `load_settings()`.
  - Fail loud in `load_settings()` if `[storage].stores_dir` is missing or not a directory (raises with a clear message; `main()` in `cli.py` should catch and emit a structured `ServerError` before exiting non-zero).
- `packages/voxhub-core/src/voxhub_core/server/cli.py`
  - Each handler resolves its path: if the positional `args.zarr_root` is provided, use it; else fall back to `settings.storage.stores_dir`. Rename the local `zarr_root` variable to `stores_dir` inside handlers.
  - `main()` loads settings once and injects `settings.storage.stores_dir` into the `args` namespace after parsing.
- `packages/voxhub-core/tests/conftest.py`
  - New fixture `server_config_env` that writes a tmp `server.toml` with `[storage].stores_dir = <tmp_path>` and sets `VOXHUB_SERVER_CONFIG`. Existing tests that pass a positional can stay; add one test per subcommand exercising the settings path.
- `packages/voxhub-core/tests/test_server_cli.py`, `test_server_cli_subprocess.py`
  - Add coverage for "no positional → read from settings" flow.

Risk: very low. Backwards-compatible with any existing client, including the stale duplicate `voxhub-client/cli.py`.

### PR 2 — Drop `zarr_root` from the protocol (atomic break)

All of the following must land together.

Critical files:

- `packages/voxhub-schema/src/voxhub_schema/models.py`
  - Remove `zarr_root: str` from `PrepareRequest` (L268) and `IntegrateRequest` (L332).
- `packages/voxhub-schema/src/voxhub_schema/manifest.py`
  - Rename `RemoteManifest.server_zarr_root` → `server_stores_dir` (L60–77). This field now carries the server-reported path, sourced from the `prepare-pull` response rather than client input.
- `packages/voxhub-schema/tests/test_models.py`, `tests/test_manifest.py`
  - Update constructors and field assertions.
- `packages/voxhub-core/src/voxhub_core/server/cli.py`
  - Drop the `zarr_root` positional from `list-stores` (L894), `prepare-pull` (L899), `integrate-annotations` (L909), `validate-attributes` (L930), `healthcheck` (L936).
  - Handlers now read exclusively from `settings.storage.stores_dir`.
  - `_run_prepare_pull` response payload gains a `server_stores_dir` key (populated from settings) at `cli.py:268`.
- `packages/voxhub-client/src/voxhub_client/ssh.py`
  - `SshTarget`: drop the `zarr_root: str` field and its docstring.
  - `SshTarget.parse()`: accept bare `user@host` or `host` (no trailing `:/path`). Update parse logic and error message.
  - `SshRunner.run(...)` call sites across the client package: remove the `target.zarr_root` positional.
- `packages/voxhub-client/tests/test_ssh.py`, `test_client_manifest.py`
  - Update `SshTarget` constructors (drop `zarr_root`).
  - Assert `server_stores_dir` is taken from the server response, not from the target.
- `scripts/deploy/voxhub-forced-command.sh`
  - Header comment: document that `VOXHUB_SERVER_CONFIG` must point at a TOML containing `[storage].stores_dir`.
  - Optional defence-in-depth: reject any arg starting with `/` or `..` for the allowed subcommands (can be deferred to a follow-up if we want to keep the wrapper minimal).
- Docs:
  - `docs/architecture.md` — update the SSH+rsync section; SSH target is now `user@host`.
  - `docs/deployment-readiness.md` — note that `stores_dir` is operator-configured; reference the TOML key.
  - `docs/testing/server-cli.md`, `docs/testing-plan.md`, `docs/testing/catalog-staging-audit.md` — adjust invocation examples.

Risk: medium but contained. Greenfield project (no compat shims). Must deploy server + client atomically.

### Out of scope (tracked in `docs/issues.md`, separate PR)

`packages/voxhub-client/src/voxhub_client/cli.py` is a stale verbatim copy of `packages/voxhub-core/src/voxhub_core/server/cli.py` and violates the core/client independence rule. Rewriting it as a real client CLI driving `SshRunner` is independent work and will pick up the new `SshTarget` shape naturally.

## Reused existing code

- `voxhub_core.catalog.discover_zarr_stores` — already the single entry point for walking `*.zarr` under a directory. No changes; it just gets a differently-sourced path.
- `voxhub_core.server.settings.load_settings` — already loads TOML from `VOXHUB_SERVER_CONFIG` or `~/.config/voxhub/server.toml`. We extend the schema, not the mechanism.
- `voxhub_core.staging.stage` — already filters by `store_names`. No signature change.
- `voxhub_core.server.provenance.record_provenance` — takes the path as an arg; unchanged.

## Verification

End-to-end, per PR:

**PR 1:**

1. `uv run ruff check packages/` and `uv run pyright` pass.
2. `uv run pytest packages/voxhub-core` passes, including new fixture-based tests that invoke each subcommand with *no* positional and a `VOXHUB_SERVER_CONFIG` pointing at a tmp TOML.
3. Manual: run `voxhub-server prepare-pull /path/to/stores --stores ct001 /tmp/out` (legacy form) against a tmp zarr tree — still works.
4. Manual: unset `VOXHUB_SERVER_CONFIG`, run any subcommand with no positional — server emits a clear `ServerError` (`storage_misconfigured` or similar), exits 1.

**PR 2:**

1. `uv run ruff check packages/` and `uv run pyright` pass across all three packages.
2. `uv run pytest` green.
3. Manual end-to-end: on a dev VPS with `server.toml` set, client runs `voxhub pull alice@server --stores ct001` (no path in target), completes successfully; the resulting `.voxhub_manifest.json` contains the correct `server_stores_dir`.
4. Manual protocol-violation check: a client that still sends a positional path gets a clean argparse error from the server (structured envelope if possible), not a silent pass-through.
5. Healthcheck: `voxhub-server healthcheck` reports the configured `stores_dir` from settings in its `zarr_root`/renamed check entry.
