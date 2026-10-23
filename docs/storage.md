# Storage

Where trains keep their state. Back to the [README](../README.md).

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
