"""Live version per platform: recorded on every deploy, used to compare and roll back per platform."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from traintraintrain import cli, live
from traintraintrain.storage import backend_for, live_record
from traintraintrain.train import FAILED, ROLLED_BACK, SUCCEEDED, CommandResult, Train, load_departure
from traintraintrain.web import App, make_handler

from test_traintraintrain import FakeClock, FakeRunner, dt, make_cfg

NOW = dt("2026-10-26T14:00")
CHANGES = {"url": "https://github.com/acme/mono/compare/{previous_version}...{version}",
           "command": "git log --oneline {previous_version}..{version} -- services/pay"}
PLATFORMS = ["us-canary", "us-east", "eu-west"]


class GitRunner(FakeRunner):
    """`git log a..b` prints one fake commit per version step between a and b (v1 -> v3: 2)."""

    def __call__(self, command, env, timeout):
        if command.startswith("git log"):
            self.calls.append((self.clock.now(), command))
            self.envs.append(env)
            rng = command.split()[3]
            a, b = (int(x.lstrip("v")) for x in rng.split(".."))
            return CommandResult(0, "".join(f"{i:07x}0 change {i}\n" for i in range(a + 1, b + 1)))
        if command.startswith("live"):
            self.calls.append((self.clock.now(), command))
            return self.live(command)
        return super().__call__(command, env, timeout)

    def live(self, command):
        return CommandResult(0, "")


def cfg_with(tmp_path, **kw):
    return make_cfg(tmp_path, train__changes=CHANGES, **kw)


def depart(cfg, version, previous=None, fail=None, **kw):
    clock = FakeClock(NOW)
    runner = GitRunner(clock, fail=fail)
    status = Train(cfg, version, previous, runner=runner, clock=clock, **kw).run()
    return status, runner


def put_live(cfg, version, *platforms):
    """Record platforms as running `version`, as if a departure had reached only them.

    (A real partial departure fails one of two parallel platforms, and whether the other one
    got the version first depends on thread timing.)
    """
    for p in platforms:
        backend_for(cfg).set_live(cfg.name, live_record(p, version, live.DEPLOYED, departure=version))


def live_versions(cfg):
    return {k: v["version"] for k, v in live.recorded(cfg, backend_for(cfg)).items()}


def test_successful_departure_records_live_versions(tmp_path):
    cfg = cfg_with(tmp_path)
    assert depart(cfg, "v1")[0] == SUCCEEDED
    assert live_versions(cfg) == {p: "v1" for p in PLATFORMS}
    hist = backend_for(cfg).live_history(cfg.name)
    assert {h["platform"] for h in hist} == set(PLATFORMS)
    assert all(h["status"] == live.DEPLOYED and h["departure"] == "v1" for h in hist)


def test_next_departure_compares_each_platform_with_what_it_runs(tmp_path):
    cfg = cfg_with(tmp_path)
    depart(cfg, "v1")
    status, runner = depart(cfg, "v3")
    assert status == SUCCEEDED
    st = load_departure(cfg, "v3")
    assert {p["from_version"] for p in st["platforms"].values()} == {"v1"}
    assert st["previous_version"] == "v1"  # found, not typed
    ch = st["changes"]
    assert ch["previous_version"] == "v1" and ch["url"].endswith("v1...v3")
    assert len(ch["groups"]) == 1 and ch["groups"][0]["platforms"] == PLATFORMS
    assert live.change_count(ch["groups"][0]) == 2
    assert runner.commands("rollback") == []
    assert live_versions(cfg) == {p: "v3" for p in PLATFORMS}


def test_regions_on_different_versions_get_their_own_change_lists(tmp_path):
    cfg = cfg_with(tmp_path, train__rollback_scope="none")
    depart(cfg, "v1")
    put_live(cfg, "v2", "us-canary", "eu-west")
    assert live_versions(cfg) == {"us-canary": "v2", "us-east": "v1", "eu-west": "v2"}
    status, runner = depart(cfg, "v4")
    assert status == SUCCEEDED
    groups = load_departure(cfg, "v4")["changes"]["groups"]
    assert [(g["previous_version"], g["platforms"], live.change_count(g)) for g in groups] == [
        ("v2", ["us-canary", "eu-west"], 2), ("v1", ["us-east"], 3)]
    assert groups[0]["url"].endswith("v2...v4") and groups[1]["url"].endswith("v1...v4")
    # The main (top-level) group is the one covering the most platforms.
    assert load_departure(cfg, "v4")["changes"]["previous_version"] == "v2"
    assert runner.envs[0]["DT_PLATFORMS"] == "us-canary,eu-west"


def test_failed_deploy_is_history_only(tmp_path):
    cfg = cfg_with(tmp_path, train__rollback_scope="none")
    depart(cfg, "v1")
    assert depart(cfg, "v2", fail={"deploy us-canary": 1})[0] == FAILED
    assert live_versions(cfg) == {p: "v1" for p in PLATFORMS}
    hist = backend_for(cfg).live_history(cfg.name, "us-canary")
    assert [(h["status"], h["version"], h["previous_version"]) for h in hist] == [
        (live.FAILED_ATTEMPT, "v2", "v1"), (live.DEPLOYED, "v1", None)]


def sequential_cfg(tmp_path, **train):
    """Like make_cfg's route, but one platform at a time, so failures happen in a fixed order."""
    from traintraintrain import config
    return config.from_dict({
        "train": {"name": "svc", "deploy_command": "deploy {platform} {version}",
                  "inspection_command": "inspect {platform}", "inspection_interval": "5m", "default_dwell": "15m",
                  "rollback_command": "rollback {platform} {previous_version}", "changes": CHANGES,
                  "state_dir": str(tmp_path / "state"), **train},
        "stops": [{"name": "canary", "platforms": ["us-canary"]},
                  {"name": "s1", "parallel_platforms": 1, "platforms": ["us-east", "eu-west"]}]},
        base_dir=tmp_path)


