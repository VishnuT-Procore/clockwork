import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from deploytrain import config
from deploytrain.notify import Notifier
from deploytrain.train import (
    AWAITING_APPROVAL, SUCCEEDED, Train, approval_path, request_approval, state_path,
)
from deploytrain.web import App, make_handler

from test_deploytrain import FakeClock, FakeRunner, dt, make_cfg


# ---- approval gates ----------------------------------------------------------

class ApprovingClock(FakeClock):
    """Approves the wave the first time the train sleeps while awaiting approval."""

    def __init__(self, start, train_ref, wave):
        super().__init__(start)
        self.train_ref, self.wave = train_ref, wave
        self.seen_status = None

    def sleep(self, seconds):
        train = self.train_ref[0]
        if train and train.state.data.get("status") == AWAITING_APPROVAL and self.seen_status is None:
            self.seen_status = AWAITING_APPROVAL
            request_approval(train.cfg, train.version, self.wave, "alice")
        super().sleep(seconds)


def test_wave_waits_for_approval(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.waves[1].approval = True
    ref = [None]
    clock = ApprovingClock(dt("2026-10-26T14:00"), ref, "w1")
    events = []

    class Rec(Notifier):
        def notify(self, event, **ctx):
            events.append((event, ctx))

    runner = FakeRunner(clock)
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock, notifier=Rec())
    ref[0] = train
    assert train.run() == SUCCEEDED
    assert clock.seen_status == AWAITING_APPROVAL
    approval = train.state.data["waves"]["w1"]["approval"]
    assert approval["status"] == "approved" and approval["by"] == "alice"
    assert not approval_path(cfg, "v2", "w1").exists()
    names = [e for e, _ in events]
    assert names.index("wave_awaiting_approval") < names.index("wave_approved") < names.index("wave_started", 2)
    # Nothing in w1 deployed before the approval.
    approved_at = next(t for t, c in runner.calls if c == "deploy us-east v2")
    assert approved_at >= dt(approval["approved_at"].replace("+00:00", ""))


def test_running_train_cannot_be_started_twice(tmp_path):
    cfg = make_cfg(tmp_path)
    path = state_path(cfg, "v2")
    path.parent.mkdir(parents=True)
    import socket
    path.write_text(json.dumps({"status": "running", "pid": os.getppid(), "host": socket.gethostname(),
                                "regions": {}, "train": "svc", "version": "v2"}))
    with pytest.raises(RuntimeError, match="already running"):
        Train(cfg, "v2", runner=FakeRunner(FakeClock(dt("2026-10-26T14:00")))).run()


# ---- HTTP API ----------------------------------------------------------------

def write_cfg(tmp_path, approval=False):
    (tmp_path / "train.yaml").write_text(f"""
train:
  name: webdemo
  deploy_command: "echo deploy {{region}} {{version}}"
  health_check_command: "true"
  rollback_command: "echo rollback {{region}}"
  health_check_interval: 1s
  default_bake: 0s
waves:
  - name: canary
    regions: [r1]
  - name: rest
    approval: {str(approval).lower()}
    regions: [r2, r3]
""")
    return tmp_path / "train.yaml"


@pytest.fixture
def server(tmp_path, request):
    approval = getattr(request, "param", False)
    path = write_cfg(tmp_path, approval)
    cfg = config.load(path)
    app = App(cfg, str(path), token="s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg
    httpd.shutdown()


def call(base, path, body=None, token="s3cret", user="bob", csrf=True):
    headers = {"X-Deploytrain-User": user}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-Requested-With"] = "deploytrain"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(base, version, pred, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, run = call(base, f"/api/run?version={version}")
        if code == 200 and pred(run):
            return run
        time.sleep(0.2)
    raise AssertionError(f"timed out; last: {run}")


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def test_auth_and_csrf(server):
    base, _ = server
    assert call(base, "/api/runs", token=None)[0] == 401
    assert call(base, "/api/runs", token="wrong")[0] == 401
    assert call(base, "/api/runs") == (200, [])
    assert call(base, "/api/runs/start", {"version": "v1"}, csrf=False)[0] == 403
    assert call(base, "/api/runs/start", {"version": "../etc"})[0] == 400
    with urllib.request.urlopen(base + "/") as r:
        assert b"Deploy Train" in r.read()


def test_config_and_plan(server):
    base, _ = server
    code, cfg = call(base, "/api/config")
    assert code == 200 and [w["name"] for w in cfg["waves"]] == ["canary", "rest"]
    assert cfg["waves"][0]["regions"][0]["open_now"] is True
    code, plan = call(base, "/api/plan")
    assert code == 200 and plan["waves"][1]["regions"][1]["name"] == "r3"


def test_start_run_to_completion(server):
    base, _ = server
    assert call(base, "/api/runs/start", {"version": "v1.0", "previous_version": "v0.9"}) == (
        200, {"ok": True, "version": "v1.0"})
    run = wait_for(base, "v1.0", lambda r: r.get("status") == "succeeded")
    assert all(r["status"] == "succeeded" for r in run["regions"].values())
    assert "deploy r3 v1.0" in run["log"]
    assert run["actions"][0]["user"] == "bob" and run["actions"][0]["action"] == "start"
    # A second start of the same version is refused.
    assert call(base, "/api/runs/start", {"version": "v1.0"})[0] == 409
    code, runs = call(base, "/api/runs")
    assert runs[0]["version"] == "v1.0" and runs[0]["counts"] == {"succeeded": 3}


@pytest.mark.parametrize("server", [True], indirect=True)
def test_approve_then_abort_flow(server):
    base, _ = server
    call(base, "/api/runs/start", {"version": "v2"})
    run = wait_for(base, "v2", lambda r: r.get("pending_approval") == "rest")
    assert run["status"] == "awaiting_approval" and run["active"]
    assert call(base, "/api/runs/approve", {"version": "v2", "wave": "canary"})[0] == 409
    assert call(base, "/api/runs/approve", {"version": "v2", "wave": "rest"}, user="carol")[0] == 200
    run = wait_for(base, "v2", lambda r: r.get("status") == "succeeded")
    assert run["waves"]["rest"]["approval"]["by"] == "carol"
    assert [a["action"] for a in run["actions"]] == ["start", "approve"]


@pytest.mark.parametrize("server", [True], indirect=True)
def test_abort_and_resume(server):
    base, _ = server
    call(base, "/api/runs/start", {"version": "v3"})
    wait_for(base, "v3", lambda r: r.get("pending_approval") == "rest")
    assert call(base, "/api/runs/abort", {"version": "v3"})[0] == 200
    run = wait_for(base, "v3", lambda r: r.get("status") == "aborted", timeout=40)
    assert not run["active"]
    assert call(base, "/api/runs/abort", {"version": "v3"})[0] == 409
    assert call(base, "/api/runs/resume", {"version": "v3"})[0] == 200
    wait_for(base, "v3", lambda r: r.get("status") == "awaiting_approval")
    call(base, "/api/runs/approve", {"version": "v3", "wave": "rest"})
    wait_for(base, "v3", lambda r: r.get("status") == "succeeded")
