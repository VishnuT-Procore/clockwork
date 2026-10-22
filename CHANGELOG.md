# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-22

First public release.

### Added
- **Deployment train:** regions are grouped into sequential waves, with `max_parallel`
  regions at a time within a wave and an optional `gap` between waves.
- **Time windows per region:**
  - IANA timezones with DST handling, day ranges and windows that cross midnight
  - `min_remaining`, so a deploy doesn't start too close to the end of a window
  - global and per-profile blackouts
- **Bake and health checks:** each region bakes for a set time while a health check runs
  periodically. On failure, rollback runs for the region, wave or whole train
  (`rollback_scope`).
- **Resume:** state is saved so runs can resume after a crash or abort. `--retry-failed`
  re-runs failed regions.
- **Approval gates:** a wave with `approval: true` waits until someone approves it in the
  web UI or with `deploytrain approve`.
- **CLI commands:** `plan`, `windows`, `run`, `status`, `abort`, `approve`, `ui` and
  `notify-test`.
- **Slack notifications:**
  - bot-token mode, with a live-updating summary message and events in its thread
  - incoming-webhook mode
  - a mention on failures and a link to the web UI for approvals
- **Web UI:**
  - runs list, wave/region pipeline, window status, events, live log and an audit trail
  - actions to start, approve, abort, resume and retry
  - token auth or SSO-proxy identity, plus CSRF protection

[Unreleased]: https://github.com/VishnuT-Procore/clockwork/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/VishnuT-Procore/clockwork/releases/tag/v0.1.0
