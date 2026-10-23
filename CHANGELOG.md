# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-10-23

### Added
- **Live version per platform**, stored with the rest of the state: in Postgres, the
  `train_platform_versions` and `train_platform_history` tables (schema version 2, created
  automatically); with files, `<train>.live.json` and `<train>.live-history.jsonl`. It's
  updated on every successful deploy and rollback. Failed and aborted deploys are kept in
  the history.
- **Per-platform comparison and rollback.** Each platform compares the new version with the
  version it runs and rolls back to it (`{previous_version}` is now per platform). Change
  lists are grouped by live version (`changes.groups`), and `$DT_PLATFORMS` names each
  group's platforms.
- **Preview before departing.** The New departure dialog shows each platform's live
  version, and lists every change in the departure in one list, marking the changes that
  only reach some platforms (because they're behind). The CLI has
  `traintraintrain preview --version V`, and the API `GET /api/preview?version=V`.
- `live_version_command` (and `live_version_timeout`) asks a platform what it runs, so
  hotfixes and rollbacks made outside traintraintrain are noticed.
- New commands: `live` (`--probe`, `--history`) and `live-set`. New API: `GET /api/live`.
  The Timetable page has a **Live now** column and a live version history.
- **Track versions by tag or by commit SHA**, per train: `track_versions_by: tag` (default)
  or `sha`. `resolve_command` looks up the commit a version points to; SHAs are stored per
  platform (`sha`, `previous_sha`) and shown next to versions. In `sha` mode, change lists
  compare commits, so a moved tag can't make them wrong, and a new tag on the same commit
  shows as already running. Commands can use `{sha}` and `{previous_sha}`.
- `db import` also copies live versions and their history.
- Versions may contain `/`, for per-service monorepo tags like `payments/v1.3.0`.

### Changed
- `--previous-version` and the dialog's "Previous version" field are only used for
  platforms with no known live version. When it's left out, the departure's rollback
  target shown in the UI is the version most platforms ran.
- `board` shows each platform's previous version (`FROM`) and the change list per group.

## [0.2.0] - 2026-10-23

### Added
- **Postgres storage.** Set `TRAINTRAINTRAIN_DATABASE_URL` to keep departures, logs, the
  audit trail and brake/signal requests in Postgres instead of files. Install it with
  `pip install "traintraintrain[postgres]"`; the Docker image includes it. Files remain
  the default.
  - Ownership uses a Postgres advisory lock, which is released the moment the driving
    process dies. A train interrupted by a crash or a container restart can then be
    resumed without `--force`.
  - New commands: `db migrate` (create tables), `db status` (which storage is in use and
    what it holds), and `db import` (copy existing file-based state into Postgres).
  - New `docker-compose.postgres.yml` example, and a database URL in the Kubernetes
    Secret example.
- **What's in this release.** `train.changes.url` and `train.changes.command` record the
  changes between a departure's version and the previous one, using `{version}`,
  `{previous_version}` and `{train}`:
  - a "What's in this release" link on the departure page, in the New departure dialog
    and in the Slack departure message
  - a **Changes** tab listing the commits
  - `board --changes` in the CLI
  The command runs once per departure and never fails a deploy.

### Changed
- CI also runs the full test suite against a real Postgres 16, and the Docker workflow
  smoke-tests the image with Postgres.

## [0.1.1] - 2026-10-23

### Changed
- **Redesigned web UI**, built to be easy to read and well organised:
  - summary tiles for progress, current stop, what happens next, and journey time
  - a single "needs you" callout when someone has to act (a red signal, a halted or
    interrupted train)
  - the route shown as a line of stations, plus one table per stop with aligned columns
    (status, timetable open/closed, start time, duration, a plain-English note), and
    failure output shown under the failed platform
  - activity grouped by day, with readable messages and filters for problems and
    inspections
  - every status shown with an icon and a label, so colour is never the only cue; light
    and dark themes; a stacked layout on phones
- README: PyPI version, Python versions and downloads badges.

## [0.1.0] - 2026-10-22

First public release: `pip install traintraintrain`. (The project was called `deploytrain`
during development; that command name and its `DEPLOYTRAIN_*` variables still work.)

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
  with `traintraintrain clear-signal`.
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
  per-release Job). Config and state paths can be set with `TRAINTRAINTRAIN_CONFIG` /
  `TRAINTRAINTRAIN_STATE_DIR`, and the UI address with `TRAINTRAINTRAIN_UI_HOST` /
  `TRAINTRAINTRAIN_UI_PORT`.
- **Restart-safe ownership:** the process driving a train writes a heartbeat to its state
  file. A train whose heartbeat is stale can be resumed even if its host or container is
  gone.
- **Compatibility:** the generic names used during development (`waves`, `regions`,
  `windows`, `bake`, `approval`, `plan`, `run`, …) are still accepted as aliases. State
  files written with them still load. See "Upgrading from the old names" in the README.

[Unreleased]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/VishnuT-Procore/traintraintrain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/VishnuT-Procore/traintraintrain/releases/tag/v0.1.0
