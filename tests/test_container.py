"""Behaviour that matters when running in a container: env config, health probe, SIGTERM."""
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from traintraintrain import cli, config
from traintraintrain.train import read_state, state_path

CONFIG = """
train:
  name: box
  deploy_command: "echo deploy {platform}"
  inspection_command: "true"
  inspection_interval: 1s
  default_dwell: 60s
stops:
  - name: only
    platforms: [p1]
"""


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


def write_config(tmp_path):
    path = tmp_path / "train.yaml"
    path.write_text(CONFIG)
    return path


def wait_until(pred, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out")


def test_state_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAINTRAINTRAIN_STATE_DIR", str(tmp_path / "volume"))
    cfg = config.load(write_config(tmp_path))
    assert cfg.state_dir == (tmp_path / "volume").resolve()


def test_config_path_env_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRAINTRAINTRAIN_CONFIG", str(write_config(tmp_path)))
    assert cli.main(["timetable"]) == 0
    assert "Timetable for train 'box'" in capsys.readouterr().out


def test_sigterm_brakes_a_departing_train(tmp_path):
    path = write_config(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "traintraintrain", "-c", str(path), "depart",
                             "--version", "v1"], cwd=tmp_path,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    cfg = config.load(path)
    wait_until(lambda: (read_state(state_path(cfg, "v1")) or {}).get("platforms", {})
               .get("p1", {}).get("status") == "dwelling")
    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 1, out
    st = read_state(state_path(cfg, "v1"))
    assert st["status"] == "aborted" and st["platforms"]["p1"]["status"] == "aborted"
    assert "emergency brake" in out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ui_healthz_and_sigterm_brakes_dispatched_trains(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    port = free_port()
    monkeypatch.setenv("TRAINTRAINTRAIN_STATE_DIR", str(tmp_path / "st"))
    env = {**os.environ, "TRAINTRAINTRAIN_UI_TOKEN": "t0ken"}
    ui = subprocess.Popen([sys.executable, "-m", "traintraintrain", "-c", str(path), "ui",
                           "--port", str(port)], cwd=tmp_path, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        # /healthz needs no token; the API does.
        try:
            health = wait_until(lambda: _get(base + "/healthz") or ui.poll() is not None)
        except AssertionError:
            health = None
        if health is not True and health != {"ok": True} or ui.poll() is not None:
            ui.kill()
            raise AssertionError(f"UI did not come up (exit {ui.poll()}):\n{ui.communicate()[0]}")
        assert health == {"ok": True}
        req = urllib.request.Request(
            base + "/api/departures/depart", data=json.dumps({"version": "v9"}).encode(),
            headers={"Authorization": "Bearer t0ken", "Content-Type": "application/json",
                     "X-Requested-With": "traintraintrain"})
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
        cfg = config.load(path)
        assert cfg.state_dir == (tmp_path / "st").resolve()
        wait_until(lambda: (read_state(state_path(cfg, "v9")) or {}).get("status") == "en_route"
                   and read_state(state_path(cfg, "v9"))["platforms"]["p1"]["status"] == "dwelling")
        ui.send_signal(signal.SIGTERM)
        out, _ = ui.communicate(timeout=40)
        assert ui.returncode == 0, out
        st = read_state(state_path(cfg, "v9"))
        assert st["status"] == "aborted", out
    finally:
        if ui.poll() is None:
            ui.kill()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read())
    except OSError:
        return None


def test_ui_server_starts_without_reverse_dns(monkeypatch):
    # HTTPServer would call socket.getfqdn(), which can hang for many seconds on macOS.
    from http.server import BaseHTTPRequestHandler

    from traintraintrain.web import Server

    def boom(*a):
        raise AssertionError("reverse DNS lookup during startup")

    monkeypatch.setattr(socket, "getfqdn", boom)
    server = Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert server.server_name == "127.0.0.1" and server.server_port > 0
    finally:
        server.server_close()


def test_pre_rename_env_vars_still_work(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TRAINTRAINTRAIN_CONFIG", raising=False)
    monkeypatch.delenv("TRAINTRAINTRAIN_STATE_DIR", raising=False)
    monkeypatch.setenv("DEPLOYTRAIN_CONFIG", str(write_config(tmp_path)))
    monkeypatch.setenv("DEPLOYTRAIN_STATE_DIR", str(tmp_path / "old-volume"))
    assert cli.main(["timetable"]) == 0
    assert "Timetable for train 'box'" in capsys.readouterr().out
    assert config.load(tmp_path / "train.yaml").state_dir == (tmp_path / "old-volume").resolve()
    # The new name wins when both are set.
    monkeypatch.setenv("TRAINTRAINTRAIN_STATE_DIR", str(tmp_path / "new-volume"))
    assert config.load(tmp_path / "train.yaml").state_dir == (tmp_path / "new-volume").resolve()


def test_existing_pre_rename_state_folder_is_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("TRAINTRAINTRAIN_STATE_DIR", raising=False)
    monkeypatch.delenv("DEPLOYTRAIN_STATE_DIR", raising=False)
    path = write_config(tmp_path)
    assert config.load(path).state_dir == (tmp_path / ".traintraintrain").resolve()
    (tmp_path / ".deploytrain").mkdir()
    assert config.load(path).state_dir == (tmp_path / ".deploytrain").resolve()
    (tmp_path / ".traintraintrain").mkdir()
    assert config.load(path).state_dir == (tmp_path / ".traintraintrain").resolve()


def test_deploytrain_command_alias_is_installed():
    from importlib.metadata import entry_points
    scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")
               if ep.value.startswith("traintraintrain.")}
    assert scripts == {"traintraintrain": "traintraintrain.cli:main",
                       "deploytrain": "traintraintrain.cli:main"}


def test_database_url_without_driver_gives_install_hint(tmp_path, monkeypatch, capsys):
    import importlib.util
    if importlib.util.find_spec("psycopg") is not None:
        pytest.skip("the Postgres driver is installed here")
    monkeypatch.setenv("TRAINTRAINTRAIN_DATABASE_URL", "postgresql://u@127.0.0.1:5432/db")
    assert cli.main(["-c", str(write_config(tmp_path)), "db", "status"]) == 1
    assert 'pip install "traintraintrain[postgres]"' in capsys.readouterr().err
