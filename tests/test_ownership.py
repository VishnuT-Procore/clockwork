"""Who owns a train: one process at a time, and recoverable when that process is gone."""
import json
import os
import socket
import time
from datetime import timedelta

import pytest

from traintraintrain import train as train_mod
from traintraintrain.train import SUCCEEDED, Train, owner_alive, state_path

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg

NOW = dt("2026-10-26T14:00")


def state(**kw):
    return {"status": "en_route", "pid": 12345, "host": "other-container", **kw}


@pytest.mark.parametrize("data, alive", [
    (state(heartbeat_at="2026-10-26T13:59:30+00:00"), True),    # fresh, other host
    (state(heartbeat_at="2026-10-26T13:50:00+00:00"), False),   # stale: container replaced
    (state(), True),                                            # pre-heartbeat file, other host
    (state(status="failed", heartbeat_at="2026-10-26T13:59:30+00:00"), False),
    ({"status": "en_route", "pid": os.getpid(), "host": socket.gethostname(),
      "heartbeat_at": "2026-10-26T13:59:30+00:00"}, True),     # this very process
    ({"status": "en_route", "pid": os.getpid(), "host": socket.gethostname(),
      "heartbeat_at": "2026-10-26T13:00:00+00:00"}, False),    # same host, PID reused, stale
])
def test_owner_alive(data, alive):
    assert owner_alive(data, now=NOW) is alive


def write_state(cfg, **kw):
    path = state_path(cfg, "v2")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "train": "svc", "version": "v2", "previous_version": "v1", "status": "en_route",
        "started_at": "2026-10-26T13:00:00+00:00", "finished_at": None,
        "stops": {"canary": {"finished_at": None, "signal": None},
                  "s1": {"finished_at": None, "signal": None}},
        "platforms": {n: {"stop": s, "status": "scheduled", "message": ""}
                      for n, s in [("us-canary", "canary"), ("us-east", "s1"), ("eu-west", "s1")]},
        "events": [], **kw}))


def test_train_interrupted_by_container_restart_resumes_without_force(tmp_path):
    cfg = make_cfg(tmp_path)
    old = dt("2020-01-01T00:00").isoformat()  # long before the real clock: stale
    write_state(cfg, pid=7, host="traintraintrain-6f9c-old", heartbeat_at=old)
    clock = FakeClock(NOW)
    assert Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run() == SUCCEEDED


def test_train_with_fresh_heartbeat_elsewhere_is_not_driven_twice(tmp_path):
    cfg = make_cfg(tmp_path)
    from datetime import datetime, timezone
    write_state(cfg, pid=7, host="traintraintrain-6f9c-new",
                heartbeat_at=datetime.now(timezone.utc).isoformat())
    clock = FakeClock(NOW)
    with pytest.raises(RuntimeError, match="already en route"):
        Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run()


def test_heartbeat_refreshes_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(train_mod, "HEARTBEAT_INTERVAL", 0.05)
    cfg = make_cfg(tmp_path)
    clock = FakeClock(NOW)
    t = Train(cfg, "v2", runner=FakeRunner(clock), clock=clock)
    t._init_state(retry_failed=False, force=False)
    t.state.data["heartbeat_at"] = "stale"
    stop = t._start_heartbeat()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            on_disk = json.loads(state_path(cfg, "v2").read_text())["heartbeat_at"]
            if on_disk != "stale":
                break
            time.sleep(0.02)
    finally:
        stop.set()
    assert on_disk != "stale"
    assert owner_alive(json.loads(state_path(cfg, "v2").read_text()))


def test_heartbeat_constants_are_consistent():
    # A live owner must beat several times within the staleness window.
    assert timedelta(seconds=train_mod.HEARTBEAT_INTERVAL * 4) < train_mod.HEARTBEAT_STALE
