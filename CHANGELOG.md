# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project aims to follow
Semantic Versioning.

## [Unreleased]

### Added

- **Token-efficient / consolidated tools** that fold multi-step workflows into a
  single high-signal call, reducing tool calls and tokens (grounded in
  Anthropic's agent-tooling guidance; see `docs/TOOL_EFFICIENCY.md`):
  `cluster_overview`, `namespace_overview`, `get_pod_context`,
  `get_deployment_context`, `project_resource`, and `batch_read` (read-only
  fan-out over an allow-list).
- `limit` parameter on `list_pods` and `list_resources`, and a
  `response_format="concise"` option on `get_pod`.
- `docs/TOOL_EFFICIENCY.md` (HLD/LLD + cited research + live-cluster
  measurements) and `.dockerignore`.
- Open-source governance and community files (`LICENSE`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, `SUPPORT.md`).
- GitHub templates for issues and pull requests.
- CI, CodeQL, Dependabot, and Scorecards workflows/configuration.

### Fixed

- Generic `get_resource`/`list_resources`/`describe_resource` (and the new
  `project_resource`) now work for **core** API kinds (Pod, Service, Node, …),
  which previously 404'd because they were routed through `CustomObjectsApi`
  (grouped-API only) instead of the core `/api/{version}` path.
- `Dockerfile` now copies the package source, `README.md`, and `LICENSE` before
  `pip install .`, so the hatchling build succeeds.

### Changed

- Server `instructions` now steer agents toward the consolidated tools.
- Standardized project metadata and documentation for open-source readiness.
