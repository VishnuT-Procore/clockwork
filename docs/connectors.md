# Steps, checks and connectors

By default every platform runs `deploy`, then `dwell`. With `steps:` you choose what
each platform does, and decide from **data** (database stats, metrics, API replies)
whether the train carries on.

- A **connector** is a named adapter to one system: an HTTP API, a Postgres database,
  Prometheus, Databricks, your own program, a shell command, or a type from a plugin.
  You declare it once; credentials come from environment variables.
- A **step** uses a connector to make one call, which returns **metrics**.
- **`expect`** rules on those metrics decide whether the step passed.

```yaml
connectors:
  payments-db: {type: postgres, url: "${PAYMENTS_DB_URL}"}
  prometheus:  {type: prometheus, url: https://prometheus.internal}
  deployer:
    type: http
    base_url: https://deployer.internal/api
    headers: {Authorization: "Bearer ${DEPLOYER_TOKEN}"}

train:
  steps:
    - name: db-ready
      use: payments-db
      query: "select pending_migrations, replication_lag_s from deploy_status where region = :platform"
      expect: ["pending_migrations == 0", "replication_lag_s < 30"]
    - name: deploy
      use: deployer
      request: {method: POST, path: "/deploy/{platform}", json: {version: "{version}"}}
    - wait: 2m
    - name: error-rate
      use: prometheus
      query: 'sum(rate(http_errors{region="{platform}"}[5m])) / sum(rate(http_requests{region="{platform}"}[5m])) * 100'
      as: error_rate
      expect: ["error_rate < 1"]
      until: 10m
      every: 30s
    - dwell
  rollback:                              # optional: roll back through a connector too
    use: deployer
    request: {method: POST, path: "/rollback/{platform}", json: {to: "{previous_version}"}}
```

The UI shows every platform's steps, their metrics and which rules passed. `board
--steps` prints the same.

## Steps

| Step | What it does |
|---|---|
| `deploy` | the built-in deploy: `deploy_command`, or the driver's `deploy` |
| `dwell` | the built-in dwell: wait `dwell`, running `inspection_command` every `inspection_interval` |
| `wait: 2m` | pause. The emergency brake still works |
| `{name, use: <connector>, ...}` | a call through a connector (the keys depend on its type) |
| `{name, command: "..."}` | a shell command (the `command` type) |
| `{name, run: <step>}` | a step of the train's driver (`train.driver`) |

- **Pipeline rules:** each pipeline needs exactly one step named `deploy`, either the
  built-in or a call that replaces it. Step names must be unique within a pipeline.
- **Where `steps:` goes:** under `train:` (every platform), under a stop, or under a
  platform. The most specific wins. Without any, it's `[deploy, dwell]`.
- **Reusing a pipeline:** YAML anchors work, e.g. `steps: &standard [...]`, then
  `steps: *standard`.

Every call step can also take:

| Key | Meaning |
|---|---|
| `expect` | rules the metrics must meet, e.g. `["error_rate < 1", "status == 'ready'"]` |
| `until: 10m` (+ `every: 30s`) | retry until the rules pass, giving up after 10m (waiting for lag to drop, a job to finish) |
| `hold: 10m` (+ `every: 1m`) | the rules must keep passing for 10m (a gate during bake time) |
| `on_fail: warn` | record a failure and carry on (default `halt`: fail the platform, roll back per `rollback_scope`, halt) |
| `timeout: 5m` | the most one call may take (default 10m) |

**What rules look like:** `<metric> <op> <value>`, where op is `< <= > >= == !=` and
the value is a number, `true`/`false`/`null`, or a quoted string. Rules are parsed, never
run as code. A metric the call didn't return fails its rule with
`no metric 'x' in the result`.

**Placeholders** in queries, paths, bodies and parameters:

| Placeholder | Value |
|---|---|
| `{platform}`, `{stop}`, `{train}` | where the step runs |
| `{version}`, `{sha}` | the version being deployed, and its commit when SHAs are tracked |
| `{previous_version}`, `{previous_sha}` | what this platform ran before |
| `{cluster}` (or `{vars.cluster}`) | the platform's `vars` |
| `{steps.db-ready.replication_lag_s}` | a metric from an earlier step |

Only these names are replaced, so other braces (`{region="..."}` in PromQL, JSON) stay
as written. SQL takes them as bind parameters instead: `:platform`, `:version`,
`:cluster`.

**Dry runs** list the steps without calling anything. **Resuming** a departure restarts
a platform's pipeline from its first step, so make steps safe to repeat.

## Connector types

Settings may use `${VAR}` or `${VAR:-default}` to read environment variables. Use that
for every secret; the UI and logs never show secret settings.

### http

```yaml
deployer:
  type: http
  base_url: https://deployer.internal/api
  headers: {Authorization: "Bearer ${DEPLOYER_TOKEN}"}
  timeout: 30s
