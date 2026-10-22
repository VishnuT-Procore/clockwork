"""deploytrain CLI: plan / run / status / abort / windows."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import config as config_mod
from .notify import Notifier, SlackNotifier, build_notifier
from .train import SUCCEEDED, Train, abort_path, plan, request_approval, state_path
from .windows import format_duration, parse_duration


def _parse_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %m-%d %H:%M %Z")


def cmd_plan(args, cfg) -> int:
    start = _parse_at(args.at)
    duration = parse_duration(args.deploy_duration) if args.deploy_duration else None
    print(f"Projected schedule for train {cfg.name!r} starting {start:%Y-%m-%d %H:%M} UTC\n")
    header = f"{'WAVE':<12} {'REGION':<18} {'WINDOW':<14} {'START (UTC)':<20} {'START (local)':<24} {'DONE (UTC)':<20} WAIT"
    print(header)
    print("-" * len(header))
    for wave, planned in plan(cfg, start, duration):
        for p in planned:
            wait = p.start - p.earliest
            print(f"{wave.name:<12} {p.region.name:<18} {p.region.window.name:<14} "
                  f"{p.start:%a %m-%d %H:%M}{'':<5} {_fmt(p.start, p.region.window.timezone):<24} "
                  f"{p.end:%a %m-%d %H:%M}{'':<5} {format_duration(wait) if wait else '-'}")
    last = plan(cfg, start, duration)[-1][1]
    end = max(p.end for p in last)
    print(f"\nEstimated completion: {end:%Y-%m-%d %H:%M} UTC (total {format_duration(end - start)})")
    return 0


def cmd_windows(args, cfg) -> int:
    start = _parse_at(args.at)
    profiles = [cfg.windows[args.profile]] if args.profile else cfg.windows.values()
    for prof in profiles:
        print(f"{prof.name} ({prof.timezone}, min_remaining={format_duration(prof.min_remaining)})")
        for iv in prof.intervals(start, days=args.days)[: args.limit]:
            print(f"  {_fmt(iv.start, prof.timezone)}  ->  {_fmt(iv.end, prof.timezone)}"
                  f"   [{format_duration(iv.end - iv.start)}]")
        for b in prof.blackouts:
            if b.end > start:
                print(f"  blackout: {b.start:%Y-%m-%d %H:%M} -> {b.end:%Y-%m-%d %H:%M} UTC  {b.reason}")
        print()
    return 0


def cmd_run(args, cfg) -> int:
    notifier = Notifier() if args.no_notify else None
    train = Train(cfg, args.version, args.previous_version, dry_run=args.dry_run, notifier=notifier)
    status = train.run(retry_failed=args.retry_failed, force=args.force)
    print(f"\ntrain status: {status}")
    return 0 if status == SUCCEEDED else 1


def cmd_status(args, cfg) -> int:
    path = state_path(cfg, args.version)
    if not path.exists():
        print(f"no state for {cfg.name} {args.version} ({path})", file=sys.stderr)
        return 1
    st = json.loads(path.read_text())
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"train {st['train']}  version {st['version']}  status {st['status'].upper()}")
    print(f"started {st['started_at']}  finished {st['finished_at'] or '-'}\n")
    for name, r in st["regions"].items():
        print(f"  {r['wave']:<12} {name:<18} {r['status']:<16} {r['message']}")
    if args.events:
        print()
        for e in st["events"][-args.events:]:
            print(f"  {e['at']}  {('[' + e['region'] + '] ') if e['region'] else ''}{e['message']}")
    return 0


def cmd_abort(args, cfg) -> int:
    path = abort_path(cfg, args.version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat())
    print(f"abort requested for {cfg.name} {args.version}; the train stops at its next checkpoint")
    return 0


def cmd_approve(args, cfg) -> int:
    if args.wave not in {w.name for w in cfg.waves}:
        print(f"unknown wave {args.wave!r}", file=sys.stderr)
        return 1
    request_approval(cfg, args.version, args.wave, args.by or getpass.getuser())
    print(f"approved wave {args.wave!r} of {cfg.name} {args.version}")
    return 0


def cmd_ui(args, cfg) -> int:
    from .web import serve
    serve(cfg, args.config, host=args.host, port=args.port)
    return 0


def cmd_notify_test(args, cfg) -> int:
    if cfg.slack is None:
        print("no notifications.slack section in config", file=sys.stderr)
        return 1
    notifier = build_notifier(cfg.slack)
    if not isinstance(notifier, SlackNotifier):
        print("slack credentials not found in environment (see warning above)", file=sys.stderr)
        return 1
    # A throwaway in-memory train so nothing touches real state files.
    probe = SimpleNamespace(cfg=cfg, state=SimpleNamespace(save=lambda: None, data={
        "train": cfg.name, "version": "notify-test", "previous_version": None, "status": "succeeded",
        "regions": {r.name: {"status": "pending"} for r in cfg.regions},
    }))
    notifier.attach(probe)
    notifier.notify("train_finished", status="succeeded",
                    message=f"Slack integration test from deploytrain ({cfg.name})")
    mode = f"bot token -> {cfg.slack.channel}" if notifier.bot_mode else "incoming webhook"
    print(f"sent test message via {mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="deploytrain", description="Region-by-region deployment train")
    p.add_argument("-c", "--config", default="train.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="project when each region would deploy")
    sp.add_argument("--at", help="start time (ISO 8601, default now, UTC if no offset)")
    sp.add_argument("--deploy-duration", help="override estimated_deploy_duration, e.g. 15m")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("windows", help="list upcoming open windows per profile")
    sp.add_argument("--profile")
    sp.add_argument("--at")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_windows)

    sp = sub.add_parser("run", help="run (or resume) the train for a version")
    sp.add_argument("--version", required=True)
    sp.add_argument("--previous-version", help="version to roll back to on failure")
    sp.add_argument("--dry-run", action="store_true", help="print commands, respect windows, skip bake")
    sp.add_argument("--retry-failed", action="store_true", help="retry regions that failed last run")
    sp.add_argument("--no-notify", action="store_true", help="don't send Slack notifications")
    sp.add_argument("--force", action="store_true",
                    help="run even if the state says another process owns this train")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="show train progress")
    sp.add_argument("--version", required=True)
    sp.add_argument("--events", type=int, default=10, help="show the last N events")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("abort", help="ask a running train to stop")
    sp.add_argument("--version", required=True)
    sp.set_defaults(func=cmd_abort)

    sp = sub.add_parser("approve", help="approve a wave that is waiting for approval")
    sp.add_argument("--version", required=True)
    sp.add_argument("--wave", required=True)
    sp.add_argument("--by", help="who is approving (default: current user)")
    sp.set_defaults(func=cmd_approve)

    sp = sub.add_parser("ui", help="serve the web UI")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("notify-test", help="send a test Slack message")
    sp.set_defaults(func=cmd_notify_test)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    try:
        return args.func(args, cfg)
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
