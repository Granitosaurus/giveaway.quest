# Implementation notes

Companion to `README.md` (how to run it). This file records *why* things are
the way they are and what was learned while building the first iteration
(2026-09-04), so the next round doesn't rediscover it.

## Architecture decisions

**One process, one sqlite file.** Expected volume is tiny. `db.py` opens a
fresh `sqlite3` connection per unit of work (`with db.connect() as conn:`),
`BEGIN`/`COMMIT` explicitly, WAL journal, `busy_timeout=10s`, foreign keys on.
No ORM, no migration tool: `SCHEMA` is `CREATE TABLE IF NOT EXISTS` statements.
When the schema needs to change, add `ALTER TABLE` steps to `init_db()` guarded
by a `PRAGMA user_version` check.

**All handlers are sync with `sync_to_thread=True`.** sqlite and httpx are
both blocking; running handlers in Litestar's thread pool keeps the event loop
free without mixing async/sync styles. The only async code is the drawer loop
in `app.py`, which calls `services.draw_due` via `anyio.to_thread`.

**Domain logic lives in `services.py`, not in routes.** Route handlers parse
input, call a service function inside a connection, flash a message and
redirect. Everything testable is in `services.py` and takes a connection as
its first argument.

**Timestamps** are ISO-8601 UTC strings with a `Z` suffix (`db.iso()`), so
lexical order equals chronological order and sqlite comparisons work on text.
Use `db.parse_iso()` to get aware datetimes back.

**Durations, not deadlines, in forms.** The create form asks "runs for N
hours" and the edit form "ends in N hours from now". This avoids asking the
browser for a timezone (which would need JS) and avoids `datetime-local`
ambiguity. Absolute times are shown as UTC server-side and localised by the
tiny inline script if JS is on.

**Slugs** come from `coolname.generate_slug(3)`, filtered to the
`adjective-adjective-noun` shape (coolname also emits `x-of-y` forms, which
were rejected as too long). Uniqueness is checked against the table.

**Giveaway status is derived**, not stored: `open` (before `ends_at`),
`drawing` (past `ends_at`, `drawn_at` still null, the background task will
pick it up within 30 s), `ended` (`drawn_at` set). Winner is `winner_id`,
which can be null after drawing if nobody entered.

