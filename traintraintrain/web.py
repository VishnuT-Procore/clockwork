"""Web UI: a small JSON API over the train state files plus a single-page dashboard.

The UI never writes a train's state file. Departures are separate `traintraintrain depart`
processes, and actions use the same marker files the CLI uses (brake, signal). The UI
can be restarted at any time without affecting trains en route.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ._compat import LEGACY_HEADER_VALUE, env
from .config import TrainConfig
from .timetable import NoServiceError, format_duration
from .train import (
    ACTIVE_STATUSES, SIGNAL_WAITING, SUCCEEDED, apply_brake, brake_path, clear_signal,
    owner_alive, project, read_state, signal_path, state_path,
)

log = logging.getLogger("traintraintrain")

STATIC_DIR = Path(__file__).parent / "ui"
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
USER_HEADERS = ("X-Forwarded-User", "X-Auth-Request-User", "X-Forwarded-Email",
                "X-Auth-Request-Email")
LOG_TAIL_LINES = 300
SHUTDOWN_GRACE_SECONDS = 20.0


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS lookup that can block
        # for many seconds (notably on macOS). The name only feeds CGI's SERVER_NAME.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - 256 * 1024))
        data = fh.read().decode(errors="replace")
    return "\n".join(data.splitlines()[-lines:])


class App:
    def __init__(self, cfg: TrainConfig, config_path: str, token: str | None = None):
        self.cfg = cfg
        self.config_path = str(Path(config_path).resolve())
        self.token = token
        self._children: list[tuple[subprocess.Popen, str]] = []
        self._lock = threading.Lock()

    # ---- paths -------------------------------------------------------------

    def _state(self, version: str) -> dict | None:
        return read_state(state_path(self.cfg, version))

    def _log_path(self, version: str) -> Path:
        return state_path(self.cfg, version).with_suffix(".log")

    def _audit_path(self) -> Path:
        return self.cfg.state_dir / f"{self.cfg.name}.audit.jsonl"

    def _audit(self, user: str, action: str, version: str, **details) -> None:
        entry = {"at": _iso(_now()), "user": user, "action": action, "version": version, **details}
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self._audit_path(), "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        log.info("audit: %s %s %s %s", user, action, version, details or "")

    def _audit_entries(self, version: str) -> list[dict]:
        path = self._audit_path()
        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("version") == version:
                out.append(e)
        return out

    def _reap(self) -> None:
        with self._lock:
            self._children = [(p, v) for p, v in self._children if p.poll() is None]

    @staticmethod
    def _check_version(version) -> str:
        if not isinstance(version, str) or not VERSION_RE.match(version):
            raise ApiError(HTTPStatus.BAD_REQUEST,
                           "version must be 1-100 chars of letters, digits, . _ + -")
        return version

    # ---- views -------------------------------------------------------------

    def config_view(self) -> dict:
        now = _now()
        stops = []
        for s in self.cfg.stops:
            platforms = []
            for p in s.platforms:
                tt = p.timetable
                try:
                    nxt = tt.next_start(now)
                except NoServiceError:
                    nxt = None
                platforms.append({
                    "name": p.name, "timetable": tt.name, "timezone": tt.timezone,
                    "dwell": format_duration(p.dwell),
                    "deploy_timeout": format_duration(p.deploy_timeout),
                    "in_service": nxt == now, "next_start": _iso(nxt),
                    "service_end": _iso(tt.service_end(now)) if nxt == now else None,
                })
            stops.append({"name": s.name, "parallel_platforms": s.parallel_platforms,
                          "signal": s.signal,
                          "headway": format_duration(s.headway) if s.headway else None,
                          "platforms": platforms})
        return {"train": self.cfg.name, "rollback_scope": self.cfg.rollback_scope,
                "has_rollback": bool(self.cfg.rollback_command),
                "slack": self.cfg.slack is not None, "now": _iso(now), "stops": stops}

    def timetable_view(self) -> dict:
        try:
            projected = project(self.cfg, _now())
        except NoServiceError as e:
            raise ApiError(HTTPStatus.CONFLICT, str(e))
        return {"stops": [
            {"name": s.name, "platforms": [
                {"name": p.platform.name, "start": _iso(p.start), "end": _iso(p.end),
                 "held": format_duration(p.start - p.earliest) if p.start > p.earliest else None}
                for p in planned]}
            for s, planned in projected]}

    def _summary(self, st: dict) -> dict:
        counts: dict[str, int] = {}
        for p in st["platforms"].values():
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        alive = owner_alive(st)
        return {
            "version": st["version"], "previous_version": st.get("previous_version"),
            "status": st["status"], "started_at": st.get("started_at"),
            "finished_at": st.get("finished_at"), "counts": counts,
            "total": len(st["platforms"]), "active": alive,
            "interrupted": st["status"] in ACTIVE_STATUSES and not alive,
        }

    def list_departures(self) -> list[dict]:
        self._reap()
        departures = []
        for path in self.cfg.state_dir.glob(f"{self.cfg.name}-*.json"):
            st = read_state(path)
            if st and st.get("train") == self.cfg.name:
                departures.append(self._summary(st))
        departures.sort(key=lambda d: d["started_at"] or "", reverse=True)
        return departures

    def get_departure(self, version: str) -> dict:
        self._reap()
        st = self._state(self._check_version(version))
        log_text = _tail(self._log_path(version), LOG_TAIL_LINES)
        if st is None:
            if log_text:  # process started but hasn't written state yet, or died early
                return {"version": version, "starting": True, "log": log_text,
                        "actions": self._audit_entries(version)}
            raise ApiError(HTTPStatus.NOT_FOUND, f"no departure for version {version}")
        at_signal = next((name for name, s in st.get("stops", {}).items()
                          if (s.get("signal") or {}).get("status") == SIGNAL_WAITING), None)
        cleared = bool(at_signal) and signal_path(self.cfg, version, at_signal).exists()
        return {
            **self._summary(st),
            "platforms": st["platforms"],
            "stops": st.get("stops", {}),
            "events": st.get("events", [])[-200:],
            "signal_at": at_signal,
            "signal_clear_submitted": cleared,
            "brake_applied": brake_path(self.cfg, version).exists(),
            "log": log_text,
            "actions": self._audit_entries(version),
        }

    # ---- actions -----------------------------------------------------------

    def _spawn(self, version: str, extra: list[str], user: str) -> None:
        cmd = [sys.executable, "-m", "traintraintrain", "-c", self.config_path, "depart",
               "--version", version, *extra]
        log_path = self._log_path(version)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as fh:
            fh.write(f"\n=== {_iso(_now())} dispatched by {user}: {' '.join(cmd[2:])} ===\n")
            fh.flush()
            proc = subprocess.Popen(
                cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=str(Path(self.config_path).parent), start_new_session=True,
                env={**os.environ, "TRAINTRAINTRAIN_TRIGGERED_BY": user})
        with self._lock:
            self._children.append((proc, version))

    def shutdown(self, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
        """Brake the trains this server dispatched and give them time to stop cleanly."""
        self._reap()
        with self._lock:
            children = list(self._children)
        if not children:
            return
        for _, version in children:
            log.warning("shutting down: applying the emergency brake to %s", version)
            apply_brake(self.cfg, version, "ui shutdown")
        deadline = time.monotonic() + grace
        for proc, _ in children:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass  # resumable: its heartbeat goes stale once it's killed

    def depart(self, body: dict, user: str) -> dict:
        version = self._check_version(body.get("version"))
        prev = body.get("previous_version") or None
        if prev is not None:
            self._check_version(prev)
        if state_path(self.cfg, version).exists():
            raise ApiError(HTTPStatus.CONFLICT,
                           f"a departure for {version} already exists; open it and use resume")
        extra = ["--previous-version", prev] if prev else []
        if body.get("dry_run"):
            extra.append("--dry-run")
        self._audit(user, "depart", version, previous_version=prev,
                    dry_run=bool(body.get("dry_run")))
        self._spawn(version, extra, user)
        return {"ok": True, "version": version}

    def resume(self, body: dict, user: str) -> dict:
        version = self._check_version(body.get("version"))
        st = self._state(version)
        if st is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no departure for version {version}")
        if owner_alive(st):
            raise ApiError(HTTPStatus.CONFLICT, f"{version} is already en route")
        if st["status"] == SUCCEEDED:
            raise ApiError(HTTPStatus.CONFLICT, f"{version} has already arrived")
        extra = ["--retry-failed"] if body.get("retry_failed") else []
        if st["status"] in ACTIVE_STATUSES:
            extra.append("--force")  # state says en route but its process is gone
        self._audit(user, "resume", version, retry_failed=bool(body.get("retry_failed")))
        self._spawn(version, extra, user)
        return {"ok": True}

    def brake(self, body: dict, user: str) -> dict:
        version = self._check_version(body.get("version"))
        st = self._state(version)
        if st is None or not owner_alive(st):
            raise ApiError(HTTPStatus.CONFLICT, f"{version} is not en route")
        apply_brake(self.cfg, version, user)
        self._audit(user, "brake", version)
        return {"ok": True}

    def clear_signal(self, body: dict, user: str) -> dict:
        version = self._check_version(body.get("version"))
        stop = body.get("stop", body.get("wave"))
        st = self._state(version)
        if st is None:
            raise ApiError(HTTPStatus.NOT_FOUND, f"no departure for version {version}")
        signal = (st.get("stops", {}).get(stop) or {}).get("signal") or {}
        if signal.get("status") != SIGNAL_WAITING:
            raise ApiError(HTTPStatus.CONFLICT, f"no signal is waiting at stop {stop!r}")
        clear_signal(self.cfg, version, stop, user)
        self._audit(user, "clear_signal", version, stop=stop)
        return {"ok": True}



def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "traintraintrain"

        def log_message(self, fmt, *args):
            log.debug("ui %s - " + fmt, self.address_string(), *args)

        # ---- plumbing ----

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _user(self) -> str:
            for h in USER_HEADERS:  # set by an authenticating proxy (oauth2-proxy, IAP, ...)
                if self.headers.get(h):
                    return self.headers[h]
            name = (self.headers.get("X-Traintraintrain-User")
                    or self.headers.get("X-Deploytrain-User") or "").strip()
            return name[:64] if name else "anonymous"

        def _authorized(self) -> bool:
            if not app.token:
                return True
            supplied = self.headers.get("Authorization") or ""
            return hmac.compare_digest(supplied.encode(), f"Bearer {app.token}".encode())

        def _api(self, fn) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or invalid token"})
                return
            try:
                self._json(HTTPStatus.OK, fn())
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("ui request failed")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

        # ---- routes ----

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/healthz":  # unauthenticated liveness probe
                self._json(HTTPStatus.OK, {"ok": True})
            elif url.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, (STATIC_DIR / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif url.path == "/api/config":
                self._api(app.config_view)
            elif url.path in ("/api/timetable", "/api/plan"):
                self._api(app.timetable_view)
            elif url.path in ("/api/departures", "/api/runs"):
                self._api(app.list_departures)
            elif url.path in ("/api/departure", "/api/run"):
                self._api(lambda: app.get_departure((q.get("version") or [""])[0]))
            elif url.path == "/api/whoami":
                self._api(lambda: {"user": self._user(), "auth": bool(app.token)})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            # Custom header + JSON body: blocks cross-site form posts (CSRF).
            if self.headers.get("X-Requested-With") not in ("traintraintrain", LEGACY_HEADER_VALUE):
                self._json(HTTPStatus.FORBIDDEN, {"error": "missing X-Requested-With header"})
                return
            routes = {
                "/api/departures/depart": app.depart, "/api/departures/resume": app.resume,
                "/api/departures/brake": app.brake,
                "/api/departures/clear-signal": app.clear_signal,
                # Pre-rename paths.
                "/api/runs/start": app.depart, "/api/runs/resume": app.resume,
                "/api/runs/abort": app.brake, "/api/runs/approve": app.clear_signal,
            }
            fn = routes.get(urlparse(self.path).path)
            if fn is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 64 * 1024)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object"})
                return
            self._api(lambda: fn(body, self._user()))

    return Handler


def serve(cfg: TrainConfig, config_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    token = env("UI_TOKEN")
    if host not in ("127.0.0.1", "localhost", "::1") and not token:
        log.warning("UI is listening on %s without TRAINTRAINTRAIN_UI_TOKEN; anyone who can reach it "
                    "can dispatch trains and pull the brake. Put it behind an auth proxy or set a token.", host)
    app = App(cfg, config_path, token)
    httpd = Server((host, port), make_handler(app))
    log.info("traintraintrain UI for %r on http://%s:%d", cfg.name, host, port)

    def on_sigterm(signum, frame):  # `docker stop`, Kubernetes pod termination
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping the UI")
    finally:
        httpd.server_close()
        app.shutdown(float(env("SHUTDOWN_GRACE", str(SHUTDOWN_GRACE_SECONDS))))
