"""The pre-railway names (waves, regions, windows, ...) keep working."""
import json
from datetime import timedelta

import pytest

from traintraintrain import cli, config
from traintraintrain.train import SUCCEEDED, Train, state_path

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg

LEGACY_RAW = {
    "train": {
        "name": "svc",
        "deploy_command": "deploy {region} {version}",
        "health_check_command": "inspect {region}",
        "rollback_command": "rollback {region} {previous_version}",
        "health_check_interval": "5m",
        "default_bake": "15m",
        "default_window": "us",
        "window_wait_timeout": "3d",
        "rollback_scope": "region",
        "blackouts": [{"start": "2026-12-24", "end": "2026-12-26", "reason": "xmas"}],
    },
    "windows": {
        "us": {"timezone": "America/New_York",
               "windows": [{"days": "mon-thu", "start": "09:00", "end": "16:00"}]},
        "eu": {"timezone": "Europe/Dublin",
               "windows": [{"days": "mon-fri", "start": "09:00", "end": "17:00"}],
               "blackouts": [{"start": "2026-11-05", "end": "2026-11-05"}]},
    },
    "waves": [
        {"name": "canary", "regions": ["us-canary"]},
        {"name": "s1", "max_parallel": 2, "gap": "1h", "approval": True, "bake": "30m",
         "regions": ["us-east", {"name": "eu-west", "window": "eu"}]},
    ],
}


def test_legacy_config_keys_load_as_railway_config(tmp_path):
    cfg = config.from_dict({**LEGACY_RAW, "train": {**LEGACY_RAW["train"],
                                                    "state_dir": str(tmp_path)}})
    assert [s.name for s in cfg.stops] == ["canary", "s1"]
    s1 = cfg.stops[1]
    assert (s1.parallel_platforms, s1.headway, s1.signal) == (2, timedelta(hours=1), True)
    assert [p.name for p in s1.platforms] == ["us-east", "eu-west"]
    assert s1.platforms[1].timetable.name == "eu" and s1.platforms[0].dwell == timedelta(minutes=30)
    assert cfg.stops[0].platforms[0].dwell == timedelta(minutes=15)
    assert cfg.inspection_command == "inspect {region}"
    assert cfg.inspection_interval == timedelta(minutes=5)
    assert cfg.max_hold == timedelta(days=3)
    assert cfg.rollback_scope == "platform"
    assert len(cfg.timetables["eu"].closures) == 2  # global + per-timetable
    assert cfg.timetables["us"].closures[0].reason == "xmas"


def test_legacy_wave_rollback_scope():
    raw = {**LEGACY_RAW, "train": {**LEGACY_RAW["train"], "rollback_scope": "wave"}}
    assert config.from_dict(raw).rollback_scope == "stop"


def test_new_and_legacy_key_together_is_an_error():
    raw = {"train": {"name": "s", "deploy_command": "d", "default_dwell": "1m",
                     "default_bake": "2m"},
           "stops": [{"platforms": ["a"]}]}
    with pytest.raises(config.ConfigError, match="aliases"):
        config.from_dict(raw)


def test_legacy_region_template_variable(tmp_path):
    cfg = make_cfg(tmp_path, train__deploy_command="deploy {region}@{wave} {version}")
    clock = FakeClock(dt("2026-10-26T14:00"))
    runner = FakeRunner(clock)
    assert Train(cfg, "v2", runner=runner, clock=clock).run() == SUCCEEDED
    assert runner.commands("deploy")[0] == "deploy us-canary@canary v2"


def test_pre_rename_state_file_resumes(tmp_path):
    cfg = make_cfg(tmp_path)
    old_state = {
        "train": "svc", "version": "v2", "previous_version": "v1", "status": "aborted",
        "pid": None, "host": None, "started_at": "2026-10-26T14:00:00+00:00", "finished_at": None,
        "waves": {"canary": {"finished_at": "2026-10-26T14:30:00+00:00", "approval": None},
                  "s1": {"finished_at": None,
                         "approval": {"status": "approved", "by": "erin",
                                      "requested_at": "x", "approved_at": "y"}}},
        "regions": {
            "us-canary": {"wave": "canary", "status": "succeeded", "started_at": None,
                          "finished_at": None, "message": ""},
            "us-east": {"wave": "s1", "status": "pending", "started_at": None,
                        "finished_at": None, "message": ""},
            "eu-west": {"wave": "s1", "status": "baking", "started_at": None,
                        "finished_at": None, "message": ""},
        },
        "events": [{"at": "2026-10-26T14:00:00+00:00", "region": "us-canary", "message": "x"}],
    }
    path = state_path(cfg, "v2")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(old_state))

    clock = FakeClock(dt("2026-10-26T15:00"))
    runner = FakeRunner(clock)
    train = Train(cfg, "v2", runner=runner, clock=clock)
    assert train.run() == SUCCEEDED
    assert sorted(runner.commands("deploy")) == ["deploy eu-west v2", "deploy us-east v2"]
    st = json.loads(path.read_text())
    assert "regions" not in st and st["platforms"]["us-east"]["stop"] == "s1"
    assert st["stops"]["s1"]["signal"]["cleared_by"] == "erin"
    assert st["events"][0]["platform"] == "us-canary"


@pytest.mark.parametrize("old, new", [("plan", "timetable"), ("windows", "service")])
def test_legacy_cli_command_names(tmp_path, capsys, old, new):
    path = tmp_path / "train.yaml"
    import yaml
    path.write_text(yaml.safe_dump({**LEGACY_RAW, "train": {**LEGACY_RAW["train"],
                                                            "state_dir": str(tmp_path)}}))
    at = ["--at", "2026-10-22T12:00"]  # fixed, so both runs compute the same schedule
    assert cli.main(["-c", str(path), old, *at]) == 0
    old_out = capsys.readouterr().out
    assert cli.main(["-c", str(path), new, *at]) == 0
    assert capsys.readouterr().out.splitlines()[2:] == old_out.splitlines()[2:]


def test_legacy_cli_run_status_approve_abort(tmp_path, capsys):
    path = tmp_path / "train.yaml"
    path.write_text(f"""
train:
  name: cli
  deploy_command: "true"
  state_dir: {tmp_path}
waves:
  - name: one
    regions: [a]
""")
    assert cli.main(["-c", str(path), "run", "--version", "v1"]) == 0
    assert cli.main(["-c", str(path), "status", "--version", "v1"]) == 0
    assert "SUCCEEDED" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "approve", "--version", "v1", "--wave", "one"]) == 0
    assert cli.main(["-c", str(path), "abort", "--version", "v1"]) == 0
