"""The deployment train: stops in sequence, platforms served within their timetables."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from . import driver, live
from ._compat import LEGACY_STATUS
from .config import Platform, Stop, TrainConfig
from .notify import Notifier, build_notifier
from .storage import BRAKE, SIGNAL, Backend, backend_for
from .timetable import NoServiceError, format_duration

log = logging.getLogger("traintraintrain")

# Platform statuses.
SCHEDULED, HELD, DEPLOYING, DWELLING = "scheduled", "held", "deploying", "dwelling"
SUCCEEDED, FAILED, ROLLED_BACK, ROLLBACK_FAILED, ABORTED = (
    "succeeded", "failed", "rolled_back", "rollback_failed", "aborted",
)
# Train statuses (also SUCCEEDED, FAILED, ABORTED).
EN_ROUTE, AT_SIGNAL = "en_route", "at_signal"
ACTIVE_STATUSES = (EN_ROUTE, AT_SIGNAL)
# Signal states at a stop.
SIGNAL_WAITING, SIGNAL_CLEARED = "waiting", "cleared"

SIGNAL_POLL_SECONDS = 5.0
MAX_SLEEP_CHUNK = 5.0  # seconds; bounds how quickly the brake or a halt is noticed
INSPECTION_TIMEOUT = timedelta(minutes=5)
# The process driving a train refreshes `heartbeat_at` in its state file this often. A
# heartbeat older than HEARTBEAT_STALE means that process is gone (crashed, killed, or its
# container was replaced), even if the recorded host/PID can't be checked or were reused.
HEARTBEAT_INTERVAL = 15.0
HEARTBEAT_STALE = timedelta(seconds=90)


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class CommandResult:
    returncode: int
    output: str
    message: str = ""  # a driver's own explanation, shown instead of "exit N"


class Runner(Protocol):
    def __call__(self, command: str, env: dict[str, str], timeout: float) -> CommandResult: ...


def shell_runner(command: str, env: dict[str, str], timeout: float) -> CommandResult:
    try:
        proc = subprocess.run(
            command, shell=True, env={**os.environ, **env}, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return CommandResult(proc.returncode, proc.stdout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return CommandResult(124, out + f"\n[timed out after {timeout:.0f}s]")


class BrakeApplied(Exception):
    """The emergency brake was pulled, or the train halted because a platform failed."""


class PlatformFailed(Exception):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def migrate_state(data: dict) -> dict:
    """Upgrade a state file written before the railway rename, in place."""
    if "regions" in data and "platforms" not in data:
        data["platforms"] = data.pop("regions")
        for p in data["platforms"].values():
            p["stop"] = p.pop("wave", None)
    if "waves" in data and "stops" not in data:
        data["stops"] = data.pop("waves")
        for s in data["stops"].values():
            approval = s.pop("approval", None)
            s["signal"] = None if not approval else {
                "status": SIGNAL_CLEARED if approval.get("status") == "approved" else SIGNAL_WAITING,
                "requested_at": approval.get("requested_at"),
                "cleared_by": approval.get("by"),
                "cleared_at": approval.get("approved_at"),
            }
    for e in data.get("events", []):
        if "region" in e:
            e["platform"] = e.pop("region")
    if "status" in data:
        data["status"] = LEGACY_STATUS.get(data["status"], data["status"])
    for p in data.get("platforms", {}).values():
        p["status"] = LEGACY_STATUS.get(p["status"], p["status"])
    return data


def read_state(path: Path) -> dict | None:
    """Read a state file (migrating old formats); None if missing or mid-write."""
    if not path.exists():
        return None
    try:
        return migrate_state(json.loads(path.read_text()))
    except ValueError:
        return None


class StateStore:
    """The state document of one (train, version); lets an interrupted train resume."""

    def __init__(self, backend: Backend, train: str, version: str):
        self.backend, self.train, self.version = backend, train, version
        self.lock = threading.RLock()
        self.data: dict = {}

    def load(self) -> bool:
        data = self.backend.load(self.train, self.version)
        if data is None:
            return False
        self.data = migrate_state(data)
        return True

    def save(self) -> None:
        with self.lock:
            self.backend.save(self.train, self.version, self.data)

    def platform(self, name: str) -> dict:
        return self.data["platforms"][name]

    def update_platform(self, name: str, **fields) -> None:
        with self.lock:
            self.data["platforms"][name].update(fields)
            self.save()

    def event(self, now: datetime, message: str, platform: str | None = None) -> None:
        with self.lock:
            self.data.setdefault("events", []).append(
                {"at": _iso(now), "platform": platform, "message": message})
            self.save()
        log.info("%s%s", f"[{platform}] " if platform else "", message)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def state_path(cfg: TrainConfig, version: str) -> Path:
    return cfg.state_dir / f"{cfg.name}-{_safe(version)}.json"


def brake_path(cfg: TrainConfig, version: str) -> Path:
    return state_path(cfg, version).with_suffix(".brake")


def signal_path(cfg: TrainConfig, version: str, stop: str) -> Path:
    return state_path(cfg, version).with_suffix(f".signal.{_safe(stop)}")


def apply_brake(cfg: TrainConfig, version: str, by: str) -> None:
    backend_for(cfg).request(cfg.name, version, BRAKE, "", by)


def clear_signal(cfg: TrainConfig, version: str, stop: str, by: str) -> None:
    backend_for(cfg).request(cfg.name, version, SIGNAL, stop, by)


def load_departure(cfg: TrainConfig, version: str) -> dict | None:
    """A departure's state (migrated to the current format), from whichever backend is in use."""
    data = backend_for(cfg).load(cfg.name, version)
    return migrate_state(data) if data is not None else None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_alive(data: dict, now: datetime | None = None) -> bool:
    """Whether the process that owns this state file is still running."""
    if data.get("status") not in ACTIVE_STATUSES or not data.get("pid"):
        return False
    same_host = not data.get("host") or data["host"] == socket.gethostname()
    beat = data.get("heartbeat_at")
    if beat:
        now = now or datetime.now(timezone.utc)
        if now - datetime.fromisoformat(beat) > HEARTBEAT_STALE:
            return False
        return pid_alive(data["pid"]) if same_host else True
    # State written before heartbeats existed: can't check another host, so assume alive.
    return pid_alive(data["pid"]) if same_host else True


