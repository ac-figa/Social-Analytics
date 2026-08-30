"""
Entrypoint for the Cloud Run Job that runs the daily automated sync --
see syncjob/README.md for how it's deployed and scheduled.

Reproduces the exact sequence webapp/src/sync.py runs locally (each
platform pipeline, then the Calcio Bros extra accounts, then
cross-platform matching, then the Instagram Duration backfill), except
credentials come from Secret Manager instead of local .env files, since
this runs unattended in the cloud with nobody's laptop involved.

Each pipeline/account's entire .env file is stored as one Secret Manager
secret (uploaded once by syncjob/README.md's migration step) and written
out to the exact path that pipeline's own config.py already expects --
no pipeline code changes needed. TikTok's refresh token rotates on every
run (see tiktokpipeline/src/config.py's update_refresh_token()) and gets
written back to the local file on disk as always; since that disk is
thrown away when the job container exits, this script diffs the file
before/after each TikTok run and pushes a new Secret Manager version
when it changed, so the next day's run picks up the rotated token
instead of the run failing with an expired/reused one.

A missing secret (e.g. a Calcio Bros account not set up yet) is skipped
with a log line, matching sync.py's local behavior -- this can go live
before every account exists. One platform failing doesn't stop the
others: every step that *can* run does, and the job only exits non-zero
(so Cloud Run/Cloud Scheduler surface it as a failed run) if something
that was actually attempted errored.
"""
import os
import subprocess
import sys
from pathlib import Path

from google.api_core.exceptions import NotFound
from google.cloud import secretmanager

REPO_ROOT = Path(__file__).resolve().parents[1]
GCP_PROJECT_ID = os.environ["GCP_PROJECT_ID"]

_sm_client = secretmanager.SecretManagerServiceClient()

# One entry per pipeline/account whose .env this job needs to materialize
# and (for TikTok) watch for a rotated refresh token. "rotates" mirrors
# tiktokpipeline/src/config.py's update_refresh_token() -- only TikTok's
# credentials ever change on their own between runs.
SYNC_TARGETS = [
    {"label": "Instagram", "dir": REPO_ROOT / "instagramanalyticspipeline",
     "secret": "sync-env-instagram", "env_file": ".env", "rotates": False},
    {"label": "Facebook", "dir": REPO_ROOT / "facebookpipeline",
     "secret": "sync-env-facebook", "env_file": ".env", "rotates": False},
    {"label": "YouTube", "dir": REPO_ROOT / "youtubepipeline",
     "secret": "sync-env-youtube", "env_file": ".env", "rotates": False},
    {"label": "TikTok", "dir": REPO_ROOT / "tiktokpipeline",
     "secret": "sync-env-tiktok", "env_file": ".env", "rotates": True},
    {"label": "Instagram (Calcio Bros)", "dir": REPO_ROOT / "instagramanalyticspipeline",
     "secret": "sync-env-instagram-calciobros", "env_file": ".env.calciobros", "rotates": False},
    {"label": "TikTok (Calcio Bros)", "dir": REPO_ROOT / "tiktokpipeline",
     "secret": "sync-env-tiktok-calciobros", "env_file": ".env.calciobros", "rotates": True},
]

# shared/.env isn't tied to any one pipeline -- it's what the final
# matching/backfill steps read (see shared/src/config.py).
SHARED_ENV_SECRET = "sync-env-shared"
SHARED_ENV_PATH = REPO_ROOT / "shared" / ".env"


def _secret_path(secret_name: str, version: str = "latest") -> str:
    return f"projects/{GCP_PROJECT_ID}/secrets/{secret_name}/versions/{version}"


def fetch_secret(secret_name: str) -> bytes | None:
    try:
        response = _sm_client.access_secret_version(name=_secret_path(secret_name))
    except NotFound:
        return None
    return response.payload.data


def add_secret_version(secret_name: str, data: bytes) -> None:
    parent = f"projects/{GCP_PROJECT_ID}/secrets/{secret_name}"
    _sm_client.add_secret_version(request={"parent": parent, "payload": {"data": data}})


def maybe_rotate_secret(secret_name: str, path: Path, original: bytes) -> None:
    if not path.exists():
        return
    current = path.read_bytes()
    if current == original:
        return
    add_secret_version(secret_name, current)
    print(f"Rotated credential detected -- pushed a new version of secret '{secret_name}'", flush=True)


def run(label: str, args: list, cwd: Path, extra_env: dict = None) -> bool:
    print(f"\n--- {label} ---", flush=True)
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.run([sys.executable, *args], cwd=str(cwd), env=env)
    if proc.returncode != 0:
        print(f"!!! {label} exited with code {proc.returncode}", flush=True)
        return False
    return True


def main() -> None:
    failures = []

    shared_env_data = fetch_secret(SHARED_ENV_SECRET)
    if shared_env_data is None:
        print(f"--- Warning: secret '{SHARED_ENV_SECRET}' not found -- "
              f"matching/backfill will likely fail ---", flush=True)
    else:
        SHARED_ENV_PATH.write_bytes(shared_env_data)

    for target in SYNC_TARGETS:
        data = fetch_secret(target["secret"])
        if data is None:
            print(f"--- Skipping {target['label']} (secret '{target['secret']}' not set up yet) ---", flush=True)
            continue

        env_path = target["dir"] / target["env_file"]
        env_path.write_bytes(data)

        ok = run(target["label"], ["-m", "src.pipeline"], target["dir"], extra_env={"ENV_FILE": target["env_file"]})
        if not ok:
            failures.append(target["label"])

        if target["rotates"]:
            maybe_rotate_secret(target["secret"], env_path, data)

    if not run("Matching across platforms", ["-m", "shared.src.run_matching"], REPO_ROOT):
        failures.append("Matching across platforms")
    if not run("Backfilling Instagram Duration from Facebook",
               ["-m", "shared.src.backfill_instagram_duration"], REPO_ROOT):
        failures.append("Instagram Duration backfill")

    print(f"\n=== Done. {len(failures)} failure(s){': ' + ', '.join(failures) if failures else ''} ===", flush=True)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
