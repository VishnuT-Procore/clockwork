"""Pipelines: the steps each platform goes through, from train.yaml's `steps:` lists.

    steps:
      - name: db-ready                      # a call: `use:` a connector (or `command:` / `run:`)
        use: payments-db
        query: "select ... where region = :platform"
        expect: ["pending_migrations == 0"]
      - deploy                              # built-in: deploy_command / the driver's deploy
      - wait: 2m                            # built-in: pause (the brake still works)
      - name: error-rate
        use: prometheus
        query: ...
        expect: ["error_rate < 1"]
        until: 10m                          # retry every `every` until the rules pass
      - dwell                               # built-in: dwell + inspection, as without steps

Without `steps:`, a platform runs [deploy, dwell], exactly as before.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from . import checks
from . import connectors as conn
from .timetable import format_duration, parse_duration

BUILTINS = ("deploy", "dwell")
COMMON_KEYS = {"name", "use", "expect", "until", "hold", "every", "timeout", "on_fail"}
ON_FAIL = ("halt", "warn")


@dataclass
class Step:
    kind: str                       # deploy | dwell | wait | call
    name: str
    connector: str | None = None    # for calls: the connector's name
    settings: dict = field(default_factory=dict)   # the connector-specific keys (query, request, ...)
    duration: timedelta = timedelta(0)             # for wait
    expect: list = field(default_factory=list)     # [checks.Rule]
    until: timedelta | None = None
    hold: timedelta | None = None
    every: timedelta = timedelta(seconds=30)
    timeout: timedelta = timedelta(minutes=10)
    on_fail: str = "halt"

    def summary(self) -> dict:
        """What the UI and state show about a step's definition."""
        out = {"name": self.name, "kind": self.kind}
        if self.connector:
            out["connector"] = self.connector
        if self.kind == "wait":
            out["wait"] = format_duration(self.duration)
        if self.expect:
            out["expect"] = [r.text for r in self.expect]
        if self.until:
            out["until"] = format_duration(self.until)
        if self.hold:
            out["hold"] = format_duration(self.hold)
        if self.on_fail != "halt":
            out["on_fail"] = self.on_fail
        return out


DEFAULT = (Step("deploy", "deploy"), Step("dwell", "dwell"))


class StepError(ValueError):
    pass


def parse_pipeline(raw, where: str, connectors: dict, base_dir: Path) -> list[Step]:
    if not isinstance(raw, list) or not raw:
        raise StepError(f"{where}: steps must be a non-empty list")
    steps = [parse_step(item, f"{where} step {i + 1}", connectors, base_dir) for i, item in enumerate(raw)]
    names = [s.name for s in steps]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise StepError(f"{where}: step names must be unique; repeated: {', '.join(dupes)}")
    if names.count("deploy") != 1:
        raise StepError(f"{where}: needs exactly one `deploy` step (the built-in `deploy`, "
                        "or a step named deploy that uses a connector)")
    return steps


def parse_step(item, where: str, connectors: dict, base_dir: Path) -> Step:
    if isinstance(item, str):
        if item in BUILTINS:
            return Step(item, item)
        raise StepError(f"{where}: {item!r} isn't a built-in step ({', '.join(BUILTINS)}); "
                        "other steps are mappings with a name and `use:`, `command:` or `run:`")
    if not isinstance(item, dict):
        raise StepError(f"{where}: a step is `deploy`, `dwell`, `wait: <duration>`, or a mapping")
    if "wait" in item:
        extra = set(item) - {"wait", "name"}
        if extra:
            raise StepError(f"{where}: a wait step only takes `wait` (and `name`), not {', '.join(sorted(extra))}")
        duration = _duration(item["wait"], where, "wait")
        return Step("wait", str(item.get("name") or f"wait {format_duration(duration)}"), duration=duration)
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise StepError(f"{where}: every step needs a `name`")
    where = f"{where} ({name})"
    if name in BUILTINS and not ({"use", "command", "run"} & set(item)):
        if set(item) - {"name"}:
            raise StepError(f"{where}: the built-in {name} step takes no settings; to replace it, "
                            "add `use:` (or `command:` / `run:`)")
        return Step(name, name)
    connector = _connector_for(item, where, connectors, base_dir)
    settings = {k: v for k, v in item.items() if k not in COMMON_KEYS}
    unknown = sorted(set(settings) - set(connector.step_keys))
    if unknown:
        raise StepError(f"{where}: {connector.type_name} steps don't take {', '.join(unknown)} "
                        f"(they take: {', '.join(connector.step_keys) or 'nothing extra'})")
    try:
        connector.validate_step(settings)
        rules = checks.parse_rules(item.get("expect"))
    except (conn.ConnectorError, ValueError) as e:
        raise StepError(f"{where}: {e}") from None
    if item.get("until") and item.get("hold"):
        raise StepError(f"{where}: use `until` (wait for the rules to pass) or `hold` (keep passing), not both")
    if (item.get("until") or item.get("hold")) and not rules:
        raise StepError(f"{where}: `until`/`hold` need `expect` rules to check")
    on_fail = item.get("on_fail", "halt")
    if on_fail not in ON_FAIL:
        raise StepError(f"{where}: on_fail must be halt or warn")
    return Step("call", name, connector=connector.name, settings=settings, expect=rules,
                until=_duration(item["until"], where, "until") if item.get("until") else None,
                hold=_duration(item["hold"], where, "hold") if item.get("hold") else None,
                every=_duration(item.get("every", "30s"), where, "every"),
                timeout=_duration(item.get("timeout", "10m"), where, "timeout"), on_fail=on_fail)


def _connector_for(item: dict, where: str, connectors: dict, base_dir: Path):
    if "use" in item:
        name = item["use"]
        if name not in connectors:
            known = ", ".join(sorted(connectors)) or "none are declared"
            raise StepError(f"{where}: no connector named {name!r} (connectors: {known})")
        return connectors[name]
    if "command" in item:
        return connectors.setdefault("command", conn.build("command", {"type": "command"}, base_dir))
    if "run" in item:
        if "driver" not in connectors:
            raise StepError(f"{where}: `run:` needs a train.driver, or `use:` a connector of type driver")
        return connectors["driver"]
    raise StepError(f"{where}: a step needs `use: <connector>`, `command:` or `run:`")


def _duration(value, where: str, key: str) -> timedelta:
    try:
        d = parse_duration(value)
    except ValueError as e:
        raise StepError(f"{where}: {key}: {e}") from None
    if d <= timedelta(0):
        raise StepError(f"{where}: {key} must be more than 0")
    return d
