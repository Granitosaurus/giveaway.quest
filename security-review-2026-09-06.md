# Security & deployment review — 2026-09-06

Scope: the giveaway.quest code base as deployed on homek14 (rootless Podman
via `podman-compose`, Cloudflare Tunnel, live at https://giveaway.quest).
Reviewed: all of `giveaway_quest/`, templates, `Dockerfile`,
`docker-compose.yml`, `deploy/`, `tasks.py`, the host's firewall/podman
setup, and the live site's responses. This file is the record of what was
found, what was changed the same day, and what is still open. Regression
tests for the fixes live in `tests/test_security.py`.

## What was already right

Worth stating so nobody "fixes" it later:

- No inbound ports anywhere; `cloudflared` dials out, the app is only on the
  compose network (`expose`, never `ports`). Origin IP is not discoverable
  from DNS. HTTP → HTTPS is a 301 at the edge.
- Container runs as UID 10001, not root. `GQ_DEBUG=0`, a real
  `GQ_SECRET_KEY`, `openapi_config=None`.
- CSRF is enforced (double-submit token; a POST without it is a 403 live).
  OAuth `state` is random, kept in the encrypted session cookie, compared
  with `compare_digest` and popped on first use. Redirect targets are
  restricted to local paths and Litestar percent-encodes them, so `/\evil`
  tricks don't work either.
- Session cookie: encrypted, `HttpOnly`, `Secure`, `SameSite=Lax`, 30 days.
  Ban status is re-checked on every request.
- Quest markdown: `markdown-it` with html/images disabled, then `nh3` with an
  explicit tag/attribute allowlist; verified that `javascript:` links are
  dropped and `rel="noopener noreferrer"` is added.
- Static files router rejects traversal (`/static/../app.py` → 404).
- Winner is drawn with `secrets.choice`; the draw `UPDATE` is guarded by
  `drawn_at IS NULL`.
- `loginctl enable-linger` is on, the compose unit and the hourly backup
  timer are active, backups are sqlite online-backup snapshots.

## Findings fixed on 2026-09-06

### High

**H1. The public app container could reach tailnet-only services.**
From inside `giveaway-quest_app_1`, `http://100.74.250.95:8001/` (Windmill)
answered 200; CloakBrowser (:8091, no auth token by design) was only
unreachable because it wasn't running. An RCE/SSRF in the internet-facing app
would have landed inside the tailnet trust boundary. Rootless Podman uses
pasta, so on the host the traffic is just dex's sockets — you cannot filter
by container subnet. The pasta process (and every other rootless-podman
process) lives in the cgroup
`user.slice/user-1000.slice/user@1000.service/user.slice`, so the fix is an
`iptables -m cgroup --path` rule.
*Fix:* `networking.firewall.extraCommands` in `/etc/nixos/configuration.nix`
adds a `gq-rootless-egress` chain rejecting that cgroup's traffic to
`127.0.0.0/8`, RFC1918, `100.64.0.0/10` (tailnet), `169.254.0.0/16`, plus
the IPv6 equivalents, with MagicDNS (`100.100.100.100`,
`fd7a:115c:a1e0::53`) exempted so containers keep DNS. Verified after
`nixos-rebuild switch`: tailnet and LAN targets → "Connection refused",
`https://fosstodon.org` → 200, DNS works, tunnel healthy.
*Caveat:* this covers every rootless container dex runs, now and in future.
Root-run `oci-containers` (CloakBrowser) are in `system.slice`, unaffected.

**H2. Stored XSS via a hostile Mastodon instance.** `profile_url` and
`avatar_url` came straight from the remote server's `verify_credentials`
response and were rendered into `href`/`src` (`giveaway.html`, `base.html`,
`_card.html`). Anyone can run an instance that returns
`"url": "javascript:…"`, log in, create a giveaway, and every visitor who
clicked the host link would run script on the giveaway.quest origin as
themselves (steal CSRF token, enter/delete, read a won code).
*Fix:* `mastodon.account_from_json` accepts only `https://` URLs (falls back
to the constructed profile URL / no avatar) and only `[A-Za-z0-9_]{1,64}`
usernames, so an instance can't craft an `acct` like
`admin@fosstodon.org@evil.example` either. Tests:
`test_remote_profile_urls_must_be_https`, `test_remote_username_is_validated`.

### Medium

**M1. No security headers.** Live responses had no HSTS, CSP,
`X-Content-Type-Options`, `Referrer-Policy` or framing protection.
*Fix:* `app.security_headers_middleware` (raw ASGI so 404/403/429 pages get
them too — Litestar's `response_headers` skips exception-handler responses)
sets: CSP `default-src 'self'; script-src 'self'; style-src 'self';
img-src 'self' https: data:; connect-src 'self'; font-src 'self';
object-src 'none'; base-uri 'self'; form-action 'self' https:;
frame-ancestors 'none'`, HSTS (1 year, includeSubDomains) when the base URL
is https, `nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`, a minimal
`Permissions-Policy`. To make `script-src 'self'` possible the two inline
scripts moved to `static/theme.js` / `static/app.js` and the one inline
`onsubmit` became `form[data-confirm]`. Verified live on `/` and `/nope`.
Note `form-action` must allow `https:`: browsers apply it to the redirect
that follows `POST /auth/login`, which goes to the user's instance.

**M2. Host secrets readable by any local user.** `.env` (tunnel token,
announce token, signing key) was 0644; `data/` and the sqlite file were
world-readable. *Fix:* `chmod 600 .env`, `podman unshare chmod 700 data`.
README deployment steps updated.

**M3. A production database copy inside the repo, and no commits.**
`dev-backup-2026-09-06-data/` (a full DB with the then-stored tokens) was
untracked but not ignored by `.gitignore` or `.dockerignore`, so `git add .`
would have committed it and every image build shipped it in the build
context. The repo also had zero commits and no remote.
*Fix:* moved to `~/giveaway-quest-dev-backup-2026-09-06-data/` (delete it
when you no longer need it — it still contains the old `access_token`
column), `dev-backup-*/` added to both ignore files, first commit made.
**Still to do: add a private remote and push** (see Remaining).

**M4. User OAuth tokens stored but never used.** Every login wrote the
`read:accounts` token to `users.access_token`; nothing read it, but it was
in the DB and every backup. *Fix:* the callback revokes the token
(`POST /oauth/revoke`, best effort) right after reading the profile and
never stores it; schema migration v1 (`PRAGMA user_version`, `db._migrate`)
dropped the column from the live database. Tests:
`test_login_token_is_revoked_and_never_stored`,
`test_migration_drops_stored_tokens`.

**M5. No rate limiting.** `POST /auth/login` makes the server `POST` to any
HTTPS host the caller names (and inserts a `mastodon_apps` row per host);
`POST /new` was unlimited; slow instances × 15 s timeout could exhaust the
sync thread pool. *Fix:* Litestar `RateLimitConfig` attached to those two
handlers, keyed by `path:client-ip` (real IP via `X-Forwarded-For` from
cloudflared), `GQ_RATE_LIMIT` requests/minute (default 10, `0` disables —
the tests do that). Verified live: the 11th POST in a minute is a 429.

**M6. The site's own account could be made to mention/tag/link.**
`notify_winner` put the host-written title verbatim into the DM from
`@giveaway_quest`; a title like `@victim@server …` mentions that person
from the official account. Same for the default `gq announce` text.
*Fix:* `services.neutralize_for_post` (full-width `＠`/`＃`, URL schemes
stripped) applied to the title in the DM and to the auto-generated announce
text (`--text` is trusted as-is). Test:
`test_official_account_posts_cannot_mention_or_link`.

## Remaining (not fixed today, by priority)

1. **Push the repo to a private remote.** Code and config currently exist
   only on homek14's disk. Also decide on `master` vs `main` (the checkout
   is on `master`; tooling here assumes `main`).
2. **Off-site, encrypted backups.** Hourly + daily snapshots live on the same
   disk as the database. Backups are plaintext sqlite (giveaway codes,
   OAuth client secrets). Set `BACKUP_RCLONE_REMOTE` to an `rclone crypt`
   remote — note it must go in the backup unit's `Environment=` line,
   `deploy/giveaway-quest-backup.service` runs with no env — and do one
   restore drill; write the steps into README.
3. **Harden the container spec.** Add to the `app` service in
   `docker-compose.yml`: `cap_drop: [ALL]`,
   `security_opt: ["no-new-privileges:true"]`, `read_only: true` with a
   `tmpfs: [/tmp]` (the app only writes `/data`). Consider the same for
   `cloudflared`.
4. **Pin the supply chain.** `cloudflared:latest`, `TAILWIND_VERSION=latest`,
   unpinned `pip install uv`, and both Tailwind/daisyUI binaries fetched by
   `curl` with no checksum. Pin versions and verify sha256 in the Dockerfile;
   pin `cloudflared` to a tag and bump deliberately.
5. **Cloudflare-side rate limiting / WAF.** The app-level limiter works but
   costs a request to the origin; the free plan's one rate-limiting rule on
   `POST /auth/login` and `POST /new` would stop floods at the edge. Also
   worth enabling: Bot Fight Mode, and HSTS at the edge as a second copy of
   the header.
6. **Multi-worker footgun.** `gq serve --workers N>1` starts N drawer loops.
   Either guard with a lock/`PRAGMA`-based claim or document "1 worker only".
7. **`forwarded_allow_ips="*"`** is only safe while port 8000 is never
   published. If a `ports:` line ever appears, client IPs (and the rate
   limiter's key) become spoofable. Prefer `CF-Connecting-IP` or the compose
   subnet.
8. **Error text leaks a little.** `MastodonError` messages include the
   underlying `httpx` exception text shown to the user
   (`Could not register with x: …`). Harmless today; consider a generic
   message + server-side log.
9. **Healthcheck doesn't restart.** The Dockerfile `HEALTHCHECK` marks the
   container unhealthy but nothing acts on it. `podman-compose` has no
   auto-heal; a `systemd` timer running `podman healthcheck run` + restart,
   or an external uptime check hitting `/robots.txt`, would close that.
10. **Privacy note.** The site stores account handles, display names, avatar
    URLs, account creation dates and entry history. A short "what we store,
    how to get it deleted" paragraph on the login page (and `gq admin`
    support for deleting a user) is cheap and expected on the Fediverse.
11. **Delete the moved DB copy** at
    `~/giveaway-quest-dev-backup-2026-09-06-data/` when it's no longer
    useful; it contains the pre-migration `access_token` column.

## Other notes

- The live secret key is fine; if it is ever rotated every session and CSRF
  cookie is invalidated at once, which is the intended behaviour.
- `mastodon_apps` grows by one row per distinct instance anyone types into
  the login box; rate limiting bounds the rate, not the total. A periodic
  `DELETE FROM mastodon_apps WHERE instance NOT IN (SELECT instance FROM
  users)` would keep it tidy; not a security issue.
- The search box's `LIKE` accepts `%`/`_` wildcards from the user. Not
  injection (parameterised), just slightly surprising results.
- Cloudflare sets a `csrftoken` cookie on every response including
  `robots.txt`, which also means nothing is edge-cached
  (`cf-cache-status: DYNAMIC/BYPASS`). Fine at this scale.
- The `Set-Cookie: csrftoken` is intentionally not `HttpOnly` — `app.js`
  reads it to send the quest-preview request.
- SSRF via the login box is bounded by TLS: the server only ever speaks
  HTTPS to port 443 with certificate verification, so an attacker-owned
  hostname pointing at a private IP fails the handshake unless that internal
  service presents a valid cert for the attacker's name. With H1's egress
  rule it can no longer reach private ranges at all.
- The nix rebuild for H1 also backed up the previous config as
  `/etc/nixos/configuration.nix.bak.20260906165626`.
