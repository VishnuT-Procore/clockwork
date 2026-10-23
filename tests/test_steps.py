"""Pipelines: custom steps per platform, with expect rules on the metrics steps return."""
import json

import pytest

from traintraintrain import cli, config
from traintraintrain.train import (
    ABORTED, FAILED, ROLLED_BACK, SUCCEEDED, CommandResult, Train, apply_brake, load_departure,
)

from test_traintraintrain import FakeClock, FakeRunner, dt

NOW = dt("2026-10-26T14:00")
PLATFORMS = ["us-canary", "us-east", "eu-west"]


class StepRunner(FakeRunner):
    """`stats ...` prints metrics: a fixed dict, or the next of a list per platform."""

    def __init__(self, clock, stats=None, **kw):
        super().__init__(clock, **kw)
        self.stats = stats or {}

    def __call__(self, command, env, timeout):
        if command.startswith("stats"):
            self.calls.append((self.clock.now(), command))
            self.envs.append(env)
            platform = command.split()[1]
            value = self.stats.get(platform, self.stats.get("*", {"errors": 0}))
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
            return CommandResult(0, "checking...\n" + json.dumps(value))
        return super().__call__(command, env, timeout)


def make(tmp_path, steps=None, stops=None, **train):
    raw = {"train": {"name": "svc", "deploy_command": "deploy {platform} {version}",
                     "inspection_command": "inspect {platform}", "inspection_interval": "5m",
                     "default_dwell": "10m", "rollback_command": "rollback {platform} {previous_version}",
                     "state_dir": str(tmp_path / "state"), **train},
           "stops": stops or [{"name": "canary", "platforms": ["us-canary"]},
                              {"name": "s1", "platforms": ["us-east", "eu-west"]}]}
    if steps is not None:
        raw["train"]["steps"] = steps
    return config.from_dict(raw, base_dir=tmp_path)


def run(cfg, version="v2", previous="v1", **kw):
    clock = FakeClock(NOW)
    runner = StepRunner(clock, **kw)
    status = Train(cfg, version, previous, runner=runner, clock=clock).run()
    return status, runner, clock


CHECK = {"name": "error-rate", "command": "stats {platform} {version}", "expect": ["errors < 5"]}


def test_without_steps_nothing_changes(tmp_path):
    cfg = make(tmp_path)
    assert all(p.steps[0].kind == "deploy" and not p.custom_steps for p in cfg.platforms)
    status, runner, _ = run(cfg)
    assert status == SUCCEEDED and "steps" not in load_departure(cfg, "v2")["platforms"]["us-east"]
    assert runner.commands("deploy") == ["deploy us-canary v2", "deploy us-east v2", "deploy eu-west v2"]


def test_steps_run_in_order_and_record_metrics(tmp_path):
    cfg = make(tmp_path, steps=[{"name": "pre", "command": "stats {platform} pre"}, "deploy", {"wait": "5m"},
                                CHECK, "dwell"])
    status, runner, clock = run(cfg, stats={"*": {"errors": 1, "p99_ms": 300}})
    assert status == SUCCEEDED
    canary = [c for _, c in runner.calls if "us-canary" in c]
    assert canary == ["stats us-canary pre", "deploy us-canary v2", "stats us-canary v2", "inspect us-canary",
                      "inspect us-canary", "inspect us-canary"]
    st = load_departure(cfg, "v2")["platforms"]["us-canary"]
    assert [(s["name"], s["status"]) for s in st["steps"]] == [
        ("pre", "passed"), ("deploy", "passed"), ("wait 5m", "passed"), ("error-rate", "passed"), ("dwell", "passed")]
    check = st["steps"][3]
    assert check["metrics"] == {"errors": 1, "p99_ms": 300} and check["checks"][0]["ok"] is True
    assert check["expect"] == ["errors < 5"] and check["attempts"] == 1
    events = [e["message"] for e in load_departure(cfg, "v2")["events"] if e["platform"] == "us-canary"]
    assert "step error-rate: errors=1, p99_ms=300 — passed" in events
    assert "step wait 5m: waiting 5m" in events


def test_a_failing_check_halts_and_rolls_back(tmp_path):
    cfg = make(tmp_path, steps=["deploy", CHECK])
    status, runner, _ = run(cfg, stats={"us-canary": {"errors": 9}})
    assert status == FAILED
    st = load_departure(cfg, "v2")["platforms"]["us-canary"]
    assert st["status"] == ROLLED_BACK
    assert st["message"].startswith("step error-rate failed: errors < 5 (errors is 9)")
    assert runner.commands("rollback") == ["rollback us-canary v1"]
    assert [s["status"] for s in st["steps"]] == ["passed", "failed"]
    assert not runner.commands("deploy us-east")


