from datetime import timedelta

import pytest

from deploytrain import config
from deploytrain.notify import Notifier, SlackNotifier, SlackSettings, build_notifier
from deploytrain.train import FAILED, SUCCEEDED, Train

from test_deploytrain import FakeClock, FakeRunner, dt, make_cfg


class FakeSlack:
    """Mimics the Slack Web API and incoming webhooks."""

    def __init__(self, fail_methods=()):
        self.calls = []
        self.fail_methods = set(fail_methods)

    def __call__(self, url, payload, headers):
        self.calls.append((url.rsplit("/", 1)[-1], payload, headers))
        method = url.rsplit("/", 1)[-1]
        if method in self.fail_methods:
            return {"ok": False, "error": "channel_not_found"}
        if method == "chat.postMessage":
            return {"ok": True, "channel": "C123", "ts": f"{len(self.calls)}.000"}
        return {"ok": True}

    def posts(self):
        return [p for m, p, _ in self.calls if m == "chat.postMessage"]

    def thread_texts(self):
        return [p["text"] for p in self.posts() if "thread_ts" in p]


def run_train(tmp_path, notifier, fail=None, **cfg_overrides):
    cfg = make_cfg(tmp_path, **cfg_overrides)
    cfg.stops[1].parallel_platforms = 1
    clock = FakeClock(dt("2026-10-26T14:00"))
    train = Train(cfg, "v2", "v1", runner=FakeRunner(clock, fail=fail), clock=clock,
                  notifier=notifier)
    return train, train.run()


def bot(fake, **kw):
    return SlackNotifier(SlackSettings(channel="#deploys", **kw), token="xoxb-test", post=fake)


def test_bot_mode_threads_and_updates_summary(tmp_path):
    fake = FakeSlack()
    train, status = run_train(tmp_path, bot(fake))
    assert status == SUCCEEDED
    parent = fake.posts()[0]
    assert "thread_ts" not in parent and parent["channel"] == "#deploys"
    assert fake.calls[0][2]["Authorization"] == "Bearer xoxb-test"
    replies = [p for p in fake.posts() if "thread_ts" in p]
    assert all(p["thread_ts"] == "1.000" and p["channel"] == "C123" for p in replies)
    texts = fake.thread_texts()
    assert any("Train departed" in t for t in texts)
    assert any("*us-canary* healthy on `v2`" in t for t in texts)
    assert not any("deploying" in t for t in texts)  # platform_deploying is off by default
    final = [p for m, p, _ in fake.calls if m == "chat.update"][-1]
    assert "SUCCEEDED" in final["text"]
    assert final["blocks"][1]["text"]["text"].count(":white_check_mark:") == 3
    assert train.state.data["slack"] == {"channel": "C123", "ts": "1.000"}


def test_failure_is_broadcast_with_mention_and_output(tmp_path):
    fake = FakeSlack()
    _, status = run_train(tmp_path, bot(fake, mention_on_failure="<!here>"),
                          fail={"inspect us-east": 1})
    assert status == FAILED
    failed = next(p for p in fake.posts() if p.get("text", "").startswith(":x: *us-east*"))
    assert failed["reply_broadcast"] is True
    assert "<!here>" in failed["text"] and "```boom```" in failed["text"]
    assert any("rolled back to `v1`" in t for t in fake.thread_texts())


def test_resume_reuses_thread(tmp_path):
    fake = FakeSlack()
    run_train(tmp_path, bot(fake), fail={"deploy eu-west": 1})
    fake2 = FakeSlack()
    cfg = make_cfg(tmp_path)
    clock = FakeClock(dt("2026-10-27T14:00"))
    Train(cfg, "v2", runner=FakeRunner(clock), clock=clock,
          notifier=bot(fake2)).run(retry_failed=True)
    assert all(p.get("thread_ts") == "1.000" for p in fake2.posts())  # no new parent
    assert any("resumed its journey" in t for t in fake2.thread_texts())


def test_webhook_mode_respects_event_filter(tmp_path):
    fake = FakeSlack()
    n = SlackNotifier(SlackSettings(events=frozenset({"train_started", "train_finished"})),
                      webhook_url="https://hooks.slack.com/x", post=fake)
    run_train(tmp_path, n)
    texts = [p["text"] for _, p, _ in fake.calls]
    assert len(texts) == 2
    assert texts[0].startswith(":steam_locomotive: Train departed")
    assert texts[1].startswith("[svc `v2`] :tada:")


def test_slack_errors_never_break_the_train(tmp_path):
    fake = FakeSlack(fail_methods={"chat.postMessage"})
    _, status = run_train(tmp_path, bot(fake))
    assert status == SUCCEEDED


def test_build_notifier_picks_mode_from_env(monkeypatch):
    s = SlackSettings(channel="#d")
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert type(build_notifier(s)) is Notifier
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/x")
    assert not build_notifier(s).bot_mode
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    assert build_notifier(s).bot_mode


def test_config_rejects_unknown_event():
    with pytest.raises(config.ConfigError, match="unknown slack events"):
        config.from_dict({
            "train": {"name": "s", "deploy_command": "d"},
            "notifications": {"slack": {"events": ["nope"]}},
            "waves": [{"regions": ["a"]}],
        })


def test_signal_events_and_legacy_event_names(tmp_path):
    fake = FakeSlack()
    n = SlackNotifier(SlackSettings.from_config({
        "events": ["wave_awaiting_approval", "signal_cleared"], "ui_url": "https://dt.example"}),
        webhook_url="https://hooks.slack.com/x", post=fake)
    assert n.settings.events == {"stop_at_signal", "signal_cleared"}
    cfg = make_cfg(tmp_path)
    cfg.stops[1].signal = True
    clock = FakeClock(dt("2026-10-26T14:00"))
    train = Train(cfg, "v2", "v1", runner=FakeRunner(clock), clock=clock, notifier=n)

    def clear_when_red(seconds, _orig=clock.sleep):
        if train.state.data.get("status") == "at_signal":
            from deploytrain.train import clear_signal
            clear_signal(cfg, "v2", "s1", "dana")
        _orig(seconds)

    clock.sleep = clear_when_red
    assert train.run() == SUCCEEDED
    texts = [p["text"] for _, p, _ in fake.calls]
    assert "Signal at *s1* is red" in texts[0] and "<https://dt.example|Clear it in the UI>" in texts[0]
    assert "cleared by *dana*" in texts[1]
