"""Load and validate the train YAML config.

Railway vocabulary: a train makes *stops* in order; each stop serves one or more
*platforms* (regions). Platforms follow a *timetable* (service hours, minus line
closures), *dwell* after a deploy while being inspected, and a stop may have a *signal*
that someone must clear before the train proceeds. The pre-rename keys (waves, regions,
windows, bake, gap, approval, ...) are still accepted as aliases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

from ._compat import LEGACY_STATE_DIR, env, pick
from .notify import SlackSettings
from .timetable import LineClosure, Timetable, parse_duration

ALWAYS_IN_SERVICE = "always"
ROLLBACK_SCOPES = ("none", "platform", "stop", "train")
LEGACY_ROLLBACK_SCOPES = {"region": "platform", "wave": "stop"}


class ConfigError(ValueError):
    pass


@dataclass
class Platform:
    name: str
    stop: str
    timetable: Timetable
    dwell: timedelta
    deploy_timeout: timedelta
    vars: dict = field(default_factory=dict)


@dataclass
class Stop:
    name: str
    platforms: list[Platform]
    parallel_platforms: int = 1
    # Minimum wait after the previous stop finished before this one may start.
    headway: timedelta = timedelta(0)
    # If true, the stop waits for someone to clear the signal (UI, CLI) before deploying.
    signal: bool = False


CHANGES_PLACEHOLDERS = {"train": "t", "version": "v2", "previous_version": "v1"}


@dataclass
class Changes:
    """How to show what's in a release: a compare link and/or a command listing the changes."""
    url: str | None = None
    command: str | None = None
    timeout: timedelta = timedelta(seconds=60)

    def render(self, template: str, train: str, version: str, previous_version: str) -> str:
        return template.format(train=train, version=version, previous_version=previous_version)


@dataclass
class TrainConfig:
    name: str
    deploy_command: str
    inspection_command: str | None
    rollback_command: str | None
    inspection_interval: timedelta
    max_hold: timedelta
    rollback_scope: str
    estimated_deploy_duration: timedelta
    timetables: dict[str, Timetable]
    stops: list[Stop]
    state_dir: Path
    slack: SlackSettings | None = None
    # Set from TRAINTRAINTRAIN_DATABASE_URL; when present, state lives in Postgres.
    database_url: str | None = None
    changes: Changes | None = None

    @property
    def platforms(self) -> list[Platform]:
        return [p for s in self.stops for p in s.platforms]


def _default_state_dir(base_dir: Path) -> str:
    # Keep using a state folder created before the rename, so journeys can be resumed.
    if not (base_dir / ".traintraintrain").exists() and (base_dir / LEGACY_STATE_DIR).is_dir():
        return LEGACY_STATE_DIR
    return ".traintraintrain"


def load(path: str | Path) -> TrainConfig:
    path = Path(path)
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    return from_dict(raw, base_dir=path.parent)


def from_dict(raw: dict, base_dir: Path = Path(".")) -> TrainConfig:
    try:
        return _from_dict(raw, base_dir)
    except ConfigError:
        raise
    except ValueError as e:  # bad durations, days, aliases used twice, ...
        raise ConfigError(str(e)) from e


