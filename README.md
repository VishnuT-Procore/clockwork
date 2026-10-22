# deploytrain

[![CI](https://github.com/VishnuT-Procore/clockwork/actions/workflows/ci.yml/badge.svg)](https://github.com/VishnuT-Procore/clockwork/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)

Rolls a release out region by region in **waves**. Each region deploys only inside its own
**time window**, which is timezone-aware, supports overnight windows and blackouts, and
can require a minimum amount of window left before a deploy starts. After each deploy the
train **bakes** the region and runs health checks, and it **rolls back** and stops on failure.

deploytrain does not deploy anything itself. You supply shell commands (helm, kubectl,
terraform, CodeDeploy, etc.), and deploytrain decides *when* and *where* to run them.

![deploytrain web UI: a release rolling out wave by wave, one region baking and one waiting for its window](docs/screenshot.png)

## Features

- **Waves:** canary first, then progressively larger groups of regions, with
  `max_parallel` regions at a time within a wave.
- **Time windows per region:** e.g. "Mon–Thu 09:00–16:00 Europe/Dublin". DST is handled,
  windows can cross midnight, and global or per-region blackouts are supported (holiday
  freezes, launch days).
- **Bake and health checks:** after each deploy the region bakes while health checks run.
  On failure the train rolls back (the region, the wave or the whole train) and stops.
- **Approval gates:** a wave can wait for a human to approve it before deploying.
- **Resumable:** the train survives crashes and restarts. Re-run the same command to
  continue, or retry just the failed regions.
- **Slack:** one live-updating message per train, with details in its thread and a
  mention on failures.
- **Web UI:** see status, start runs, approve, abort and retry, with an audit trail of who
  did what.
- **Small:** one runtime dependency (PyYAML). The UI has no build step and needs no CDN.

## Install

Requires Python 3.10+, on Linux or macOS.

```
pip install "git+https://github.com/VishnuT-Procore/clockwork"
```

## Quick start

```
deploytrain -c examples/train.yaml plan                 # projected schedule per region
deploytrain -c examples/train.yaml windows              # upcoming open windows
deploytrain -c examples/train.yaml run --version v1.4.0 --previous-version v1.3.2
deploytrain -c examples/train.yaml status --version v1.4.0
deploytrain -c examples/train.yaml abort  --version v1.4.0   # from another shell
deploytrain -c examples/train.yaml approve --version v1.4.0 --wave wave-2
deploytrain -c examples/train.yaml ui      # web dashboard on http://127.0.0.1:8080
```

## How it runs

1. Waves run in order. A wave starts only after every region in the previous wave has
   finished baking, plus the optional `gap`.
2. Within a wave, up to `max_parallel` regions run at once. Each region:
   - **waits** until its window profile allows a start (`next_start`). If that is further
     away than `window_wait_timeout`, the region fails.
   - **deploys** by running `deploy_command`, which is subject to `deploy_timeout`.
   - **bakes** for `bake`, running `health_check_command` every `health_check_interval`.
3. On any failure the train stops scheduling new regions. In-flight regions finish, and
   rollback runs according to `rollback_scope`:
   - `none`
   - `region`: only the failed region
   - `wave`: the failed wave
   - `train`: every region updated in this run
4. State is saved to `.deploytrain/<train>-<version>.json`. Re-running the same command
   resumes from that state, and succeeded regions are skipped. Failed regions need `--retry-failed`.

## Config

See [`examples/train.yaml`](examples/train.yaml). Settings cascade from region to wave to
`train` defaults, for `window`, `bake` and `deploy_timeout`.

| Window profile key | Meaning |
|---|---|
| `timezone` | IANA zone for the window times, e.g. `Europe/Dublin`. DST is handled. |
| `windows[].days` | `mon-thu`, `[mon, wed]`, `fri-mon` (wraps around) or `all` |
| `windows[].start/end` | `"HH:MM"` local time. If `end <= start`, the window crosses midnight. |
| `min_remaining` | Don't start a deploy unless at least this much of the window is left |
| `blackouts` | `{start, end, reason, timezone?}`. A date-only `end` is inclusive. |

Global `train.blackouts`, such as a holiday freeze, apply to every profile. A profile
without `windows` is always open. The built-in profile `always` is also available.

Commands are templates using `{train} {region} {wave} {version} {previous_version}` plus
any region `vars`. The same values are exported as env vars (`DT_REGION`, `DT_CLUSTER`, …).
Escape literal braces as `{{ }}`.

## Approval gates

A wave with `approval: true` pauses before deploying. The run's status is set to
`awaiting_approval` and Slack posts a message, which links to the UI if `ui_url` is set.
The wave continues once someone approves it, either in the web UI or with
`deploytrain approve --version V --wave W`. The approver's name is recorded in the run.
If you abort and resume a run, an approval it already has is kept.

## Web UI

```
deploytrain -c train.yaml ui --host 127.0.0.1 --port 8080
```

**What you can see**
- **Runs list**, showing each run's status and progress.
- **Pipeline view.** Each region shows its status, its failure output, and whether its
  window is open (or when it opens next, in the region's own timezone).
- **Events**, with routine health checks hidden unless you ask for them.
- **Live log** of the run.
- **"Who did what"**, an audit trail of actions taken in the UI.
- **Regions & windows**, an overview of every region's window right now, plus a projected
  schedule for a run started now.

**What you can do**
- **New run**, with a dry-run option and a preview of the projected schedule.
- **Approve** a wave.
- **Abort** a run.
- **Resume** an interrupted or aborted run.
- **Retry failed regions.**

**How it works**
- The UI only reads state files. Runs are started as separate `deploytrain run` processes,
  logging to `.deploytrain/<train>-<version>.log`, so the UI can restart without affecting them.
- Abort and approve use the same marker files as the CLI.
- Only one process may own a run at a time. A run whose process died shows as
  *interrupted*, and you can resume it.

**Access and auth**
- By default the UI listens only on localhost.
- **Shared deployments:** put the UI behind your SSO proxy (oauth2-proxy, IAP, etc.).
  The user name is taken from `X-Forwarded-User` / `X-Auth-Request-User` (or the
  matching `...-Email` headers) and recorded on every action.
- **Shared token:** alternatively, set `DEPLOYTRAIN_UI_TOKEN` to require a token. Users
  then enter a name in the header, which is self-reported.
- Every POST must send `X-Requested-With: deploytrain` (CSRF protection).
- Actions are appended to `.deploytrain/<train>.audit.jsonl`.

## Slack notifications

```yaml
notifications:
  slack:
    channel: "#deploys"             # bot mode needs a channel plus $SLACK_BOT_TOKEN
    token_env: SLACK_BOT_TOKEN      # default
    webhook_url_env: SLACK_WEBHOOK_URL  # default; used when no bot token is set
    mention_on_failure: "<!here>"   # or "<!subteam^S0123ABCD>" / "<@U0123ABCD>"
    events: [...]                   # optional filter, see below
```

**Bot mode (recommended).** Create a Slack app with the `chat:write` scope, install it,
invite it to the channel and export `SLACK_BOT_TOKEN`.
- Each train gets **one message in the channel**. It is edited live with a per-wave status
  board, e.g. `canary ✅ us-west-2 · wave-2 ⏳ eu-west-1 🚀 us-east-1`.
- Every event is posted as a **reply in that message's thread**.
- Failures and the final result are also broadcast to the channel.
- If you resume the train, updates go to the same thread.

**Webhook mode.** Export `SLACK_WEBHOOK_URL` (an incoming webhook). Each event is posted as
its own message.

The available events are `train_started`, `train_resumed`, `train_finished`, `wave_started`,
`wave_completed`, `wave_awaiting_approval`, `wave_approved`, `region_waiting`, `region_deploying`, `region_succeeded`, `region_failed`
and `region_rolled_back`. All are sent by default except `region_deploying`. Failure
messages include the last part of the failing command's output, plus the
`mention_on_failure` mention.

**If Slack is unavailable.** A Slack error or a missing credential is logged as a warning
and never stops a deploy.
- `deploytrain notify-test` sends a test message so you can check the setup.
- `run --no-notify` turns notifications off for one run.
- Notifications are always off in `--dry-run`.

## Running it in CI

A train can run for days while it waits for windows, so run it as a long-lived job, such
as a Kubernetes Job or a self-hosted runner, with `state_dir` on persistent storage.
Alternatively, run it on a schedule (cron or a scheduled pipeline) with a short
`window_wait_timeout`. Each invocation then resumes and advances as far as the currently
open windows allow.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
development setup, and our [Code of Conduct](CODE_OF_CONDUCT.md). To report a security
issue, please follow [SECURITY.md](SECURITY.md) rather than opening a public issue.

```
pip install -e ".[dev]" && python -m pytest
```

## License

[Apache License 2.0](LICENSE). Copyright 2026 Vishnuvardhan.
