"""/test_connection.html: trying connectors (Jira, Databricks, HTTP, ...) from the web UI."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from traintraintrain import cli, config
from traintraintrain.web import App, Server, make_handler

from test_connectors import Fake, databricks_routes
from test_jira import jira_routes


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL", raising=False)


@pytest.fixture
def services(monkeypatch):
    """One stand-in server answering both Jira and Databricks."""
    f = Fake()
    jira_routes(f)
    databricks_routes(f)
    f.routes[("GET", "/rest/api/2/myself")] = lambda q: (200, {"displayName": "Vishnu"})
    f.routes[("GET", "/api/2.0/preview/scim/v2/Me")] = lambda q: (200, {"userName": "vishnu@acme.test"})
    f.routes[("GET", "/api/2.0/sql/warehouses/wh1")] = lambda q: (200, {"name": "ops", "state": "RUNNING"})
    monkeypatch.setattr("traintraintrain.connectors.databricks.POLL_SECONDS", 0.01)
    yield f
    f.httpd.shutdown()


def make(tmp_path, url):
    raw = {
        "connectors": {
            "jira": {"type": "jira", "url": url, "token": "pat"},
            "lake": {"type": "databricks", "host": url, "token": "dapi", "warehouse_id": "wh1"},
            "script": {"type": "command"},
        },
        "train": {"name": "cde", "deploy_command": "true", "state_dir": str(tmp_path / "state"),
                  "before": [{"name": "approved", "use": "jira", "action": "approval", "key": "{steps.change.key}",
                              "expect": ["approval == 'approved'"], "stop_if": ["approval == 'declined'"]}],
                  "steps": [{"name": "incidents", "use": "jira", "action": "search", "jql": "labels = {platform}",
                             "expect": ["count == 0"]},
                            "deploy",
                            {"name": "fresh", "use": "lake", "query": "select 1 as ok", "expect": ["ok == 1"]},
                            {"name": "note", "use": "jira", "action": "comment", "key": "CHG-7", "body": "hi"}]},
        "stops": [{"platforms": ["p1", "p2"]}],
    }
    (tmp_path / "train.yaml").write_text(json.dumps(raw))
    return config.from_dict(raw, base_dir=tmp_path)


@pytest.fixture
def ui(tmp_path, services):
    cfg = make(tmp_path, services.url)
    app = App(cfg, str(tmp_path / "train.yaml"))
    httpd = Server(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app, services
    httpd.shutdown()


def call(base, path, body=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"X-Traintraintrain-User": "vishnu"}
    data = None
    if body is not None:
        headers.update({"Content-Type": "application/json", "X-Requested-With": "traintraintrain"})
        data = json.dumps(body).encode()
    try:
        with opener.open(urllib.request.Request(base + path, data=data, headers=headers)) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith("/api") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_page_lists_connectors_and_their_steps(ui):
    base, _, _ = ui
    code, html = call(base, "/test_connection.html")
    assert code == 200 and "Test connections" in html
    code, data = call(base, "/api/connectors")
    by = {c["name"]: c for c in data["connectors"]}
    assert by["jira"]["can_ping"] and by["lake"]["can_ping"] and not by["script"]["can_ping"]
    steps = {s["name"]: s for s in by["jira"]["steps"]}
    assert steps["approved"]["where"] == "before departing" and steps["approved"]["needs"] == ["change.key"]
    assert steps["approved"]["stop_if"] == ["approval == 'declined'"]
    assert steps["incidents"]["read_only"] and not steps["note"]["read_only"]
    assert steps["incidents"]["platforms"] == ["p1", "p2"] and "pat" not in json.dumps(data)
    assert call(base, "/api/config")[1]["connectors"] == 3


def test_test_connection(ui):
    base, _, _ = ui
    code, r = call(base, "/api/connectors/ping", {"connector": "jira"})
    assert code == 200 and r["ok"] and r["metrics"]["user"] == "Vishnu"
    code, r = call(base, "/api/connectors/ping", {"connector": "lake"})
    assert r["ok"] and r["metrics"] == {"user": "vishnu@acme.test", "warehouse": "ops", "warehouse_state": "RUNNING"}
    code, r = call(base, "/api/connectors/ping", {"connector": "script"})
    assert code == 200 and r["supported"] is False
    assert call(base, "/api/connectors/ping", {"connector": "nope"})[0] == 400


def test_running_steps(ui):
    base, app, services = ui
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "incidents", "platform": "p2"})
    assert code == 200 and r["platform"] == "p2" and r["metrics"]["count"] == 1 and not r["passed"]
    assert r["checks"][0] == {"rule": "count == 0", "metric": "count", "actual": 1, "ok": False, "why": "count is 1"}
    code, r = call(base, "/api/connectors/test", {"connector": "lake", "step": "fresh"})
    assert r["ok"] and r["metrics"]["null_amounts"] == 0  # the stand-in's canned row
    # Needs a value from an earlier step: without it, a clear message; with it, the real call.
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "approved"})
    assert not r["ok"] and "no issue key" in r["message"]
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "approved",
                                                  "values": {"change.key": "CHG-7"}})
    assert r["ok"] and r["metrics"]["approval"] == "pending" and not r["passed"] and r["stopped"] is None
    services.routes[("GET", "/rest/servicedeskapi/request/CHG-9/approval")] = \
        lambda q: (200, {"values": [{"name": "CAB", "finalDecision": "declined"}]})
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "approved",
                                                  "values": {"change.key": "CHG-9"}})
    assert not r["passed"] and r["stopped"] == "approval is 'declined' (stop_if: approval == 'declined')"
    # Steps that change things need for_real.
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "note"})
    assert code == 400 and "for-real" in r["error"]
    code, r = call(base, "/api/connectors/test", {"connector": "jira", "step": "note", "for_real": True})
    assert code == 200 and r["ok"] and r["metrics"]["key"] == "CHG-7"
    tests = [e for e in app.backend.audit_entries("cde", "connectors") if e["action"] == "connector_test"]
    assert tests[-1]["user"] == "vishnu" and tests[-1]["step"] == "note" and tests[-1]["for_real"] is True


def test_cli_ping_and_values(ui, capsys, tmp_path):
    base, _, _ = ui
    path = str(tmp_path / "train.yaml")
    assert cli.main(["-c", path, "connectors", "ping", "lake"]) == 0
    assert "warehouse 'ops' is RUNNING" in capsys.readouterr().out
    args = ["-c", path, "connectors", "test", "jira", "--step", "approved", "--value", "change.key=CHG-7"]
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "approval = 'pending'" in out and "step would FAIL" in out
    assert cli.main(["-c", path, "connectors", "test", "jira", "--step", "approved", "--value", "nonsense"]) == 1
    assert "step.metric=value" in capsys.readouterr().err
