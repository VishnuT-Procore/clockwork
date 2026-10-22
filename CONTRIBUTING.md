# Contributing to deploytrain

Thanks for your interest! Bug reports, docs fixes and features are all welcome.

## Before you start

- **Bugs:** open an issue using the bug report template. Include a redacted config and the output of
  `deploytrain status`.
- **Features:** open an issue first to discuss the idea, especially for new config options.
  deploytrain aims to stay small, dependency-light and tool-agnostic. It decides *when* and
  *where* to run your commands, but it doesn't know about Kubernetes, AWS and so on.
- **Security issues:** don't open a public issue. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/VishnuT-Procore/clockwork
cd clockwork
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest
```

Supported Python versions: 3.10 – 3.13. CI runs the tests on Linux for every version and on macOS.

## Project layout

| Path | What it does |
|---|---|
| `deploytrain/windows.py` | Time windows, timezones, blackouts. Pure logic with no I/O. |
| `deploytrain/config.py` | YAML loading and validation |
| `deploytrain/train.py` | The orchestrator: waves, regions, bake, rollback, approvals, state file |
| `deploytrain/notify.py` | Slack notifications |
| `deploytrain/web.py`, `deploytrain/ui/index.html` | Web UI: JSON API plus a single-page dashboard with no build step |
| `deploytrain/cli.py` | Command-line interface |

## Guidelines

- **Tests:** add a test for every behaviour change. The train tests use a fake clock and a
  fake command runner (see `tests/test_deploytrain.py`), so they run instantly and need no
  real infrastructure.
- **Dependencies:** keep runtime dependencies minimal. Today there is one, PyYAML. The web
  UI is plain HTML/CSS/JS with no CDN, so it works on air-gapped networks.
- **Notifications:** a notification failure must never break a deploy.
- **State format:** state files must stay resumable across versions. If you change the state
  format, make sure older state files still load.
- **CHANGELOG:** update `CHANGELOG.md` under "Unreleased" for user-visible changes.

## Pull requests

1. Fork the repo, then create a branch from `main`.
2. Make your change with tests, and make sure `python -m pytest` passes.
3. Open a PR describing what changed and why.

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).

## Releasing (maintainers)

Releases are published to PyPI by `.github/workflows/release.yml` using
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored.

One-time setup:
1. On PyPI, add a pending trusted publisher for the project `deploytrain`, with this repo,
   workflow `release.yml` and environment `pypi`.
2. In the GitHub repo settings, create an environment named `pypi`. Optionally require a
   reviewer for it.

To cut a release:
1. Bump `version` in `pyproject.toml` and `deploytrain/__init__.py`.
2. Move the "Unreleased" notes in `CHANGELOG.md` under the new version.
3. Merge to `main`, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