def test_rollback_goes_to_each_platforms_own_version(tmp_path):
    cfg = sequential_cfg(tmp_path, rollback_scope="train")
    depart(cfg, "v1")
    backend_for(cfg).set_live(cfg.name, live_record("us-canary", "v0", live.SET_BY_HAND, "v1", by="ops"))
    status, runner = depart(cfg, "v2", fail={"inspect eu-west": 1})
    assert status == FAILED
    assert sorted(runner.commands("rollback")) == [
        "rollback eu-west v1", "rollback us-canary v0", "rollback us-east v1"]
    st = load_departure(cfg, "v2")
    assert {n: p["status"] for n, p in st["platforms"].items()} == {n: ROLLED_BACK for n in PLATFORMS}
    assert live_versions(cfg) == {"us-canary": "v0", "us-east": "v1", "eu-west": "v1"}
    whats = [(h["platform"], h["status"]) for h in backend_for(cfg).live_history(cfg.name, "eu-west")]
    assert whats[:2] == [("eu-west", live.ROLLED_BACK_TO), ("eu-west", live.FAILED_ATTEMPT)]


def test_given_previous_version_only_fills_in_unknown_platforms(tmp_path):
    cfg = cfg_with(tmp_path)
    backend_for(cfg).set_live(cfg.name, live_record("us-east", "v2", live.SET_BY_HAND, None))
    depart(cfg, "v3", previous="v1")
    st = load_departure(cfg, "v3")
    assert {n: (p["from_version"], p["from_source"]) for n, p in st["platforms"].items()} == {
        "us-canary": ("v1", "given"), "us-east": ("v2", "recorded"), "eu-west": ("v1", "given")}
    assert st["previous_version"] == "v1"


def test_first_departure_without_any_version_has_nothing_to_compare(tmp_path):
    cfg = cfg_with(tmp_path)
    status, runner = depart(cfg, "v1")
    ch = load_departure(cfg, "v1")["changes"]
    assert status == SUCCEEDED and ch["error"] == live.NOTHING_TO_COMPARE and ch["url"] is None
    assert not runner.commands("git")


def test_live_versions_fall_back_to_departure_history(tmp_path):
    """State written before 0.3.0 has no live records: derive them from past departures."""
    cfg = cfg_with(tmp_path)
    depart(cfg, "v1")
    (cfg.state_dir / "svc.live.json").unlink()
    known = live.recorded(cfg, backend_for(cfg))
    assert {k: (v["version"], v["source"]) for k, v in known.items()} == {p: ("v1", "history") for p in PLATFORMS}


def test_live_version_command_is_the_source_of_truth(tmp_path):
    cfg = cfg_with(tmp_path, train__live_version_command="live {platform}")
    depart(cfg, "v1")

    class Hotfixed(GitRunner):
        def live(self, command):
            return CommandResult(0, "noise\nv2\n" if command == "live us-east" else "v1\n")

    clock = FakeClock(NOW)
    runner = Hotfixed(clock)
    assert Train(cfg, "v3", runner=runner, clock=clock).run() == SUCCEEDED
    st = load_departure(cfg, "v3")
    assert st["platforms"]["us-east"]["from_version"] == "v2"
    assert st["platforms"]["us-east"]["from_source"] == "probe"
    assert any("live version is v2, not v1" in e["message"] for e in st["events"])
    hist = backend_for(cfg).live_history(cfg.name, "us-east")
    assert (hist[1]["status"], hist[1]["version"], hist[1]["previous_version"]) == (live.OBSERVED, "v2", "v1")