**Winner notification is best effort.** `notify_winner` posts a
`visibility=direct` status from the *site's own* account
(`GQ_ANNOUNCE_*`) mentioning the winner. If that account isn't configured or
the post fails, the DM is skipped, `winner_notified_at` stays null, and the
host sees so on the page. The site is the source of truth: the winner sees
the reward when they log in, and `reward_viewed_at` is set on first load. A
Mastodon "direct" post is not private (both instances' admins can read it),
which is why the reward itself never travels in the DM — only a link.

**The winner claims the reward; unclaimed rewards are re-drawn.** After the
draw the winner sees a *Claim reward* button, not the reward itself. `draw()`
stamps `giveaways.claim_deadline` = `drawn_at + services.CLAIM_WINDOW` (2 days).
`POST /{slug}/claim` (`services.claim_reward`) sets `claimed_at` and unlocks the
Markdown; the claim UPDATE is guarded (`claimed_at IS NULL`, and
`claim_deadline > now` *only* when `restart_if_unclaimed` is set) so it can't
race the background job. `services.reward_claim_status(giveaway)` →
`none`/`claimed`/`waiting`/`unclaimed` drives both the winner block and the host
panel; it only returns `unclaimed` when `restart_if_unclaimed` is on — without
the flag the reward stays claimable forever rather than hard-locking a winner
who logs in late (that flag is the only thing the deadline gates).
`services.process_unclaimed()` (background loop + `gq draw`) picks up
`unclaimed` giveaways: it drops the no-show's entry, then **re-draws among the
remaining entrants** (new `claim_deadline`, `winner_notified_at` reset so the
replacement is DM'd) or, if nobody is left, **reopens** the giveaway
(`winner_id`/`drawn_at`/`claim_deadline` cleared, `ends_at` pushed out by
`duration_hours`, back to `open`). `unclaimed_count` counts the cycles; there is
no cap — it keeps going while `restart_if_unclaimed = 1`. The no-show is *not*
banned from re-entering if it reopens. The create/edit forms carry the
*Restart if unclaimed* checkbox (`restart_if_unclaimed`, checked by default).
Schema v5 added `restart_if_unclaimed`, `duration_hours`, `claim_deadline`,
`claimed_at`, `unclaimed_count` and grandfathered every already-drawn giveaway
as claimed (`claimed_at = COALESCE(reward_viewed_at, drawn_at)`) so the deploy
neither re-draws historic winners nor hides rewards they can already see.

**Posts from the site's own account never carry raw host text.**
`services.neutralize_for_post` swaps `@`/`#` for their full-width look-alikes
and strips URL schemes before a title lands in the winner DM or the default
`gq announce` text, otherwise a title like `@victim@server …` would make the
official account mention people. `--text` on `gq announce` is trusted as-is.

**Rate limiting** is Litestar's `RateLimitConfig` attached per handler
(`web.write_rate_limit`) to `POST /auth/login` (it makes the server register
an app on any host the caller names) and `POST /new`, keyed by
`path:client-ip` (uvicorn rewrites the client from `X-Forwarded-For`, which
cloudflared passes through). `GQ_RATE_LIMIT=0` disables it, which the tests
do because the app object is shared across the whole run.

**Moderation** is two flags: `giveaways.hidden` (admin, page 404s for
everyone but host/admins, gone from index and sitemap) and `users.banned`
(login rejected, existing session dropped on next request, excluded from
draws). Both are only set from the `gq admin` CLI. `GQ_ADMINS` only grants
*viewing* hidden pages in the web UI; there is no web admin on purpose.

**Only text is accepted from users**: title, reward, quest, conditions,
allowed-instance list, post URL (must be `https://`). Everything is
escaped by Jinja autoescape. Length limits are in `services.py`.

**The reward is host-written Markdown**, not a bare code. Stored in
`giveaways.reward` (still winner-only; schema v2 renamed the old `secret` /
`secret_viewed_at` columns to `reward` / `reward_viewed_at`),
rendered through `services.render_markdown` — the same restricted
CommonMark subset as the quest text (`_MD_TAGS`: inline emphasis, links,
lists, headings, code, blockquote; no images, no raw HTML; nh3 is the
second layer). The create form asks for *title, quest, reward*, then puts
*conditions* under the "Rules" fieldset alongside duration and eligibility.
Quest and reward both use the `md_editor` macro in `templates/_forms.html.jinja`
(a Write/Preview textarea; the Preview tab POSTs to `/md-preview`).

**`/{slug}/edit` is two forms in one, split on `drawn_at`.** Before the draw it
edits reward/quest/conditions/duration/post URL/flags in one go
(`services.update_giveaway`) — only the title is frozen; delete and recreate
instead if that's wrong. After the draw the same URL serves a reward-only form
(`services.update_reward`), because a typo'd code or a dead link would otherwise
leave the winner with nothing and no fix: deleting and recreating throws away
the entrants and the winner. Everything *except* the reward stays frozen once
drawn — changing the quest, the rules or the deadline after the fact rewrites
the terms people entered under, but the reward is host-owned content, not a
term the entrants agreed to, so it's always editable, pre- or post-draw. The
post-draw UPDATE is guarded with `drawn_at IS NOT NULL` so it can't race a
reopen from `process_unclaimed` (a reopened giveaway clears `drawn_at` and is
back on the full edit form). Editing is allowed whether or not the winner has
claimed already — the form warns when `claimed_at` is set, since they may have
copied the old text. The host panel's button reads *Edit reward* once drawn.

**Announcement flow.** Logged-in users get *no* write access. Instead:

- **Personal share.** The giveaway page shows a "Share on Mastodon" button,
  a plain server-side link to Mastodon's official share page, pre-filled
  (`mastodon.share_url` → `https://share.joinmastodon.org/#text=…`). Same
  link for everyone, logged in or not — no token, no API call, no JS, and it
  degrades to the copy-link box. We used to build `https://<instance>/share`
  URLs, but that per-instance intent is deprecated and breaks when the text
  contains a URL (mastodon#33681) — it blanked the page and did nothing.
  share.joinmastodon.org asks for the user's server once and remembers it.
- **Official announcement.** `announce_on_mastodon` posts from the site's own
  account (`GQ_ANNOUNCE_*`), appending `\n\n<giveaway url>` and storing the
  returned `url`/`id` as `post_url`/`post_id`. Triggered three ways:
  - **Auto** (`GQ_AUTO_ANNOUNCE=1`, the default): `services.announce_due()` runs
    from the background loop and posts every *listed*, un-hidden, still-open
    giveaway that has no post bound yet. A failed attempt stamps
    `giveaways.announce_attempted_at` and is retried no more than once per
    `ANNOUNCE_RETRY` (30 min) so a broken token doesn't spin; the default text
    is the same neutralised `title — quest` as the CLI. `services.announce_state`
    (`announced`/`pending`/`retrying`/`unlisted`/`manual`) drives the note in
    the host panel. Set `GQ_AUTO_ANNOUNCE=0` to opt out.
  - **CLI:** `gq announce <slug> [--text "…"]` — always works, ignores the retry
    spacing, `--text` is trusted as-is.
  - **Paste:** a URL in the edit form's "Post URL" field (any `https://`); if
    it's a status on `GQ_ANNOUNCE_INSTANCE`, `post_id` is derived from it too so
    comments work.

  Auto-announce reverses the original "CLI-only, human-in-the-loop" choice —
  the account owner asked for an announcement on every public giveaway anyway.
  Unlisted giveaways are still never auto-announced.

The Mastodon post itself is never modified or deleted by the site.

**Comments.** The comment thread on a giveaway page *is* the reply tree under
its announcement post — the same "reply from your own account" model every
"comments powered by Mastodon" setup uses, so logged-in users still get no
write access here. `services.get_comments(giveaway)`:

- Resolves the status id from `giveaways.post_id` (set when the giveaway is
  announced, auto or manual), or falls back to parsing one out of a hand-pasted
  `post_url` *if* it is a status on `GQ_ANNOUNCE_INSTANCE`
  (`mastodon.status_id_from_url`) — the only server whose numeric ids our token
  can query. No announcement ⇒ no comments section.
- Calls `GET /api/v1/statuses/:id/context` **authenticated as the announcement
  account** (`mastodon.fetch_context`, needs `read:statuses`). Authenticated so
  the announcement account's own blocks and the instance's domain blocks are
  applied for us, and to skip the unauthenticated cap (60 descendants, depth 20).
- Caches the normalised, **already-sanitised** comment list in `comment_threads`
  (schema v3, keyed by `giveaway_id`) for `COMMENT_TTL` (60 s). On an API error
  the last good copy is served, or an "unavailable, read it on Mastodon" note if
  there is nothing cached. Never a per-visitor API call.
- Freshness: the background loop (`services.refresh_comment_threads`) re-warms
  the cache each tick for every giveaway that is still open or ended within
  `COMMENT_WARM_AFTER_END` (3 days), so those pages never do the fetch
  themselves and new replies appear within ~a loop tick + TTL. Older giveaways
  aren't warmed — opening one refetches inline (once per `COMMENT_TTL`, in the
  handler thread, so the blocking call is fine). Web Push / the streaming API
  were considered for instant updates and rejected: Push needs a public
  encrypted-payload receiver, streaming needs a persistent authenticated
  WebSocket + reconnect logic — too much moving part to shave a minute.
- `parse_descendants` keeps only `public`/`unlisted` statuses from accounts not
  `banned` on giveaway.quest, threads them depth-first from the root (orphans
  whose parent was filtered out still show, near the top), and caps the indent
  at depth 3. `content` is remote server-rendered HTML → `nh3` allowlist
  (`_COMMENT_TAGS`, only `href` on `<a>`, `rel="nofollow noopener noreferrer
  ugc"`, http/https/mailto only); avatars/links go through
  `mastodon.safe_https_url`. Custom emoji are left as `:shortcode:` text for now.
- Rendered server-side in `templates/_comments.html.jinja` (no JS, so the strict
  CSP is untouched — same-origin HTML, `img-src https:` already covers avatars).
  "Reply on Mastodon" is a plain link to `post_url`.
- `GQ_COMMENTS=0` is a kill switch; otherwise the feature is gated on
  `announce_enabled and` a resolvable status id. No `configuration.nix` egress
  change needed — the announcement instance is public internet, already reached
  for login/announce/DMs.

## Mastodon / OAuth details

- App registration is dynamic: `POST /api/v1/apps` per instance, cached in
  `mastodon_apps`. Re-registered automatically if `GQ_BASE_URL` (thus the
  redirect URI) or the scope string changes.
- Scope: `read:accounts` only (login just reads the profile). The scope
  string must match at registration and authorization or Mastodon rejects
  the token exchange; changing `MASTODON_SCOPES` re-registers the app per
  instance automatically. The site's own account uses a separate
  manually-issued token (`GQ_ANNOUNCE_TOKEN`) with `write:statuses` (announce,
  winner DMs) and `read:statuses` (the announcement's reply thread for
  comments), not this flow.
- The OAuth `state` lives in the cookie session (`request.session["oauth"]`),
  is compared with `secrets.compare_digest`, and is popped on the first
  callback, so a callback URL can't be replayed.
- `verify_credentials` gives `created_at`, used for the minimum-account-age
  rule, and `avatar_static`/`url` for display.
- Account identity key is `(instance, remote_id)`; `acct` is
  `username@instance` lower-cased and is what admins use on the CLI.
- The user's token is never stored: the callback reads the profile with it,
  then calls `/oauth/revoke` (best effort) and drops it. Schema v1
  (`PRAGMA user_version`, see `db._migrate`) removed the old
  `users.access_token` column. The db is still secret because of giveaway
  rewards (`giveaways.reward`) and `mastodon_apps.client_secret`.
- Everything that comes back from `verify_credentials` is attacker-controlled
  (anyone can run an instance): `mastodon.account_from_json` only accepts
  `https://` profile/avatar URLs (a `javascript:` URL in `href` would have
  been stored XSS on every page showing the host) and `[A-Za-z0-9_]`
  usernames, so `acct` can't be made to look like someone on another server.
- Not yet tested against a live server; the test-suite uses `FakeMastodon`
  in `tests/conftest.py` which monkeypatches the four functions in
  `mastodon.py`.

## Front end

- Tailwind v4 standalone CLI (`pkgs.tailwindcss_4`) + daisyUI 5 loaded as a
  local plugin file: `assets/app.css` has `@plugin "./vendor/daisyui.mjs"`.
  The flake fetches `daisyui.mjs`/`daisyui-theme.mjs` from the daisyUI GitHub
  release and symlinks them into `assets/vendor/` (git-ignored) inside
  `build-css`. No node_modules anywhere. Bump `daisyuiVersion` + hashes in
  `flake.nix` to upgrade.
- `@source "../giveaway_quest/templates"` tells Tailwind where to scan for
  classes. Built output `giveaway_quest/static/app.css` is git-ignored;
  `dev` and deployments build it.
- `@import "tailwindcss" source(none)` disables Tailwind's *automatic* content
  detection so only that explicit `@source` counts. Two reasons: auto-detection
  resolves relative to the build CWD (repo root for `build-css`, `/build` in the
  Dockerfile's css stage — different trees), and in the container it would also
  crawl `assets/vendor/*.mjs` and bloat the output ~4x. The Dockerfile css stage
  must therefore `COPY giveaway_quest/templates` in — miss it and every utility
  class is purged, leaving daisyUI's base but no layout (everything renders
  stacked/unstyled). Symptom seen once in the Cloudflare Tunnel deploy.
- Themes: two hand-rolled daisyUI themes in `assets/app.css` (built-in
  `themes: false`), defined with repeated `@plugin "./vendor/daisyui-theme.mjs"`
  blocks — `quest` (parchment/sepia, `default: true`) and `questdark` (dark
  slate + quest gold, `prefersdark: true`). Untouched, the page follows
  `prefers-color-scheme`; the navbar sun/moon toggle (`[data-theme-toggle]`)
  sets `data-theme` on `<html>` and remembers the pick in `localStorage`
  under `theme`. A tiny blocking script in `<head>` reapplies it before
  first paint. `mq.change` keeps the toggle in sync with the OS while no
  choice is stored.
- Quest markers: `templates/_icons.html.jinja` `quest_marker(status, class)` macro,
  a WoW-style glyph — gold `!` for `open`, silver `?` for `drawing`, gold `?`
  for `ended`. Inline `<svg>` with a `<text>` glyph (no icon font). Used in
  `_card.html.jinja`, the giveaway-page status badges and the "Your Quest" panel.
- Headings and the wordmark use `.font-quest` (a system serif stack, no
  webfont).
- Favicons live in `static/icons/` (`favicon.svg` is the primary, plus
  `.ico` and PNGs at 16/32/48/64/180/192/512). `base.html.jinja` `<head>`
  wires them up: `rel="icon"` svg + ico + PNGs, `apple-touch-icon` (180),
  Safari `mask-icon`, and `rel="manifest"` → `static/site.webmanifest`
  (192/512 PNGs, gold `theme_color`). `theme-color` `<meta>` tags switch
  white / slate by `prefers-color-scheme`. All same-origin, so the CSP
  `default-src 'self'` covers the manifest fetch with no new directive.
- JS lives in two static files, never inline, so the CSP can be
  `script-src 'self'`: `static/theme.js` (blocking, in `<head>`, reapplies
  the saved theme before first paint) and `static/app.js` (theme toggle,
  localises `<time data-local>`, ticks `[data-countdown]`, copy button, live
  filter, the Markdown write/preview toggle (`[data-md-editor]` → `/md-preview`),
  and `form[data-confirm]` confirm dialogs — use that
  attribute instead of `onsubmit`, inline handlers are blocked by the CSP).
  Everything degrades to server-rendered UTC times and the default theme.
- Security headers (CSP, HSTS when `GQ_BASE_URL` is https, nosniff, DENY
  framing, referrer policy) are added by `app.security_headers_middleware`,
  a raw ASGI wrapper: Litestar's `response_headers` does not apply to
  responses produced by exception handlers, and the 404/403/429 pages need
  them too. `img-src https:` is as tight as it gets while avatars come from
  arbitrary instances; `form-action` must include `https:` because browsers
  apply it to the redirect after `POST /auth/login`.
- Rich previews: `og:title`, `og:description`, `og:url`, `twitter:card`, and
  `fediverse:creator`. `og:image`/`twitter:image` point at the 512×512 favicon
  PNG (no per-giveaway art yet), which is enough for Mastodon to render a
  thumbnailed card instead of a bare text one. Mastodon caches preview cards
  for ~2 weeks, so already-posted links keep the old card until it expires.
- Structured data (JSON-LD, `<script type="application/ld+json">` — not
  executed, so `script-src 'self'` does not block it): base template emits a
  site-wide `Organization` + `WebSite` `@graph` (logo + `sameAs` the
  fosstodon account — feeds Google's site name/logo); `giveaway.html.jinja`
  adds a `BreadcrumbList` and an `Event` (start = `created_at`, end =
  `ends_at`, online `VirtualLocation`, `organizer` = the host `Person`).
  Interpolate every value through Jinja's `| tojson` so it stays JSON- and
  HTML-safe under autoescape. `Event` is a slight stretch for a giveaway and
  Google may not grant rich results; `Product`/`Offer` on the prize was
  skipped deliberately (price-0 giveaway offers risk the structured-data spam
  policy, and the reward is winner-only anyway).

## Litestar gotchas hit (2.24.0)

- **Double-wrapped sync handlers → `'coroutine' object has no attribute
  'to_asgi_response'`.** Registering a `sync_to_thread=True` GET handler
  directly on `Litestar(route_handlers=[...])` when a POST handler shares the
  same path made `on_registration` run twice on the GET handler, and
  `is_async_callable` does not recognise Litestar's own `AsyncCallable`
  wrapper, so the fn got wrapped twice. Symptom: only `GET /new` and
  `GET /{slug}/edit` returned 500. Fix: all handlers are grouped in
  `Router` objects (`pages.router`, `auth.router`), which registers each
  handler once. Comment in `app.py`.
- `state` is a reserved kwarg name (the app State). The OAuth callback's
  `?state=` query param is declared as
  `oauth_state: Annotated[str | None, QueryParameter(name="state")]`.
- 2.24 deprecates inferred parameter styles. Use `FromQuery[...]`,
  `FromPath[...]`, `NamedDependency[...]` and import `JinjaTemplateEngine`
  from `litestar.plugins.jinja`. Tests run with
  `-W error::DeprecationWarning` to keep it that way.
- CSRF: `CSRFConfig` checks the `_csrf_token` form field for URL-encoded
  bodies. Templates emit it with `{{ csrf_input | safe }}`. Any POST without
  it gets 403.
- Cookie session backend needs a 16/24/32-byte secret; `settings.session_secret`
  is `sha256(GQ_SECRET_KEY)`.
- `NotAuthorizedException` from the `require_login` guard is turned into a
  redirect to `/auth/login?next=…` in the app-wide exception handler; all
  other `HTTPException`s render `error.html.jinja`.
- `openapi_config=None` disables the `/schema` routes.
- Route precedence: literal paths (`/new`, `/mine`, `/robots.txt`) win over
  `/{slug:str}`, verified by tests.

## NixOS / tooling gotchas

- Flakes are disabled in this machine's `nix.conf`; set
  `NIX_CONFIG="experimental-features = nix-command flakes"` or pass
  `--extra-experimental-features`. Files must be `git add`ed before a flake
  can see them.
- PyPI wheels with bundled dynamic binaries (ruff) do not run on NixOS
  ("Could not start dynamically linked executable"). ruff comes from
  nixpkgs in the dev shell instead of `uv`. Pure-Python and manylinux
  wheels that only need libc (cryptography, httpx) work fine because
  `UV_PYTHON` points at the nix interpreter.
- `uv sync` in the shellHook is quiet; if the venv looks stale run it
  manually.
- Headless Chromium from nixpkgs could not load pages from the Claude Code
  sandbox (about:blank worked, `http://127.0.0.1` hung), so no screenshots
  were taken. Visual checks were done by binding the dev server to the
  Tailscale IP (`gq serve --host 100.74.250.95`) and opening it on a phone.
  Remember to set `GQ_BASE_URL` to the same host so OAuth redirects work.

## Testing

`tests/conftest.py` builds one `TestClient` per test with a fresh sqlite
file, and provides helpers: `make_user`, `login_as` (writes the session
cookie directly), `csrf`, `create_giveaway`. Tests reach into the db to move
`ends_at` into the past and then call `services.draw_due()` instead of
waiting on the background loop.

The module-level `app` from `giveaway_quest.app` is reused across tests.
Creating a new `Litestar` per test with the same handler objects is *not*
safe (see double-wrap above).

## Follow-ups worth doing

1. Log in with a real account and run through create → post → enter → draw
   on a second account.
2. ~~Encrypt `access_token` at rest.~~ Done differently: not stored at all.
3. Per-giveaway `og:image` (a generated PNG with the title/host rendered in).
   `og:image` now falls back to the 512×512 favicon, so cards are no longer
   text-only, but a real banner would read far better in a timeline.
4. Surface `GQ_ANNOUNCE_*` health somewhere (a `gq` check, or a warning at
   startup) so a dead site token doesn't just silently skip winner DMs — now
   partly visible via `announce_state` = `retrying` on the giveaway page.
   ~~Optionally auto-announce listed giveaways instead of CLI-only.~~ Done
   (`GQ_AUTO_ANNOUNCE`, `services.announce_due`).
5. ~~Rate limits on login and create.~~ Done (`GQ_RATE_LIMIT`); a
   Cloudflare WAF rate-limiting rule in front would still be cheaper.
6. Live on homek14 (not a VPS) behind Cloudflare Tunnel — no inbound ports,
   TLS terminated at Cloudflare's edge. `docker-compose.yml` (Podman via
   `podman-compose`), all host-side ops consolidated into `tasks.py`
   (`uv run invoke -l`) rather than scattered `deploy/*.sh` scripts, driven
   by two `systemctl --user` units (rootless Podman + `loginctl
   enable-linger`). See README's "Deployment sketch" / "Maintaining it".
7. Security review 2026-09-06 (`security-review-2026-09-06.md`): fixed the
   high/medium findings; the "Remaining" section there is the open list.
