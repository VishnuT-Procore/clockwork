# Running traintraintrain

In containers, on Kubernetes and in CI. Back to the [README](../README.md).

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
[`examples/docker/Dockerfile.tools`](../examples/docker/Dockerfile.tools) shows how.

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
- [`examples/docker/docker-compose.yml`](../examples/docker/docker-compose.yml): the UI
  with a persistent volume. Run it with
  `TRAINTRAINTRAIN_UI_TOKEN=change-me docker compose up --build`.
- [`examples/docker/docker-compose.postgres.yml`](../examples/docker/docker-compose.postgres.yml):
  the UI with its state in a Postgres container. To use your own Postgres, drop that
  service and set `TRAINTRAINTRAIN_DATABASE_URL`.
- [`examples/kubernetes/ui.yaml`](../examples/kubernetes/ui.yaml): the UI as a
  single-replica Deployment (`Recreate` strategy) with a PersistentVolumeClaim, probes,
  a non-root security context and a Service.
- [`examples/kubernetes/job.yaml`](../examples/kubernetes/job.yaml): one train per release
  as a Kubernetes Job, for CD pipelines.

## Running it in CI

A train can take days to complete its journey while it waits for service hours, so run
it as a long-lived job, such as a Kubernetes Job (see above) or a self-hosted runner,
with `state_dir` on persistent storage. Alternatively, run it on a schedule (cron or a
scheduled pipeline) with a short `max_hold`. Each invocation then resumes and travels as
far as the current timetables allow.
