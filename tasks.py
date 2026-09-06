"""Ops tasks for the homek14 deployment (rootless Podman + Cloudflare Tunnel).

Run `uv run invoke -l` to list tasks, `uv run invoke <task>` to run one.
This is host-side tooling only -- it shells out to podman/git and is never
copied into the app image (kept out via `uv sync --no-dev` in the Dockerfile).
The `gq` CLI (giveaway_quest/cli.py) is the separate, in-container app CLI.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from invoke import task

ROOT = Path(__file__).resolve().parent
PODMAN_COMPOSE = "podman-compose"

_ROTATE_SCRIPT = """
set -euo pipefail
mkdir -p data/backups/daily
latest=$(ls -t data/backups/giveaway-*.sqlite3 2>/dev/null | head -1)
today=$(date -u +%Y%m%d)
if [ -n "$latest" ] && [ -z "$(ls data/backups/daily/giveaway-${today}* 2>/dev/null)" ]; then
    cp "$latest" data/backups/daily/
fi
find data/backups/daily -name "giveaway-*.sqlite3" -mtime +30 -delete
"""


@task
def up(c):
    """Start the app + cloudflared containers (idempotent)."""
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} up -d")


@task
def down(c):
    """Stop and remove the app + cloudflared containers."""
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} down")


@task
def restart(c):
    """Restart the containers on the current image (no rebuild)."""
    down(c)
    up(c)


@task
def redeploy(c):
    """Pull latest code, rebuild the app image, and force the app container onto it.

    podman-compose does not recreate an already-running container just
    because its image changed, so `up -d --build` alone is not enough --
    --force-recreate is required, or the old code keeps serving silently.
    """
    with c.cd(str(ROOT)):
        # c.run("git pull")
        c.run(f"{PODMAN_COMPOSE} build app")
        c.run(f"{PODMAN_COMPOSE} up -d --force-recreate app")
        c.run("podman image prune -f")


@task
def logs(c, service="app", follow=True):
    """Tail container logs. `service` is 'app' or 'cloudflared'."""
    flag = "-f" if follow else ""
    c.run(f"podman logs {flag} giveaway-quest_{service}_1", pty=True)


@task
def status(c):
    """Show container and systemd unit status."""
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} ps")
    c.run("systemctl --user status giveaway-quest-compose.service --no-pager", warn=True)
    c.run("systemctl --user list-timers giveaway-quest-backup.timer --no-pager", warn=True)


@task
def backup(c):
    """Run one sqlite backup now: hourly snapshot inside the container, then
    host-side daily-copy/retention rotation.

    The rotation needs `podman unshare` because the app container owns
    data/backups under its rootless-mapped UID, not the host user's.
    """
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} exec -T app gq backup --keep 48")
        c.run(f"podman unshare bash -c {shlex.quote(_ROTATE_SCRIPT)}")
        remote = os.environ.get("BACKUP_RCLONE_REMOTE")
        if remote:
            c.run(f"podman unshare rclone sync data/backups {shlex.quote(remote)} --quiet")