class Train:
    def __init__(self, cfg: TrainConfig, version: str, previous_version: str | None = None,
                 runner: Runner = shell_runner, clock: Clock | None = None,
                 dry_run: bool = False, notifier: Notifier | None = None,
                 backend: Backend | None = None):
        self.cfg = cfg
        self.version = version
        self.previous_version = previous_version
        self.sha: str | None = None  # the commit `version` points to, when SHAs are tracked
        self.runner = runner
        self.clock = clock or Clock()
        self.dry_run = dry_run
        self.backend = backend or backend_for(cfg)
        self.state = StateStore(self.backend, cfg.name, version)
        self._halt = threading.Event()
        if notifier is None:
            notifier = Notifier() if dry_run else build_notifier(cfg.slack)
        self.notifier = notifier
        self.notifier.attach(self)

    def _notify(self, event: str, **ctx) -> None:
        # Notifications must never break a deploy.
        try:
            self.notifier.notify(event, **ctx)
        except Exception as e:  # noqa: BLE001
            log.warning("notification %s failed: %s", event, e)

    # ---- state -------------------------------------------------------------

    def _init_state(self, retry_failed: bool, force: bool) -> None:
        now = self.clock.now()
        if self.state.load():
            st = self.state.data
            if st["status"] == SUCCEEDED:
                return
            # Postgres ownership is the advisory lock taken in run(); files use PID + heartbeat.
            if (self.backend.kind == "files" and not force
                    and st.get("pid") != os.getpid() and owner_alive(st)):
                raise RuntimeError(
                    f"train {self.version} is already en route (pid {st['pid']} on {st.get('host')}); "
                    "use --force only if that process is really gone")
            failed = [n for n, p in st["platforms"].items()
                      if p["status"] in (FAILED, ROLLED_BACK, ROLLBACK_FAILED)]
            if failed and not retry_failed:
                raise RuntimeError(
                    f"previous departure failed at {failed}; fix the issue and re-run with --retry-failed")
            for p in st["platforms"].values():
                if p["status"] != SUCCEEDED:
                    p["status"] = SCHEDULED
                    p.pop("output", None)
            if self.previous_version and not st.get("previous_version"):
                st["previous_version"] = self.previous_version
            self.previous_version = st.get("previous_version")
            self.sha = st.get("sha")
            st["status"] = EN_ROUTE
            st.update(pid=os.getpid(), host=socket.gethostname(),
                      heartbeat_at=_iso(datetime.now(timezone.utc)))
            self.state.event(now, "train resumed its journey")
            self._notify("train_resumed")
            return
        shas = live.ShaResolver(self.cfg, self.runner)
        self.sha, sha_error = shas(self.version)
        resolved = live.resolve(self.cfg, self.backend, self.runner, fallback=self.previous_version, shas=shas)
        self.state.data = {
            "train": self.cfg.name,
            "version": self.version,
            "sha": self.sha,
            "previous_version": self.previous_version,
            "status": EN_ROUTE,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "heartbeat_at": _iso(datetime.now(timezone.utc)),
            "started_at": _iso(now),
            "finished_at": None,
            "stops": {s.name: {"finished_at": None, "signal": None} for s in self.cfg.stops},
            "platforms": {p.name: {"stop": p.stop, "status": SCHEDULED, "started_at": None,
                                   "finished_at": None, "message": "",
                                   "from_version": resolved[p.name]["version"],
                                   "from_sha": resolved[p.name]["sha"],
                                   "from_source": resolved[p.name]["source"]}
                          for p in self.cfg.platforms},
            "events": [],
        }
        self._collect_changes()
        if not self.previous_version:
            # Nothing given: the version most platforms run now is the departure's rollback target.
            self.previous_version = self.state.data["previous_version"] = (
                (self.state.data.get("changes") or {}).get("previous_version") or self._most_common_base())
        self.state.event(now, f"train {self.cfg.name} departed with version {self.version}")
        if self.sha and self.sha != self.version.lower():
            self.state.event(now, f"{self.version} is commit {self.sha}")
        elif sha_error:
            self.state.event(now, f"couldn't look up the commit of {self.version}: {sha_error}")
        for name, r in resolved.items():
            if r["drift"]:
                self.state.event(now, f"live version is {r['version']}, not {r['record']['version']} as "
                                 "recorded (changed outside traintraintrain?)", name)
                self._record_live(name, r["version"], live.OBSERVED, r["record"]["version"],
                                  sha=r["sha"], previous_sha=r["record"].get("sha"))
            elif r["probe_error"]:
                self.state.event(now, f"couldn't read the live version: {r['probe_error']}", name)
        self._notify("train_started")

    def _most_common_base(self) -> str | None:
        bases = [p.get("from_version") for p in self.state.data["platforms"].values() if p.get("from_version")]
        return max(bases, key=bases.count) if bases else None

    def _collect_changes(self) -> None:
        """Record what's in this release for each platform, compared with what it runs now.

        Never fails a train.
        """
        if self.cfg.changes is None and not self.cfg.handles("changes"):
            return
        platforms = self.state.data["platforms"]
        info = live.collect_changes(self.cfg, self.version, {n: p.get("from_version") for n, p in platforms.items()},
                                    self.runner, _iso(self.clock.now()), sha=self.sha,
                                    base_shas={n: p.get("from_sha") for n, p in platforms.items()})
        self.state.data["changes"] = info
        for g in info["groups"]:
            where = ", ".join(g["platforms"])
            if g["up_to_date"]:
                log.info("changes for %s: %s already run it", where, self.version)
            elif g["error"]:
                log.warning("changes for %s: %s", where, g["error"])
            else:
                log.info("changes for %s since %s: %s%s", where, g["previous_version"],
                         f"{live.change_count(g)} line(s) from the changes command" if g["command"] else "",
                         f" · {g['url']}" if g["url"] else "")

    def _record_live(self, platform: str, version: str | None, status: str,
                     previous_version: str | None, current: bool = True,
                     sha: str | None = None, previous_sha: str | None = None) -> None:
        """Store what a platform runs now. A storage hiccup must not fail a deploy that worked."""
        if self.dry_run:
            return
        try:
            self.backend.set_live(self.cfg.name, live.live_record(
                platform, version, status, previous_version, departure=self.version,
                by=os.environ.get("TRAINTRAINTRAIN_TRIGGERED_BY"), sha=sha, previous_sha=previous_sha),
                current=current)
        except Exception as e:  # noqa: BLE001
            log.error("[%s] couldn't record the live version %s: %s", platform, version, e)

    def _finish(self, status: str, message: str) -> str:
        self.state.data["status"] = status
        self.state.data["finished_at"] = _iso(self.clock.now())
        self.state.data["pid"] = None
        self.state.event(self.clock.now(), message)
        self._notify("train_finished", status=status, message=message)
        return status

    # ---- helpers -----------------------------------------------------------

    def _check_brake(self) -> None:
        if self.backend.get_request(self.cfg.name, self.version, BRAKE) is not None:
            raise BrakeApplied()

    def _sleep(self, seconds: float, stop_on_halt: bool = False) -> None:
        while seconds > 0:
            self._check_brake()
            if stop_on_halt and self._halt.is_set():
                raise BrakeApplied()
            chunk = min(seconds, MAX_SLEEP_CHUNK)
            self.clock.sleep(chunk)
            seconds -= chunk
        self._check_brake()

    def _rollback_target(self, platform: str) -> str | None:
        # What this platform ran before the departure; older state only has the train-wide one.
        p = self.state.data.get("platforms", {}).get(platform) or {}
        return p.get("from_version") or self.previous_version

    def _rollback_sha(self, platform: str) -> str | None:
        p = self.state.data.get("platforms", {}).get(platform) or {}
        return p.get("from_sha")

    def _vars(self, platform: Platform) -> dict[str, str]:
        return live.platform_vars(self.cfg, platform, self.version, self._rollback_target(platform.name),
                                  self.sha, self._rollback_sha(platform.name))

    # The driver action for each kind of step.
    _ACTIONS = {"deploy": "deploy", "inspection": "inspect", "rollback": "rollback"}

    def _has(self, kind: str) -> bool:
        template = {"deploy": self.cfg.deploy_command, "inspection": self.cfg.inspection_command,
                    "rollback": self.cfg.rollback_command}[kind]
        return bool(template) or self.cfg.handles(self._ACTIONS[kind])

    def _failure(self, what: str, res: CommandResult) -> str:
        return f"{what}: {res.message}" if res.message else f"{what} (exit {res.returncode})"

    def _run(self, kind: str, template: str | None, platform: Platform, timeout: timedelta) -> CommandResult:
        action = self._ACTIONS[kind]
        if not template and self.cfg.handles(action):
            return self._run_driver(kind, action, platform, timeout)
        variables = self._vars(platform)
        command = template.format_map(variables)
        env = {f"DT_{k.upper()}": v for k, v in variables.items()}
        if self.dry_run:
            self.state.event(self.clock.now(), f"[dry-run] {kind}: {command}", platform.name)
            return CommandResult(0, "")
        self.state.event(self.clock.now(), f"{kind}: {command}", platform.name)
        result = self.runner(command, env, timeout.total_seconds())
        if result.output.strip():
            for line in result.output.strip().splitlines()[-20:]:
                log.info("[%s]   | %s", platform.name, line)
        return result

    def _run_driver(self, kind: str, action: str, platform: Platform, timeout: timedelta) -> CommandResult:
        request = driver.build_request(
            action, self.cfg, departure=self.version, triggered_by=os.environ.get("TRAINTRAINTRAIN_TRIGGERED_BY"),
            dry_run=self.dry_run, platform=platform.name, stop=platform.stop, vars=dict(platform.vars),
            version=self.version, sha=self.sha, previous_version=self._rollback_target(platform.name),
            previous_sha=self._rollback_sha(platform.name))
        what = f"driver {action} {platform.name} {self.version}"
        if self.dry_run:
            self.state.event(self.clock.now(), f"[dry-run] {kind}: {what}", platform.name)
            return CommandResult(0, "")
        self.state.event(self.clock.now(), f"{kind}: {what}", platform.name)
        reply = driver.call(self.cfg, request, timeout.total_seconds())
        output = "\n".join(x for x in (reply.message, reply.log) if x)
        for line in output.splitlines()[-20:]:
            log.info("[%s]   | %s", platform.name, line)
        if reply.unsupported:
            return CommandResult(1, output, f"the driver doesn't support {action}")
        return CommandResult(0 if reply.ok else 1, output, "" if reply.ok else reply.message)

    # ---- per-platform lifecycle --------------------------------------------

    def _wait_for_service(self, platform: Platform) -> None:
        started = self.clock.now()
        announced = None
        tt = platform.timetable
        while True:
            self._check_brake()
            if self._halt.is_set():
                raise BrakeApplied()
            now = self.clock.now()
            try:
                nxt = tt.next_start(now)
            except NoServiceError as e:
                raise PlatformFailed(str(e))
            if nxt <= now:
                return
            if nxt - started > self.cfg.max_hold:
                raise PlatformFailed(
                    f"next service starts at {_iso(nxt)}, beyond max_hold "
                    f"({format_duration(self.cfg.max_hold)})")
            if announced != nxt:
                announced = nxt
                local = nxt.astimezone(ZoneInfo(tt.timezone))
                closure = tt.active_closure(now)
                why = f" (line closure: {closure.reason})" if closure and closure.reason else ""
                msg = (f"held at signal: timetable {tt.name!r}{why} resumes service "
                       f"{local:%a %Y-%m-%d %H:%M %Z} (in {format_duration(nxt - now)})")
                self.state.update_platform(platform.name, status=HELD, message=msg)
                self.state.event(now, msg, platform.name)
                self._notify("platform_held", platform=platform.name, message=msg, opens_at=nxt)
            self._sleep(min((nxt - now).total_seconds(), 300), stop_on_halt=True)

    def _dwell(self, platform: Platform) -> None:
        cfg = self.cfg
        deadline = self.clock.now() + platform.dwell
        self.state.update_platform(platform.name, status=DWELLING,
                                   message=f"dwelling for {format_duration(platform.dwell)}")
        if platform.dwell:
            self.state.event(self.clock.now(), f"dwelling for {format_duration(platform.dwell)}",
                             platform.name)
        inspects = self._has("inspection")
        while True:
            if inspects:
                res = self._run("inspection", cfg.inspection_command, platform, INSPECTION_TIMEOUT)
                if res.returncode != 0:
                    raise PlatformFailed(self._failure("inspection failed", res), res.output)
            remaining = (deadline - self.clock.now()).total_seconds()
            if remaining <= 0 or self.dry_run:
                return
            step = remaining if not inspects else min(
                remaining, cfg.inspection_interval.total_seconds())
            self._sleep(step)

    def _rollback(self, platform: Platform, reason: str) -> None:
        if not self._has("rollback"):
            self.state.event(self.clock.now(), "no rollback_command configured; skipping rollback",
                             platform.name)
            return
        res = self._run("rollback", self.cfg.rollback_command, platform, platform.deploy_timeout)
        status = ROLLED_BACK if res.returncode == 0 else ROLLBACK_FAILED
        target = self._rollback_target(platform.name)
        target_sha = self._rollback_sha(platform.name)
        if res.returncode == 0:
            self._record_live(platform.name, target, live.ROLLED_BACK_TO, self.version, current=bool(target),
                              sha=target_sha, previous_sha=self.sha)
        else:
            self._record_live(platform.name, self.version, live.ROLLBACK_FAILED_ATTEMPT, target, current=False,
                              sha=self.sha, previous_sha=target_sha)
        self.state.update_platform(
            platform.name, status=status,
            message=f"{reason}; rollback {'ok' if res.returncode == 0 else 'FAILED'}"
                    + (f" ({res.message})" if res.returncode and res.message else ""))
        self._notify("platform_rolled_back", platform=platform.name, ok=res.returncode == 0)

    def _serve_platform(self, platform: Platform) -> str:
        name = platform.name
        try:
            self._wait_for_service(platform)
            self.state.update_platform(name, status=DEPLOYING, started_at=_iso(self.clock.now()),
                                       message=f"deploying {self.version}")
            self._notify("platform_deploying", platform=name)
            res = self._run("deploy", self.cfg.deploy_command, platform, platform.deploy_timeout)
            if res.returncode != 0:
                raise PlatformFailed(self._failure("deploy failed", res), res.output)
            self._check_brake()
            self._dwell(platform)
            self.state.update_platform(name, status=SUCCEEDED, finished_at=_iso(self.clock.now()),
                                       message=f"{self.version} healthy")
            self._record_live(name, self.version, live.DEPLOYED, self._rollback_target(name),
                              sha=self.sha, previous_sha=self._rollback_sha(name))
            self.state.event(self.clock.now(), "succeeded", name)
            self._notify("platform_succeeded", platform=name)
            return SUCCEEDED
        except BrakeApplied:
            st = self.state.platform(name)["status"]
            if st in (DEPLOYING, DWELLING):
                self.state.update_platform(name, status=ABORTED, message="aborted mid-deploy")
                self._record_live(name, self.version, live.ABORTED_ATTEMPT, self._rollback_target(name),
                                  current=False, sha=self.sha, previous_sha=self._rollback_sha(name))
            else:
                self.state.update_platform(name, status=SCHEDULED,
                                           message="not served (train halted)")
            return ABORTED
        except PlatformFailed as e:
            self._halt.set()
            self.state.update_platform(name, status=FAILED, finished_at=_iso(self.clock.now()),
                                       message=str(e), output=e.output.strip()[-2000:])
            self.state.event(self.clock.now(), f"FAILED: {e}", name)
            self._record_live(name, self.version, live.FAILED_ATTEMPT, self._rollback_target(name),
                              current=False, sha=self.sha, previous_sha=self._rollback_sha(name))
            self._notify("platform_failed", platform=name, error=str(e), output=e.output.strip())
            if self.cfg.rollback_scope != "none":
                self._rollback(platform, str(e))
            return FAILED

    # ---- train -------------------------------------------------------------

    def _serve_stop(self, stop: Stop) -> list[str]:
        todo = [p for p in stop.platforms if self.state.platform(p.name)["status"] != SUCCEEDED]
        if not todo:
            return []
        self.state.event(self.clock.now(), f"arriving at stop {stop.name!r}: "
                         f"{', '.join(p.name for p in todo)} "
                         f"(parallel_platforms={stop.parallel_platforms})")
        self._notify("stop_started", stop=stop.name, platforms=[p.name for p in todo])
        if stop.parallel_platforms == 1:
            return [self._serve_platform(p) for p in todo]
        with ThreadPoolExecutor(max_workers=stop.parallel_platforms,
                                thread_name_prefix=f"stop-{stop.name}") as pool:
            return list(pool.map(self._serve_platform, todo))

    def _wait_for_signal(self, stop: Stop, todo: list[Platform]) -> None:
        info = self.state.data["stops"].setdefault(stop.name, {"finished_at": None})
        if (info.get("signal") or {}).get("status") == SIGNAL_CLEARED:
            return
        info["signal"] = {"status": SIGNAL_WAITING, "requested_at": _iso(self.clock.now())}
        self.state.data["status"] = AT_SIGNAL
        self.state.event(self.clock.now(),
                         f"signal at stop {stop.name!r} is red; waiting for someone to clear it")
        self._notify("stop_at_signal", stop=stop.name, platforms=[p.name for p in todo])
        while (req := self.backend.get_request(self.cfg.name, self.version, SIGNAL, stop.name)) is None:
            self._sleep(SIGNAL_POLL_SECONDS)
        by = req.get("by") or "unknown"
        self.backend.delete_request(self.cfg.name, self.version, SIGNAL, stop.name)
        info["signal"] = {**info["signal"], "status": SIGNAL_CLEARED, "cleared_by": by,
                          "cleared_at": _iso(self.clock.now())}
        self.state.data["status"] = EN_ROUTE
        self.state.event(self.clock.now(), f"signal at stop {stop.name!r} cleared by {by}")
        self._notify("signal_cleared", stop=stop.name, by=by)

    def _rollback_succeeded(self, platforms: list[Platform], reason: str) -> None:
        for p in reversed(platforms):
            if self.state.platform(p.name)["status"] == SUCCEEDED:
                self._rollback(p, reason)

    def _start_heartbeat(self) -> threading.Event:
        """Refresh `heartbeat_at` in the background, even during long deploy commands."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_INTERVAL):
                try:
                    with self.state.lock:
                        self.state.data["heartbeat_at"] = _iso(datetime.now(timezone.utc))
                        self.state.save()
                except Exception as e:  # noqa: BLE001 - retried on the next beat
                    log.debug("heartbeat failed: %s", e)

        threading.Thread(target=beat, name="heartbeat", daemon=True).start()
        return stop

    def run(self, retry_failed: bool = False, force: bool = False) -> str:
        if not self.backend.acquire(self.cfg.name, self.version):
            raise RuntimeError(f"train {self.version} is already en route (another process is driving it)")
        try:
            self.backend.delete_request(self.cfg.name, self.version, BRAKE)
            self._init_state(retry_failed, force)
            if self.state.data["status"] == SUCCEEDED:
                log.info("train already arrived for %s", self.version)
                return SUCCEEDED
            heartbeat = self._start_heartbeat()
            try:
                return self._journey()
            finally:
                heartbeat.set()
        finally:
            self.backend.release(self.cfg.name, self.version)

    def _journey(self) -> str:
        served: list[Platform] = []
        stops_state = self.state.data["stops"]
        try:
            for i, stop in enumerate(self.cfg.stops):
                if i > 0 and stop.headway:
                    prev = stops_state[self.cfg.stops[i - 1].name]["finished_at"]
                    if prev:
                        ready = datetime.fromisoformat(prev) + stop.headway
                        wait = (ready - self.clock.now()).total_seconds()
                        if wait > 0:
                            self.state.event(self.clock.now(), f"headway before stop {stop.name!r}: "
                                             f"{format_duration(stop.headway)}")
                            self._sleep(wait)
                todo = [p for p in stop.platforms
                        if self.state.platform(p.name)["status"] != SUCCEEDED]
                if stop.signal and todo:
                    self._wait_for_signal(stop, todo)
                results = self._serve_stop(stop)
                if FAILED in results:
                    reason = f"failure at stop {stop.name!r}"
                    if self.cfg.rollback_scope == "stop":
                        self._rollback_succeeded(stop.platforms, reason)
                    elif self.cfg.rollback_scope == "train":
                        self._rollback_succeeded(served + stop.platforms, reason)
                    return self._finish(FAILED, f"train halted: {reason}")
                if ABORTED in results:
                    raise BrakeApplied()
                served.extend(stop.platforms)
                if results or not stops_state[stop.name]["finished_at"]:
                    stops_state[stop.name]["finished_at"] = _iso(self.clock.now())
                    self.state.event(self.clock.now(), f"departed stop {stop.name!r}")
                    self._notify("stop_completed", stop=stop.name)
        except BrakeApplied:
            return self._finish(ABORTED, "emergency brake applied; re-run the same command to resume")
        return self._finish(SUCCEEDED, f"train arrived: {self.version} is live on all platforms")


# ---- timetable projection ---------------------------------------------------

@dataclass
class PlannedPlatform:
    platform: Platform
    earliest: datetime
    start: datetime
    end: datetime


def project(cfg: TrainConfig, start_at: datetime,
            deploy_duration: timedelta | None = None) -> list[tuple[Stop, list[PlannedPlatform]]]:
    """Project when each platform would be served if every step succeeds on time."""
    duration = deploy_duration if deploy_duration is not None else cfg.estimated_deploy_duration
    out = []
    t = start_at
    for i, stop in enumerate(cfg.stops):
        if i > 0:
            t = t + stop.headway
        slots = [t] * stop.parallel_platforms
        planned = []
        for platform in stop.platforms:
            slot = min(range(len(slots)), key=lambda k: slots[k])
            earliest = slots[slot]
            start = platform.timetable.next_start(earliest)
            end = start + duration + platform.dwell
            slots[slot] = end
            planned.append(PlannedPlatform(platform, earliest, start, end))
        t = max(p.end for p in planned)
        out.append((stop, planned))
    return out
