"""What's live on each platform, and what a new version would change there.

The live version of a platform comes from, in order:

1. the platform itself, if `live_version_command` is configured (the source of truth: it
   also sees hotfixes and rollbacks made outside traintraintrain),
2. what traintraintrain recorded the last time it deployed or rolled back that platform,
3. the departure history (for state written before live versions were recorded),
4. the previous version given when departing, for platforms never deployed before.

Each platform then compares against its own live version, so regions that are behind get
the longer change list, and roll back to what they were really running.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .config import Platform, TrainConfig
from .storage import Backend, live_record  # noqa: F401 - re-exported for the train

log = logging.getLogger("traintraintrain")

VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,99}$")

# Where a platform's live version came from.
SOURCE_PROBE, SOURCE_RECORDED, SOURCE_HISTORY, SOURCE_GIVEN = "probe", "recorded", "history", "given"
# Live-history statuses.
DEPLOYED, ROLLED_BACK_TO, FAILED_ATTEMPT, ROLLBACK_FAILED_ATTEMPT, ABORTED_ATTEMPT, OBSERVED, SET_BY_HAND = (
    "deployed", "rolled_back", "failed", "rollback_failed", "aborted", "observed", "set")

NOTHING_TO_COMPARE = "no version is known to be live here yet, so there is nothing to compare against"
PROBE_WORKERS = 8


def platform_vars(cfg: TrainConfig, platform: Platform, version: str, previous_version: str | None) -> dict[str, str]:
    """The placeholders available to deploy/inspection/rollback/live-version commands."""
    return {
        "train": cfg.name,
        "platform": platform.name,
        "stop": platform.stop,
        "version": version,
        "previous_version": previous_version or "",
        # Pre-rename names, so existing command templates keep working.
        "region": platform.name,
        "wave": platform.stop,
        **{k: str(v) for k, v in platform.vars.items()},
    }


def from_history(cfg: TrainConfig, backend: Backend) -> dict[str, dict]:
    """Each platform's last known version according to the departures stored so far."""
    from .train import ROLLED_BACK, SUCCEEDED, migrate_state
    names = {p.name for p in cfg.platforms}
    best: dict[str, dict] = {}
    for raw in backend.list(cfg.name):
        st = migrate_state(raw)
        for name, p in (st.get("platforms") or {}).items():
            at = p.get("finished_at")
            if name not in names or not at:
                continue
            if p.get("status") == SUCCEEDED:
                version = st["version"]
            elif p.get("status") == ROLLED_BACK:
                version = p.get("from_version") or st.get("previous_version")
            else:
                continue
            if version and (name not in best or at > best[name]["at"]):
                best[name] = {"platform": name, "version": version, "status": DEPLOYED if version == st["version"]
                              else ROLLED_BACK_TO, "departure": st["version"], "at": at, "by": None,
                              "previous_version": None}
    return best


def recorded(cfg: TrainConfig, backend: Backend) -> dict[str, dict]:
    """Live versions recorded in storage, falling back to the departure history per platform."""
    stored = {k: {**v, "source": SOURCE_RECORDED} for k, v in backend.live(cfg.name).items()}
    missing = [p.name for p in cfg.platforms if p.name not in stored]
    if missing:
        history = from_history(cfg, backend)
        for name in missing:
            if name in history:
                stored[name] = {**history[name], "source": SOURCE_HISTORY}
    return stored


def probe(cfg: TrainConfig, platform: Platform, runner) -> tuple[str | None, str | None]:
    """Ask the platform what it's running: (version, None) or (None, why not)."""
    if not cfg.live_version_command:
        return None, None
    variables = platform_vars(cfg, platform, "", "")
    try:
        command = cfg.live_version_command.format_map(variables)
        res = runner(command, {f"DT_{k.upper()}": v for k, v in variables.items()},
                     cfg.live_version_timeout.total_seconds())
    except Exception as e:  # noqa: BLE001 - a probe failing must not stop anything
        return None, f"live_version_command failed: {e}"
    if res.returncode != 0:
        return None, f"live_version_command exited {res.returncode}"
    lines = [ln.strip() for ln in res.output.splitlines() if ln.strip()]
    if not lines:
        return None, "live_version_command printed nothing"
    if not VERSION_RE.match(lines[-1]):
        return None, f"live_version_command printed {lines[-1][:60]!r}, which isn't a version"
    return lines[-1], None


