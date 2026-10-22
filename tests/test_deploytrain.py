import threading
from datetime import datetime, timedelta, timezone

import pytest

from deploytrain import config
from deploytrain.train import (
    ABORTED, FAILED, PENDING, ROLLED_BACK, SUCCEEDED, CommandResult, Train, abort_path, plan,
)
from deploytrain.windows import WindowProfile, parse_duration

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


class FakeClock:
    def __init__(self, start):
        self.t = start
        self.lock = threading.Lock()

    def now(self):
        return self.t

    def sleep(self, seconds):
        with self.lock:
            self.t += timedelta(seconds=seconds)


class FakeRunner:
    """Records commands; `fail` maps a substring to an exit code."""

    def __init__(self, clock, deploy_time=timedelta(minutes=10), fail=None, on_call=None):
        self.clock, self.deploy_time = clock, deploy_time
        self.fail = fail or {}
        self.on_call = on_call
        self.calls = []

    def __call__(self, command, env, timeout):
        self.calls.append((self.clock.now(), command))
        if self.on_call:
            self.on_call(command)
        if command.startswith("deploy"):
            self.clock.sleep(self.deploy_time.total_seconds())
        for needle, rc in self.fail.items():
            if needle in command:
                return CommandResult(rc, "boom")
        return CommandResult(0, "")

    def commands(self, prefix):
        return [c for _, c in self.calls if c.startswith(prefix)]


def make_cfg(tmp_path, **overrides):
    raw = {
        "train": {
            "name": "svc",
            "deploy_command": "deploy {region} {version}",
            "health_check_command": "health {region}",
            "rollback_command": "rollback {region} {previous_version}",
            "health_check_interval": "5m",
            "default_bake": "15m",
            "default_window": "us",
            "rollback_scope": "region",
            "state_dir": str(tmp_path / "state"),
        },
        "windows": {
            "us": {"timezone": "America/New_York",
                   "windows": [{"days": "mon-thu", "start": "09:00", "end": "16:00"}]},
            "eu": {"timezone": "Europe/Dublin",
                   "windows": [{"days": "mon-fri", "start": "09:00", "end": "17:00"}]},
        },
        "waves": [
            {"name": "canary", "regions": ["us-canary"]},
            {"name": "w1", "max_parallel": 2,
             "regions": ["us-east", {"name": "eu-west", "window": "eu"}]},
        ],
    }
    for k, v in overrides.items():
        section, key = k.split("__")
        raw[section][key] = v
    return config.from_dict(raw, base_dir=tmp_path)


# ---- windows -----------------------------------------------------------------

def test_parse_duration():
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("45s") == timedelta(seconds=45)
    assert parse_duration(60) == timedelta(minutes=1)
    with pytest.raises(ValueError):
        parse_duration("5 parsecs")


def test_window_open_and_next_start():
    prof = WindowProfile.from_config("us", {
        "timezone": "America/New_York",
        "windows": [{"days": "mon-thu", "start": "09:00", "end": "16:00"}],
    }, [])
    # Thu 2026-10-22 14:00 UTC = 10:00 EDT -> open
    assert prof.can_start(dt("2026-10-22T14:00"))
    # Thu 21:00 UTC = 17:00 EDT -> closed, next is Mon 09:00 EDT = 13:00 UTC
    assert prof.next_start(dt("2026-10-22T21:00")) == dt("2026-10-26T13:00")


def test_window_after_dst_change():
    prof = WindowProfile.from_config("us", {
        "timezone": "America/New_York",
        "windows": [{"days": "mon", "start": "09:00", "end": "16:00"}],
    }, [])
    # US DST ends Nov 1 2026: Mon Nov 2 09:00 EST = 14:00 UTC
    assert prof.next_start(dt("2026-10-27T00:00")) == dt("2026-11-02T14:00")


def test_overnight_window():
    prof = WindowProfile.from_config("night", {
        "timezone": "UTC", "windows": [{"days": "fri", "start": "22:00", "end": "04:00"}],
    }, [])
    assert prof.can_start(dt("2026-10-24T02:00"))  # Saturday early hours belong to Friday's window
    assert not prof.can_start(dt("2026-10-24T05:00"))


def test_min_remaining():
    prof = WindowProfile.from_config("us", {
        "timezone": "UTC", "min_remaining": "1h",
        "windows": [{"days": "all", "start": "09:00", "end": "17:00"}],
    }, [])
    assert prof.can_start(dt("2026-10-22T15:59"))
    assert prof.next_start(dt("2026-10-22T16:30")) == dt("2026-10-23T09:00")


