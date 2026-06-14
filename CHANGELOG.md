# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project aims to follow
Semantic Versioning.

## [Unreleased]

### Added

- **Custom Resource (CRD/CR) support**: `list_crds`, `list_api_resources`
  (like `kubectl api-resources`), and `list_custom_resources`/`get_custom_resource`
  that query CRs **by Kind alone** — the CRD is looked up to resolve group,
  served version, plural, and scope automatically.
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
- Generic resource ops now resolve the plural name and scope via **API
  discovery** (cached, with heuristic fallback) instead of the `_kind_to_plural`
  guess, which produced wrong plurals for many CRDs (e.g. Kind `Gateway` →
  `gatewaies`).
- `Dockerfile` now copies the package source, `README.md`, and `LICENSE` before
  `pip install .`, so the hatchling build succeeds.

### Changed

- Server `instructions` now steer agents toward the consolidated tools.
- Standardized project metadata and documentation for open-source readiness.
