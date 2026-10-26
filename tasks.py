"""Ops tasks for the stork deployment (rootless Podman + Cloudflare Tunnel).

Run `uv run invoke -l` to list tasks, `uv run invoke <task>` to run one.
This is host-side tooling only -- it shells out to podman/git and is never
copied into the app image (kept out via `uv sync --no-dev` in the Dockerfile).
The `gq` CLI (giveaway_quest/cli.py) is the separate, in-container app CLI.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shlex
from pathlib import Path

import httpx
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
        # GQ_VERSION too, or a boot-time `up` (giveaway-quest-compose.service)
        # recreates the app with it blank and `deploy` can't tell what's running.
        c.run(f"{PODMAN_COMPOSE} up -d", env={"GQ_VERSION": _checkout_version(c)})


def _checkout_version(c) -> str:
    version = c.run("git describe --tags --always --dirty", hide=True).stdout.strip()
    return version.removeprefix("v")


@task
def down(c):
    """Stop and remove the app + cloudflared containers."""
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} down")


@task
def restart(c):
    """Recreate the containers on the current image (no rebuild), through the
    compose unit so they stay owned by it (see `_build_and_restart`)."""
    c.run(f"systemctl --user restart {COMPOSE_UNIT}")


@task
def redeploy(c):
    """Rebuild the app image from the current checkout and restart onto it.
    No git changes -- production updates by release tag via `deploy`; this is
    for rebuilding what's already checked out.
    """
    with c.cd(str(ROOT)):
        _build_and_restart(c)


COMPOSE_UNIT = "giveaway-quest-compose.service"


def _build_and_restart(c) -> None:
    """Build the app image, then recreate the containers by restarting the
    compose unit -- never with `podman-compose up` from here.

    A container's conmon (its monitor process) stays in the cgroup of
    whatever started it. Started from `deploy`, that's the oneshot
    giveaway-quest-deploy.service, and systemd kills the conmon when that
    unit finishes -- the app keeps serving but podman loses its exit status
    and logs. The compose unit (RemainAfterExit) is where boot puts them
    anyway. Its `down`+`up` recreates every container from
    docker-compose.yml, so a new app image or a Renovate cloudflared bump is
    always picked up (plain `up -d` never moves a running container to a new
    image), and `up` sets GQ_VERSION from the checkout -- the release tag.
    """
    c.run(f"{PODMAN_COMPOSE} build app")
    c.run(f"systemctl --user restart {COMPOSE_UNIT}")
    c.run("podman image prune -f")
    print(f"deployed {_checkout_version(c)}")


APP_CONTAINER = "giveaway-quest_app_1"


def _running_version(c) -> str | None:
    """GQ_VERSION of the live app container -- what's actually serving, which
    can differ from the checkout if a build failed after checking out a tag."""
    result = c.run(
        "podman inspect --format '{{range .Config.Env}}{{println .}}{{end}}' " + APP_CONTAINER,
        hide=True,
        warn=True,
    )
    for line in result.stdout.splitlines() if result.ok else []:
        if line.startswith("GQ_VERSION="):
            return line.removeprefix("GQ_VERSION=")
    return None


@task
def deploy(c, force=False):
    """Deploy the newest release tag, if it isn't what's already running.

    Production's only way to update: run every few minutes by
    deploy/giveaway-quest-deploy.timer (as dex on stork), and kicked
    immediately by `release` when GQ_DEPLOY_HOST is set. Fetches over the
    anonymous HTTPS remote -- the production host holds no GitHub
    credentials. Only tags whose commit is on origin/main are deployed, and
    who may create `v*` tags / push `main` is restricted by GitHub rulesets,
    so a sandboxed agent (or anyone else) with a PR-only token can't ship.
    """
    with c.cd(str(ROOT)):
        # --force: production's tags mirror GitHub's (where v* tags can't be
        # moved, see the ruleset), so a stale local tag -- e.g. left from
        # before a history rewrite -- must not block every future deploy.
        # Not --quiet, so a fetch failure says why in the journal.
        c.run("git fetch --force --tags origin main")
        tags = c.run("git tag --list 'v*' --sort=-v:refname", hide=True).stdout.split()
        tag = tags[0] if tags else None
        if not tag:
            raise SystemExit("no v* release tags found")
        if not c.run(f"git merge-base --is-ancestor {tag} origin/main", warn=True, hide=True).ok:
            raise SystemExit(f"{tag} is not on origin/main -- refusing to deploy it")
        if _running_version(c) == tag.removeprefix("v") and not force:
            print(f"{tag} already running")
            return
        if c.run("git status --porcelain --untracked-files=no", hide=True).stdout.strip():
            raise SystemExit("production checkout has local changes -- not checking out over them")
        c.run(f"git checkout --quiet --detach {tag}")
        _build_and_restart(c)


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

NEW_MD = ROOT / "NEW.md"
_NEW_MD_TEMPLATE = """<!--
Add a bullet point per notable change as you work. `uv run invoke release`
turns everything below this comment into the GitHub release notes for the
next tag, then empties this file back to just this comment.
-->
"""


def _changelog_entries() -> str:
    """NEW.md's content with its instructional HTML comment stripped. Empty if
    nobody added anything since the last release."""
    return re.sub(r"<!--.*?-->", "", NEW_MD.read_text(), flags=re.S).strip()


def _github_repo(c) -> tuple[str, str]:
    url = c.run("git remote get-url origin", hide=True).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/(.+?)(\.git)?$", url)
    if not match:
        raise SystemExit(f"could not parse a GitHub owner/repo out of remote {url!r}")
    return match.group(1), match.group(2)


def _create_github_release(c, tag: str, notes: str) -> None:
    """POST a GitHub release for `tag` with `notes` as its body.

    Needs `GH_TOKEN` in `.env` -- a fine-grained PAT scoped to just this repo
    with "Contents: read and write" permission (that's what release creation
    needs; nothing broader). See NOTES.md for why this isn't `gh auth login`.
    Never raises: the tag and push already happened by the time this runs, so
    a release failure here only warns instead of undoing that.
    """
    token = os.environ.get("GH_TOKEN")
    if not token:
        print("GH_TOKEN not set in .env -- skipped the GitHub release, tag was still pushed")
        return
    owner, repo = _github_repo(c)
    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{owner}/{repo}/releases",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={"tag_name": tag, "name": tag, "body": notes},
            timeout=15,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"GitHub release creation failed ({exc}) -- tag was still pushed")


@task
def release(c, part):
    """Bump the version (major|minor|patch), tag it, push, and cut a GitHub
    release from NEW.md. Doesn't deploy: production picks the new tag up via
    `deploy` (its timer), or immediately if GQ_DEPLOY_HOST is set, which
    makes this trigger stork's deploy service over ssh.

    Runs from a dev checkout (raven) or the `release` GitHub Actions
    workflow -- never on the production host, which holds no GitHub
    credentials.

    Refuses on a dirty working tree or a branch that isn't pushed, since the
    git tag is meant to be the source of truth for "what's actually deployed"
    -- a tag made from unpushed or uncommitted state would lie about that.

    NEW.md is the running changelog: add a bullet per notable change as you
    work. Whatever is in it becomes the GitHub release's notes, then the
    file is emptied back to its instructional comment as part of the
    version-bump commit. An empty NEW.md, or no GH_TOKEN in .env, just skips
    the GitHub release -- the tag is still cut and pushed either way.
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

        notes = _changelog_entries()

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
        # Keep the lockfile's recorded project version in sync too, or it
        # drifts on the next `uv sync`/`nix develop` and leaves the tree dirty.
        c.run("uv lock", hide=True)
        NEW_MD.write_text(_NEW_MD_TEMPLATE)

        c.run(f"git add {pyproject} {init_py} {ROOT / 'uv.lock'} {NEW_MD}")
        c.run(f'git commit -m "release {tag}"')
        c.run(f'git tag -a {tag} -m "release {tag}"')
        c.run("git push")
        c.run(f"git push origin {tag}")

        if notes:
            _create_github_release(c, tag, notes)
        else:
            print("NEW.md was empty -- tag pushed, no GitHub release created")

    deploy_host = os.environ.get("GQ_DEPLOY_HOST")
    if deploy_host:
        host = shlex.quote(deploy_host)
        c.run(f"ssh {host} systemctl --user start giveaway-quest-deploy.service")
    else:
        print(f"pushed {tag} -- production deploys it within 5 min (giveaway-quest-deploy.timer)")


