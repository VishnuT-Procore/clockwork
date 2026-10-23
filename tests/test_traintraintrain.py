import threading
from datetime import datetime, timedelta, timezone

import pytest

from traintraintrain import config
from traintraintrain.timetable import LineClosure, Timetable, parse_duration
from traintraintrain.train import (
    ABORTED, FAILED, ROLLED_BACK, SCHEDULED, SUCCEEDED, BrakeApplied, CommandResult, Train,
    brake_path, project,
)

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
        self.envs = []

    def __call__(self, command, env, timeout):
        self.calls.append((self.clock.now(), command))
        self.envs.append(env)
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
            "deploy_command": "deploy {platform} {version}",
            "inspection_command": "inspect {platform}",
            "rollback_command": "rollback {platform} {previous_version}",
            "inspection_interval": "5m",
            "default_dwell": "15m",
            "default_timetable": "us",
            "rollback_scope": "platform",
            "state_dir": str(tmp_path / "state"),
        },
        "timetables": {
            "us": {"timezone": "America/New_York",
                   "service_hours": [{"days": "mon-thu", "start": "09:00", "end": "16:00"}]},
            "eu": {"timezone": "Europe/Dublin",
                   "service_hours": [{"days": "mon-fri", "start": "09:00", "end": "17:00"}]},
        },
        "stops": [
            {"name": "canary", "platforms": ["us-canary"]},
            {"name": "s1", "parallel_platforms": 2,
             "platforms": ["us-east", {"name": "eu-west", "timetable": "eu"}]},
        ],
    }
    for k, v in overrides.items():
        section, key = k.split("__")
        raw[section][key] = v
    return config.from_dict(raw, base_dir=tmp_path)


# ---- timetables --------------------------------------------------------------

def test_parse_duration():
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("45s") == timedelta(seconds=45)
    assert parse_duration(60) == timedelta(minutes=1)
    with pytest.raises(ValueError):
        parse_duration("5 parsecs")


def test_in_service_and_next_start():
    tt = Timetable.from_config("us", {
        "timezone": "America/New_York",
        "service_hours": [{"days": "mon-thu", "start": "09:00", "end": "16:00"}],
    }, [])
    # Thu 2026-10-22 14:00 UTC = 10:00 EDT -> in service
    assert tt.can_start(dt("2026-10-22T14:00"))
    # Thu 21:00 UTC = 17:00 EDT -> no service, next is Mon 09:00 EDT = 13:00 UTC
    assert tt.next_start(dt("2026-10-22T21:00")) == dt("2026-10-26T13:00")


def test_service_after_dst_change():
    tt = Timetable.from_config("us", {
        "timezone": "America/New_York",
        "service_hours": [{"days": "mon", "start": "09:00", "end": "16:00"}],
    }, [])
    # US DST ends Nov 1 2026: Mon Nov 2 09:00 EST = 14:00 UTC
    assert tt.next_start(dt("2026-10-27T00:00")) == dt("2026-11-02T14:00")


def test_overnight_service():
    tt = Timetable.from_config("night", {
        "timezone": "UTC", "service_hours": [{"days": "fri", "start": "22:00", "end": "04:00"}],
    }, [])
    assert tt.can_start(dt("2026-10-24T02:00"))  # Saturday early hours belong to Friday's service
    assert not tt.can_start(dt("2026-10-24T05:00"))


def test_min_remaining():
    tt = Timetable.from_config("us", {
        "timezone": "UTC", "min_remaining": "1h",
        "service_hours": [{"days": "all", "start": "09:00", "end": "17:00"}],
    }, [])
    assert tt.can_start(dt("2026-10-22T15:59"))
    assert tt.next_start(dt("2026-10-22T16:30")) == dt("2026-10-23T09:00")


def test_line_closure_carves_service():
    closures = [LineClosure.from_config(
        {"start": "2026-10-22T10:00", "end": "2026-10-22T12:00", "reason": "x"})]
    tt = Timetable.from_config("p", {
        "timezone": "UTC", "service_hours": [{"days": "all", "start": "09:00", "end": "17:00"}],
    }, closures)
    assert tt.next_start(dt("2026-10-22T10:30")) == dt("2026-10-22T12:00")
    assert tt.active_closure(dt("2026-10-22T11:00")).reason == "x"
    # Whole-day closure with inclusive end date
    day = LineClosure.from_config({"start": "2026-10-22", "end": "2026-10-23"})
    assert day.end == dt("2026-10-24T00:00")


# ---- config ------------------------------------------------------------------

