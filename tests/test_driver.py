"""The driver contract: one program performs the train's steps over JSON on stdin/stdout."""
import json
import shutil
import sys
from pathlib import Path

import pytest

from traintraintrain import cli, config, live
from traintraintrain.storage import backend_for
from traintraintrain.train import FAILED, ROLLED_BACK, SUCCEEDED, Train, load_departure

from test_traintraintrain import FakeClock, FakeRunner, dt

NOW = dt("2026-10-26T14:00")
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "driver" / "deployer.py"
PLATFORMS = ["us-canary", "us-east", "eu-west"]

# A driver whose behaviour each test picks with $TEST_DRIVER_MODE. It appends every request
# to requests.jsonl so tests can check what traintraintrain sent.
TEST_DRIVER = r'''
import json, os, sys, time
req = json.load(sys.stdin)
with open("requests.jsonl", "a") as fh:
    fh.write(json.dumps(req) + "\n")
mode = os.environ.get("TEST_DRIVER_MODE", "ok")
action = req["action"]
print(f"handling {action}", file=sys.stderr)
if mode == "badjson":
    print("deployed, I think")
elif mode == "exit3":
    print(json.dumps({"ok": True})); sys.exit(3)
elif mode == "sleep" and action == "deploy":
    time.sleep(10)
elif mode == "invalid":
    print(json.dumps({"ok": "yes"}))
elif mode == "unsupported" or (mode == "no-live" and action == "live_version"):
    print(json.dumps({"ok": False, "unsupported": True}))
elif mode == "fail" and action == "deploy" and req["platform"] == "us-east":
    print(json.dumps({"ok": False, "message": "helm: timed out waiting for the rollout"}))
elif action == "resolve":
    print(json.dumps({"ok": True, "sha": {"v1": "a" * 40, "v2": "b" * 40}.get(req["version"], "c" * 40)}))
elif action == "changes":
    print("some chatter first")
    print(json.dumps({"ok": True, "url": "https://example.test/compare",
                      "changes": [{"id": "abc1234", "title": "Fix rounding", "url": "https://example.test/c/1"},
                                  {"title": "Untracked note"}]}))
elif action == "live_version":
    print(json.dumps({"ok": True, "version": "v1", "sha": "a" * 40}))
else:
    print(json.dumps({"ok": True, "message": f"{action} {req.get('platform')} {req.get('version')} done"}))
'''


def make(tmp_path, monkeypatch, script=TEST_DRIVER, mode="ok", **train):
    (tmp_path / "driver.py").write_text(script)
    monkeypatch.setenv("TEST_DRIVER_MODE", mode)
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL", raising=False)
    raw = {"train": {"name": "svc", "driver": {"command": f"{sys.executable} driver.py"},
                     "default_dwell": "0s", "state_dir": str(tmp_path / "state"), **train},
           "stops": [{"name": "canary", "platforms": [{"name": "us-canary", "vars": {"cluster": "c1"}}]},
                     {"name": "s1", "platforms": ["us-east", "eu-west"]}]}
    return config.from_dict(raw, base_dir=tmp_path)


def requests(tmp_path, action=None):
    path = tmp_path / "requests.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if action is None or r["action"] == action]


def depart(cfg, version, previous=None):
    clock = FakeClock(NOW)
    return Train(cfg, version, previous, runner=FakeRunner(clock), clock=clock).run()


def test_driver_performs_every_step(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, track_versions_by="sha", changes={"head": "main"})
    assert cfg.driver.actions == frozenset(["deploy", "inspect", "rollback", "live_version", "resolve", "changes"])
    assert cfg.deploy_command is None
    assert depart(cfg, "v2") == SUCCEEDED
    deploys = requests(tmp_path, "deploy")
    assert [d["platform"] for d in deploys] == PLATFORMS
    first = deploys[0]
    assert first["contract"] == 1 and first["train"] == "svc" and first["departure"] == "v2"
    assert (first["version"], first["sha"]) == ("v2", "b" * 40)
    assert (first["previous_version"], first["previous_sha"]) == ("v1", "a" * 40)  # from live_version
    assert first["vars"] == {"cluster": "c1"} and first["stop"] == "canary" and first["dry_run"] is False
    assert len(requests(tmp_path, "inspect")) == 3 and len(requests(tmp_path, "live_version")) == 3
    st = load_departure(cfg, "v2")
    assert any(e["message"] == "deploy: driver deploy us-canary v2" for e in st["events"])
    group = st["changes"]["groups"][0]
    assert group["output"] == "abc1234 Fix rounding\nUntracked note\n"
    assert group["items"][0]["url"] == "https://example.test/c/1" and group["url"] == "https://example.test/compare"
    assert requests(tmp_path, "changes")[0]["platforms"] == PLATFORMS
    rec = backend_for(cfg).live(cfg.name)["eu-west"]
    assert (rec["version"], rec["sha"]) == ("v2", "b" * 40)


def test_a_failed_reply_fails_the_platform_with_its_message(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, mode="fail", rollback_scope="platform")
    assert depart(cfg, "v2", "v1") == FAILED
    st = load_departure(cfg, "v2")
    east = st["platforms"]["us-east"]
    assert east["status"] == ROLLED_BACK
    assert east["message"].startswith("deploy failed: helm: timed out waiting for the rollout")
    assert "handling deploy" in east["output"]  # the driver's stderr is kept
    assert requests(tmp_path, "rollback")[0]["previous_version"] == "v1"