def test_bad_probe_output_falls_back_to_the_record(tmp_path):
    cfg = cfg_with(tmp_path, train__live_version_command="live {platform}")
    depart(cfg, "v1")

    class Broken(GitRunner):
        def live(self, command):
            return CommandResult(0, "Error from server (NotFound): deployments.apps not found")

    resolved = live.resolve(cfg, backend_for(cfg), Broken(FakeClock(NOW)))
    assert resolved["us-east"]["version"] == "v1" and resolved["us-east"]["source"] == "recorded"
    assert "isn't a version" in resolved["us-east"]["probe_error"]


def test_dry_run_records_nothing(tmp_path):
    cfg = cfg_with(tmp_path)
    assert depart(cfg, "v1", dry_run=True)[0] == SUCCEEDED
    assert backend_for(cfg).live(cfg.name) == {} and backend_for(cfg).live_history(cfg.name) == []


def test_preview_and_live_in_web_ui(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    cfg = cfg_with(tmp_path, train__rollback_scope="none")
    depart(cfg, "v1")
    put_live(cfg, "v2", "us-canary", "eu-west")
    app = App(cfg, str(tmp_path / "train.yaml"))
    app.runner = GitRunner(FakeClock(NOW))
    pv = app.preview("v3")
    assert [(p["platform"], p["version"]) for p in pv["platforms"]] == [
        ("us-canary", "v2"), ("us-east", "v1"), ("eu-west", "v2")]
    assert [(g["previous_version"], live.change_count(g)) for g in pv["changes"]["groups"]] == [("v2", 1), ("v1", 2)]
    assert pv["exists"] is False and app.preview("v1")["exists"] is True
    calls = len(app.runner.calls)
    app.preview("v3")
    assert len(app.runner.calls) == calls  # cached
    view = app.live_view()
    assert {p["platform"]: p["version"] for p in view["platforms"]} == {
        "us-canary": "v2", "us-east": "v1", "eu-west": "v2"}
    assert view["history"][0]["status"] == live.DEPLOYED
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        with opener.open(base + "/api/preview?version=v3") as r:
            assert json.loads(r.read())["changes"]["groups"][0]["platforms"] == ["us-canary", "eu-west"]
        with opener.open(base + "/api/live") as r:
            assert len(json.loads(r.read())["platforms"]) == 3
        try:
            opener.open(base + "/api/preview?version=v3;rm%20-rf")
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()


def write_config(tmp_path, **train):
    path = tmp_path / "train.yaml"
    path.write_text(yaml.safe_dump({
        "train": {"name": "svc", "deploy_command": "true", "state_dir": str(tmp_path / "state"), **train},
        "stops": [{"name": "canary", "platforms": ["us-canary"]},
                  {"name": "s1", "platforms": ["us-east", "eu-west"]}]}))
    return path


def test_cli_live_live_set_and_preview(tmp_path, capsys):
    path = write_config(tmp_path, changes={"url": CHANGES["url"]})
    assert cli.main(["-c", str(path), "depart", "--version", "v1", "--no-notify"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(path), "live-set", "--platform", "us-east", "--version", "payments/v2",
                     "--by", "ops"]) == 0
    assert "us-east: live version set to payments/v2 (was v1)" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "live"]) == 0
    out = capsys.readouterr().out
    assert "us-east" in out and "payments/v2" in out and "set" in out
    assert cli.main(["-c", str(path), "live", "--history"]) == 0
    out = capsys.readouterr().out
    assert "set" in out and "payments/v2" in out and "ops" in out
    assert cli.main(["-c", str(path), "preview", "--version", "v3"]) == 0
    out = capsys.readouterr().out
    assert "us-canary" in out and "v1 -> v3" in out and "compare/payments/v2...v3" in out
    assert cli.main(["-c", str(path), "live-set", "--platform", "nope", "--version", "v1"]) == 1
    assert cli.main(["-c", str(path), "board", "--version", "v1"]) == 0
    assert "FROM" in capsys.readouterr().out


# ---- tracking by tag or by SHA ---------------------------------------------------------

SHAS = {"v1": "a1" * 20, "v2": "b2" * 20, "v2-hotfix": "b2" * 20, "v3": "c3" * 20}