def resolve(cfg: TrainConfig, backend: Backend, runner, fallback: str | None = None,
            use_probe: bool = True) -> dict[str, dict]:
    """For each platform: {version, source, record, probe_error, drift}."""
    known = recorded(cfg, backend)
    probes: dict[str, tuple[str | None, str | None]] = {}
    if use_probe and cfg.live_version_command:
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(cfg.platforms))) as pool:
            for p, result in zip(cfg.platforms, pool.map(lambda p: probe(cfg, p, runner), cfg.platforms)):
                probes[p.name] = result
    out = {}
    for p in cfg.platforms:
        rec = known.get(p.name)
        probed, probe_error = probes.get(p.name, (None, None))
        if probed:
            version, source = probed, SOURCE_PROBE
        elif rec:
            version, source = rec["version"], rec["source"]
        elif fallback:
            version, source = fallback, SOURCE_GIVEN
        else:
            version, source = None, None
        out[p.name] = {"platform": p.name, "stop": p.stop, "version": version, "source": source,
                       "record": rec, "probe_error": probe_error,
                       "drift": bool(probed and rec and rec["version"] != probed)}
    return out


def collect_changes(cfg: TrainConfig, version: str, bases: dict[str, str | None], runner,
                    collected_at: str | None) -> dict:
    """What `version` brings to each platform, grouped by the version each one runs now.

    The top-level fields describe the main group (the most platforms with something to
    compare), so older readers of `changes` keep working; `groups` has all of them.
    """
    ch = cfg.changes
    order: list[str | None] = []
    members: dict[str | None, list[str]] = {}
    for p in cfg.platforms:
        base = bases.get(p.name)
        if base not in members:
            order.append(base)
            members[base] = []
        members[base].append(p.name)
    groups = []
    for base in order:
        g: dict = {"previous_version": base, "platforms": members[base], "url": None, "command": None,
                   "output": None, "ok": None, "error": None, "up_to_date": base == version}
        if base is None:
            g["error"] = NOTHING_TO_COMPARE
        elif base != version and ch is not None:
            if ch.url:
                g["url"] = ch.render(ch.url, cfg.name, version, base)
            if ch.command:
                command = ch.render(ch.command, cfg.name, version, base)
                g["command"] = command
                env = {"DT_TRAIN": cfg.name, "DT_VERSION": version, "DT_PREVIOUS_VERSION": base,
                       "DT_PLATFORMS": ",".join(members[base])}
                try:
                    res = runner(command, env, ch.timeout.total_seconds())
                    g["output"] = res.output[-20000:]
                    g["ok"] = res.returncode == 0
                    if res.returncode != 0:
                        g["error"] = f"changes command exited {res.returncode}"
                except Exception as e:  # noqa: BLE001 - informational only
                    g["ok"], g["error"] = False, f"changes command failed: {e}"
        groups.append(g)
    comparable = [g for g in groups if g["previous_version"] and not g["up_to_date"]]
    main = max(comparable, key=lambda g: len(g["platforms"])) if comparable else groups[0]
    return {**{k: main[k] for k in ("previous_version", "url", "command", "output", "ok", "error")},
            "collected_at": collected_at, "groups": groups}


def change_count(group: dict) -> int:
    return len([ln for ln in (group.get("output") or "").splitlines() if ln.strip()])


def preview(cfg: TrainConfig, backend: Backend, version: str, runner, fallback: str | None = None,
            collected_at: str | None = None) -> dict:
    """What departing `version` now would do: each platform's live version and the changes."""
    resolved = resolve(cfg, backend, runner, fallback)
    changes = collect_changes(cfg, version, {k: v["version"] for k, v in resolved.items()}, runner, collected_at)
    return {"version": version, "platforms": list(resolved.values()), "changes": changes}