def _from_dict(raw: dict, base_dir: Path) -> TrainConfig:
    t = raw.get("train") or {}
    if "name" not in t:
        raise ConfigError("train.name is required")
    if "deploy_command" not in t:
        raise ConfigError("train.deploy_command is required")

    closures = [LineClosure.from_config(b)
                for b in pick(t, "line_closures", "blackouts", default=[]) or []]
    timetables = {
        name: Timetable.from_config(name, tt or {}, closures)
        for name, tt in (pick(raw, "timetables", "windows", default={}) or {}).items()
    }
    timetables.setdefault(ALWAYS_IN_SERVICE, Timetable.from_config(ALWAYS_IN_SERVICE, {}, closures))

    default_timetable = pick(t, "default_timetable", "default_window", default=ALWAYS_IN_SERVICE)
    default_dwell = pick(t, "default_dwell", "default_bake", default="0s")
    default_timeout = t.get("deploy_timeout", "1h")

    stops: list[Stop] = []
    seen: set[str] = set()
    for i, s in enumerate(pick(raw, "stops", "waves", default=[]) or []):
        sname = s.get("name", f"stop-{i + 1}")
        stop_timetable = pick(s, "timetable", "window", default=default_timetable)
        stop_dwell = pick(s, "dwell", "bake", default=default_dwell)
        platforms = []
        for p in pick(s, "platforms", "regions", default=[]) or []:
            if isinstance(p, str):
                p = {"name": p}
            pname = p["name"]
            if pname in seen:
                raise ConfigError(f"platform {pname!r} appears at more than one stop")
            seen.add(pname)
            tt = pick(p, "timetable", "window", default=stop_timetable)
            if tt not in timetables:
                raise ConfigError(f"platform {pname!r} references unknown timetable {tt!r}")
            platforms.append(Platform(
                name=pname,
                stop=sname,
                timetable=timetables[tt],
                dwell=parse_duration(pick(p, "dwell", "bake", default=stop_dwell)),
                deploy_timeout=parse_duration(
                    p.get("deploy_timeout", s.get("deploy_timeout", default_timeout))),
                vars=dict(p.get("vars") or {}),
            ))
        if not platforms:
            raise ConfigError(f"stop {sname!r} has no platforms")
        parallel = int(pick(s, "parallel_platforms", "max_parallel", default=1))
        if parallel < 1:
            raise ConfigError(f"stop {sname!r}: parallel_platforms must be >= 1")
        stops.append(Stop(
            name=sname,
            platforms=platforms,
            parallel_platforms=parallel,
            headway=parse_duration(pick(s, "headway", "gap", default=0)),
            signal=bool(pick(s, "signal", "approval", default=False)),
        ))
    if not stops:
        raise ConfigError("at least one stop is required")

    scope = t.get("rollback_scope", "platform")
    scope = LEGACY_ROLLBACK_SCOPES.get(scope, scope)
    if scope not in ROLLBACK_SCOPES:
        raise ConfigError(f"rollback_scope must be one of {ROLLBACK_SCOPES}")

    changes = None
    if t.get("changes"):
        c = t["changes"]
        if not isinstance(c, dict) or not (c.get("url") or c.get("command")):
            raise ConfigError("train.changes needs a `url` and/or a `command`")
        changes = Changes(url=c.get("url"), command=c.get("command"),
                          timeout=parse_duration(c.get("timeout", "60s")))
        if changes.url and not changes.url.startswith(("https://", "http://")):
            raise ConfigError("train.changes.url must start with https:// or http://")
        for field in ("url", "command"):
            template = getattr(changes, field)
            if template:
                try:
                    template.format(**CHANGES_PLACEHOLDERS)
                except (KeyError, IndexError, ValueError) as e:
                    raise ConfigError(
                        f"train.changes.{field}: unknown placeholder {e}; use {{version}}, "
                        "{previous_version} or {train} (and {{ }} for literal braces)") from None

    slack_raw = (raw.get("notifications") or {}).get("slack")
    slack = SlackSettings.from_config(slack_raw) if slack_raw is not None else None

    return TrainConfig(
        name=t["name"],
        deploy_command=t["deploy_command"],
        inspection_command=pick(t, "inspection_command", "health_check_command"),
        rollback_command=t.get("rollback_command"),
        inspection_interval=parse_duration(
            pick(t, "inspection_interval", "health_check_interval", default="1m")),
        max_hold=parse_duration(pick(t, "max_hold", "window_wait_timeout", default="7d")),
        rollback_scope=scope,
        estimated_deploy_duration=parse_duration(t.get("estimated_deploy_duration", "10m")),
        timetables=timetables,
        stops=stops,
        # TRAINTRAINTRAIN_STATE_DIR wins, so containers can keep state on a volume while the
        # config itself is mounted read-only.
        state_dir=Path(env("STATE_DIR") or base_dir / t.get("state_dir", _default_state_dir(base_dir)))
        .resolve(),
        slack=slack,
        database_url=env("DATABASE_URL"),
        changes=changes,
    )
