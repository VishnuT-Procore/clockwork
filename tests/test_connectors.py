"""Connectors: http, prometheus, databricks (against local stand-in servers), postgres
(against TEST_DATABASE_URL), driver steps, plugins and the `connectors` CLI."""
import json
import os
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from traintraintrain import connectors
from traintraintrain.connectors import ConnectorError, Context, build

T = timedelta(seconds=10)


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


class Fake:
    """A local HTTP server; `routes` maps (method, path) to a function(request) -> (status, body)."""

    def __init__(self):
        self.routes, self.requests = {}, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = {"method": method, "path": url.path, "query": parse_qs(url.query),
                       "headers": dict(self.headers), "body": body}
                fake.requests.append(req)
                fn = fake.routes.get((method, url.path))
                status, payload = fn(req) if fn else (404, {"error": "no route"})
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()


CTX = Context(train="pay", platform="us east/1", stop="s1", vars={"cluster": "use1"}, version="v2",
              previous_version="v1", steps={"db": {"lag": 3}})


# ---- http ---------------------------------------------------------------------------------

def test_http_request_metrics_and_escaping(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOY_TOKEN", "s3cret")
    reply = {"id": "d-42", "status": {"ready": True}}
    fake.routes[("POST", "/api/regions/us%20east%2F1/deploy")] = lambda r: (202, reply)
    c = build("deployer", {"type": "http", "base_url": fake.url + "/api",
                           "headers": {"Authorization": "Bearer ${DEPLOY_TOKEN}"}}, tmp_path)
    step = {"request": {"method": "POST", "path": "/regions/{platform}/deploy",
                        "json": {"version": "{version}", "cluster": "{cluster}", "lag": "{steps.db.lag}"},
                        "query": {"dry": "no"}},
            "metrics": {"deploy_id": "id", "ready": "status.ready", "nope": "missing.path"}}
    c.validate_step(step)
    r = c.safe_call(step, CTX, T)
    assert r.ok and r.metrics == {"status": 202, "deploy_id": "d-42", "ready": True}
    assert "not in the reply: missing.path" in r.message
    req = fake.requests[-1]
    assert req["path"] == "/api/regions/us%20east%2F1/deploy"  # quoted: stays one path segment
    assert json.loads(req["body"]) == {"version": "v2", "cluster": "use1", "lag": "3"}
    assert req["query"] == {"dry": ["no"]} and req["headers"]["Authorization"] == "Bearer s3cret"
    assert "s3cret" not in c.describe()
    assert not c.step_is_read_only(step)


def test_http_failures(fake, tmp_path, monkeypatch):
    fake.routes[("GET", "/health")] = lambda r: (503, {"error": "draining"})
    c = build("api", {"type": "http", "base_url": fake.url}, tmp_path)
    step = {"request": {"path": "/health"}}
    r = c.safe_call(step, CTX, T)
    assert not r.ok and "returned HTTP 503" in r.message and "draining" in r.message and r.metrics["status"] == 503
    assert c.safe_call({**step, "ok_status": [503]}, CTX, T).ok
    dead = build("dead", {"type": "http", "base_url": "http://127.0.0.1:9"}, tmp_path)
    assert "failed" in dead.safe_call(step, CTX, T).message
    needs_env = build("x", {"type": "http", "base_url": fake.url, "headers": {"A": "${NOT_SET_ANYWHERE}"}}, tmp_path)
    assert needs_env.safe_call(step, CTX, T).message == "environment variable NOT_SET_ANYWHERE isn't set"
    with pytest.raises(ConnectorError, match="needs a `request`"):
        c.validate_step({})
    with pytest.raises(ConnectorError, match="base_url"):
        build("y", {"type": "http"}, tmp_path).validate_step({"request": {"path": "/x"}})


# ---- prometheus ------------------------------------------------------------------------------

def test_prometheus(fake, tmp_path):
    def query(req):
        q = req["query"]["query"][0]
        if "by (code)" in q:
            return 200, {"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"code": "500"}, "value": [1, "3"]}, {"metric": {"code": "503"}, "value": [1, "1"]}]}}
        if "nothing" in q:
            return 200, {"status": "success", "data": {"resultType": "vector", "result": []}}
        if "bad(" in q:
            return 400, {"status": "error", "error": "parse error at char 4"}
        return 200, {"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {}, "value": [1, "0.42"]}]}}
    fake.routes[("GET", "/api/v1/query")] = query
    c = build("prom", {"type": "prometheus", "url": fake.url}, tmp_path)
    r = c.safe_call({"query": 'sum(rate(errors{region="{platform}"}[5m]))', "as": "error_rate"}, CTX, T)
    assert r.ok and r.metrics == {"error_rate": 0.42}
    assert fake.requests[-1]["query"]["query"][0] == 'sum(rate(errors{region="us east/1"}[5m]))'
    r = c.safe_call({"query": "sum by (code) (x)", "as": "errors", "label": "code"}, CTX, T)
    assert r.metrics == {"errors.500": 3.0, "errors.503": 1.0}
    r = c.safe_call({"query": "sum by (code) (x)"}, CTX, T)
    assert not r.ok and "returned 2 series" in r.message
    r = c.safe_call({"query": "nothing"}, CTX, T)
    assert r.ok and r.metrics == {} and "no data" in r.message
    r = c.safe_call({"query": "bad("}, CTX, T)
    assert not r.ok and "parse error" in r.message


# ---- databricks ------------------------------------------------------------------------------

def databricks_routes(fake):
    state = {"polls": 0}

    def statements(req):
        body = json.loads(req["body"])
        state["submitted"] = body
        return 200, {"statement_id": "st-1", "status": {"state": "PENDING"}}

    def statement(req):
        state["polls"] += 1
        return 200, {"statement_id": "st-1", "status": {"state": "SUCCEEDED"},
                     "manifest": {"schema": {"columns": [
                         {"name": "null_amounts", "type_name": "LONG"}, {"name": "lag_s", "type_name": "DOUBLE"},
                         {"name": "ok", "type_name": "BOOLEAN"}, {"name": "region", "type_name": "STRING"}]}},
                     "result": {"data_array": [["0", "12.5", "true", "us east/1"]]}}

    def token(req):
        state["token_auth"] = req["headers"].get("Authorization")
        return 200, {"access_token": "oauth-abc", "expires_in": 3600}

    def run_now(req):
        state["run"] = json.loads(req["body"])
        state["run_auth"] = req["headers"].get("Authorization")
        return 200, {"run_id": 77}

    def runs_get(req):
        state["run_polls"] = state.get("run_polls", 0) + 1
        life = "RUNNING" if state["run_polls"] < 2 else "TERMINATED"
        return 200, {"run_id": 77, "state": {"life_cycle_state": life, "result_state": "SUCCESS"},
                     "start_time": 1000, "end_time": 31000, "tasks": [{"run_id": 78}],
                     "run_page_url": "https://acme/run/77"}

    def get_output(req):
        return 200, {"notebook_output": {"result": json.dumps({"rows_rejected": 2, "note": "ok"})}}

    fake.routes.update({("POST", "/api/2.0/sql/statements"): statements,
                        ("GET", "/api/2.0/sql/statements/st-1"): statement,
                        ("POST", "/oidc/v1/token"): token,
                        ("POST", "/api/2.1/jobs/run-now"): run_now,
                        ("GET", "/api/2.1/jobs/runs/get"): runs_get,
                        ("GET", "/api/2.1/jobs/runs/get-output"): get_output})
    return state


def test_databricks_sql_query(fake, tmp_path, monkeypatch):
    monkeypatch.setattr("traintraintrain.connectors.databricks.POLL_SECONDS", 0.01)
    monkeypatch.setenv("DBX_TOKEN", "dapi-123")
    state = databricks_routes(fake)
    c = build("lake", {"type": "databricks", "host": fake.url, "token": "${DBX_TOKEN}", "warehouse_id": "wh1",
                       "catalog": "main"}, tmp_path)
    step = {"query": "select ... where region = :platform and v = :version and x::int > 0"}
    c.validate_step(step)
    r = c.safe_call(step, CTX, T)
    assert r.ok and r.metrics == {"null_amounts": 0, "lag_s": 12.5, "ok": True, "region": "us east/1"}
    sub = state["submitted"]
    assert sub["warehouse_id"] == "wh1" and sub["catalog"] == "main" and sub["statement"] == step["query"]
    assert sub["parameters"] == [{"name": "platform", "value": "us east/1", "type": "STRING"},
                                 {"name": "version", "value": "v2", "type": "STRING"}]
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer dapi-123"
    assert state["polls"] == 1 and c.step_is_read_only(step)
    assert "unknown query parameter" in c.safe_call({"query": "select :nope"}, CTX, T).message


def test_databricks_job_with_service_principal(fake, tmp_path, monkeypatch):
    monkeypatch.setattr("traintraintrain.connectors.databricks.POLL_SECONDS", 0.01)
    state = databricks_routes(fake)
    c = build("lake", {"type": "databricks", "host": fake.url, "client_id": "sp", "client_secret": "pw"}, tmp_path)
    step = {"job": 912345, "params": {"region": "{platform}", "version": "{version}"}}
    c.validate_step(step)
    r = c.safe_call(step, CTX, T)
    assert r.ok, r.message
    assert r.metrics == {"run_id": 77, "result_state": "SUCCESS", "duration_s": 30.0, "rows_rejected": 2, "note": "ok"}
    assert state["run"] == {"job_id": 912345, "job_parameters": {"region": "us east/1", "version": "v2"}}
    assert state["token_auth"].startswith("Basic ") and state["run_auth"] == "Bearer oauth-abc"


def test_databricks_config_errors(tmp_path):
    with pytest.raises(ConnectorError, match="needs a `host`"):
        build("x", {"type": "databricks", "token": "t"}, tmp_path)
    with pytest.raises(ConnectorError, match="either `token` or"):
        build("x", {"type": "databricks", "host": "https://a"}, tmp_path)
    c = build("x", {"type": "databricks", "host": "https://a", "token": "t"}, tmp_path)
    with pytest.raises(ConnectorError, match="warehouse_id"):
        c.validate_step({"query": "select 1"})
    with pytest.raises(ConnectorError, match="either `query:`"):
        c.validate_step({"query": "select 1", "job": 1, "warehouse_id": "w"})


# ---- postgres --------------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_postgres_stats_query(tmp_path, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS ttt_stats")
        conn.execute("CREATE TABLE ttt_stats (region text, pending int, lag numeric, at timestamptz default now())")
        conn.execute("INSERT INTO ttt_stats (region, pending, lag) VALUES ('us east/1', 0, 2.5), ('eu', 4, 9)")
    monkeypatch.setenv("STATS_DB", url)
    c = build("db", {"type": "postgres", "url": "${STATS_DB}"}, tmp_path)
    try:
        r = c.safe_call({"query": "select pending, lag, lag::int as lag_int, '100%' as pct "
                                  "from ttt_stats where region = :platform"}, CTX, T)
        assert r.ok and r.metrics == {"pending": 0, "lag": 2.5, "lag_int": 3, "pct": "100%"}
        r = c.safe_call({"query": "select pending from ttt_stats where region = :cluster"}, CTX, T)
        assert r.ok and r.metrics == {} and "no rows" in r.message
        r = c.safe_call({"query": "delete from ttt_stats"}, CTX, T)
        assert not r.ok and "read-only" in r.message
        assert "unknown query parameter" in c.safe_call({"query": "select :nope"}, CTX, T).message
        r = c.safe_call({"query": "select pg_sleep(5)"}, CTX, timedelta(seconds=1))
        assert not r.ok and "statement timeout" in r.message
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP TABLE ttt_stats")


# ---- driver steps and plugins -------------------------------------------------------------------

def test_driver_step(tmp_path):
    (tmp_path / "ops.py").write_text(
        "import json, sys\nreq = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'metrics': {'step': req['step'], 'db': req['params']['db'],"
        " 'lag_seen': req['context']['steps']['db']['metrics']['lag'], 'dry': req['dry_run']}}))\n")
    c = build("ops", {"type": "driver", "command": f"{sys.executable} ops.py"}, tmp_path)
    r = c.safe_call({"run": "db_stats", "params": {"db": "{cluster}"}}, CTX, T)
    assert r.ok and r.metrics == {"step": "db_stats", "db": "use1", "lag_seen": 3, "dry": False}


def test_plugin_connector_types(monkeypatch, tmp_path):
    class Datadog(connectors.Connector):
        type_name = "datadog"
        step_keys = ("monitor",)

        def call(self, step, ctx, timeout):
            return connectors.StepResult(True, {"monitor_ok": step["monitor"] == "ok"})

    class EP:
        name = "datadog"

        def load(self):
            return Datadog

    monkeypatch.setattr("importlib.metadata.entry_points", lambda group=None: [EP()])
    monkeypatch.setattr(connectors, "_registry", None)
    assert "datadog" in connectors.types()
    c = build("dd", {"type": "datadog"}, tmp_path)
    assert c.safe_call({"monitor": "ok"}, CTX, T).metrics == {"monitor_ok": True}
    monkeypatch.setattr(connectors, "_registry", None)


def test_connector_bugs_become_failed_results(tmp_path):
    class Broken(connectors.Connector):
        type_name = "broken"

        def call(self, step, ctx, timeout):
            raise RuntimeError("boom")

    r = Broken("b", {"type": "broken"}, Path(tmp_path)).safe_call({}, CTX, T)
    assert not r.ok and "RuntimeError: boom" in r.message