def test_blackout_carves_window():
    cfg_blackouts = [config.Blackout.from_config(
        {"start": "2026-10-22T10:00", "end": "2026-10-22T12:00", "reason": "x"})]
    prof = WindowProfile.from_config("p", {
        "timezone": "UTC", "windows": [{"days": "all", "start": "09:00", "end": "17:00"}],
    }, cfg_blackouts)
    assert prof.next_start(dt("2026-10-22T10:30")) == dt("2026-10-22T12:00")
    # Whole-day blackout with inclusive end date
    day = config.Blackout.from_config({"start": "2026-10-22", "end": "2026-10-23"})
    assert day.end == dt("2026-10-24T00:00")


# ---- config ------------------------------------------------------------------

def test_config_rejects_unknown_window(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown window"):
        make_cfg(tmp_path, train__default_window="nope")


def test_config_rejects_duplicate_region():
    with pytest.raises(config.ConfigError, match="more than one wave"):
        config.from_dict({
            "train": {"name": "s", "deploy_command": "d"},
            "waves": [{"regions": ["a"]}, {"regions": ["a"]}],
        })


# ---- train -------------------------------------------------------------------

def test_happy_path_waits_for_windows(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-22T21:00"))  # Thu 17:00 EDT: US window closed
    runner = FakeRunner(clock)
    status = Train(cfg, "v2", "v1", runner=runner, clock=clock).run()
    assert status == SUCCEEDED
    deploys = [(t, c) for t, c in runner.calls if c.startswith("deploy")]
    assert deploys[0] == (dt("2026-10-26T13:00"), "deploy us-canary v2")
    # Wave 1 starts only after canary deploy (10m) + bake (15m)
    w1 = {c: t for t, c in deploys[1:]}
    assert set(w1) == {"deploy us-east v2", "deploy eu-west v2"}
    assert all(t >= dt("2026-10-26T13:25") for t in w1.values())
    assert runner.commands("health")  # health checks ran during bake
    assert not runner.commands("rollback")


def test_failure_rolls_back_region_and_halts(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"health us-canary": 1})
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock)
    assert train.run() == FAILED
    assert runner.commands("rollback") == ["rollback us-canary v1"]
    regions = train.state.data["regions"]
    assert regions["us-canary"]["status"] == ROLLED_BACK
    assert regions["us-east"]["status"] == PENDING
    assert not [c for c in runner.commands("deploy") if "us-east" in c]


def test_train_rollback_scope(tmp_path):
    cfg = make_cfg(tmp_path, train__rollback_scope="train")
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"deploy eu-west": 1})
    cfg.waves[1].max_parallel = 1  # deterministic ordering
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED
    assert sorted(runner.commands("rollback")) == [
        "rollback eu-west v1", "rollback us-canary v1", "rollback us-east v1"]


def test_resume_skips_succeeded_regions(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"deploy eu-west": 1})
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED

    with pytest.raises(RuntimeError, match="retry-failed"):
        Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run()

    runner2 = FakeRunner(clock)
    assert Train(cfg, "v2", runner=runner2, clock=clock).run(retry_failed=True) == SUCCEEDED
    assert runner2.commands("deploy") == ["deploy eu-west v2"]


def test_abort_and_resume(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))

    def abort_after_canary(cmd):
        if cmd.startswith("health us-canary"):
            abort_path(cfg, "v2").write_text("now")

    runner = FakeRunner(clock, on_call=abort_after_canary)
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock)
    assert train.run() == ABORTED
    assert train.state.data["regions"]["us-canary"]["status"] == ABORTED

    runner2 = FakeRunner(clock)
    assert Train(cfg, "v2", runner=runner2, clock=clock).run() == SUCCEEDED
    assert "deploy us-canary v2" in runner2.commands("deploy")


def test_window_wait_timeout_fails_region(tmp_path):
    cfg = make_cfg(tmp_path, train__window_wait_timeout="1h")
    clock = FakeClock(dt("2026-10-22T21:00"))
    runner = FakeRunner(clock)
    train = Train(cfg, "v2", runner=runner, clock=clock)
    assert train.run() == FAILED
    assert "window_wait_timeout" in train.state.data["regions"]["us-canary"]["message"]
    assert not runner.commands("deploy")


def test_plan_projects_schedule(tmp_path):
    cfg = make_cfg(tmp_path)
    waves = plan(cfg, dt("2026-10-22T21:00"), timedelta(minutes=10))
    canary = waves[0][1][0]
    assert canary.start == dt("2026-10-26T13:00")
    assert canary.end == dt("2026-10-26T13:25")
    assert {p.region.name: p.start for p in waves[1][1]} == {
        "us-east": dt("2026-10-26T13:25"), "eu-west": dt("2026-10-26T13:25")}
