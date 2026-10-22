"""Notifications for train progress. Currently: Slack (bot token or incoming webhook)."""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("deploytrain")

EVENTS = (
    "train_started", "train_resumed", "train_finished",
    "wave_started", "wave_completed",
    "region_waiting", "region_deploying", "region_succeeded",
    "region_failed", "region_rolled_back",
)
DEFAULT_EVENTS = frozenset(EVENTS) - {"region_deploying"}
# Events posted to the channel (not just the thread) in bot mode.
BROADCAST_EVENTS = frozenset({"region_failed", "train_finished"})

STATUS_EMOJI = {
    "pending": ":white_circle:",
    "waiting": ":hourglass_flowing_sand:",
    "deploying": ":rocket:",
    "baking": ":stopwatch:",
    "succeeded": ":white_check_mark:",
    "failed": ":x:",
    "rolled_back": ":rewind:",
    "rollback_failed": ":rotating_light:",
    "aborted": ":octagonal_sign:",
    "running": ":steam_locomotive:",
}

SLACK_API = "https://slack.com/api/"


class NotificationError(RuntimeError):
    pass


class Notifier:
    """No-op base. `attach` gives access to the train (state, config) before any event."""

    def attach(self, train) -> None:
        self.train = train

    def notify(self, event: str, **ctx) -> None:
        pass


@dataclass
class SlackSettings:
    webhook_url_env: str = "SLACK_WEBHOOK_URL"
    token_env: str = "SLACK_BOT_TOKEN"
    channel: str | None = None
    mention_on_failure: str = ""
    events: frozenset = DEFAULT_EVENTS
    timeout: float = 10.0

    @classmethod
    def from_config(cls, raw: dict) -> "SlackSettings":
        events = raw.get("events")
        if events is not None:
            unknown = set(events) - set(EVENTS)
            if unknown:
                raise ValueError(f"unknown slack events {sorted(unknown)}; valid: {list(EVENTS)}")
            events = frozenset(events)
        return cls(
            webhook_url_env=raw.get("webhook_url_env", cls.webhook_url_env),
            token_env=raw.get("token_env", cls.token_env),
            channel=raw.get("channel"),
            mention_on_failure=raw.get("mention_on_failure", ""),
            events=events if events is not None else DEFAULT_EVENTS,
            timeout=float(raw.get("timeout", cls.timeout)),
        )


PostFn = Callable[[str, dict, dict], dict]


