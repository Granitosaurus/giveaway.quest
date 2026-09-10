# giveaway.quest

Give away spare game keys and other digital codes to *real* people on the
Fediverse. Hosts log in with Mastodon, write up the reward (a code, or Markdown
with some flavor text), set a deadline and a little quest ("pet a cat");
participants log in with Mastodon, agree to the conditions and enter; at the
deadline the site picks a winner at random and DMs them from the giveaway.quest
account. The winner has two days to claim the reward (a button reveals it); if
they don't, it's re-drawn among the other entrants, or the giveaway reopens if
nobody is left. Turn that off per-giveaway with the "restart if unclaimed" box.
Logging in only ever reads your profile &mdash; the site never posts as you.

Stack: Python 3.13 · [Litestar](https://litestar.dev) · SQLite · Jinja ·
Tailwind v4 + [daisyUI](https://daisyui.com) (no JS framework, one tiny inline
script for countdowns) · [cyclopts](https://github.com/BrianPugh/cyclopts) CLI.

## Development

Everything is provided by the Nix flake (Python, uv, Tailwind standalone CLI
with daisyUI, sqlite, ruff):

```sh
cp .env.example .env          # edit GQ_SECRET_KEY at least
nix develop                   # runs `uv sync` on entry
dev                           # build CSS and run http://127.0.0.1:8000 with reload
```

Other things available inside the shell:

```sh
build-css --watch             # rebuild the stylesheet as templates change
uv run pytest                 # end-to-end tests against a fake Mastodon
ruff check . && ruff format . # lint / format
uv run gq --help              # admin CLI (see below)
```

If your Nix does not have flakes enabled:
`nix --extra-experimental-features 'nix-command flakes' develop`.

### Testing the Mastodon login locally

Mastodon redirects back to `GQ_BASE_URL/auth/callback`, so the base URL has to
be reachable from your browser. `http://127.0.0.1:8000` works with any real
Mastodon server as long as the browser you use is on the same machine (or use
the Tailscale IP). The OAuth app is registered automatically on each server
the first time someone from it logs in and is cached in the database.

The only scope requested from a user is `read:accounts` (who are you, how old
is the account). The site never posts, follows or messages as a logged-in
user. The login token is used once to read the profile, then revoked and
discarded — it is never stored. Announcements (auto-posted for every listed
giveaway, or `gq announce`), winner DMs and the on-page comment threads all go
through the site's own account, configured via `GQ_ANNOUNCE_INSTANCE` /
`GQ_ANNOUNCE_TOKEN` (an app token on that account with the `write:statuses` and
`read:statuses` scopes). The database still holds giveaway rewards and
per-instance OAuth client secrets; treat it (and backups) as secret.

## Configuration

Environment variables (a `.env` file in the working directory is loaded):

| Variable        | Default                 | Meaning                                                    |
| --------------- | ----------------------- | ---------------------------------------------------------- |
| `GQ_BASE_URL`   | `http://localhost:8000` | Public URL, used for OAuth redirect, links, sitemap        |
| `GQ_SECRET_KEY` | `change-me`             | Signs session + CSRF cookies. Generate a long random value |
| `GQ_DATA_DIR`   | `./data`                | Holds `giveaway.sqlite3` and `backups/`                    |
| `GQ_DEBUG`      | off                     | `1` for tracebacks in responses                            |
| `GQ_ADMINS`     | empty                   | Comma separated `user@instance` that can see hidden posts   |
| `GQ_ANNOUNCE_INSTANCE` | empty            | Host of the site's own Mastodon account                    |
| `GQ_ANNOUNCE_TOKEN`    | empty            | Its `write:statuses`+`read:statuses` token; enables `gq announce`, winner DMs, comments |
| `GQ_COMMENTS`   | `1`                     | Show announcement replies as comments; `0` disables        |
| `GQ_AUTO_ANNOUNCE` | `1`                  | Auto-announce listed giveaways from the site account; `0` = `gq announce` only |
| `GQ_RATE_LIMIT` | `10`                    | Per-IP POSTs per minute on `/auth/login` and `/new`; `0` disables |

## Admin CLI

```sh
gq serve [--host 0.0.0.0] [--port 8000]   # run the server
gq draw                                  # draw overdue giveaways + re-draw unclaimed ones (server does it every 30s too)
gq announce <slug> [--text "..."]        # post an announcement from the site's Mastodon account
gq backup [--keep 48]                    # snapshot the sqlite db into data/backups/
gq admin list [--all] [--hidden] [-q x]  # H=hidden L=listed D=drawn
gq admin show <slug>                     # everything incl. the reward and entrants
gq admin hide <slug> / unhide <slug>     # soft moderation (host still sees it)
gq admin delete <slug> --yes             # hard delete
gq admin ban user@instance [--unban]     # blocks login, entering and winning
gq admin users
```

See `NOTES.md` for design decisions, Litestar/NixOS gotchas and follow-ups.

## How it fits together

- `giveaway_quest/app.py` builds the Litestar app: cookie sessions, CSRF,
  Jinja, static files, and a background task that draws overdue giveaways,
  re-draws unclaimed rewards, auto-announces listed ones and keeps the comment
  caches warm.
- `routes/auth.py` is the Mastodon OAuth flow (dynamic app registration per
  instance), `routes/pages.py` the HTML pages, `routes/meta.py` robots.txt and
  sitemap.xml.
- `services.py` holds all domain logic (validation, eligibility, drawing,
  notifications) on top of `db.py` (plain sqlite, one connection per unit of
  work, WAL mode, online backups).
- `mastodon.py` is the small httpx client. Endpoints used: `/api/v1/apps`,
  `/oauth/token`, `/api/v1/accounts/verify_credentials` for login,
  `/api/v1/statuses` for posts made by the site's own account (announcements,
  winner DMs), and `/api/v1/statuses/:id/context` (as that account) to pull the
  announcement's reply thread for the comments shown on each giveaway page.
  `share_url()` just builds a prefilled `share.joinmastodon.org` link.
- Giveaway ids are `coolname` slugs (`blue-jelly-cat` style).
- Giveaway pages carry OpenGraph tags, so the Mastodon post shows a link card.

Only text is ever accepted from users (title, quest, conditions), all escaped
by Jinja; there are no uploads.

## Deployment sketch

Runs on homek14 (the always-on home NixOS box) behind [Cloudflare
Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
not a VPS. Two containers, no inbound ports opened on the router at all: the
app (built from the `Dockerfile`, non-root, only reachable on the compose
network) and `cloudflared`, which makes an *outbound* connection to
Cloudflare's edge and tunnels `giveaway.quest` traffic back to `app:8000`.
Cloudflare's edge terminates public TLS — the tunnel and the compose network
only ever carry plain HTTP internally. Containers run via Podman (already
used for CloakBrowser on this box), driven with `podman-compose` against the
same `docker-compose.yml` docker would use.

1. Sign up at Cloudflare (free plan) and add `giveaway.quest` as a site —
   it'll give you two nameservers. Update those at Porkbun (Domain settings ->
   Nameservers). NS changes can take anywhere from minutes to most of a day
   to fully propagate through caching resolvers.
2. In the Cloudflare dashboard, go to Zero Trust -> Networks -> Tunnels ->
   Create a tunnel (name it e.g. `giveaway-quest`, connector type "Docker").
   Copy the token out of the install command it shows you — the long base64
   string after `--token`, not the short tunnel-ID UUID shown elsewhere on
   the page.
3. Route it to the app. Cloudflare has renamed this screen more than once —
   look for **Published application routes** (older UIs call it "Public
   Hostname"). A separate **Hostname routes** tab also exists but is for
   private-network/WARP routing, not this — it won't have a service/URL
   field. Add: Hostname `giveaway.quest`, Service `HTTP` -> `app:8000`; if it
   asks about an Access policy, pick the public/no-auth option.
4. On homek14: `git clone … ~/projects/giveaway-quest && cd ~/projects/giveaway-quest`
5. `cp .env.example .env` and edit it: `GQ_BASE_URL=https://giveaway.quest`,
   a real `GQ_SECRET_KEY` (`python3 -c 'import secrets;print(secrets.token_hex(32))'`),
   the `CLOUDFLARE_TUNNEL_TOKEN` from step 2, plus `GQ_ANNOUNCE_*`/`GQ_ADMINS`
   as needed. Leave `GQ_DATA_DIR` alone — compose sets it to `/data` inside
   the container regardless.
6. `chmod 600 .env` — it holds three secrets and nothing but you and
   `podman-compose` (which runs as you) needs to read it.
7. `mkdir -p data && podman unshare chown -R 10001:10001 data && podman unshare chmod 700 data` — the app
   container runs as an unprivileged UID (10001); under rootless Podman that
   maps to a subordinate host UID, so `podman unshare` (not plain `chown`) is
   what sets ownership correctly for the bind mount, and it needs `-R` since
   this recurses into `backups/` too. The `chmod 700` keeps other host
   users out of the database (it holds giveaway codes).
8. Install `podman-compose` once: `nix profile install nixpkgs#podman-compose`,
   then `uv sync` to pull in `invoke` (the ops task runner, see below).
   `uv run invoke up`; check `uv run invoke logs --service=cloudflared` for
   "Registered tunnel connection", and confirm `https://giveaway.quest` loads.
9. Make it survive reboots — everything below runs as your own user (`dex`),
   not root, since these are rootless Podman containers:
   - `loginctl enable-linger dex` — lets your user's systemd instance keep
     running (and start at boot) without an active login session.
   - `mkdir -p ~/.config/systemd/user && cp deploy/giveaway-quest-compose.service deploy/giveaway-quest-backup.{service,timer} ~/.config/systemd/user/`
   - `systemctl --user daemon-reload`
   - `systemctl --user enable --now giveaway-quest-compose.service giveaway-quest-backup.timer`

## Maintaining it

All host-side ops (start/stop/redeploy/backup) go through `tasks.py`
([invoke](https://www.pyinvoke.org/)) — run `uv run invoke -l` to list tasks,
or open `tasks.py` to read what each one does. This is separate from `gq`
(`giveaway_quest/cli.py`): `gq` is the app's own CLI and runs *inside* the
container; `tasks.py` runs on the host and shells out to `podman-compose`,
`git`, etc., so it's a dev-only dependency, never copied into the image.

- **List tasks**: `uv run invoke -l`
- **Status**: `uv run invoke status` (containers + the systemd units)
- **Logs**: `uv run invoke logs` (app) or `uv run invoke logs --service=cloudflared`
- **Restart on the current image**: `uv run invoke restart`
- **Stop everything**: `uv run invoke down`
- **Ship new code**: `uv run invoke redeploy` — pulls, rebuilds the app
  image, and force-recreates the app container. This one matters: plain
  `podman-compose up -d --build` does *not* recreate an already-running
  container just because its image changed, so the old code would keep
  serving silently without the `--force-recreate` that `redeploy` does.
- **Run a backup now**: `uv run invoke backup`; `systemctl --user list-timers`
  shows the next scheduled hourly run.
- The two systemd units (`giveaway-quest-compose.service`,
  `giveaway-quest-backup.timer`) call `uv run invoke up`/`down`/`backup`
  under the hood — reboots need no manual steps.

Backups are consistent sqlite snapshots made with the backup API, so they
are safe to copy off-site with rsync/rclone from `data/backups/`.

### Host firewall: containers can't reach the tailnet

`/etc/nixos/configuration.nix` carries a `networking.firewall.extraCommands`
block (`gq-rootless-egress` chain) that rejects traffic from *any* of dex's
rootless Podman containers to loopback, RFC1918, the tailnet (`100.64.0.0/10`)
and link-local ranges, with MagicDNS exempted. Without it the public app
container could reach Windmill/CloakBrowser on the Tailscale IP. If a
container ever legitimately needs a private destination, add a `RETURN` rule
for it there. Verify after changes with
`podman exec giveaway-quest_app_1 python -c "import urllib.request as u; u.urlopen('http://100.74.250.95:8001/', timeout=3)"`
— it must fail with "Connection refused". Background and remaining items:
`security-review-2026-09-06.md`.

`deploy/giveaway-quest.service` and `deploy/Caddyfile` are a non-container,
directly-exposed-VPS alternative kept for reference; you don't need either
alongside `docker-compose.yml` + Cloudflare Tunnel.
