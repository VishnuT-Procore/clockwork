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

from deploytrain import cli, config
from deploytrain.train import read_state, state_path

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
    monkeypatch.setenv("DEPLOYTRAIN_STATE_DIR", str(tmp_path / "volume"))
    cfg = config.load(write_config(tmp_path))
    assert cfg.state_dir == (tmp_path / "volume").resolve()


def test_config_path_env_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOYTRAIN_CONFIG", str(write_config(tmp_path)))
    assert cli.main(["timetable"]) == 0
    assert "Timetable for train 'box'" in capsys.readouterr().out


def test_sigterm_brakes_a_departing_train(tmp_path):
    path = write_config(tmp_path)
    proc = subprocess.Popen([sys.executable, "-m", "deploytrain", "-c", str(path), "depart",
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
    monkeypatch.setenv("DEPLOYTRAIN_STATE_DIR", str(tmp_path / "st"))
    env = {**os.environ, "DEPLOYTRAIN_UI_TOKEN": "t0ken"}
    ui = subprocess.Popen([sys.executable, "-m", "deploytrain", "-c", str(path), "ui",
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
                     "X-Requested-With": "deploytrain"})
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