def test_config_rejects_unknown_timetable(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown timetable"):
        make_cfg(tmp_path, train__default_timetable="nope")


def test_config_rejects_duplicate_platform():
    with pytest.raises(config.ConfigError, match="more than one stop"):
        config.from_dict({
            "train": {"name": "s", "deploy_command": "d"},
            "stops": [{"platforms": ["a"]}, {"platforms": ["a"]}],
        })


def test_config_rejects_bad_duration():
    with pytest.raises(config.ConfigError, match="invalid duration"):
        config.from_dict({
            "train": {"name": "s", "deploy_command": "d", "default_dwell": "soon"},
            "stops": [{"platforms": ["a"]}],
        })


# ---- train -------------------------------------------------------------------

def test_happy_path_waits_for_service(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-22T21:00"))  # Thu 17:00 EDT: no US service
    runner = FakeRunner(clock)
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock)
    assert train.run() == SUCCEEDED
    deploys = [(t, c) for t, c in runner.calls if c.startswith("deploy")]
    assert deploys[0] == (dt("2026-10-26T13:00"), "deploy us-canary v2")
    # Stop s1 is served only after the canary deploy (10m) + dwell (15m)
    s1 = {c: t for t, c in deploys[1:]}
    assert set(s1) == {"deploy us-east v2", "deploy eu-west v2"}
    assert all(t >= dt("2026-10-26T13:25") for t in s1.values())
    assert runner.commands("inspect")  # inspections ran while dwelling
    assert not runner.commands("rollback")
    held = [e for e in train.state.data["events"] if e["message"].startswith("held at signal")]
    assert held and held[0]["platform"] == "us-canary"


def test_command_variables_and_env(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock)
    Train(cfg, "v2", "v1", runner=runner, clock=clock).run()
    env = runner.envs[0]
    assert env["DT_PLATFORM"] == env["DT_REGION"] == "us-canary"
    assert env["DT_STOP"] == env["DT_WAVE"] == "canary"
    assert env["DT_VERSION"] == "v2" and env["DT_PREVIOUS_VERSION"] == "v1"


def test_failure_rolls_back_platform_and_halts(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"inspect us-canary": 1})
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock)
    assert train.run() == FAILED
    assert runner.commands("rollback") == ["rollback us-canary v1"]
    platforms = train.state.data["platforms"]
    assert platforms["us-canary"]["status"] == ROLLED_BACK
    assert platforms["us-canary"]["output"] == "boom"
    assert platforms["us-east"]["status"] == SCHEDULED
    assert not [c for c in runner.commands("deploy") if "us-east" in c]


def test_train_rollback_scope(tmp_path):
    cfg = make_cfg(tmp_path, train__rollback_scope="train")
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"deploy eu-west": 1})
    cfg.stops[1].parallel_platforms = 1  # deterministic ordering
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED
    assert sorted(runner.commands("rollback")) == [
        "rollback eu-west v1", "rollback us-canary v1", "rollback us-east v1"]


def test_stop_rollback_scope(tmp_path):
    cfg = make_cfg(tmp_path, train__rollback_scope="stop")
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"deploy eu-west": 1})
    cfg.stops[1].parallel_platforms = 1
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED
    assert sorted(runner.commands("rollback")) == ["rollback eu-west v1", "rollback us-east v1"]


def test_resume_skips_succeeded_platforms(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock, fail={"deploy eu-west": 1})
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == FAILED

    with pytest.raises(RuntimeError, match="retry-failed"):
        Train(cfg, "v2", runner=FakeRunner(clock), clock=clock).run()

    runner2 = FakeRunner(clock)
    assert Train(cfg, "v2", runner=runner2, clock=clock).run(retry_failed=True) == SUCCEEDED
    assert runner2.commands("deploy") == ["deploy eu-west v2"]


def test_brake_and_resume(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-26T14:00"))

    def brake_after_canary(cmd):
        if cmd.startswith("inspect us-canary"):
            brake_path(cfg, "v2").write_text("now")

    runner = FakeRunner(clock, on_call=brake_after_canary)
    train = Train(cfg, "v2", "v1", runner=runner, clock=clock)
    assert train.run() == ABORTED
    assert train.state.data["platforms"]["us-canary"]["status"] == ABORTED

    runner2 = FakeRunner(clock)
    assert Train(cfg, "v2", runner=runner2, clock=clock).run() == SUCCEEDED
    assert "deploy us-canary v2" in runner2.commands("deploy")


def test_max_hold_fails_platform(tmp_path):
    cfg = make_cfg(tmp_path, train__max_hold="1h")
    clock = FakeClock(dt("2026-10-22T21:00"))
    runner = FakeRunner(clock)
    train = Train(cfg, "v2", runner=runner, clock=clock)
    assert train.run() == FAILED
    assert "max_hold" in train.state.data["platforms"]["us-canary"]["message"]
    assert not runner.commands("deploy")


def test_timetable_projection(tmp_path):
    cfg = make_cfg(tmp_path)
    stops = project(cfg, dt("2026-10-22T21:00"), timedelta(minutes=10))
    canary = stops[0][1][0]
    assert canary.start == dt("2026-10-26T13:00")
    assert canary.end == dt("2026-10-26T13:25")
    assert {p.platform.name: p.start for p in stops[1][1]} == {
        "us-east": dt("2026-10-26T13:25"), "eu-west": dt("2026-10-26T13:25")}


def test_halt_interrupts_hold_sleep(tmp_path):
    class CountingClock(FakeClock):
        sleeps = 0

        def sleep(self, seconds):
            self.sleeps += 1
            super().sleep(seconds)

    clock = CountingClock(dt("2026-10-26T14:00"))
    train = Train(make_cfg(tmp_path), "v2", runner=FakeRunner(clock), clock=clock)
    train._halt.set()  # another platform at the stop failed
    with pytest.raises(BrakeApplied):
        train._sleep(300, stop_on_halt=True)
    assert clock.sleeps == 0
    train._sleep(10)  # in-flight work (dwell) ignores the halt and finishes
    assert clock.sleeps == 2