@pytest.mark.parametrize("mode, why", [
    ("badjson", "replied with no JSON object"),
    ("exit3", "driver said ok but exited 3"),
    ("invalid", 'breaks the contract: "ok" must be true or false'),
    ("unsupported", "the driver doesn't support deploy"),
])
def test_broken_replies_fail_the_step_cleanly(tmp_path, monkeypatch, mode, why):
    cfg = make(tmp_path, monkeypatch, mode=mode, rollback_scope="none")
    assert depart(cfg, "v2", "v1") == FAILED
    assert why in load_departure(cfg, "v2")["platforms"]["us-canary"]["message"]


def test_a_hung_driver_times_out(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, mode="sleep", rollback_scope="none", deploy_timeout="1s")
    assert depart(cfg, "v2", "v1") == FAILED
    msg = load_departure(cfg, "v2")["platforms"]["us-canary"]["message"]
    assert "exited 124" in msg and "timed out after 1s" in msg


def test_unsupported_optional_steps_fall_back(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, mode="no-live")
    assert depart(cfg, "v1") == SUCCEEDED
    assert depart(cfg, "v2") == SUCCEEDED
    st = load_departure(cfg, "v2")
    assert {(p["from_version"], p["from_source"]) for p in st["platforms"].values()} == {("v1", "recorded")}


def test_commands_win_over_the_driver(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, deploy_command="deploy {platform} {version}",
               driver={"command": f"{sys.executable} driver.py", "actions": ["deploy", "inspect"]})
    assert cfg.driver.actions == frozenset(["inspect"])
    clock = FakeClock(NOW)
    runner = FakeRunner(clock)
    assert Train(cfg, "v1", runner=runner, clock=clock).run() == SUCCEEDED
    assert runner.commands("deploy") == ["deploy us-canary v1", "deploy us-east v1", "deploy eu-west v1"]
    assert requests(tmp_path, "deploy") == [] and len(requests(tmp_path, "inspect")) == 3
    assert requests(tmp_path, "resolve") == []  # not in the driver's actions


def test_the_example_driver_end_to_end(tmp_path, monkeypatch):
    shutil.copy(EXAMPLE, tmp_path / "deployer.py")
    cfg = make(tmp_path, monkeypatch, script=(tmp_path / "deployer.py").read_text(), rollback_scope="stop")
    assert depart(cfg, "v1") == SUCCEEDED
    assert json.loads((tmp_path / ".fake-cluster.json").read_text())["eu-west"]["version"] == "v1"
    monkeypatch.setenv("FAIL_DEPLOY", "eu-west:v2")
    assert depart(cfg, "v2") == FAILED
    cluster = json.loads((tmp_path / ".fake-cluster.json").read_text())
    assert {p: cluster[p]["version"] for p in PLATFORMS} == {"us-canary": "v2", "us-east": "v1", "eu-west": "v1"}
    st = load_departure(cfg, "v2")
    assert "rollout of v2 timed out on eu-west" in st["platforms"]["eu-west"]["message"]
    resolved = live.resolve(cfg, backend_for(cfg), FakeRunner(FakeClock(NOW)))
    assert {p: (r["version"], r["source"]) for p, r in resolved.items()} == {
        "us-canary": ("v2", "probe"), "us-east": ("v1", "probe"), "eu-west": ("v1", "probe")}


def test_contract_cli(tmp_path, monkeypatch, capsys):
    shutil.copy(EXAMPLE, tmp_path / "deployer.py")
    path = tmp_path / "train.yaml"
    path.write_text(json.dumps({"train": {"name": "svc", "driver": f"{sys.executable} deployer.py",
                                          "state_dir": str(tmp_path / "state")},
                                "stops": [{"platforms": ["p1"]}]}))
    monkeypatch.delenv("TRAINTRAINTRAIN_DATABASE_URL", raising=False)
    assert cli.main(["-c", str(path), "contract", "test", "--action", "deploy", "--version", "v9"]) == 0
    out = capsys.readouterr().out
    assert '"dry_run": true' in out and "contract OK: deploy succeeded (dry run: would deploy p1:v9)" in out
    assert not (tmp_path / ".fake-cluster.json").exists()  # a dry run changes nothing
    assert cli.main(["-c", str(path), "contract", "test", "--action", "inspect"]) == 1
    assert "contract OK, but the step failed" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "contract", "test", "--action", "changes"]) == 0
    assert "doesn't support changes" in capsys.readouterr().out
    (tmp_path / "deployer.py").write_text('print("{\\"ok\\": 1}")')
    assert cli.main(["-c", str(path), "contract", "test", "--action", "resolve"]) == 1
    assert "contract BROKEN" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "contract", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["request"]["properties"]["contract"]["const"] == 1


def test_driver_config_validation(tmp_path):
    def cfg(**train):
        return config.from_dict({"train": {"name": "s", **train}, "stops": [{"platforms": ["p"]}]},
                                base_dir=tmp_path)
    with pytest.raises(config.ConfigError, match="deploy_command is required"):
        cfg()
    with pytest.raises(config.ConfigError, match="needs a `command`"):
        cfg(driver={"actions": ["deploy"]})
    with pytest.raises(config.ConfigError, match="must be a list of"):
        cfg(driver={"command": "x", "actions": ["deploy", "launch"]})
    with pytest.raises(config.ConfigError, match="deploy_command is required"):
        cfg(driver={"command": "x", "actions": ["inspect"]})
    c = cfg(driver="./deploy.sh", changes={"head": "main"})
    assert c.handles("deploy") and c.handles("changes") and c.driver.argv == ["./deploy.sh"]
    assert c.driver.cwd == tmp_path.resolve()
