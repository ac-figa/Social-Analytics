"""
Runs "Update all datasets" as a background thread: each platform pipeline
in turn, then cross-platform matching, then the Instagram Duration
backfill -- the exact sequence documented in shared/README.md, just
triggered from the dashboard instead of four separate terminal commands.

Runs in a background thread (not the request thread) since a full run
takes several minutes -- an HTTP request that long would just time out in
most browsers/proxies. The dashboard polls get_status() instead.
"""
import os
import subprocess
import sys
import threading
import time

from . import config

_lock = threading.Lock()
_status = {
    "running": False,
    "current_step": None,
    "log": [],
    "started_at": None,
    "finished_at": None,
    "error": None,
}

_MAX_LOG_LINES = 1000


def get_status() -> dict:
    with _lock:
        return {**_status, "log": list(_status["log"])}


def _append_log(line: str) -> None:
    with _lock:
        _status["log"].append(line)
        if len(_status["log"]) > _MAX_LOG_LINES:
            _status["log"] = _status["log"][-_MAX_LOG_LINES:]


def _set_step(name: str) -> None:
    with _lock:
        _status["current_step"] = name
    _append_log(f"--- {name} ---")


def _run_step(name: str, cmd: list, cwd, extra_env: dict = None) -> None:
    _set_step(name)
    env = {**os.environ, **extra_env} if extra_env else None
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
    )
    for line in proc.stdout:
        _append_log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{name} exited with code {proc.returncode}")


def _run_all() -> None:
    try:
        with _lock:
            _status["running"] = True
            _status["error"] = None
            _status["log"] = []
            _status["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _status["finished_at"] = None

        repo_root = config.SHARED_DIR.parent
        for platform, pipeline_dir in config.PIPELINE_DIRS.items():
            _run_step(f"Syncing {platform}", [sys.executable, "-m", "src.pipeline"], pipeline_dir)

        for extra in config.EXTRA_ACCOUNT_SYNCS:
            if not (extra["pipeline_dir"] / extra["env_file"]).exists():
                _append_log(f"--- Skipping {extra['label']} ({extra['env_file']} not found) ---")
                continue
            _run_step(
                f"Syncing {extra['label']}", [sys.executable, "-m", "src.pipeline"], extra["pipeline_dir"],
                extra_env={"ENV_FILE": extra["env_file"]},
            )

        _run_step("Matching across platforms", [sys.executable, "-m", "shared.src.run_matching"], repo_root)
        _run_step(
            "Backfilling Instagram Duration from Facebook",
            [sys.executable, "-m", "shared.src.backfill_instagram_duration"],
            repo_root,
        )
        _append_log("--- Done ---")
    except Exception as e:  # noqa: BLE001 -- surfaced to the dashboard via get_status()["error"]
        with _lock:
            _status["error"] = str(e)
        _append_log(f"ERROR: {e}")
    finally:
        with _lock:
            _status["running"] = False
            _status["current_step"] = None
            _status["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def start_sync() -> bool:
    """Returns False (no-op) if a sync is already running, True if a new
    one was started."""
    with _lock:
        if _status["running"]:
            return False
    threading.Thread(target=_run_all, daemon=True).start()
    return True