@task
def gq(c, cmd):
    """Run a `gq` (in-container admin CLI) command against the live container.

    `cmd` is the whole `gq` command line as one quoted string, e.g.
    `uv run invoke gq "admin reopen-empty --yes"`.
    """
    with c.cd(str(ROOT)):
        c.run(f"{PODMAN_COMPOSE} exec app gq {cmd}", pty=True)


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


DOCKERFILE = ROOT / "Dockerfile"
FLAKE_NIX = ROOT / "flake.nix"
_TAILWIND_URL = (
    "https://github.com/tailwindlabs/tailwindcss/releases/download/{version}/tailwindcss-{arch}"
)
_DAISYUI_URL = "https://github.com/saadeghi/daisyui/releases/download/v{version}/{name}"


def _sha256(url: str) -> bytes:
    resp = httpx.get(url, follow_redirects=True, timeout=60)
    resp.raise_for_status()
    return hashlib.sha256(resp.content).digest()


def _set_dockerfile_arg(text: str, name: str, value: str) -> str:
    text, count = re.subn(rf"^ARG {name}=\S+$", f"ARG {name}={value}", text, flags=re.M)
    if count != 1:
        raise SystemExit(f"could not find ARG {name} in {DOCKERFILE}")
    return text


@task
def update_asset_hashes(c):
    """Recompute the pinned sha256s of the Tailwind/daisyUI downloads.

    Renovate bumps TAILWIND_VERSION/DAISYUI_VERSION (Dockerfile) and
    daisyuiVersion (flake.nix) but can't recompute checksums, so the image
    build fails on those PRs until this is run on the branch. It trusts
    whatever the release currently serves -- review the diff before pushing.
    """
    dockerfile = DOCKERFILE.read_text()
    tailwind = re.search(r"^ARG TAILWIND_VERSION=(\S+)$", dockerfile, re.M).group(1)
    daisyui = re.search(r"^ARG DAISYUI_VERSION=(\S+)$", dockerfile, re.M).group(1)
    flake = FLAKE_NIX.read_text()
    flake_daisyui = re.search(r'daisyuiVersion = "([^"]+)";', flake).group(1)
    if flake_daisyui != daisyui:
        raise SystemExit(f"daisyUI differs: Dockerfile {daisyui}, flake.nix {flake_daisyui}")

    for arg, arch in (
        ("TAILWIND_SHA256_AMD64", "linux-x64"),
        ("TAILWIND_SHA256_ARM64", "linux-arm64"),
    ):
        digest = _sha256(_TAILWIND_URL.format(version=tailwind, arch=arch))
        dockerfile = _set_dockerfile_arg(dockerfile, arg, digest.hex())
    for arg, name in (
        ("DAISYUI_SHA256", "daisyui.mjs"),
        ("DAISYUI_THEME_SHA256", "daisyui-theme.mjs"),
    ):
        digest = _sha256(_DAISYUI_URL.format(version=daisyui, name=name))
        dockerfile = _set_dockerfile_arg(dockerfile, arg, digest.hex())
        sri = "sha256-" + base64.b64encode(digest).decode()
        flake, count = re.subn(rf'(/{re.escape(name)}";\s*hash = )"[^"]+"', rf'\g<1>"{sri}"', flake)
        if count != 1:
            raise SystemExit(f"could not find the {name} hash in {FLAKE_NIX}")

    DOCKERFILE.write_text(dockerfile)
    FLAKE_NIX.write_text(flake)
    print(f"hashes updated for tailwind {tailwind}, daisyUI {daisyui} -- review `git diff`")