def test_missing_metric_fails_with_a_clear_reason(tmp_path):
    cfg = make(tmp_path, steps=["deploy", CHECK], rollback_scope="none")
    run(cfg, stats={"*": {"error_count": 0}})
    msg = load_departure(cfg, "v2")["platforms"]["us-canary"]["message"]
    assert "no metric 'errors' in the result (got: error_count)" in msg


def test_until_waits_for_the_rules_to_pass(tmp_path):
    cfg = make(tmp_path, steps=["deploy", {**CHECK, "until": "5m", "every": "1m"}])
    status, runner, _ = run(cfg, stats={"*": [{"errors": 9}, {"errors": 7}, {"errors": 2}]})
    assert status == SUCCEEDED
    st = load_departure(cfg, "v2")["platforms"]["us-canary"]["steps"][1]
    assert st["attempts"] == 3 and st["metrics"] == {"errors": 2}


def test_until_gives_up(tmp_path):
    cfg = make(tmp_path, steps=["deploy", {**CHECK, "until": "2m", "every": "30s"}], rollback_scope="none")
    status, runner, _ = run(cfg, stats={"*": {"errors": 9}})
    assert status == FAILED
    st = load_departure(cfg, "v2")["platforms"]["us-canary"]
    assert st["message"].startswith("step error-rate timed out after 2m: errors < 5 (errors is 9)")
    assert st["steps"][1]["status"] == "timed_out"
    assert st["steps"][1]["attempts"] == 5  # at 0, 30s, 1m, 1m30s and 2m


def test_hold_must_keep_passing(tmp_path):
    cfg = make(tmp_path, steps=["deploy", {**CHECK, "hold": "10m", "every": "2m"}], rollback_scope="none")
    status, _, _ = run(cfg, stats={"*": [{"errors": 1}, {"errors": 2}, {"errors": 8}]})
    assert status == FAILED
    assert "stopped passing after 4m" in load_departure(cfg, "v2")["platforms"]["us-canary"]["message"]
    cfg2 = make(tmp_path / "b", steps=["deploy", {**CHECK, "hold": "10m", "every": "2m"}])
    status, runner, _ = run(cfg2, stats={"*": {"errors": 1}})
    assert status == SUCCEEDED and len(runner.commands("stats us-canary")) == 6


def test_on_fail_warn_carries_on(tmp_path):
    cfg = make(tmp_path, steps=["deploy", {**CHECK, "on_fail": "warn"}])
    status, _, _ = run(cfg, stats={"*": {"errors": 9}})
    assert status == SUCCEEDED
    st = load_departure(cfg, "v2")["platforms"]["eu-west"]["steps"][1]
    assert st["status"] == "warned" and "errors is 9" in st["message"]


def test_stop_and_platform_pipelines_override_the_train_one(tmp_path):
    stops = [{"name": "canary", "steps": ["deploy", {"name": "load-test", "command": "stats {platform} load"}],
              "platforms": ["us-canary"]},
             {"name": "s1", "platforms": ["us-east", {"name": "eu-west", "steps": ["deploy"]}]}]
    cfg = make(tmp_path, steps=["deploy", CHECK], stops=stops)
    assert [s.name for s in cfg.platforms[0].steps] == ["deploy", "load-test"]
    assert [s.name for s in cfg.platforms[1].steps] == ["deploy", "error-rate"]
    assert [s.name for s in cfg.platforms[2].steps] == ["deploy"]
    status, runner, _ = run(cfg)
    assert status == SUCCEEDED
    assert runner.commands("stats") == ["stats us-canary load", "stats us-east v2"]


def test_later_steps_see_earlier_metrics(tmp_path):
    cfg = make(tmp_path, steps=[{"name": "db", "command": "stats {platform} db"}, "deploy",
                                {"name": "compare", "command": "stats {platform} {steps.db.rows}"}])
    status, runner, _ = run(cfg, stats={"*": {"rows": 1234}})
    assert status == SUCCEEDED and "stats us-east 1234" in runner.commands("stats")


def test_the_brake_stops_a_wait(tmp_path):
    cfg = make(tmp_path, steps=[{"wait": "1h"}, "deploy", "dwell"])

    class BrakingClock(FakeClock):
        def sleep(self, seconds):  # someone pulls the brake while the step waits
            super().sleep(seconds)
            apply_brake(cfg, "v2", "ops")

    clock = BrakingClock(NOW)
    runner = StepRunner(clock)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == ABORTED
    st = load_departure(cfg, "v2")["platforms"]["us-canary"]
    assert st["status"] == "aborted" and [s["status"] for s in st["steps"]] == ["stopped", "pending", "pending"]
    assert runner.commands("deploy") == []


