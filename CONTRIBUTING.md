# Contributing to Kubernetes MCP Server

Thank you for your interest in contributing.
This document defines the expected workflow for changes, testing, and reviews.

## Before You Start

- Search existing issues and pull requests to avoid duplicate work.
- For large changes, open an issue first to align on approach.
- Be respectful and follow the project Code of Conduct.

## Development Setup

Prerequisites:

- Python 3.11+
- Go 1.26+ (for `proxy/` changes)

Setup:

```bash
git clone <your-fork-or-repo-url>
cd kubernetes-mcp
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

## Local Validation

Run these before opening a PR:

```bash
ruff check src tests
PYTHONPATH=. pytest tests/test_server.py -q
```

If your change affects the Go proxy:

```bash
cd proxy
go build ./...
```

## Live/Environment-Dependent Tests

Some tests require a live cluster (OpenShift CRC or Kubernetes API proxy), for
example:

- `tests/test_mcp_integration.py`
- `tests/test_e2e_openshift.py`
- `tests/test_live_openshift.py`
- `tests/test_live_tools.py`

Run them only when your environment is configured for live access.

## Pull Request Guidelines

- Keep changes focused and atomic.
- Include/adjust tests for behavior changes.
- Update docs when interfaces, defaults, or workflows change.
- Use clear commit messages.

PR checklist:

- [ ] I ran lint and relevant tests locally.
- [ ] I updated docs/config references impacted by this change.
- [ ] I verified no secrets or credentials were committed.
- [ ] I linked related issues (if applicable).

## Reporting Security Issues

Do not open public issues for vulnerabilities.
See `SECURITY.md` for private disclosure instructions.