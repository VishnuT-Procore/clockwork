"""Load and validate the train YAML config."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import yaml

from .notify import SlackSettings
from .windows import Blackout, WindowProfile, parse_duration

ALWAYS_OPEN = "always"
ROLLBACK_SCOPES = ("none", "region", "wave", "train")


class ConfigError(ValueError):
    pass


@dataclass
class Region:
    name: str
    wave: str
    window: WindowProfile
    bake: timedelta
    deploy_timeout: timedelta
    vars: dict = field(default_factory=dict)


@dataclass
class Wave:
    name: str
    regions: list[Region]
    max_parallel: int = 1
    # Minimum wait after the previous wave finished before this one may start.
    gap: timedelta = timedelta(0)


@dataclass
class TrainConfig:
    name: str
    deploy_command: str
    health_check_command: str | None
    rollback_command: str | None
    health_check_interval: timedelta
    window_wait_timeout: timedelta
    rollback_scope: str
    estimated_deploy_duration: timedelta
    windows: dict[str, WindowProfile]
    waves: list[Wave]
    state_dir: Path
    slack: SlackSettings | None = None

    @property
    def regions(self) -> list[Region]:
        return [r for w in self.waves for r in w.regions]


def load(path: str | Path) -> TrainConfig:
    path = Path(path)
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    return from_dict(raw, base_dir=path.parent)


def from_dict(raw: dict, base_dir: Path = Path(".")) -> TrainConfig:
    t = raw.get("train") or {}
    if "name" not in t:
        raise ConfigError("train.name is required")
    if "deploy_command" not in t:
        raise ConfigError("train.deploy_command is required")

    global_blackouts = [Blackout.from_config(b) for b in t.get("blackouts", [])]
    windows = {
        name: WindowProfile.from_config(name, w or {}, global_blackouts)
        for name, w in (raw.get("windows") or {}).items()
    }
    windows.setdefault(ALWAYS_OPEN, WindowProfile.from_config(ALWAYS_OPEN, {}, global_blackouts))

    default_window = t.get("default_window", ALWAYS_OPEN)
    default_bake = t.get("default_bake", "0s")
    default_timeout = t.get("deploy_timeout", "1h")

    waves: list[Wave] = []
    seen: set[str] = set()
    for i, w in enumerate(raw.get("waves") or []):
        wname = w.get("name", f"wave-{i + 1}")
        regions = []
        for r in w.get("regions") or []:
            if isinstance(r, str):
                r = {"name": r}
            rname = r["name"]
            if rname in seen:
                raise ConfigError(f"region {rname!r} appears in more than one wave")
            seen.add(rname)
            win = r.get("window", w.get("window", default_window))
            if win not in windows:
                raise ConfigError(f"region {rname!r} references unknown window {win!r}")
            regions.append(Region(
                name=rname,
                wave=wname,
                window=windows[win],
                bake=parse_duration(r.get("bake", w.get("bake", default_bake))),
                deploy_timeout=parse_duration(r.get("deploy_timeout", w.get("deploy_timeout", default_timeout))),
                vars=dict(r.get("vars") or {}),
            ))
        if not regions:
            raise ConfigError(f"wave {wname!r} has no regions")
        max_parallel = int(w.get("max_parallel", 1))
        if max_parallel < 1:
            raise ConfigError(f"wave {wname!r}: max_parallel must be >= 1")
        waves.append(Wave(wname, regions, max_parallel, parse_duration(w.get("gap", 0))))
    if not waves:
        raise ConfigError("at least one wave is required")

    scope = t.get("rollback_scope", "region")
    if scope not in ROLLBACK_SCOPES:
        raise ConfigError(f"rollback_scope must be one of {ROLLBACK_SCOPES}")

    slack_raw = (raw.get("notifications") or {}).get("slack")
    try:
        slack = SlackSettings.from_config(slack_raw) if slack_raw is not None else None
    except ValueError as e:
        raise ConfigError(str(e))

    return TrainConfig(
        name=t["name"],
        deploy_command=t["deploy_command"],
        health_check_command=t.get("health_check_command"),
        rollback_command=t.get("rollback_command"),
        health_check_interval=parse_duration(t.get("health_check_interval", "1m")),
        window_wait_timeout=parse_duration(t.get("window_wait_timeout", "7d")),
        rollback_scope=scope,
        estimated_deploy_duration=parse_duration(t.get("estimated_deploy_duration", "10m")),
        windows=windows,
        waves=waves,
        state_dir=(base_dir / t.get("state_dir", ".deploytrain")).resolve(),
        slack=slack,
    )