class ShaRunner(FakeRunner):
    """`resolve V` prints V's commit; `git log A..B` prints one line naming the range."""

    def __call__(self, command, env, timeout):
        if command.startswith("resolve "):
            self.calls.append((self.clock.now(), command))
            v = command.split()[1]
            return CommandResult(0, SHAS[v] + "\n") if v in SHAS else CommandResult(128, "unknown revision")
        if command.startswith("git log"):
            self.calls.append((self.clock.now(), command))
            self.envs.append(env)
            return CommandResult(0, f"0000000 range {command.split()[3]}\n")
        return super().__call__(command, env, timeout)


def sha_cfg(tmp_path, **kw):
    return make_cfg(tmp_path, train__changes=CHANGES, train__track_versions_by="sha",
                    train__resolve_command="resolve {version}", **kw)


def run_sha(cfg, version, previous=None, fail=None):
    clock = FakeClock(NOW)
    runner = ShaRunner(clock, fail=fail)
    return Train(cfg, version, previous, runner=runner, clock=clock).run(), runner


def test_sha_tracking_stores_and_compares_commits(tmp_path):
    cfg = sha_cfg(tmp_path)
    run_sha(cfg, "v1")
    status, runner = run_sha(cfg, "v2")
    assert status == SUCCEEDED
    st = load_departure(cfg, "v2")
    assert st["sha"] == SHAS["v2"] and {p["from_sha"] for p in st["platforms"].values()} == {SHAS["v1"]}
    ch = st["changes"]
    assert ch["url"].endswith(f"{SHAS['v1']}...{SHAS['v2']}") and ch["previous_sha"] == SHAS["v1"]
    assert runner.commands("git log") == [f"git log --oneline {SHAS['v1']}..{SHAS['v2']} -- services/pay"]
    env = runner.envs[0]
    assert (env["DT_VERSION_NAME"], env["DT_PREVIOUS_VERSION_NAME"]) == ("v2", "v1")
    rec = backend_for(cfg).live(cfg.name)["us-east"]
    assert (rec["version"], rec["sha"], rec["previous_version"], rec["previous_sha"]) == (
        "v2", SHAS["v2"], "v1", SHAS["v1"])
    assert backend_for(cfg).live_history(cfg.name, "us-east")[0]["sha"] == SHAS["v2"]


def test_sha_tracking_sees_through_a_new_tag_on_the_same_commit(tmp_path):
    cfg = sha_cfg(tmp_path)
    run_sha(cfg, "v2")
    pv = live.preview(cfg, backend_for(cfg), "v2-hotfix", ShaRunner(FakeClock(NOW)))
    assert all(g["up_to_date"] for g in pv["changes"]["groups"])
    # By tag, a new name is something to deploy and compare.
    tag_cfg = make_cfg(tmp_path, train__changes=CHANGES, train__resolve_command="resolve {version}")
    pv = live.preview(tag_cfg, backend_for(tag_cfg), "v2-hotfix", ShaRunner(FakeClock(NOW)))
    assert not any(g["up_to_date"] for g in pv["changes"]["groups"])
    assert pv["changes"]["command"] == "git log --oneline v2..v2-hotfix -- services/pay"


def test_tag_tracking_with_resolve_command_still_records_shas(tmp_path):
    cfg = make_cfg(tmp_path, train__changes=CHANGES, train__resolve_command="resolve {version}")
    run_sha(cfg, "v1")
    status, runner = run_sha(cfg, "v3")
    assert runner.commands("git log") == ["git log --oneline v1..v3 -- services/pay"]
    assert backend_for(cfg).live(cfg.name)["us-canary"]["sha"] == SHAS["v3"]


def test_sha_tracking_without_resolve_command_uses_sha_versions(tmp_path):
    cfg = make_cfg(tmp_path, train__changes=CHANGES, train__track_versions_by="sha")
    a, b = "1234567abc", "89abcde012"
    run_sha(cfg, a)
    status, runner = run_sha(cfg, b)
    assert load_departure(cfg, b)["sha"] == b
    assert runner.commands("git log") == [f"git log --oneline {a}..{b} -- services/pay"]
    # A tag can't be tracked by SHA without a way to look it up: compared by name instead.
    status, runner = run_sha(cfg, "v9")
    st = load_departure(cfg, "v9")
    assert st["sha"] is None and any("isn't a commit SHA" in e["message"] for e in st["events"])
    assert runner.commands("git log") == [f"git log --oneline {b}..v9 -- services/pay"]