```
Step keys:
- `request`: `method` (default GET), `path` (joined to `base_url`) or `url`, plus
  optional `headers`, `query`, and `json` or `body`.
- `metrics`: maps names to dotted paths in the JSON reply, e.g.
  `{ready: status.ready, first: items.0.id}`.
- `ok_status`: a list of acceptable codes (default any 2xx).

The HTTP status is always the metric `status`. Values in the path are URL-encoded, so
`{platform}` can't escape its path segment.

### postgres

```yaml
payments-db:
  type: postgres
  url: ${PAYMENTS_DB_URL}          # postgresql://user:pass@host:5432/db
  statement_timeout: 30s
```
Step key: `query`.
- The first row's columns become metrics (`count(*) as pending` gives `pending`); no
  rows means no metrics.
- The query runs in a **read-only transaction** with a statement timeout.
- `:name` placeholders are bind parameters, never pasted into the SQL.
- Needs `pip install "traintraintrain[postgres]"`; the Docker image already has it.

### prometheus

Works with Prometheus, Thanos, Mimir, VictoriaMetrics or Grafana Cloud.

```yaml
prometheus: {type: prometheus, url: https://prometheus.internal, headers: {Authorization: "Bearer ${PROM_TOKEN}"}}
```
Step keys:
- `query`: an instant query.
- `as`: the metric name (default `value`).
- `label`: for several series, one metric per label value (`as: errors, label: code`
  gives `errors.500`, `errors.503`).

A query with no data returns no metric.

### databricks

```yaml
lakehouse:
  type: databricks
  host: https://acme.cloud.databricks.com
  token: ${DATABRICKS_TOKEN}                 # or a service principal:
  # client_id: ${DATABRICKS_CLIENT_ID}
  # client_secret: ${DATABRICKS_CLIENT_SECRET}
  warehouse_id: abc123def456
  catalog: main                              # optional
  schema: payments                           # optional
```
- **SQL:** `query` (plus optional `warehouse_id`, `catalog`, `schema` per step) runs on a
  SQL warehouse through the Statement Execution API.
  - `:name` placeholders are sent as statement parameters.
  - The first row's columns become metrics, typed from the result schema.
  - Give the token or service principal **read-only (SELECT) grants**: Databricks has no
    read-only transactions.
- **Jobs:** `job: <job id>` with `params` (job parameters, placeholders allowed) runs the
  job and waits for it.
  - Metrics: `run_id`, `result_state` (e.g. `SUCCESS`) and `duration_s`.
  - For a single-task notebook job, the metrics also include what the notebook returns
    with `dbutils.notebook.exit(json.dumps({"rows_rejected": 2}))`.
  - A run that doesn't finish within the step's `timeout` is cancelled.

#### Checking Databricks status

[`examples/databricks/train.yaml`](../examples/databricks/train.yaml) runs three checks
before each platform deploys:

| Step | Connector | Passes when |
|---|---|---|
| `databricks-ok` | `lakehouse` (databricks): `select 1 as ok, current_user() as user` | the workspace answers and SQL runs |
| `last-etl` | `databricks-api` (http): `GET /api/2.1/jobs/runs/list?job_id=…&completed_only=true&limit=1` | the job's most recent completed run has `result_state == 'SUCCESS'` |
| `warehouse` | `databricks-api` (http): `GET /api/2.0/sql/warehouses/<id>` | always; it reports `state` (RUNNING, STOPPED, …). Add `expect: ["state == 'RUNNING'"]` to require it |

The `http` connector reaches any Databricks REST endpoint the `databricks` type doesn't
wrap: the same host and token, with `metrics:` picking fields from the reply.

What you need from the workspace:

| Value | Where | Goes in |
|---|---|---|
| Workspace URL | the browser address bar, `https://<name>.cloud.databricks.com` | `DATABRICKS_HOST` |
| Token | avatar → Settings → Developer → Access tokens (or a service principal's OAuth secret) | `DATABRICKS_TOKEN` (or `client_id` / `client_secret`) |
| SQL warehouse ID | SQL Warehouses → the warehouse → Connection details; the end of `/sql/1.0/warehouses/<id>` | `DATABRICKS_WAREHOUSE_ID`, and the `warehouse` step's path |
| Job ID | Workflows → the job; the number in its URL | the `last-etl` step's `job_id` |

Try each check before relying on it:

```bash
traintraintrain -c train.yaml connectors test lakehouse
traintraintrain -c train.yaml connectors test databricks-api --step last-etl
traintraintrain -c train.yaml connectors test databricks-api --step warehouse
```

In Docker, pass the variables through in `docker-compose.yml`'s `environment:`
(`DATABRICKS_TOKEN: ${DATABRICKS_TOKEN}`, and so on).

| You see | Fix |
|---|---|
| `environment variable DATABRICKS_TOKEN isn't set` | export it where traintraintrain runs; in Docker, add it to `environment:` and recreate the container |
| `HTTP 401` / `HTTP 403` | the token is wrong or expired, or lacks permission on the job or warehouse |
| `the query didn't finish in time` | the warehouse was starting; raise the step's `timeout` (the example uses 5m) |
| `no metric 'result_state'` | wrong job ID, or the job has no completed runs yet |

### driver

```yaml
ops: {type: driver, command: python3 ./ops.py}
```
Step keys: `run` (the step's name for your program) and `params`. The program gets
`{"action": "step", "step": ..., "params": ..., "context": {"steps": {...}}, ...}` and
replies `{"ok": true, "metrics": {...}}`; see [driver-contract.md](driver-contract.md).
`train.driver` is available as the connector `driver`, so `run:` works without `use:`.

### command

Step key: `command`. Exit code 0 means the step ran. If the last line of output is a JSON
object, it becomes the metrics (or its `"metrics"` key, if it has one). A bare `command:`
step uses this type.

## Checking connectors

```bash
traintraintrain connectors list                                  # what's declared, and which steps use it
traintraintrain connectors test payments-db --platform eu-west-1  # run the step's call, show metrics and rules
traintraintrain connectors test deployer --step deploy --for-real
```
`connectors test` only runs calls that can't change anything (SQL, Prometheus, HTTP GET)
unless you add `--for-real`. Driver steps are sent with `dry_run: true`.

## Writing your own connector type (plugins)

A connector type is a Python class. Ship it in a package that registers it under the
`traintraintrain.connectors` entry point group, and `type: <name>` becomes usable in
every train.yaml where that package is installed.

```python
# acme_connectors/datadog.py
import json, urllib.parse, urllib.request
from traintraintrain.connectors import Connector, ConnectorError, StepResult, render

class DatadogConnector(Connector):
    type_name = "datadog"                    # what `type:` says in train.yaml
    step_keys = ("query", "as")              # the keys its steps may use
    read_only = True                         # calls never change anything
    secret_settings = ("api_key", "app_key") # never shown

    def validate_settings(self):             # at config load; raise ConnectorError
        if not self.settings.get("api_key"):
            raise ConnectorError(f"connector {self.name!r} needs an api_key")

    def validate_step(self, step):
        if not step.get("query"):
            raise ConnectorError("a datadog step needs a `query`")

    def call(self, step, ctx, timeout):      # -> StepResult(ok, metrics, message, log)
        query = render(step["query"], ctx)   # fills {platform}, {version}, ...
        req = urllib.request.Request(
            "https://api.datadoghq.com/api/v1/query?query=" + urllib.parse.quote(query),
            headers={"DD-API-KEY": self.setting("api_key"), "DD-APPLICATION-KEY": self.setting("app_key")})
        with urllib.request.urlopen(req, timeout=timeout.total_seconds()) as r:
            series = json.loads(r.read())["series"]
        if not series:
            return StepResult(True, {}, "no data")
        value = series[0]["pointlist"][-1][1]
        return StepResult(True, {step.get("as", "value"): value}, f"{query} = {value}")
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."traintraintrain.connectors"]
datadog = "acme_connectors.datadog:DatadogConnector"
```

- `self.setting(key)` reads a setting with `${VAR}` filled in.
- `ctx` has `platform`, `stop`, `vars`, `version`, `sha`, `previous_version`,
  `previous_sha`, `steps` (earlier metrics) and `dry_run`.
- `render(template, ctx)` fills placeholders.
- Exceptions raised in `call` become failed steps with the error as the message, so a
  buggy plugin can't crash a train.
