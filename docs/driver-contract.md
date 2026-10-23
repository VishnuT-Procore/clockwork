# The driver contract (v1)

A **driver** is one program of yours that performs the train's steps: deploying a
platform, checking its health, rolling it back, saying which version it runs, turning a
version into a commit, and listing the changes between two versions. Write it in any
language and wrap any deploy system (Helm, Argo CD, Spinnaker, Terraform, an internal API).

```yaml
train:
  driver:
    command: "python3 ./deployer.py"
    actions: [deploy, inspect, rollback, live_version, resolve, changes]   # optional
```

- `command` runs **without a shell**, from the folder that holds `train.yaml`. Quote
  arguments as you would in a shell (`"node driver.js --env prod"`).
- `actions` lists the steps the driver performs. It defaults to all of them. A step that
  also has its own command in `train.yaml` (`deploy_command`, `inspection_command`,
  `rollback_command`, `live_version_command`, `resolve_command`, `changes.command`) uses
  the command. That lets you move steps into the driver one at a time.
- `deploy_command` isn't needed when the driver deploys.

An example driver covering every action is in
[`examples/driver/deployer.py`](../examples/driver/deployer.py).

## How a call works

1. traintraintrain starts the program and writes **one JSON request** to its stdin.
2. The program does the work and prints **one JSON reply** to stdout. If it prints other
   lines too, the reply must be the last line.
3. Anything printed to **stderr** is the step's log. It's kept with the platform and
   shown in the UI, and never parsed.

The request's fields are also in the environment as `DT_*` variables (`DT_ACTION`,
`DT_PLATFORM`, `DT_VERSION`, … and one per platform var, e.g. `DT_CLUSTER`), which is
handy for drivers written as shell scripts. Secrets reach the driver the normal way:
through the environment traintraintrain runs in.

## Requests

Every request has:

| Field | Meaning |
|---|---|
| `contract` | `1`. Refuse requests with a contract you don't know. |
| `action` | `deploy`, `inspect`, `rollback`, `live_version`, `resolve` or `changes` |
| `train` | the train's name |

`deploy`, `inspect` and `rollback` also have:

| Field | Meaning |
|---|---|
| `departure` | the version the train is deploying |
| `triggered_by` | who dispatched it, when known |
| `dry_run` | `true`: **change nothing**, just check and say what you would do (sent by `contract test`) |

Then, per action:

| Action | Extra fields | When |
|---|---|---|
| `deploy` | `platform`, `stop`, `vars`, `version`, `sha`, `previous_version`, `previous_sha` | for each platform, in timetable order |
| `inspect` | same as `deploy` | while a platform dwells after its deploy, every `inspection_interval` |
| `rollback` | same as `deploy`; `previous_version`/`previous_sha` are what **this platform** ran before | after a failure, per `rollback_scope` |
| `live_version` | `platform`, `stop`, `vars` | when a train departs, in previews, `pending` and `live --probe` |
| `resolve` | `version` | to turn a version (tag, branch) into a commit SHA |
| `changes` | `version`, `sha`, `previous_version`, `previous_sha`, `platforms` | once per group of platforms that run the same version |

`vars` are the platform's `vars` from `train.yaml` (e.g. `{"cluster": "use1"}`). `sha`
fields are `null` when unknown.

Example:

```json
{"contract": 1, "action": "deploy", "train": "payments-api", "departure": "v1.4.0",
 "triggered_by": "vishnu", "dry_run": false,
 "platform": "us-east-1", "stop": "atlantic", "vars": {"cluster": "use1"},
 "version": "v1.4.0", "sha": "2339111f…", "previous_version": "v1.3.0", "previous_sha": "792be66a…"}
```

## Replies

Every reply is a JSON object with:

| Field | Meaning |
|---|---|
| `ok` | **required**. `true` if the step worked |
| `message` | optional. A short explanation, shown in the UI (especially useful when `ok` is false) |
| `unsupported` | optional. `true` means "I don't do this action" (see below) |

And, when `ok` is true:

| Action | Fields |
|---|---|
| `deploy`, `inspect`, `rollback` | nothing else |
| `live_version` | `version` (required), `sha` (optional) |
| `resolve` | `sha` (required, 7–64 hex characters) |
| `changes` | `changes` (required): a list of `{"id", "title", "url", "author"}` (only `title` is required, newest first), and optionally `url`, a link to the full comparison |

Examples:

```json
{"ok": true, "message": "rollout complete in 94s"}
{"ok": false, "message": "helm: timed out waiting for the rollout"}
{"ok": true, "version": "v1.3.0", "sha": "792be66a…"}
{"ok": true, "changes": [{"id": "2339111", "title": "Add idempotency keys to refunds", "author": "Ana"}],
 "url": "https://github.com/acme/payments/compare/v1.3.0...v1.4.0"}
```

## What traintraintrain does with a reply

| Situation | Result |
|---|---|
| `ok: true`, exit code 0 | the step worked |
| `ok: false` | the step failed. For `deploy`/`inspect` the platform fails and the train halts (and rolls back per `rollback_scope`), with your `message` shown |
| `unsupported: true` | `deploy` fails; the other actions are treated as not configured. For example, no `live_version` means the recorded version is used, and no `changes` means no change list |
| no JSON reply, a reply that breaks the rules above, or `ok: true` with a non-zero exit code | the step failed, with the reason and the tail of stderr shown |
| no reply within the step's timeout | the program is stopped and the step failed |

Timeouts: `deploy` and `rollback` use the platform's `deploy_timeout`, `inspect` 5
minutes, `live_version` `live_version_timeout`, `resolve` `resolve_timeout`, and `changes`
`changes.timeout` (default 60s).

`live_version`, `resolve` and `changes` are informational. When they fail, the train
carries on and the departure notes why.

## Testing your driver

```bash
traintraintrain contract test --action deploy                     # sent with dry_run: true
traintraintrain contract test --action live_version --platform us-east-1
traintraintrain contract test --action changes --version v1.4.0 --previous-version v1.3.0
traintraintrain contract test --action deploy --for-real          # really deploys
traintraintrain contract schema                                   # the JSON Schema
```

`contract test` sends one real request, prints the request, the reply and the driver's
log, and says whether the reply follows the contract. `deploy` and `rollback` are sent
with `dry_run: true` unless you add `--for-real`.

## Versioning

This is contract version 1. New optional request fields may be added without changing
the version, so ignore fields you don't know. A change that would break existing drivers
gets a new version number, and traintraintrain will keep accepting version 1 drivers
alongside it.