def test_dry_run_skips_calls(tmp_path):
    cfg = make(tmp_path, steps=["deploy", CHECK, "dwell"])
    clock = FakeClock(NOW)
    runner = StepRunner(clock)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock, dry_run=True).run() == SUCCEEDED
    assert runner.calls == []
    st = load_departure(cfg, "v2")["platforms"]["us-east"]
    assert st["steps"][1]["status"] == "skipped"


def test_rollback_through_a_connector(tmp_path):
    cfg = make(tmp_path, steps=["deploy", CHECK], rollback_command=None,
               rollback={"command": "stats {platform} undo-to-{previous_version}"})
    status, runner, _ = run(cfg, stats={"us-canary": [{"errors": 9}, {"undone": True}]})
    assert status == FAILED
    assert "stats us-canary undo-to-v1" in runner.commands("stats")
    assert load_departure(cfg, "v2")["platforms"]["us-canary"]["status"] == ROLLED_BACK


@pytest.mark.parametrize("steps, error", [
    (["dwell"], "exactly one `deploy` step"),
    (["deploy", "deploy"], "unique"),
    (["deploy", "launch"], "isn't a built-in step"),
    (["deploy", {"name": "x", "use": "nope"}], "no connector named 'nope'"),
    (["deploy", {"name": "x", "command": "c", "query": "q"}], "command steps don't take query"),
    (["deploy", {"name": "x", "command": "c", "expect": ["errors <"]}], "can't read the rule"),
    (["deploy", {"name": "x", "command": "c", "until": "5m"}], "need `expect` rules"),
    (["deploy", {"name": "x", "command": "c", "expect": ["a == 1"], "until": "5m", "hold": "5m"}], "not both"),
    (["deploy", {"name": "x", "run": "stats"}], "needs a train.driver"),
    (["deploy", {"name": "x", "command": "c", "on_fail": "ignore"}], "halt or warn"),
    (["deploy", {"wait": "soon"}], "wait"),
    (["deploy", {"command": "c"}], "needs a `name`"),
    ([{"name": "deploy", "expect": ["a == 1"]}], "built-in deploy step takes no settings"),
])
def test_step_config_errors(tmp_path, steps, error):
    with pytest.raises(config.ConfigError, match=error):
        make(tmp_path, steps=steps)


def test_deploy_command_not_needed_when_a_connector_deploys(tmp_path):
    raw = {"train": {"name": "s", "steps": [{"name": "deploy", "command": "ship {platform}"}],
                     "state_dir": str(tmp_path / "state")},
           "stops": [{"platforms": ["p1"]}]}
    cfg = config.from_dict(raw, base_dir=tmp_path)
    clock = FakeClock(NOW)
    runner = StepRunner(clock)
    assert Train(cfg, "v1", runner=runner, clock=clock).run() == SUCCEEDED
    assert runner.commands("ship") == ["ship p1"]


def test_cli_board_steps_and_connectors(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL", raising=False)
    path = tmp_path / "train.yaml"
    path.write_text(json.dumps({
        "connectors": {"api": {"type": "http", "base_url": "http://127.0.0.1:9"}},
        "train": {"name": "svc", "deploy_command": "true", "state_dir": str(tmp_path / "state"),
                  "steps": ["deploy", {"name": "ping", "use": "api", "request": {"path": "/ping"},
                                       "expect": ["status == 200"], "on_fail": "warn"},
                            {"name": "notify", "use": "api", "request": {"method": "POST", "path": "/n"}}]},
        "stops": [{"platforms": ["p1"]}]}))
    assert cli.main(["-c", str(path), "connectors", "list"]) == 0
    out = capsys.readouterr().out
    assert "api" in out and "http" in out and "notify, ping" in out and "databricks" in out
    assert cli.main(["-c", str(path), "connectors", "test", "api"]) == 1
    assert "pick one with --step: notify, ping" in capsys.readouterr().err
    assert cli.main(["-c", str(path), "connectors", "test", "api", "--step", "notify"]) == 1
    assert "add --for-real" in capsys.readouterr().err
    assert cli.main(["-c", str(path), "connectors", "test", "api", "--step", "ping"]) == 1
    assert "step would FAIL" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "depart", "--version", "v1", "--no-notify"]) == 1
    capsys.readouterr()
    assert cli.main(["-c", str(path), "board", "--version", "v1", "--steps"]) == 0
    out = capsys.readouterr().out
    assert "warned   ping" in out and "FAILED   notify" in out