def http_post(url: str, payload: dict, headers: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    try:
        return json.loads(body)
    except ValueError:  # incoming webhooks answer with plain "ok"
        return {"ok": body.strip() == "ok", "raw": body}


@dataclass
class SlackNotifier(Notifier):
    settings: SlackSettings
    token: str | None = None
    webhook_url: str | None = None
    post: PostFn | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def bot_mode(self) -> bool:
        return bool(self.token and self.settings.channel)

    # ---- transport ---------------------------------------------------------

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        if self.post:
            return self.post(url, payload, headers)
        return http_post(url, payload, headers, self.settings.timeout)

    def _api(self, method: str, payload: dict) -> dict:
        resp = self._post(SLACK_API + method, payload, {"Authorization": f"Bearer {self.token}"})
        if not resp.get("ok"):
            raise NotificationError(f"slack {method} failed: {resp.get('error', resp)}")
        return resp

    def _webhook(self, text: str) -> None:
        resp = self._post(self.webhook_url, {"text": text}, {})
        if not resp.get("ok"):
            raise NotificationError(f"slack webhook failed: {resp.get('raw', resp)}")

    # ---- formatting --------------------------------------------------------

    def _state(self) -> dict:
        return self.train.state.data

    def _header(self) -> str:
        st = self._state()
        prev = f" (rollback target `{st['previous_version']}`)" if st.get("previous_version") else ""
        status = st.get("status", "running")
        return (f"{STATUS_EMOJI.get(status, '')} Deployment train *{st['train']}* "
                f"`{st['version']}`{prev}: *{status.upper()}*")

    def _summary_blocks(self) -> list[dict]:
        st = self._state()
        lines = []
        for wave in self.train.cfg.waves:
            regions = "   ".join(
                f"{STATUS_EMOJI.get(st['regions'][r.name]['status'], '')} {r.name}"
                for r in wave.regions)
            lines.append(f"*{wave.name}*   {regions}")
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": self._header()}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        ]

    def format(self, event: str, ctx: dict) -> str:
        st = self._state()
        version, prev = st["version"], st.get("previous_version") or "previous version"
        region = f"*{ctx['region']}*" if ctx.get("region") else ""
        mention = f" {self.settings.mention_on_failure}" if self.settings.mention_on_failure else ""
        if event == "train_started":
            waves = " → ".join(w.name for w in self.train.cfg.waves)
            return f":steam_locomotive: Train started for `{version}`. Waves: {waves}"
        if event == "train_resumed":
            return f":repeat: Train resumed for `{version}`"
        if event == "wave_started":
            return f":arrow_forward: Wave *{ctx['wave']}* starting: {', '.join(ctx['regions'])}"
        if event == "wave_completed":
            return f":checkered_flag: Wave *{ctx['wave']}* complete"
        if event == "region_waiting":
            return f":hourglass_flowing_sand: {region} {ctx['message']}"
        if event == "region_deploying":
            return f":rocket: {region} deploying `{version}`"
        if event == "region_succeeded":
            return f":white_check_mark: {region} healthy on `{version}`"
        if event == "region_failed":
            text = f":x: {region} failed: {ctx['error']}{mention}"
            if ctx.get("output"):
                text += f"\n```{ctx['output'][-1500:]}```"
            return text
        if event == "region_rolled_back":
            if ctx["ok"]:
                return f":rewind: {region} rolled back to `{prev}`"
            return f":rotating_light: {region} rollback to `{prev}` FAILED{mention}"
        if event == "train_finished":
            status = ctx["status"]
            emoji = {"succeeded": ":tada:", "failed": ":x:", "aborted": ":octagonal_sign:"}.get(status, "")
            return f"{emoji} {ctx['message']}" + (mention if status == "failed" else "")
        return f"{event}: {ctx}"

    # ---- delivery ----------------------------------------------------------

    def notify(self, event: str, **ctx) -> None:
        with self._lock:
            if self.bot_mode:
                self._notify_bot(event, ctx)
            elif event in self.settings.events:
                st = self._state()
                prefix = "" if event == "train_started" else f"[{st['train']} `{st['version']}`] "
                self._webhook(prefix + self.format(event, ctx))

    def _notify_bot(self, event: str, ctx: dict) -> None:
        slack = self._state().setdefault("slack", {})
        if not slack.get("ts"):
            resp = self._api("chat.postMessage", {
                "channel": self.settings.channel, "text": self._header(),
                "blocks": self._summary_blocks()})
            slack.update(channel=resp["channel"], ts=resp["ts"])
            self.train.state.save()
        if event in self.settings.events:
            self._api("chat.postMessage", {
                "channel": slack["channel"], "thread_ts": slack["ts"],
                "text": self.format(event, ctx),
                "reply_broadcast": event in BROADCAST_EVENTS})
        self._api("chat.update", {
            "channel": slack["channel"], "ts": slack["ts"],
            "text": self._header(), "blocks": self._summary_blocks()})


def build_notifier(settings: SlackSettings | None, post: PostFn | None = None) -> Notifier:
    if settings is None:
        return Notifier()
    token = os.environ.get(settings.token_env)
    webhook = os.environ.get(settings.webhook_url_env)
    if settings.channel and token:
        return SlackNotifier(settings, token=token, post=post)
    if webhook:
        return SlackNotifier(settings, webhook_url=webhook, post=post)
    log.warning("slack notifications configured but neither $%s (with channel) nor $%s is set; "
                "notifications disabled", settings.token_env, settings.webhook_url_env)
    return Notifier()
