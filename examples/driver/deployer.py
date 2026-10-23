#!/usr/bin/env python3
"""Example traintraintrain driver: one program for every step of the train.

traintraintrain runs it once per step, sends one JSON request on stdin and reads one JSON
reply from stdout. Anything printed to stderr is kept as the step's log. The contract is
in docs/driver-contract.md; `traintraintrain contract test --action deploy` checks a
driver against it.

This example doesn't touch real infrastructure. It keeps a pretend cluster state in
.fake-cluster.json next to the config, so live versions, rollbacks and failures all
behave realistically. Replace the bodies of deploy/inspect/rollback/live_version with
calls to your deployer (Helm, Argo CD, Spinnaker, an internal API, ...).

Environment:
  GIT_REPO        a git checkout, for `resolve` (tag -> commit) and `changes` (git log)
  CHANGES_PATHS   for monorepos: limit `changes` to these folders, e.g. "services/pay libs/billing"
  FAIL_DEPLOY     "platform:version" pairs that should fail, e.g. "ap-northeast-1:v1.4.0"
  FAKE_CLUSTER    where to keep the pretend cluster state (default .fake-cluster.json)
  ERROR_RATE      what the `error_stats` step reports (default 0.2); e.g. "us-west-2-canary=3.5,0.2"

"""
import json
import os
import subprocess
import sys
from pathlib import Path

STATE = Path(os.environ.get("FAKE_CLUSTER", ".fake-cluster.json"))


def log(msg):
    print(msg, file=sys.stderr)  # stderr: shown as the step's log, never parsed


def cluster():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(state):
    STATE.write_text(json.dumps(state, indent=2))


def git(*args):
    repo = os.environ.get("GIT_REPO")
    if not repo:
        return None
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


# ---- actions: each returns the reply (a dict with at least "ok") --------------------

def deploy(req):
    target = f"{req['platform']}:{req['version']}"
    log(f"deploying {req['version']} ({req.get('sha') or 'no sha'}) to {req['platform']} "
        f"with vars {req.get('vars')}")
    if req.get("dry_run"):
        return {"ok": True, "message": f"dry run: would deploy {target}"}
    if target in os.environ.get("FAIL_DEPLOY", "").split():
        return {"ok": False, "message": f"rollout of {req['version']} timed out on {req['platform']}"}
    state = cluster()
    state[req["platform"]] = {"version": req["version"], "sha": req.get("sha")}
    save(state)
    return {"ok": True, "message": f"{req['platform']} now runs {req['version']}"}


def inspect(req):
    running = cluster().get(req["platform"], {}).get("version")
    if running != req["version"]:
        return {"ok": False, "message": f"{req['platform']} runs {running}, expected {req['version']}"}
    return {"ok": True, "message": "GET /healthz -> 200"}


def rollback(req):
    back_to = req.get("previous_version")
    if not back_to:
        return {"ok": False, "message": "nothing to roll back to"}
    if req.get("dry_run"):
        return {"ok": True, "message": f"dry run: would roll {req['platform']} back to {back_to}"}
    state = cluster()
    state[req["platform"]] = {"version": back_to, "sha": req.get("previous_sha")}
    save(state)
    return {"ok": True, "message": f"{req['platform']} rolled back to {back_to}"}


def live_version(req):
    running = cluster().get(req["platform"])
    if not running:
        return {"ok": False, "message": f"nothing deployed on {req['platform']} yet"}
    return {"ok": True, "version": running["version"], "sha": running.get("sha")}


def resolve(req):
    out = git("rev-parse", f"{req['version']}^{{commit}}")
    if out is None:
        return {"ok": False, "unsupported": True, "message": "set GIT_REPO to resolve versions"}
    return {"ok": True, "sha": out.strip()}


def changes(req):
    frm = req.get("previous_sha") or req["previous_version"]
    to = req.get("sha") or req["version"]
    paths = os.environ.get("CHANGES_PATHS", "").split()
    out = git("log", "--format=%H%x09%an%x09%s", f"{frm}..{to}", *(["--", *paths] if paths else []))
    if out is None:
        return {"ok": False, "unsupported": True, "message": "set GIT_REPO to list changes"}
    items = []
    for line in out.splitlines():
        sha, author, title = line.split("\t", 2)
        items.append({"id": sha[:9], "title": title, "author": author})
    return {"ok": True, "changes": items}


# ---- pipeline steps (`steps:` with `run: <name>`), replying with metrics -------------

def db_stats(req):
    """Pretend database stats; a real one would query the database for req["platform"]."""
    return {"ok": True, "metrics": {"pending_migrations": 0, "replication_lag_s": 4.2}}


def error_stats(req):
    """Pretend error rate, set per platform with ERROR_RATE ("canary=3.5,0.2": canary 3.5, others 0.2)."""
    rates, default = {}, 0.2
    for part in os.environ.get("ERROR_RATE", "").split(","):
        if "=" in part:
            name, value = part.split("=", 1)
            rates[name.strip()] = float(value)
        elif part.strip():
            default = float(part)
    return {"ok": True, "metrics": {"error_rate": rates.get(req["platform"], default)}}


STEPS = {"db_stats": db_stats, "error_stats": error_stats}


def step(req):
    handler = STEPS.get(req["step"])
    if handler is None:
        return {"ok": False, "unsupported": True, "message": f"no step called {req['step']!r}"}
    return handler(req)


ACTIONS = {"deploy": deploy, "inspect": inspect, "rollback": rollback,
           "live_version": live_version, "resolve": resolve, "changes": changes, "step": step}


def main():
    req = json.load(sys.stdin)
    handler = ACTIONS.get(req.get("action"))
    if req.get("contract") != 1 or handler is None:
        reply = {"ok": False, "unsupported": True, "message": f"unsupported request {req.get('action')!r}"}
    else:
        try:
            reply = handler(req)
        except Exception as e:  # report, don't crash: a crash is a failed step either way
            reply = {"ok": False, "message": f"{type(e).__name__}: {e}"}
    print(json.dumps(reply))


if __name__ == "__main__":
    main()
