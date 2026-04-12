# known issues

Metainfo: write issues as checkable markdown list items here for future reference. Update upn tackling the issue to keep list fresh. 

- [ ] `packages/voxhub-client/src/voxhub_client/cli.py` is a stale verbatim copy of `packages/voxhub-core/src/voxhub_core/server/cli.py` (same `"""SSH-invoked server CLI."""` docstring, same handlers, imports from `voxhub_core.server.*`). Violates the core/client independence rule in `CLAUDE.md` — client must not depend on core. Needs to be replaced with an actual client-side CLI that drives `SshRunner` against a remote `voxhub-server`.
