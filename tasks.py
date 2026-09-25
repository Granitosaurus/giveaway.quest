"""Ops tasks for the homek14 deployment (rootless Podman + Cloudflare Tunnel).

Run `uv run invoke -l` to list tasks, `uv run invoke <task>` to run one.
This is host-side tooling only -- it shells out to podman/git and is never
copied into the app image (kept out via `uv sync --no-dev` in the Dockerfile).
The `gq` CLI (giveaway_quest/cli.py) is the separate, in-container app CLI.
"""

from __future__ import annotations

import os
import re
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
def redeploy(c, version=None):
    """Pull latest code, rebuild the app image, and force the app container onto it.

    podman-compose does not recreate an already-running container just
    because its image changed, so `up -d --build` alone is not enough --
    --force-recreate is required, or the old code keeps serving silently.

    `version` is passed into the container as GQ_VERSION, shown in the footer
    (and the static-asset cache-busting query string) so you can actually tell
    a redeploy took effect. Defaults to `git describe`, so even a redeploy
    between tagged releases is distinguishable; `release` passes the tag it
    just made instead.
    """
    with c.cd(str(ROOT)):
        c.run("git pull")
        if version is None:
            version = c.run("git describe --tags --always --dirty", hide=True).stdout.strip()
        version = version.removeprefix("v")
        c.run(f"{PODMAN_COMPOSE} build app")
        c.run(f"{PODMAN_COMPOSE} up -d --force-recreate app", env={"GQ_VERSION": version})
        c.run("podman image prune -f")
    print(f"deployed {version}")


def _read_version(path: Path, pattern: str) -> tuple[int, int, int]:
    match = re.search(pattern, path.read_text(), re.M)
    if not match:
        raise SystemExit(f"could not find a version in {path}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _write_version(path: Path, pattern: str, replacement: str) -> None:
    text, count = re.subn(pattern, replacement, path.read_text(), count=1, flags=re.M)
    if count != 1:
        raise SystemExit(f"could not update version in {path}")
    path.write_text(text)


_PYPROJECT_VERSION_RE = r'^version = "(\d+)\.(\d+)\.(\d+)"'
_INIT_VERSION_RE = r'^__version__ = "(\d+)\.(\d+)\.(\d+)"'


@task
def release(c, part):
    """Bump the version (major|minor|patch), tag it, push, and redeploy with that tag.

    Refuses on a dirty working tree or a branch that isn't pushed, since the
    git tag is meant to be the source of truth for "what's actually deployed"
    -- a tag made from unpushed or uncommitted state would lie about that.
    """
    if part not in ("major", "minor", "patch"):
        raise SystemExit(f"part must be major, minor or patch, not {part!r}")

    with c.cd(str(ROOT)):
        if c.run("git status --porcelain", hide=True).stdout.strip():
            raise SystemExit("working tree is dirty -- commit or stash first")
        c.run("git fetch origin", hide=True)
        local = c.run("git rev-parse HEAD", hide=True).stdout.strip()
        try:
            upstream = c.run("git rev-parse @{u}", hide=True).stdout.strip()
        except Exception as exc:
            raise SystemExit("current branch has no upstream to compare against") from exc
        if local != upstream:
            raise SystemExit("local branch is not in sync with its upstream -- pull/push first")

        pyproject = ROOT / "pyproject.toml"
        init_py = ROOT / "giveaway_quest" / "__init__.py"
        major, minor, patch = _read_version(pyproject, _PYPROJECT_VERSION_RE)
        if part == "major":
            major, minor, patch = major + 1, 0, 0
        elif part == "minor":
            minor, patch = minor + 1, 0
        else:
            patch += 1
        new_version = f"{major}.{minor}.{patch}"
        tag = f"v{new_version}"

        _write_version(pyproject, _PYPROJECT_VERSION_RE, f'version = "{new_version}"')
        _write_version(init_py, _INIT_VERSION_RE, f'__version__ = "{new_version}"')

        c.run(f"git add {pyproject} {init_py}")
        c.run(f'git commit -m "release {tag}"')
        c.run(f'git tag -a {tag} -m "release {tag}"')
        c.run("git push")
        c.run(f"git push origin {tag}")

    redeploy(c, version=tag)


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
