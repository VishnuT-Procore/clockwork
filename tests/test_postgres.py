"""The Postgres storage backend, against a real server.

Skipped unless TEST_DATABASE_URL points at a Postgres database the tests may use, e.g.
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres. Each test runs in its own
schema, which is dropped afterwards.
"""
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from urllib.parse import quote

import pytest

psycopg = pytest.importorskip("psycopg")
BASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="TEST_DATABASE_URL not set")

from traintraintrain import cli, config, storage  # noqa: E402
from traintraintrain.storage import BRAKE, SIGNAL, FileBackend, PostgresBackend, backend_for  # noqa: E402
from traintraintrain.train import (  # noqa: E402
    ABORTED, FAILED, ROLLED_BACK, SUCCEEDED, Train, clear_signal, load_departure,
)

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg  # noqa: E402

NOW = dt("2026-10-26T14:00")


@pytest.fixture
def db_url(monkeypatch):
    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in BASE_URL else "?"
    url = f"{BASE_URL}{sep}options={quote(f'-csearch_path={schema}')}"
    monkeypatch.setenv("TRAINTRAINTRAIN_DATABASE_URL", url)
    yield url
    for key, backend in list(storage._backends.items()):
        if isinstance(backend, PostgresBackend) and backend.url == url:
            backend.close()
            del storage._backends[key]
    with psycopg.connect(BASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def pg_cfg(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    assert cfg.database_url, "TRAINTRAINTRAIN_DATABASE_URL should be picked up"
    return cfg


# ---- backend ---------------------------------------------------------------------

def test_backend_round_trips(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    b = backend_for(cfg)
    assert isinstance(b, PostgresBackend) and "postgres" in b.describe()
    assert b.load("svc", "v1") is None and not b.exists("svc", "v1")
    b.save("svc", "v1", {"train": "svc", "version": "v1", "status": "en_route", "x": [1, 2]})
    b.save("svc", "v1", {"train": "svc", "version": "v1", "status": "succeeded", "x": [3]})
    assert b.load("svc", "v1")["x"] == [3] and b.exists("svc", "v1")
    b.save("other", "v9", {"train": "other", "version": "v9", "status": "failed"})
    assert [d["version"] for d in b.list("svc")] == ["v1"]

    assert b.get_request("svc", "v1", SIGNAL, "eu") is None
    b.request("svc", "v1", SIGNAL, "eu", "alice")
    assert b.get_request("svc", "v1", SIGNAL, "eu")["by"] == "alice"
    b.delete_request("svc", "v1", SIGNAL, "eu")
    assert b.get_request("svc", "v1", SIGNAL, "eu") is None

    b.audit({"train": "svc", "version": "v1", "user": "bob", "action": "depart", "dry_run": False})
    assert b.audit_entries("svc", "v1")[0]["user"] == "bob"
    assert b.audit_entries("svc", "v1")[0]["dry_run"] is False
    b.append_log("svc", "v1", "line one\nline two")
    b.append_log_lines("svc", "v1", [f"n{i}" for i in range(500)])
    tail = b.read_log("svc", "v1", lines=3).split("\n")
    assert tail == ["n497", "n498", "n499"]
    counts = b.counts()
    assert counts["departures"] == 2 and counts["schema version"] == storage.SCHEMA_VERSION


def test_schema_setup_is_idempotent(db_url):
    a, b = PostgresBackend(db_url), PostgresBackend(db_url)
    a.migrate(), b.migrate(), a.migrate()
    a.close(), b.close()


# ---- the train on Postgres --------------------------------------------------------

def test_journey_state_lives_in_postgres(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    clock = FakeClock(NOW)
    train = Train(cfg, "v2", "v1", runner=FakeRunner(clock), clock=clock)
    assert train.run() == SUCCEEDED
    st = load_departure(cfg, "v2")
    assert st["status"] == SUCCEEDED and {p["status"] for p in st["platforms"].values()} == {SUCCEEDED}
    assert not (tmp_path / "state").exists()  # nothing written to files
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT status FROM train_departures").fetchone()[0] == "succeeded"


def test_failure_rollback_and_retry(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    clock = FakeClock(NOW)
    runner = FakeRunner(clock, fail={"inspect us-canary": 1})
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED
    st = load_departure(cfg, "v2")
    assert st["platforms"]["us-canary"]["status"] == ROLLED_BACK
    with pytest.raises(RuntimeError, match="retry-failed"):
        Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run()
    assert Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run(retry_failed=True) == SUCCEEDED


def test_brake_and_signal_requests(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    cfg.stops[1].signal = True
    clock = FakeClock(NOW)

    def on_call(cmd):  # clear the signal once the canary is healthy
        if cmd.startswith("inspect us-canary"):
            clear_signal(cfg, "v2", "s1", "dana")

    assert Train(cfg, "v2", "v1", runner=FakeRunner(clock, on_call=on_call), clock=clock).run() == SUCCEEDED
    assert load_departure(cfg, "v2")["stops"]["s1"]["signal"]["cleared_by"] == "dana"
    assert backend_for(cfg).get_request("svc", "v2", SIGNAL, "s1") is None  # consumed

    def brake(cmd):
        if cmd.startswith("inspect us-canary"):
            backend_for(cfg).request("svc", "v3", BRAKE, "", "erin")

    assert Train(cfg, "v3", runner=FakeRunner(clock, on_call=brake), clock=clock).run() == ABORTED


# ---- ownership --------------------------------------------------------------------

def test_one_driver_at_a_time(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    other = PostgresBackend(db_url)  # a second process, in effect
    assert other.acquire("svc", "v2")
    try:
        clock = FakeClock(NOW)
        with pytest.raises(RuntimeError, match="already en route"):
            Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run(force=True)
        b = backend_for(cfg)
        assert b.owner_alive("svc", "v2", {"status": "en_route"})
        assert not b.owner_alive("svc", "v2", {"status": "failed"})
    finally:
        other.release("svc", "v2")
        other.close()
    assert not backend_for(cfg).owner_alive("svc", "v2", {"status": "en_route"})


DRIVER = textwrap.dedent("""
    import sys, time
    from traintraintrain.storage import PostgresBackend
    b = PostgresBackend(sys.argv[1])
    assert b.acquire("svc", "v2")
    print("locked", flush=True)
    time.sleep(600)
""")


def test_killed_driver_releases_ownership_and_train_resumes(db_url, tmp_path):
    cfg = pg_cfg(tmp_path)
    b = backend_for(cfg)
    b.save("svc", "v2", {"train": "svc", "version": "v2", "previous_version": "v1", "status": "en_route",
                         "pid": 1, "host": "some-old-container", "started_at": "2026-10-26T13:00:00+00:00",
                         "finished_at": None, "stops": {"canary": {"finished_at": None, "signal": None},
                                                         "s1": {"finished_at": None, "signal": None}},
                         "platforms": {n: {"stop": s, "status": "scheduled", "message": ""} for n, s in
                                       [("us-canary", "canary"), ("us-east", "s1"), ("eu-west", "s1")]},
                         "events": []})
    proc = subprocess.Popen([sys.executable, "-c", DRIVER, db_url], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert b.owner_alive("svc", "v2", {"status": "en_route"})
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        deadline = time.time() + 10
        while b.owner_alive("svc", "v2", {"status": "en_route"}) and time.time() < deadline:
            time.sleep(0.1)
        assert not b.owner_alive("svc", "v2", {"status": "en_route"})
    finally:
        if proc.poll() is None:
            proc.kill()
    clock = FakeClock(NOW)  # no --force needed: the lock is gone
    assert Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run() == SUCCEEDED


# ---- CLI --------------------------------------------------------------------------

def write_config(tmp_path, name="box", dwell="0s"):
    path = tmp_path / "train.yaml"
    path.write_text(f"""
train:
  name: {name}
  deploy_command: "echo deploy {{platform}} {{version}}"
  inspection_command: "true"
  inspection_interval: 1s
  default_dwell: {dwell}
  state_dir: {tmp_path / 'files'}
stops:
  - name: only
    signal: false
    platforms: [p1, p2]
""")
    return path


def test_cli_db_commands_and_import(db_url, tmp_path, monkeypatch, capsys):
    path = write_config(tmp_path)
    # A departure made with file storage first...
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL")
    assert cli.main(["-c", str(path), "depart", "--version", "v1"]) == 0
    files = FileBackend(tmp_path / "files")
    files.audit({"train": "box", "version": "v1", "user": "zoe", "action": "depart"})
    files.append_log("box", "v1", "old log line")
    monkeypatch.setenv("TRAINTRAINTRAIN_DATABASE_URL", db_url)
    capsys.readouterr()
    # ...then moved into Postgres.
    assert cli.main(["-c", str(path), "db", "migrate"]) == 0
    assert cli.main(["-c", str(path), "db", "import"]) == 0
    assert "imported 1 departure" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "db", "import"]) == 0
    assert "skipped 1" in capsys.readouterr().out
    cfg = config.load(path)
    b = backend_for(cfg)
    assert load_departure(cfg, "v1")["status"] == SUCCEEDED
    assert b.audit_entries("box", "v1")[0]["user"] == "zoe"
    assert "old log line" in b.read_log("box", "v1")
    assert cli.main(["-c", str(path), "board", "--version", "v1"]) == 0
    assert "SUCCEEDED" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "db", "status"]) == 0
    out = capsys.readouterr().out
    assert "departures: 1" in out and "postgres" in out


def test_db_commands_need_a_database(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL", raising=False)
    assert cli.main(["-c", str(write_config(tmp_path)), "db", "migrate"]) == 1
    assert "TRAINTRAINTRAIN_DATABASE_URL" in capsys.readouterr().err


def test_bad_database_url_is_reported(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRAINTRAINTRAIN_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    assert cli.main(["-c", str(write_config(tmp_path)), "db", "status"]) == 1
    err = capsys.readouterr().err
    assert "can't connect to postgres nobody@127.0.0.1:1/none" in err


# ---- web UI against Postgres (real processes) ------------------------------------

def test_web_ui_end_to_end_on_postgres(db_url, tmp_path):
    import socket
    import urllib.request
    path = write_config(tmp_path, dwell="2s")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "TRAINTRAINTRAIN_DATABASE_URL": db_url, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    ui = subprocess.Popen([sys.executable, "-m", "traintraintrain", "-c", str(path), "ui", "--port", str(port)],
                          cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"

    def call(p, body=None):
        headers = {"X-Traintraintrain-User": "pat"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-Requested-With": "traintraintrain"})
        with opener.open(urllib.request.Request(base + p, data=data, headers=headers), timeout=5) as r:
            return json.loads(r.read())

    try:
        for _ in range(100):
            try:
                call("/healthz")
                break
            except OSError:
                time.sleep(0.1)
        assert call("/api/departures/depart", {"version": "v7"})["ok"]
        dep = None
        for _ in range(150):
            try:
                dep = call("/api/departure?version=v7")
            except OSError:
                dep = None
            if dep and dep.get("status") == "succeeded":
                break
            time.sleep(0.2)
        assert dep and dep["status"] == "succeeded", dep
        assert "deploy p2 v7" in dep["log"] and "dispatched by pat" in dep["log"]
        assert dep["actions"][0]["action"] == "depart" and dep["actions"][0]["user"] == "pat"
        assert [d["version"] for d in call("/api/departures")] == ["v7"]
    finally:
        ui.send_signal(signal.SIGTERM)
        try:
            ui.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            ui.kill()
    assert not (tmp_path / "files").exists()  # everything went to Postgres


# ---- live versions -----------------------------------------------------------------

def test_live_versions_stored_in_postgres(db_url, tmp_path):
    from traintraintrain import live
    from traintraintrain.storage import live_record
    cfg = pg_cfg(tmp_path, train__rollback_scope="none")
    backend = backend_for(cfg)
    clock = FakeClock(NOW)
    assert Train(cfg, "v1", runner=FakeRunner(clock), clock=clock).run() == SUCCEEDED
    clock = FakeClock(NOW)
    assert Train(cfg, "v2", runner=FakeRunner(clock, fail={"deploy us-east": 1}), clock=clock).run() == FAILED
    assert {k: v["version"] for k, v in backend.live(cfg.name).items()} == {
        "us-canary": "v2", "us-east": "v1", "eu-west": "v2"}
    row = backend.live(cfg.name)["us-canary"]
    assert (row["status"], row["previous_version"], row["departure"]) == (live.DEPLOYED, "v1", "v2")
    hist = backend.live_history(cfg.name, "us-east")
    assert [(h["status"], h["version"]) for h in hist] == [(live.FAILED_ATTEMPT, "v2"), (live.DEPLOYED, "v1")]
    st = load_departure(cfg, "v2")
    assert {p["from_version"] for p in st["platforms"].values()} == {"v1"}
    backend.set_live(cfg.name, live_record("us-east", "v1.1", live.SET_BY_HAND, "v1", by="ops"))
    assert backend.live(cfg.name)["us-east"]["by"] == "ops"
    with psycopg.connect(db_url, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM train_platform_versions").fetchone()[0] == 3
        assert conn.execute("SELECT max(version) FROM train_schema").fetchone()[0] == 2
    assert backend.counts()["live platform versions"] == 3


def test_db_import_copies_live_versions(db_url, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL")
    files_cfg = make_cfg(tmp_path)
    clock = FakeClock(NOW)
    Train(files_cfg, "v1", runner=FakeRunner(clock), clock=clock).run()
    monkeypatch.setenv("TRAINTRAINTRAIN_DATABASE_URL", db_url)
    path = tmp_path / "train.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"train": {"name": "svc", "deploy_command": "true",
                                              "state_dir": str(tmp_path / "state")},
                                    "stops": [{"platforms": ["us-canary", "us-east", "eu-west"]}]}))
    assert cli.main(["-c", str(path), "db", "import"]) == 0
    assert "and 3 live version(s)" in capsys.readouterr().out
    backend = backend_for(pg_cfg(tmp_path))
    assert {k: v["version"] for k, v in backend.live("svc").items()} == {
        "us-canary": "v1", "us-east": "v1", "eu-west": "v1"}
    assert len(backend.live_history("svc")) == 3
