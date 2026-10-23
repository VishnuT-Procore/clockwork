# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-22

First public release, published on PyPI as `traintraintrain` (`pip install traintraintrain`).
It installs the `deploytrain` command and Python package.

### Added
- **Deployment train:** each version is a train that calls at **stops** in order, serving
  up to `parallel_platforms` **platforms** (regions, clusters, …) at a time. An optional
  `headway` sets a minimum gap between stops.
- **Timetables per platform:**
  - **service hours** in IANA timezones, with DST handling, day ranges and overnight
    service
  - `min_remaining`, so a deploy doesn't start too close to the end of service
  - global and per-timetable **line closures**
- **Dwell and inspection:** each platform dwells for a set time after deploying while
  inspections (health checks) run. On failure, rollback runs for the platform, the stop or
  the whole train (`rollback_scope`).
- **Resume:** state is saved so a journey can resume after a crash or the emergency brake.
  `--retry-failed` re-runs failed platforms.
- **Signals:** a stop with `signal: true` waits until someone clears it, in the web UI or
  with `deploytrain clear-signal`.
- **CLI commands:** `timetable`, `service`, `depart`, `board`, `clear-signal`, `brake`,
  `ui` and `notify-test`.
- **Slack notifications:**
  - bot-token mode, with a live-updating summary message and events in its thread
  - incoming-webhook mode
  - a mention on failures and a link to the web UI for signals
- **Web UI:**
  - departures, the route view, service status, events, live log and an audit trail
  - actions to dispatch, clear signals, brake, resume and retry
  - token auth or SSO-proxy identity, plus CSRF protection
- **Container image:** a multi-arch image (amd64 and arm64) on GitHub Container
  Registry. It runs as a non-root user, keeps its state on a `/state` volume, has a
  `/healthz` health check, and shuts down gracefully on SIGTERM by braking running
  trains. There are Docker Compose and Kubernetes examples (a UI Deployment and a
  per-release Job). Config and state paths can be set with `DEPLOYTRAIN_CONFIG` /
  `DEPLOYTRAIN_STATE_DIR`, and the UI address with `DEPLOYTRAIN_UI_HOST` /
  `DEPLOYTRAIN_UI_PORT`.
- **Restart-safe ownership:** the process driving a train writes a heartbeat to its state
  file. A train whose heartbeat is stale can be resumed even if its host or container is
  gone.
- **Compatibility:** the generic names used during development (`waves`, `regions`,
  `windows`, `bake`, `approval`, `plan`, `run`, …) are still accepted as aliases. State
  files written with them still load. See "Upgrading from the old names" in the README.

[Unreleased]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/VishnuT-Procore/traintraintrain/releases/tag/v0.1.0
