"""traintraintrain CLI: timetable / service / depart / board / clear-signal / brake / ui.

The pre-rename command names (plan, windows, run, status, approve, abort) still work.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from . import config as config_mod
from ._compat import env
from .notify import Notifier, SlackNotifier, build_notifier
from .timetable import format_duration, parse_duration
from .train import (
    SUCCEEDED, Train, apply_brake, clear_signal, project, read_state, state_path,
)


def _parse_at(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fmt(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %m-%d %H:%M %Z")


def _label(status: str) -> str:
    return status.replace("_", " ")


def cmd_timetable(args, cfg) -> int:
    start = _parse_at(args.at)
    duration = parse_duration(args.deploy_duration) if args.deploy_duration else None
    projected = project(cfg, start, duration)
    print(f"Timetable for train {cfg.name!r} departing {start:%Y-%m-%d %H:%M} UTC\n")
    header = (f"{'STOP':<12} {'PLATFORM':<18} {'TIMETABLE':<14} {'DEPARTS (UTC)':<20} "
              f"{'DEPARTS (local)':<24} {'DONE (UTC)':<20} HELD")
    print(header)
    print("-" * len(header))
    for stop, planned in projected:
        for p in planned:
            held = p.start - p.earliest
            tt = p.platform.timetable
            print(f"{stop.name:<12} {p.platform.name:<18} {tt.name:<14} "
                  f"{p.start:%a %m-%d %H:%M}{'':<5} {_fmt(p.start, tt.timezone):<24} "
                  f"{p.end:%a %m-%d %H:%M}{'':<5} {format_duration(held) if held else '-'}")
    end = max(p.end for p in projected[-1][1])
    print(f"\nEstimated arrival: {end:%Y-%m-%d %H:%M} UTC (journey {format_duration(end - start)})")
    return 0


def cmd_service(args, cfg) -> int:
    start = _parse_at(args.at)
    timetables = [cfg.timetables[args.timetable]] if args.timetable else cfg.timetables.values()
    for tt in timetables:
        print(f"{tt.name} ({tt.timezone}, min_remaining={format_duration(tt.min_remaining)})")
        for iv in tt.intervals(start, days=args.days)[: args.limit]:
            print(f"  {_fmt(iv.start, tt.timezone)}  ->  {_fmt(iv.end, tt.timezone)}"
                  f"   [{format_duration(iv.end - iv.start)}]")
        for c in tt.closures:
            if c.end > start:
                print(f"  line closure: {c.start:%Y-%m-%d %H:%M} -> {c.end:%Y-%m-%d %H:%M} UTC  "
                      f"{c.reason}")
        print()
    return 0


def cmd_depart(args, cfg) -> int:
    notifier = Notifier() if args.no_notify else None
    train = Train(cfg, args.version, args.previous_version, dry_run=args.dry_run, notifier=notifier)

    def on_sigterm(signum, frame):
        # e.g. `docker stop` or a Kubernetes pod shutting down: stop at the next checkpoint
        # so the journey can be resumed cleanly.
        logging.getLogger("traintraintrain").warning("SIGTERM received; applying the emergency brake")
        apply_brake(cfg, args.version, "SIGTERM")

    signal.signal(signal.SIGTERM, on_sigterm)
    status = train.run(retry_failed=args.retry_failed, force=args.force)
    print(f"\ntrain status: {_label(status)}")
    return 0 if status == SUCCEEDED else 1


def cmd_board(args, cfg) -> int:
    path = state_path(cfg, args.version)
    st = read_state(path)
    if st is None:
        print(f"no departure for {cfg.name} {args.version} ({path})", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"train {st['train']}  version {st['version']}  status {_label(st['status']).upper()}")
    ended = "arrived" if st["status"] == SUCCEEDED else "stopped"
    print(f"departed {st['started_at']}  {ended} {st['finished_at'] or '-'}\n")
    print(f"  {'STOP':<12} {'PLATFORM':<18} {'STATUS':<16} REMARKS")
    for name, p in st["platforms"].items():
        print(f"  {p['stop']:<12} {name:<18} {_label(p['status']):<16} {p['message']}")
    if args.events:
        print()
        for e in st["events"][-args.events:]:
            where = f"[{e['platform']}] " if e.get("platform") else ""
            print(f"  {e['at']}  {where}{e['message']}")
    return 0


def cmd_brake(args, cfg) -> int:
    apply_brake(cfg, args.version, getpass.getuser())
    print(f"emergency brake applied to {cfg.name} {args.version}; "
          "the train stops at its next checkpoint")
    return 0


def cmd_clear_signal(args, cfg) -> int:
    if args.stop not in {s.name for s in cfg.stops}:
        print(f"unknown stop {args.stop!r}", file=sys.stderr)
        return 1
    clear_signal(cfg, args.version, args.stop, args.by or getpass.getuser())
    print(f"signal cleared at stop {args.stop!r} for {cfg.name} {args.version}")
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
        "platforms": {p.name: {"status": "scheduled"} for p in cfg.platforms},
    }))
    notifier.attach(probe)
    notifier.notify("train_finished", status="succeeded",
                    message=f"Slack integration test from traintraintrain ({cfg.name})")
    mode = f"bot token -> {cfg.slack.channel}" if notifier.bot_mode else "incoming webhook"
    print(f"sent test message via {mode}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="traintraintrain",
        description="Deployment train: roll a release out stop by stop, platform by platform, "
                    "within each platform's timetable.")
    p.add_argument("-c", "--config", default=env("CONFIG", "train.yaml"),
                   help="config file (default: $TRAINTRAINTRAIN_CONFIG or train.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    sp = sub.add_parser("timetable", aliases=["plan"],
                        help="project when each platform would be served")
    sp.add_argument("--at", help="departure time (ISO 8601, default now, UTC if no offset)")
    sp.add_argument("--deploy-duration", help="override estimated_deploy_duration, e.g. 15m")
    sp.set_defaults(func=cmd_timetable)

    sp = sub.add_parser("service", aliases=["windows"],
                        help="list upcoming service hours per timetable")
    sp.add_argument("--timetable", "--profile", dest="timetable")
    sp.add_argument("--at")
    sp.add_argument("--days", type=int, default=14)
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_service)

    sp = sub.add_parser("depart", aliases=["run"], help="start (or resume) the train for a version")
    sp.add_argument("--version", required=True)
    sp.add_argument("--previous-version", help="version to roll back to on failure")
    sp.add_argument("--dry-run", action="store_true",
                    help="print commands, respect timetables, skip dwell")
    sp.add_argument("--retry-failed", action="store_true",
                    help="retry platforms that failed last time")
    sp.add_argument("--no-notify", action="store_true", help="don't send Slack notifications")
    sp.add_argument("--force", action="store_true",
                    help="depart even if the state says another process owns this train")
    sp.set_defaults(func=cmd_depart)

    sp = sub.add_parser("board", aliases=["status"], help="show the departure board for a version")
    sp.add_argument("--version", required=True)
    sp.add_argument("--events", type=int, default=10, help="show the last N events")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_board)

    sp = sub.add_parser("brake", aliases=["abort"], help="pull the emergency brake on a train")
    sp.add_argument("--version", required=True)
    sp.set_defaults(func=cmd_brake)

    sp = sub.add_parser("clear-signal", aliases=["approve"],
                        help="clear the signal at a stop so the train can proceed")
    sp.add_argument("--version", required=True)
    sp.add_argument("--stop", "--wave", dest="stop", required=True)
    sp.add_argument("--by", help="who is clearing the signal (default: current user)")
    sp.set_defaults(func=cmd_clear_signal)

    sp = sub.add_parser("ui", help="serve the web UI")
    sp.add_argument("--host", default=env("UI_HOST", "127.0.0.1"),
                    help="listen address (default: $TRAINTRAINTRAIN_UI_HOST or 127.0.0.1)")
    sp.add_argument("--port", type=int, default=int(env("UI_PORT", "8080")),
                    help="listen port (default: $TRAINTRAINTRAIN_UI_PORT or 8080)")
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
