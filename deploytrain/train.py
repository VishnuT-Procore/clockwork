"""The deployment train orchestrator: waves in sequence, regions gated by their windows."""
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
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from .config import Region, TrainConfig, Wave
from .notify import Notifier, build_notifier
from .windows import NoWindowError, format_duration

log = logging.getLogger("deploytrain")

PENDING, WAITING, DEPLOYING, BAKING = "pending", "waiting", "deploying", "baking"
SUCCEEDED, FAILED, ROLLED_BACK, ROLLBACK_FAILED, ABORTED = (
    "succeeded", "failed", "rolled_back", "rollback_failed", "aborted",
)
RUNNING, AWAITING_APPROVAL = "running", "awaiting_approval"
ACTIVE_STATUSES = (RUNNING, AWAITING_APPROVAL)
APPROVAL_POLL_SECONDS = 5.0
MAX_SLEEP_CHUNK = 5.0  # seconds; bounds how quickly an abort or halt is noticed
HEALTH_CHECK_TIMEOUT = timedelta(minutes=5)


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class CommandResult:
    returncode: int
    output: str


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


class AbortRequested(Exception):
    pass


class RegionFailed(Exception):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


class StateStore:
    """JSON state file per (train, version); lets an interrupted train resume."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict = {}

    def load(self) -> bool:
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            return True
        return False

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)

    def region(self, name: str) -> dict:
        return self.data["regions"][name]

    def update_region(self, name: str, **fields) -> None:
        with self.lock:
            self.data["regions"][name].update(fields)
            self.save()

    def event(self, now: datetime, message: str, region: str | None = None) -> None:
        with self.lock:
            self.data.setdefault("events", []).append(
                {"at": _iso(now), "region": region, "message": message})
            self.save()
        log.info("%s%s", f"[{region}] " if region else "", message)


def state_path(cfg: TrainConfig, version: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in version)
    return cfg.state_dir / f"{cfg.name}-{safe}.json"


def abort_path(cfg: TrainConfig, version: str) -> Path:
    return state_path(cfg, version).with_suffix(".abort")


def approval_path(cfg: TrainConfig, version: str, wave: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in wave)
    return state_path(cfg, version).with_suffix(f".approve.{safe}")


def request_approval(cfg: TrainConfig, version: str, wave: str, by: str) -> Path:
    path = approval_path(cfg, version, wave)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"by": by, "at": _iso(datetime.now(timezone.utc))}))
    return path


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


def owner_alive(data: dict) -> bool:
    """Whether the process that owns this state file is still running."""
    if data.get("status") not in ACTIVE_STATUSES or not data.get("pid"):
        return False
    if data.get("host") and data["host"] != socket.gethostname():
        return True  # can't check another host; assume it's alive
    return pid_alive(data["pid"])


class Train:
    def __init__(self, cfg: TrainConfig, version: str, previous_version: str | None = None,
                 runner: Runner = shell_runner, clock: Clock | None = None,
                 dry_run: bool = False, notifier: Notifier | None = None):
        self.cfg = cfg
        self.version = version
        self.previous_version = previous_version
        self.runner = runner
        self.clock = clock or Clock()
        self.dry_run = dry_run
        self.state = StateStore(state_path(cfg, version))
        self.abort_file = abort_path(cfg, version)
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
            if not force and st.get("pid") != os.getpid() and owner_alive(st):
                raise RuntimeError(
                    f"train {self.version} is already running (pid {st['pid']} on {st.get('host')}); "
                    "use --force only if that process is really gone")
            failed = [n for n, r in st["regions"].items()
                      if r["status"] in (FAILED, ROLLED_BACK, ROLLBACK_FAILED)]
            if failed and not retry_failed:
                raise RuntimeError(
                    f"previous run failed in {failed}; fix the issue and re-run with --retry-failed")
            for r in st["regions"].values():
                if r["status"] != SUCCEEDED:
                    r["status"] = PENDING
                    r.pop("output", None)
            if self.previous_version and not st.get("previous_version"):
                st["previous_version"] = self.previous_version
            self.previous_version = st.get("previous_version")
            st["status"] = RUNNING
            st.update(pid=os.getpid(), host=socket.gethostname())
            self.state.event(now, "resuming train")
            self._notify("train_resumed")
            return
        self.state.data = {
            "train": self.cfg.name,
            "version": self.version,
            "previous_version": self.previous_version,
            "status": RUNNING,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _iso(now),
            "finished_at": None,
            "waves": {w.name: {"finished_at": None, "approval": None} for w in self.cfg.waves},
            "regions": {r.name: {"wave": r.wave, "status": PENDING, "started_at": None,
                                 "finished_at": None, "message": ""}
                        for r in self.cfg.regions},
            "events": [],
        }
        self.state.event(now, f"train {self.cfg.name} started for version {self.version}")
        self._notify("train_started")

    def _finish(self, status: str, message: str) -> str:
        self.state.data["status"] = status
        self.state.data["finished_at"] = _iso(self.clock.now())
        self.state.data["pid"] = None
        self.state.event(self.clock.now(), message)
        self._notify("train_finished", status=status, message=message)
        return status

    # ---- helpers -----------------------------------------------------------

    def _check_abort(self) -> None:
        if self.abort_file.exists():
            raise AbortRequested()

    def _sleep(self, seconds: float, stop_on_halt: bool = False) -> None:
        while seconds > 0:
            self._check_abort()
            if stop_on_halt and self._halt.is_set():
                raise AbortRequested()
            chunk = min(seconds, MAX_SLEEP_CHUNK)
            self.clock.sleep(chunk)
            seconds -= chunk
        self._check_abort()

    def _vars(self, region: Region) -> dict[str, str]:
        return {
            "train": self.cfg.name,
            "region": region.name,
            "wave": region.wave,
            "version": self.version,
            "previous_version": self.previous_version or "",
            **{k: str(v) for k, v in region.vars.items()},
        }

    def _run(self, kind: str, template: str, region: Region, timeout: timedelta) -> CommandResult:
        variables = self._vars(region)
        command = template.format_map(variables)
        env = {f"DT_{k.upper()}": v for k, v in variables.items()}
        if self.dry_run:
            self.state.event(self.clock.now(), f"[dry-run] {kind}: {command}", region.name)
            return CommandResult(0, "")
        self.state.event(self.clock.now(), f"{kind}: {command}", region.name)
        result = self.runner(command, env, timeout.total_seconds())
        if result.output.strip():
            for line in result.output.strip().splitlines()[-20:]:
                log.info("[%s]   | %s", region.name, line)
        return result

    # ---- per-region lifecycle ----------------------------------------------

    def _wait_for_window(self, region: Region) -> None:
        started = self.clock.now()
        announced = None
        while True:
            self._check_abort()
            if self._halt.is_set():
                raise AbortRequested()
            now = self.clock.now()
            try:
                nxt = region.window.next_start(now)
            except NoWindowError as e:
                raise RegionFailed(str(e))
            if nxt <= now:
                return
            if nxt - started > self.cfg.window_wait_timeout:
                raise RegionFailed(
                    f"next window opens at {_iso(nxt)}, beyond window_wait_timeout "
                    f"({format_duration(self.cfg.window_wait_timeout)})")
            if announced != nxt:
                announced = nxt
                local = nxt.astimezone(ZoneInfo(region.window.timezone))
                blackout = region.window.active_blackout(now)
                why = f" (blackout: {blackout.reason})" if blackout and blackout.reason else ""
                msg = (f"waiting for window {region.window.name!r}{why}; opens "
                       f"{local:%a %Y-%m-%d %H:%M %Z} (in {format_duration(nxt - now)})")
                self.state.update_region(region.name, status=WAITING, message=msg)
                self.state.event(now, msg, region.name)
                self._notify("region_waiting", region=region.name, message=msg, opens_at=nxt)
            self._sleep(min((nxt - now).total_seconds(), 300), stop_on_halt=True)

    def _bake(self, region: Region) -> None:
        cfg = self.cfg
        deadline = self.clock.now() + region.bake
        self.state.update_region(region.name, status=BAKING,
                                 message=f"baking for {format_duration(region.bake)}")
        if region.bake:
            self.state.event(self.clock.now(), f"baking for {format_duration(region.bake)}", region.name)
        while True:
            if cfg.health_check_command:
                res = self._run("health check", cfg.health_check_command, region,
                                HEALTH_CHECK_TIMEOUT)
                if res.returncode != 0:
                    raise RegionFailed(f"health check failed (exit {res.returncode})", res.output)
            remaining = (deadline - self.clock.now()).total_seconds()
            if remaining <= 0 or self.dry_run:
                return
            step = remaining if not cfg.health_check_command else min(
                remaining, cfg.health_check_interval.total_seconds())
            self._sleep(step)

    def _rollback(self, region: Region, reason: str) -> None:
        if not self.cfg.rollback_command:
            self.state.event(self.clock.now(), "no rollback_command configured; skipping rollback",
                             region.name)
            return
        res = self._run("rollback", self.cfg.rollback_command, region, region.deploy_timeout)
        status = ROLLED_BACK if res.returncode == 0 else ROLLBACK_FAILED
        self.state.update_region(region.name, status=status,
                                 message=f"{reason}; rollback {'ok' if res.returncode == 0 else 'FAILED'}")
        self._notify("region_rolled_back", region=region.name, ok=res.returncode == 0)

    def _deploy_region(self, region: Region) -> str:
        name = region.name
        try:
            self._wait_for_window(region)
            self.state.update_region(name, status=DEPLOYING, started_at=_iso(self.clock.now()),
                                     message=f"deploying {self.version}")
            self._notify("region_deploying", region=name)
            res = self._run("deploy", self.cfg.deploy_command, region, region.deploy_timeout)
            if res.returncode != 0:
                raise RegionFailed(f"deploy failed (exit {res.returncode})", res.output)
            self._check_abort()
            self._bake(region)
            self.state.update_region(name, status=SUCCEEDED, finished_at=_iso(self.clock.now()),
                                     message=f"{self.version} healthy")
            self.state.event(self.clock.now(), "succeeded", name)
            self._notify("region_succeeded", region=name)
            return SUCCEEDED
        except AbortRequested:
            st = self.state.region(name)["status"]
            if st in (DEPLOYING, BAKING):
                self.state.update_region(name, status=ABORTED, message="aborted mid-deploy")
            else:
                self.state.update_region(name, status=PENDING, message="not started (train halted)")
            return ABORTED
        except RegionFailed as e:
            self._halt.set()
            self.state.update_region(name, status=FAILED, finished_at=_iso(self.clock.now()),
                                     message=str(e), output=e.output.strip()[-2000:])
            self.state.event(self.clock.now(), f"FAILED: {e}", name)
            self._notify("region_failed", region=name, error=str(e), output=e.output.strip())
            if self.cfg.rollback_scope != "none":
                self._rollback(region, str(e))
            return FAILED

    # ---- train -------------------------------------------------------------

    def _run_wave(self, wave: Wave) -> list[str]:
        todo = [r for r in wave.regions if self.state.region(r.name)["status"] != SUCCEEDED]
        if not todo:
            return []
        self.state.event(self.clock.now(), f"wave {wave.name!r} starting: "
                         f"{', '.join(r.name for r in todo)} (max_parallel={wave.max_parallel})")
        self._notify("wave_started", wave=wave.name, regions=[r.name for r in todo])
        if wave.max_parallel == 1:
            return [self._deploy_region(r) for r in todo]
        with ThreadPoolExecutor(max_workers=wave.max_parallel,
                                thread_name_prefix=f"wave-{wave.name}") as pool:
            return list(pool.map(self._deploy_region, todo))

    def _wait_for_approval(self, wave: Wave, todo: list[Region]) -> None:
        waves = self.state.data["waves"]
        info = waves.setdefault(wave.name, {"finished_at": None})
        if (info.get("approval") or {}).get("status") == "approved":
            return
        path = approval_path(self.cfg, self.version, wave.name)
        info["approval"] = {"status": "pending", "requested_at": _iso(self.clock.now())}
        self.state.data["status"] = AWAITING_APPROVAL
        self.state.event(self.clock.now(), f"wave {wave.name!r} is waiting for approval")
        self._notify("wave_awaiting_approval", wave=wave.name, regions=[r.name for r in todo])
        while not path.exists():
            self._sleep(APPROVAL_POLL_SECONDS)
        try:
            by = json.loads(path.read_text()).get("by") or "unknown"
        except ValueError:
            by = "unknown"
        path.unlink(missing_ok=True)
        info["approval"] = {**info["approval"], "status": "approved", "by": by,
                            "approved_at": _iso(self.clock.now())}
        self.state.data["status"] = RUNNING
        self.state.event(self.clock.now(), f"wave {wave.name!r} approved by {by}")
        self._notify("wave_approved", wave=wave.name, by=by)

    def _rollback_succeeded(self, regions: list[Region], reason: str) -> None:
        for r in reversed(regions):
            if self.state.region(r.name)["status"] == SUCCEEDED:
                self._rollback(r, reason)

    def run(self, retry_failed: bool = False, force: bool = False) -> str:
        self.abort_file.unlink(missing_ok=True)
        self._init_state(retry_failed, force)
        if self.state.data["status"] == SUCCEEDED:
            log.info("train already succeeded for %s", self.version)
            return SUCCEEDED
        completed: list[Region] = []
        try:
            for i, wave in enumerate(self.cfg.waves):
                if i > 0 and wave.gap:
                    prev = self.state.data["waves"][self.cfg.waves[i - 1].name]["finished_at"]
                    if prev:
                        ready = datetime.fromisoformat(prev) + wave.gap
                        wait = (ready - self.clock.now()).total_seconds()
                        if wait > 0:
                            self.state.event(self.clock.now(),
                                             f"gap before wave {wave.name!r}: {format_duration(wave.gap)}")
                            self._sleep(wait)
                todo = [r for r in wave.regions if self.state.region(r.name)["status"] != SUCCEEDED]
                if wave.approval and todo:
                    self._wait_for_approval(wave, todo)
                results = self._run_wave(wave)
                if FAILED in results:
                    reason = f"failure in wave {wave.name!r}"
                    if self.cfg.rollback_scope == "wave":
                        self._rollback_succeeded(wave.regions, reason)
                    elif self.cfg.rollback_scope == "train":
                        self._rollback_succeeded(completed + wave.regions, reason)
                    return self._finish(FAILED, f"train halted: {reason}")
                if ABORTED in results:
                    raise AbortRequested()
                completed.extend(wave.regions)
                if results or not self.state.data["waves"][wave.name]["finished_at"]:
                    self.state.data["waves"][wave.name]["finished_at"] = _iso(self.clock.now())
                    self.state.event(self.clock.now(), f"wave {wave.name!r} complete")
                    self._notify("wave_completed", wave=wave.name)
        except AbortRequested:
            return self._finish(ABORTED, "train aborted; re-run the same command to resume")
        return self._finish(SUCCEEDED, f"train complete: {self.version} is live in all regions")


# ---- planning ---------------------------------------------------------------

@dataclass
class PlannedRegion:
    region: Region
    earliest: datetime
    start: datetime
    end: datetime


def plan(cfg: TrainConfig, start_at: datetime,
         deploy_duration: timedelta | None = None) -> list[tuple[Wave, list[PlannedRegion]]]:
    """Project when each region would deploy if every step succeeds on time."""
    duration = deploy_duration if deploy_duration is not None else cfg.estimated_deploy_duration
    out = []
    t = start_at
    for i, wave in enumerate(cfg.waves):
        if i > 0:
            t = t + wave.gap
        slots = [t] * wave.max_parallel
        planned = []
        for region in wave.regions:
            slot = min(range(len(slots)), key=lambda k: slots[k])
            earliest = slots[slot]
            start = region.window.next_start(earliest)
            end = start + duration + region.bake
            slots[slot] = end
            planned.append(PlannedRegion(region, earliest, start, end))
        t = max(p.end for p in planned)
        out.append((wave, planned))
    return out
