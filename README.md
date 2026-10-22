# deploytrain

Rolls a release out region by region in **waves**. Each region deploys only inside its own
**time window**, which is timezone-aware, supports overnight windows and blackouts, and
can require a minimum amount of window left before a deploy starts. After each deploy the
train **bakes** the region and runs health checks, and it **rolls back** and stops on failure.

deploytrain does not deploy anything itself. You supply shell commands (helm, kubectl,
terraform, CodeDeploy, etc.), and deploytrain decides *when* and *where* to run them.

```
pip install -e .
deploytrain -c examples/train.yaml plan                 # projected schedule per region
deploytrain -c examples/train.yaml windows              # upcoming open windows
deploytrain -c examples/train.yaml run --version v1.4.0 --previous-version v1.3.2
deploytrain -c examples/train.yaml status --version v1.4.0
deploytrain -c examples/train.yaml abort  --version v1.4.0   # from another shell
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
`wave_completed`, `region_waiting`, `region_deploying`, `region_succeeded`, `region_failed`
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

## Tests

```
pip install pytest && python -m pytest
```