def test_rollback_and_deploy_commands_get_shas(tmp_path):
    cfg = sha_cfg(tmp_path, train__rollback_command="rollback {platform} {previous_version} {previous_sha}",
                  train__deploy_command="deploy {platform} {version} {sha}")
    run_sha(cfg, "v1")
    status, runner = run_sha(cfg, "v2", fail={"inspect us-canary": 1})
    assert status == FAILED
    assert runner.commands("deploy") == [f"deploy us-canary v2 {SHAS['v2']}"]
    assert runner.commands("rollback") == [f"rollback us-canary v1 {SHAS['v1']}"]
    rec = backend_for(cfg).live(cfg.name)["us-canary"]
    assert (rec["version"], rec["sha"], rec["status"]) == ("v1", SHAS["v1"], live.ROLLED_BACK_TO)


def test_unresolvable_version_never_stops_the_train(tmp_path):
    cfg = sha_cfg(tmp_path)
    status, _ = run_sha(cfg, "v404")
    st = load_departure(cfg, "v404")
    assert status == SUCCEEDED and st["sha"] is None
    assert any("resolve_command exited 128" in e["message"] for e in st["events"])


def test_track_versions_by_config_validation(tmp_path):
    import pytest
    from traintraintrain import config
    with pytest.raises(config.ConfigError, match="tag` or `sha"):
        make_cfg(tmp_path, train__track_versions_by="hash")
    with pytest.raises(config.ConfigError, match="resolve_command"):
        make_cfg(tmp_path, train__resolve_command="git rev-parse {version}^{commit}")
    assert make_cfg(tmp_path, train__resolve_command="git rev-parse {version}^{{commit}}").resolve_command


def test_cli_shows_and_sets_shas(tmp_path, capsys):
    path = write_config(tmp_path, track_versions_by="sha")
    assert cli.main(["-c", str(path), "depart", "--version", "abcdef1234", "--no-notify"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(path), "live-set", "--platform", "us-east", "--version", "v1.3.1",
                     "--sha", "FEEDBEEF00"]) == 0
    assert "v1.3.1 (feedbeef0)" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "live"]) == 0
    out = capsys.readouterr().out
    assert "tracked by sha" in out and "v1.3.1 (feedbeef0)" in out and "abcdef1234" in out
    assert cli.main(["-c", str(path), "live-set", "--platform", "us-east", "--version", "v1",
                     "--sha", "not-a-sha"]) == 1


# ---- not in production yet ---------------------------------------------------------------

def test_pending_compares_the_head_branch_with_what_is_live(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    cfg = cfg_with(tmp_path, train__rollback_scope="none")
    assert live.pending(cfg, backend_for(cfg), GitRunner(FakeClock(NOW))) is None  # no head set
    cfg = make_cfg(tmp_path, train__rollback_scope="none", train__changes={**CHANGES, "head": "v5"})
    depart(cfg, "v1")
    put_live(cfg, "v3", "us-canary", "eu-west")
    result = live.pending(cfg, backend_for(cfg), GitRunner(FakeClock(NOW)))
    assert result["head"] == "v5"
    assert [(g["previous_version"], g["platforms"], live.change_count(g)) for g in result["changes"]["groups"]] == [
        ("v3", ["us-canary", "eu-west"], 2), ("v1", ["us-east"], 4)]
    app = App(cfg, str(tmp_path / "train.yaml"))
    app.runner = GitRunner(FakeClock(NOW))
    assert app.config_view()["changes_head"] == "v5"
    assert app.pending()["head"] == "v5" and len(app.pending()["changes"]["groups"]) == 2
    # Once everything runs the head, nothing is waiting.
    depart(cfg, "v5")
    result = live.pending(cfg, backend_for(cfg), GitRunner(FakeClock(NOW)))
    assert all(g["up_to_date"] for g in result["changes"]["groups"])


def test_pending_by_sha_and_cli(tmp_path, capsys):
    import pytest
    from traintraintrain import config
    with pytest.raises(config.ConfigError, match="branch or ref"):
        make_cfg(tmp_path, train__changes={**CHANGES, "head": "main; rm -rf /"})
    path = write_config(tmp_path, changes={"url": CHANGES["url"], "head": "main"})
    assert cli.main(["-c", str(path), "pending"]) == 0  # nothing deployed yet: unknown everywhere
    assert "unknown: never deployed?" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "depart", "--version", "main", "--no-notify"]) == 0
    capsys.readouterr()
    assert cli.main(["-c", str(path), "pending"]) == 0
    assert "every platform runs main: nothing is waiting" in capsys.readouterr().out
    path = write_config(tmp_path, changes={"url": CHANGES["url"]})
    assert cli.main(["-c", str(path), "pending"]) == 1
