"""'What's in this release': the compare link and change list recorded with each departure."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from traintraintrain import cli, config
from traintraintrain.notify import SlackNotifier, SlackSettings
from traintraintrain.train import SUCCEEDED, CommandResult, Train, load_departure
from traintraintrain.web import App, make_handler

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg

NOW = dt("2026-10-26T14:00")
LOG = "a1b2c3d4e5 Fix currency rounding\n0f1e2d3c4b Add EUR payouts\n9988776655 Bump deps\n"
CHANGES = {"url": "https://github.com/acme/pay/compare/{previous_version}...{version}",
           "command": "git log --oneline {previous_version}..{version}"}


class GitRunner(FakeRunner):
    def __init__(self, clock, git_rc=0, **kw):
        super().__init__(clock, **kw)
        self.git_rc = git_rc

    def __call__(self, command, env, timeout):
        if command.startswith("git log"):
            self.calls.append((self.clock.now(), command))
            self.envs.append(env)
            return CommandResult(self.git_rc, LOG if self.git_rc == 0 else "fatal: bad revision")
        return super().__call__(command, env, timeout)


def cfg_with(tmp_path, changes=CHANGES):
    return make_cfg(tmp_path, train__changes=changes)


def test_config_validation(tmp_path):
    assert cfg_with(tmp_path).changes.url.endswith("{version}")
    with pytest.raises(config.ConfigError, match="unknown placeholder"):
        cfg_with(tmp_path, {"url": "https://x/{prev}...{version}"})
    with pytest.raises(config.ConfigError, match="url` and/or a `command"):
        cfg_with(tmp_path, {"timeout": "5s"})
    with pytest.raises(config.ConfigError, match="https://"):
        cfg_with(tmp_path, {"url": "javascript:alert(1)//{version}"})
    assert make_cfg(tmp_path).changes is None


def test_changes_recorded_when_train_departs(tmp_path):
    cfg = cfg_with(tmp_path)
    clock = FakeClock(NOW)
    runner = GitRunner(clock)
    assert Train(cfg, "v2", "v1", runner=runner, clock=clock).run() == SUCCEEDED
    ch = load_departure(cfg, "v2")["changes"]
    assert ch["url"] == "https://github.com/acme/pay/compare/v1...v2"
    assert ch["command"] == "git log --oneline v1..v2" and ch["ok"] is True and ch["output"] == LOG
    assert runner.calls[0][1] == "git log --oneline v1..v2"  # before any deploy
    assert runner.envs[0]["DT_PREVIOUS_VERSION"] == "v1"


def test_changes_command_failure_never_stops_the_train(tmp_path):
    cfg = cfg_with(tmp_path)
    clock = FakeClock(NOW)
    assert Train(cfg, "v2", "v1", runner=GitRunner(clock, git_rc=128), clock=clock).run() == SUCCEEDED
    ch = load_departure(cfg, "v2")["changes"]
    assert ch["ok"] is False and "exited 128" in ch["error"] and ch["url"]


def test_no_previous_version_means_nothing_to_compare(tmp_path):
    cfg = cfg_with(tmp_path)
    clock = FakeClock(NOW)
    runner = GitRunner(clock)
    assert Train(cfg, "v2", runner=runner, clock=clock).run() == SUCCEEDED
    ch = load_departure(cfg, "v2")["changes"]
    assert ch["url"] is None and "nothing to compare" in ch["error"]
    assert not [c for _, c in runner.calls if c.startswith("git")]


def test_changes_collected_once_not_on_resume(tmp_path):
    cfg = cfg_with(tmp_path)
    clock = FakeClock(NOW)
    assert Train(cfg, "v2", "v1", runner=GitRunner(clock, fail={"deploy eu-west": 1}), clock=clock).run() == "failed"
    runner2 = GitRunner(clock)
    assert Train(cfg, "v2", runner=runner2, clock=clock).run(retry_failed=True) == SUCCEEDED
    assert not [c for _, c in runner2.calls if c.startswith("git")]
    assert load_departure(cfg, "v2")["changes"]["output"] == LOG


def test_slack_departure_message_links_the_changes(tmp_path):
    cfg = cfg_with(tmp_path)
    sent = []
    n = SlackNotifier(SlackSettings(events=frozenset({"train_started"})), webhook_url="https://hooks.example/x",
                      post=lambda url, payload, headers: sent.append(payload["text"]) or {"ok": True})
    clock = FakeClock(NOW)
    Train(cfg, "v2", "v1", runner=GitRunner(clock), clock=clock, notifier=n).run()
    assert "<https://github.com/acme/pay/compare/v1...v2|What's in this release> (3 changes)" in sent[0]


def test_web_and_cli_show_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    cfg = cfg_with(tmp_path)
    clock = FakeClock(NOW)
    Train(cfg, "v2", "v1", runner=GitRunner(clock), clock=clock).run()
    app = App(cfg, str(tmp_path / "train.yaml"))
    assert app.config_view()["changes_url"] == CHANGES["url"]
    dep = app.get_departure("v2")
    assert dep["changes"]["url"].endswith("v1...v2") and dep["changes"]["output"] == LOG
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{httpd.server_address[1]}/api/departure?version=v2") as r:
            assert json.loads(r.read())["changes"]["ok"] is True
    finally:
        httpd.shutdown()
    path = tmp_path / "train.yaml"
    import yaml
    path.write_text(yaml.safe_dump({"train": {"name": "svc", "deploy_command": "true",
                                              "state_dir": str(tmp_path / "state"), "changes": CHANGES},
                                    "stops": [{"platforms": ["us-canary", "us-east", "eu-west"]}]}))
    assert cli.main(["-c", str(path), "board", "--version", "v2", "--changes"]) == 0
    out = capsys.readouterr().out
    assert "what's in this release: https://github.com/acme/pay/compare/v1...v2" in out
    assert "changes since v1: 3 line(s)" in out and "Add EUR payouts" in out
