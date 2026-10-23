# traintraintrain

[![CI](https://github.com/VishnuT-Procore/traintraintrain/actions/workflows/ci.yml/badge.svg)](https://github.com/VishnuT-Procore/traintraintrain/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/traintraintrain.svg)](https://pypi.org/project/traintraintrain/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python versions](https://img.shields.io/pypi/pyversions/traintraintrain.svg)](https://pypi.org/project/traintraintrain/)
[![Downloads](https://img.shields.io/pypi/dm/traintraintrain.svg)](https://pypistats.org/packages/traintraintrain)

Runs your releases like a railway. Each version is a **train** that calls at a series of
**stops** in order: a canary first, then progressively larger groups of **platforms**
(regions, clusters, …). Each platform keeps its own **timetable**, so deploys only start
during its service hours, in its own timezone. After each deploy the train **dwells** at
the platform while it's inspected (health checks). On failure it **rolls back** and halts.
A **signal** can hold the train at a stop until a person clears it.

traintraintrain does not deploy anything itself. You supply shell commands (helm, kubectl,
terraform, CodeDeploy, etc.), and traintraintrain decides *when* and *where* to run them.

![traintraintrain web UI: a release on its route, one platform dwelling and one held at a signal](docs/screenshot.png)

## Vocabulary

| Term | Meaning |
|---|---|
| **train** | One version being rolled out |
| **stop** | A step in the rollout: a group of platforms served together |
| **platform** | One deploy target: a region, cluster, data centre, … |
| **timetable** | When a platform may receive deploys: **service hours** in a timezone, minus **line closures** |
| **held at signal** | The platform is waiting for its timetable to come into service |
| **dwell** | How long the train waits at a platform after deploying, while **inspections** (health checks) run |
| **headway** | Minimum gap between finishing one stop and starting the next |
| **signal** | A manual gate: the stop waits until someone **clears the signal** |
| **emergency brake** | Stop the train at its next checkpoint (resumable) |

## Features

- **Stops:** a canary first, then progressively larger stops, serving up to
  `parallel_platforms` platforms at a time.
- **Timetables per platform:** e.g. "Mon–Thu 09:00–16:00 Europe/Dublin". DST is handled,
  service hours can cross midnight, and line closures can apply globally or to one
  timetable (holiday freezes, launch days).
- **Dwell and inspection:** after each deploy, the platform dwells while inspections run.
  On failure the train rolls back (the platform, the stop or the whole train) and halts.
- **Signals:** a stop can wait for someone to clear its signal before deploying.
- **Knows what's live where:** every deploy and rollback is recorded per platform, so each
  region compares with, and rolls back to, the version it really runs, and you see what a
  version would change in every region before it departs.
- **Resumable:** the train survives crashes and restarts. Re-run the same command to
  continue, or retry just the failed platforms.
- **Slack:** one live-updating message per train, with details in its thread and a
  mention on failures.
- **Web UI:** a departure board where you can dispatch trains, clear signals, pull the
  brake and retry, with an audit trail of who did what.
- **Small:** one runtime dependency (PyYAML). The UI has no build step and needs no CDN.

## Install

Requires Python 3.10+, on Linux or macOS.

```
pip install traintraintrain
```

This installs the `traintraintrain` command.

Or use the container image; see [Docker and Kubernetes](#docker-and-kubernetes).

## Quick start

```
traintraintrain -c examples/train.yaml timetable            # projected departure per platform
traintraintrain -c examples/train.yaml service              # upcoming service hours
traintraintrain -c examples/train.yaml depart --version v1.4.0 --previous-version v1.3.2
traintraintrain -c examples/train.yaml board --version v1.4.0
traintraintrain -c examples/train.yaml clear-signal --version v1.4.0 --stop atlantic
traintraintrain -c examples/train.yaml brake --version v1.4.0   # from another shell
traintraintrain -c examples/train.yaml ui      # web dashboard on http://127.0.0.1:8080
```

## How a journey runs

1. The train calls at stops in order. It leaves a stop only after every platform there
   has finished dwelling. Then it waits out the next stop's `headway`, if one is set.
2. If a stop has `signal: true`, the train waits there until someone clears the signal.
3. At each stop, up to `parallel_platforms` platforms are served at once. Each platform:
   - **is held at a signal** until its timetable is in service. If that's further away
     than `max_hold`, the platform fails.
   - **deploys** by running `deploy_command`, which is subject to `deploy_timeout`.
   - **dwells** for `dwell`, running `inspection_command` every `inspection_interval`.
4. On any failure the train halts and serves no new platforms. Platforms already being
   served finish, and rollback runs according to `rollback_scope`:
   - `none`
   - `platform`: only the failed platform
   - `stop`: the failed stop
   - `train`: every platform updated on this journey
5. State is saved to `.traintraintrain/<train>-<version>.json`. Re-running the same command
   resumes from that state, and platforms that succeeded are skipped. Failed platforms
   need `--retry-failed`.

## Config

See [`examples/train.yaml`](examples/train.yaml). `timetable`, `dwell` and
`deploy_timeout` can be set per platform, per stop, or as train defaults
(`default_timetable`, `default_dwell`, `deploy_timeout`). The most specific setting wins.

| Timetable key | Meaning |
|---|---|
| `timezone` | IANA zone for the service hours, e.g. `Europe/Dublin`. DST is handled. |
| `service_hours[].days` | `mon-thu`, `[mon, wed]`, `fri-mon` (wraps around) or `all` |
| `service_hours[].start/end` | `"HH:MM"` local time. If `end <= start`, service runs past midnight. |
| `min_remaining` | Don't start a deploy unless at least this much service time is left |
| `line_closures` | `{start, end, reason, timezone?}`. A date-only `end` is inclusive. |

Global `train.line_closures`, such as a holiday freeze, apply to every timetable. A
timetable without `service_hours` is always in service. The built-in timetable `always`
is also available.

Commands are templates using `{train} {platform} {stop} {version} {previous_version}`
plus any platform `vars`. The same values are exported as env vars (`DT_PLATFORM`,
`DT_STOP`, `DT_CLUSTER`, …). Escape literal braces as `{{ }}`.

## Signals

A stop with `signal: true` stops the train before deploying. The train's status becomes
`at_signal`, and Slack posts a message that links to the UI if `ui_url` is set. The
train proceeds once someone clears the signal, either in the web UI or with
`traintraintrain clear-signal --version V --stop S`. The name of whoever cleared it is
recorded. If you brake and then resume, a signal that was already cleared stays cleared.

## Web UI

```
traintraintrain -c train.yaml ui --host 127.0.0.1 --port 8080
```

**What you can see**
- **Departures**, showing each train's status and progress.
- **Route view.** Each platform shows its status and any failure output. It also shows
  whether its timetable is in service, or when service resumes in the platform's own
  timezone.
- **Events**, with routine inspections hidden unless you ask for them.
- **Live log** of the journey.
- **"Who did what"**, an audit trail of actions taken in the UI.
- **Timetable**, showing each platform's service right now and projected departures for a
  train leaving now.

**What you can do**
- **New departure**, with a dry-run option and a preview of the timetable.
- **Clear a signal.**
- **Pull the emergency brake.**
- **Resume** an interrupted or braked journey.
- **Retry failed platforms.**

**How it works**
- The UI only reads state files. Each train runs as a separate `traintraintrain depart`
  process, logging to `.traintraintrain/<train>-<version>.log`. The UI can restart without
  affecting trains en route.
- The brake and signals use the same marker files as the CLI.
- Only one process may drive a train at a time. A train whose process died shows as
  *interrupted*, and you can resume it.

**Access and auth**
- By default the UI listens only on localhost.
- **Shared deployments:** put the UI behind your SSO proxy (oauth2-proxy, IAP, etc.).
  The user name is taken from `X-Forwarded-User` / `X-Auth-Request-User` (or the
  matching `...-Email` headers) and recorded on every action.
- **Shared token:** alternatively, set `TRAINTRAINTRAIN_UI_TOKEN` to require a token. Users
  then enter a name in the header, which is self-reported.
- Every POST must send `X-Requested-With: traintraintrain` (CSRF protection).
- Actions are appended to `.traintraintrain/<train>.audit.jsonl`.

## Slack notifications

```yaml
notifications:
  slack:
    channel: "#deploys"             # bot mode needs a channel plus $SLACK_BOT_TOKEN
    token_env: SLACK_BOT_TOKEN      # default
    webhook_url_env: SLACK_WEBHOOK_URL  # default; used when no bot token is set
    mention_on_failure: "<!here>"   # or "<!subteam^S0123ABCD>" / "<@U0123ABCD>"
    ui_url: https://traintraintrain.example.com  # signal messages link here
    events: [...]                   # optional filter, see below
```

**Bot mode (recommended).** Create a Slack app with the `chat:write` scope, install it,
invite it to the channel and export `SLACK_BOT_TOKEN`.
- Each train gets **one message in the channel**. It is edited live with a per-stop
  status board, e.g. `canary ✅ us-west-2 · atlantic 🚦 eu-west-1 🚀 us-east-1`.
- Every event is posted as a **reply in that message's thread**.
- Failures, red signals and the final result are also broadcast to the channel.
- If you resume the train, updates go to the same thread.

**Webhook mode.** Export `SLACK_WEBHOOK_URL` (an incoming webhook). Each event is posted as
its own message.

The available events are `train_started`, `train_resumed`, `train_finished`,
`stop_started`, `stop_completed`, `stop_at_signal`, `signal_cleared`, `platform_held`,
`platform_deploying`, `platform_succeeded`, `platform_failed` and
`platform_rolled_back`. All are sent by default except `platform_deploying`. Failure
messages include the last part of the failing command's output, plus the
`mention_on_failure` mention.

**If Slack is unavailable.** A Slack error or a missing credential is logged as a warning
and never stops a deploy.
- `traintraintrain notify-test` sends a test message so you can check the setup.
- `depart --no-notify` turns notifications off for one journey.
- Notifications are always off in `--dry-run`.

## What's in this release

Tell the train how to find the changes between the version it's deploying and the one
each platform runs now (see [Live versions](#live-versions-per-platform)):

```yaml
train:
  changes:
    url: "https://github.com/acme/payments-api/compare/{previous_version}...{version}"
    command: "git -C /repos/payments-api log --oneline {previous_version}..{version}"
```

- **`url`:** a compare link, shown as **"What's in this release"** on the departure page,
  in the New departure dialog as you type the version, and in the Slack departure
  message. Any `http(s)` URL works: GitHub, GitLab, Bitbucket, your own changelog.
- **`command`:** runs once when a train departs. Its output is saved with the departure
  and listed in the UI's **Changes** tab; `git log --oneline` lines show the short hash
  and message.
- **CLI:** `traintraintrain board --version V --changes` prints the same list.

Both use `{version}`, `{previous_version}` and `{train}`. They run once per distinct live
version: if the canary is already on `v1.3.0` and the other regions are on `v1.2.0`, you
get two lists, each naming its platforms (`$DT_PLATFORMS` holds them for the command).
A platform with no known version has nothing to compare against. The command is
informational only: if it fails, the error is recorded and the deploy carries on. It
also runs for dry runs and previews, so keep it read-only.

**Monorepos:** limit the command to the folders the service is built from, so commits to
other services are left out:

```yaml
    command: "git -C /repos/mono log --oneline {previous_version}..{version} -- services/payments libs/billing"
```

Versions may contain `/`, so per-service tags such as `payments/v1.3.0` work as they are.

## Live versions per platform

traintraintrain records which version each platform runs, in the same storage as
everything else (the `train_platform_versions` and `train_platform_history` tables in
Postgres). It's updated when a platform's deploy succeeds or a rollback completes; failed
and aborted deploys go into the history only.

When a train departs, each platform:

- **compares** the new version with what it runs, so a region that's behind gets the
  longer change list
- **rolls back** to what it ran, through `{previous_version}` in `rollback_command`

`--previous-version` (the dialog's optional field) is now only used for platforms with no
known version, such as the very first departure. If you leave it out, the departure's
rollback target shown in the UI is the version most platforms ran.

**Before departing**, the New departure dialog shows each platform's live version and what
the typed version would change there. The CLI does the same:

```console
$ traintraintrain preview --version v1.4.0 --changes
  STOP         PLATFORM           LIVE NOW         CHANGES
  canary       us-west-2-canary   v1.3.0           2 change(s)
  atlantic     us-east-1          v1.3.0           2 change(s)
  worldwide    ap-northeast-1     v1.2.0           6 change(s)
  ...
```

**Ask the platform itself.** Deploys made outside traintraintrain (a hotfix, a rollback in
Argo CD) aren't recorded. Set `live_version_command` and the platform is asked first; it
gets the platform's `vars` and prints the version as its last line:

```yaml
train:
  live_version_command: >-
    kubectl --context {cluster} -n payments get deploy api
    -o jsonpath='{{.spec.template.spec.containers[0].image}}' | sed 's/.*://'
  live_version_timeout: 30s
```

If the answer differs from the record, the departure logs it and records the observed
version. If the command fails, the recorded version is used.

**Other commands:**

```bash
traintraintrain live                     # what each platform runs (--probe to ask them)
traintraintrain live --history           # every deploy, rollback and failure, newest first
traintraintrain live-set --platform us-east-1 --version v1.3.1   # record a hand-made change
```

The UI's Timetable page shows a **Live now** column and the same history. Existing
installs need nothing: until a platform has a record, its version is worked out from past
departures. With Postgres, the new tables are created automatically, or by `db migrate`.

## Storage: files or Postgres

By default, each train's state, its log, the audit trail and brake/signal requests are
kept as files in `state_dir` (`.traintraintrain/` next to the config, or `/state` in the
container). That needs nothing extra and suits a single machine or container.

To keep everything in **Postgres** instead, install the driver and give it a connection URL:

```bash
pip install "traintraintrain[postgres]"     # the Docker image already includes it
export TRAINTRAINTRAIN_DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/traintraintrain"
traintraintrain -c train.yaml db migrate    # creates the tables (also done automatically)
```

Keep the URL in an environment variable or secret, not in `train.yaml`. Any libpq URL works,
e.g. add `?sslmode=require` for TLS.

| | Files | Postgres |
|---|---|---|
| Setup | none | a database and a URL |
| Who is driving a train | PID + heartbeat in the state file | an advisory lock, released the moment that process dies |
| UI and Jobs sharing state | need a shared volume | just share the URL |
| Logs and audit trail | files in `state_dir` | `train_logs` and `train_audit` tables |
| Querying history | read JSON files | SQL (`train_departures.data` is JSONB) |

**Moving existing state into Postgres:**
1. Set `TRAINTRAINTRAIN_DATABASE_URL`.
2. Run `traintraintrain -c train.yaml db import`. Add `--from DIR` for another state folder,
   or `--overwrite` to replace departures already in the database.
3. Run `traintraintrain -c train.yaml db status` to see which storage is in use and how
   much it holds.

The tables are `train_departures`, `train_requests`, `train_audit`, `train_logs` and
`train_schema`, created in the connection's default schema.

## Docker and Kubernetes

Images are published to GitHub Container Registry for `linux/amd64` and `linux/arm64`:
`ghcr.io/vishnut-procore/traintraintrain:<version>`, plus `:edge` built from `main`.

```bash
# Web UI, with config mounted read-only and state on a volume
docker run -p 127.0.0.1:8080:8080 -e TRAINTRAINTRAIN_UI_TOKEN=change-me \
  -v ./config:/config:ro -v traintraintrain-state:/state ghcr.io/vishnut-procore/traintraintrain:0.3.0

# One train as a one-off job (exits 0 when it arrives, 1 if it fails or is braked)
docker run -v ./config:/config:ro -v traintraintrain-state:/state \
  ghcr.io/vishnut-procore/traintraintrain:0.3.0 depart --version v1.4.0 --previous-version v1.3.2
```

**Add your deploy tools to the image.** Your `deploy_command`, `inspection_command` and
`rollback_command` run inside the container. Build your own image `FROM` this one with
kubectl, helm, aws, … installed;
[`examples/docker/Dockerfile.tools`](examples/docker/Dockerfile.tools) shows how.

**How the image behaves**
- **Config and state:** the config is read from `/config/train.yaml`
  (`TRAINTRAINTRAIN_CONFIG`), and progress is saved under `/state` (`TRAINTRAINTRAIN_STATE_DIR`,
  which overrides the config's `state_dir`). Keep `/state` on a persistent volume.
- **Non-root:** the image runs as user `10001`. Bind-mounted state directories must be
  writable by that user.
- **Health check:** `/healthz` needs no token, and the image's `HEALTHCHECK` uses it.
- **Stopping a container** (`docker stop`, a pod shutting down) is graceful. A `depart`
  job pulls its train's emergency brake. The UI brakes the trains it dispatched and waits
  up to `TRAINTRAINTRAIN_SHUTDOWN_GRACE` seconds (default 20). The journey can then be
  resumed.
- **Restarts are recoverable.** The process driving a train writes a heartbeat to the
  state file. If a container is replaced mid-journey, the train shows as *interrupted*
  once its heartbeat is 90 seconds old, and you can resume it with no `--force`.
- **Run one UI instance per state directory.** Two instances could drive the same train
  twice.

**Examples**
- [`examples/docker/docker-compose.yml`](examples/docker/docker-compose.yml): the UI
  with a persistent volume. Run it with
  `TRAINTRAINTRAIN_UI_TOKEN=change-me docker compose up --build`.
- [`examples/docker/docker-compose.postgres.yml`](examples/docker/docker-compose.postgres.yml):
  the UI with its state in a Postgres container. To use your own Postgres, drop that
  service and set `TRAINTRAINTRAIN_DATABASE_URL`.
- [`examples/kubernetes/ui.yaml`](examples/kubernetes/ui.yaml): the UI as a
  single-replica Deployment (`Recreate` strategy) with a PersistentVolumeClaim, probes,
  a non-root security context and a Service.
- [`examples/kubernetes/job.yaml`](examples/kubernetes/job.yaml): one train per release
  as a Kubernetes Job, for CD pipelines.

## Running it in CI

A train can take days to complete its journey while it waits for service hours, so run
it as a long-lived job, such as a Kubernetes Job (see above) or a self-hosted runner,
with `state_dir` on persistent storage. Alternatively, run it on a schedule (cron or a
scheduled pipeline) with a short `max_hold`. Each invocation then resumes and travels as
far as the current timetables allow.

## Upgrading from the old names

Earlier development versions used generic terms, and the project was called
`deploytrain`. The old names are still accepted, so existing configs, scripts and state
files keep working. Using an old name and its new name together is an error.

| Old | New |
|---|---|
| `waves:` / `regions:` | `stops:` / `platforms:` |
| `windows:` (profiles) / `windows:` (inside a profile) | `timetables:` / `service_hours:` |
| `window:` / `default_window` | `timetable:` / `default_timetable` |
| `blackouts` | `line_closures` |
| `bake` / `default_bake` | `dwell` / `default_dwell` |
| `gap` | `headway` |
| `max_parallel` | `parallel_platforms` |
| `approval: true` | `signal: true` |
| `health_check_command` / `health_check_interval` | `inspection_command` / `inspection_interval` |
| `window_wait_timeout` | `max_hold` |
| `rollback_scope: region / wave` | `rollback_scope: platform / stop` |
| `{region}` / `{wave}`, `DT_REGION` / `DT_WAVE` | `{platform}` / `{stop}`, `DT_PLATFORM` / `DT_STOP` |
| `plan`, `windows`, `run`, `status`, `approve --wave`, `abort` | `timetable`, `service`, `depart`, `board`, `clear-signal --stop`, `brake` |
| Slack events `wave_*` / `region_*` | `stop_*`, `signal_cleared` / `platform_*` |
| `deploytrain` command, `python -m deploytrain` | `traintraintrain` (the `deploytrain` command still works) |
| `DEPLOYTRAIN_*` environment variables | `TRAINTRAINTRAIN_*` (old names still read; new ones win) |
| `.deploytrain` state folder | `.traintraintrain` (an existing `.deploytrain` folder keeps being used) |
| `import deploytrain`, image `ghcr.io/vishnut-procore/deploytrain` | `import traintraintrain`, image `ghcr.io/vishnut-procore/traintraintrain` |

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, and our [Code of Conduct](CODE_OF_CONDUCT.md). To report a security
issue, please follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

```
pip install -e ".[dev]" && python -m pytest
```

## License

[Apache License 2.0](LICENSE). Copyright 2026 Vishnuvardhan.
