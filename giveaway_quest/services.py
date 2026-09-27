"""Giveaway domain logic on top of the sqlite layer. All functions take an open connection."""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import timedelta

import markdown_it
import nh3

from . import db, mastodon
from .config import settings
from .slugs import new_slug

log = logging.getLogger(__name__)

TITLE_MAX = 120
TEXT_MAX = 2000
REWARD_MAX = TEXT_MAX  # the reward is Markdown now, so it gets the same room as quest/conditions
POST_MAX = 450  # leave room below Mastodon's default 500 char limit
PAGE_SIZE = 12
MAX_DURATION_HOURS = 24 * 90
MAX_WINNERS = 10  # seats/rewards per giveaway; site is low-volume, bump if ever needed

# How long the winner has to claim the reward (click "Claim reward" to unlock the
# code) before it is re-drawn among the remaining entrants, or the giveaway is
# reopened if nobody is left. Only applies when `restart_if_unclaimed` is set.
CLAIM_WINDOW = timedelta(days=2)

# Host-written prose (quest instructions, the reward blurb): a restricted CommonMark
# subset. No images or raw HTML - markdown-it-py already refuses dangerous link
# schemes (javascript:, etc.) at render time, and nh3 strips anything outside the
# tag/attribute allowlist as a second layer.
_MD = markdown_it.MarkdownIt("commonmark", {"breaks": True}).disable(
    ["image", "html_block", "html_inline"]
)
_MD_TAGS = {
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "a",
    "code",
    "pre",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_MD_ATTRS = {"a": {"href"}}


class ValidationError(Exception):
    pass


def render_markdown(text: str | None) -> str:
    if not text:
        return ""
    return nh3.clean(_MD.render(text), tags=_MD_TAGS, attributes=_MD_ATTRS)


# ------------------------------------------------------------------------- comments
#
# Comments are the reply thread under the giveaway's announcement post, pulled
# from Mastodon and cached in `comment_threads`. Logged-in users get no write
# access here either: to comment you reply from your own Mastodon account.

COMMENT_TTL = timedelta(seconds=60)  # how long a cached thread is served before a refetch
# The background loop keeps threads this fresh for giveaways that are still open
# or ended recently, so their page never pays for the fetch; older giveaways
# refetch on demand (once per COMMENT_TTL) when someone opens the page.
COMMENT_WARM_AFTER_END = timedelta(days=3)
COMMENT_MAX_DEPTH = 3  # how deep to indent; deeper replies still show, flattened
COMMENT_HTML_MAX = 8192
_COMMENT_VISIBILITIES = {"public", "unlisted"}
# Remote, server-rendered HTML from whatever instance the replier is on. Keep the
# inline text tags Mastodon actually emits and links; drop everything else
# (including class/style, so mention/hashtag spans become plain text/links). nh3
# also strips <script>/<style> content outright.
_COMMENT_TAGS = {
    "p",
    "br",
    "a",
    "span",
    "strong",
    "b",
    "em",
    "i",
    "del",
    "ul",
    "ol",
    "li",
    "blockquote",
    "code",
    "pre",
}
_COMMENT_ATTRS = {"a": {"href"}}


def sanitize_comment_html(html: str | None) -> str:
    return nh3.clean(
        (html or "")[:COMMENT_HTML_MAX],
        tags=_COMMENT_TAGS,
        attributes=_COMMENT_ATTRS,
        link_rel="nofollow noopener noreferrer ugc",
        url_schemes={"http", "https", "mailto"},
    )


@dataclass(slots=True)
class Comment:
    id: str
    author_name: str
    author_acct: str
    author_url: str
    author_avatar: str
    html: str
    created_at: str
    permalink: str
    depth: int


@dataclass(slots=True)
class CommentThread:
    items: list[Comment] = field(default_factory=list)
    updated_at: str | None = None
    unavailable: bool = False  # True only when there is nothing to show *and* the fetch failed

    @property
    def count(self) -> int:
        return len(self.items)


def _comment_from_status(status: dict, depth: int) -> Comment | None:
    account = status.get("account")
    if not isinstance(account, dict):
        return None
    name = account.get("display_name") or account.get("username") or "someone"
    acct = account.get("acct") or account.get("username") or ""
    return Comment(
        id=str(status.get("id", "")),
        author_name=str(name)[:200],
        author_acct=str(acct)[:200],
        author_url=mastodon.safe_https_url(account.get("url")),
        author_avatar=mastodon.safe_https_url(
            account.get("avatar_static") or account.get("avatar")
        ),
        html=sanitize_comment_html(status.get("content")),
        created_at=str(status.get("created_at") or ""),
        permalink=mastodon.safe_https_url(status.get("url") or status.get("uri")),
        depth=min(depth, COMMENT_MAX_DEPTH),
    )


def parse_descendants(root_id: str, descendants: list, banned_accts: set[str]) -> list[Comment]:
    """Turn a Mastodon `context.descendants` list into a threaded comment list.

    Filters to public/unlisted statuses from non-banned accounts, then walks the
    reply tree depth-first from the root post so replies sit under their parent.
    Anything whose parent got filtered out is still shown, near the top.
    """
    kept: dict[str, dict] = {}
    for status in descendants:
        if not isinstance(status, dict) or not status.get("id"):
            continue
        if status.get("visibility") not in _COMMENT_VISIBILITIES:
            continue
        account = status.get("account")
        acct = (account.get("acct") if isinstance(account, dict) else "") or ""
        if acct.lower() in banned_accts:
            continue
        kept[str(status["id"])] = status

    children: dict[str, list[str]] = {}
    for sid, status in kept.items():
        parent = str(status.get("in_reply_to_id") or "")
        children.setdefault(parent, []).append(sid)

    def _sort(ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda sid: str(kept[sid].get("created_at") or ""))

    out: list[Comment] = []
    seen: set[str] = set()

    def walk(parent_id: str, depth: int) -> None:
        for sid in _sort(children.get(parent_id, [])):
            if sid in seen:
                continue
            seen.add(sid)
            comment = _comment_from_status(kept[sid], depth)
            if comment:
                out.append(comment)
            walk(sid, depth + 1)

    walk(str(root_id), 0)
    # Orphans: kept replies whose parent was filtered out (e.g. a deleted or
    # followers-only mid-thread post). Show them rather than silently dropping.
    for sid in _sort([s for s in kept if s not in seen]):
        comment = _comment_from_status(kept[sid], 1)
        if comment:
            out.append(comment)
    return out


def _banned_accts(conn: sqlite3.Connection) -> set[str]:
    return {row["acct"].lower() for row in conn.execute("SELECT acct FROM users WHERE banned = 1")}


def _load_comments(payload: str) -> list[Comment]:
    try:
        return [Comment(**row) for row in json.loads(payload)]
    except (ValueError, TypeError):
        return []


def comments_status_id(giveaway: dict) -> str:
    """The Mastodon status id whose replies are this giveaway's comments, or ''."""
    if giveaway["post_id"]:
        return str(giveaway["post_id"])
    return mastodon.status_id_from_url(giveaway["post_url"], settings.announce_instance) or ""


def get_comments(giveaway: dict) -> CommentThread:
    """Cached reply thread for a giveaway. Opens its own connections (it does I/O)."""
    if not (settings.comments_enabled and settings.announce_enabled):
        return CommentThread()
    status_id = comments_status_id(giveaway)
    if not status_id:
        return CommentThread()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM comment_threads WHERE giveaway_id = ?",
            (giveaway["id"],),
        ).fetchone()
    if (
        row
        and db.parse_iso(row["fetched_at"])
        and db.parse_iso(row["fetched_at"]) > (db.now() - COMMENT_TTL)
    ):
        return CommentThread(_load_comments(row["payload"]), row["fetched_at"])

    try:
        context = mastodon.fetch_context(
            settings.announce_instance, settings.announce_token, status_id
        )
    except mastodon.MastodonError:
        if row:
            return CommentThread(_load_comments(row["payload"]), row["fetched_at"])
        return CommentThread(unavailable=True)

    descendants = context.get("descendants")
    now = db.iso(db.now())
    with db.connect() as conn:
        items = parse_descendants(
            status_id, descendants if isinstance(descendants, list) else [], _banned_accts(conn)
        )
        conn.execute(
            """INSERT INTO comment_threads (giveaway_id, status_id, payload, fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(giveaway_id) DO UPDATE SET
                 status_id = excluded.status_id, payload = excluded.payload,
                 fetched_at = excluded.fetched_at""",
            (giveaway["id"], status_id, json.dumps([asdict(c) for c in items]), now),
        )
    return CommentThread(items, now)


def refresh_comment_threads() -> int:
    """Warm the comment cache for giveaways still open or ended recently.

    Runs from the background loop so their page never does the fetch itself.
    `get_comments` already no-ops when the cache is still fresh, so this costs
    one Mastodon call per active thread per `COMMENT_TTL`. Returns how many
    threads it looked at.
    """
    if not (settings.comments_enabled and settings.announce_enabled):
        return 0
    with db.connect() as conn:
        rows = conn.execute(
            GIVEAWAY_SELECT
            + """ WHERE g.hidden = 0
                    AND ((g.post_id IS NOT NULL AND g.post_id != '')
                         OR (g.post_url IS NOT NULL AND g.post_url != ''))
                    AND (g.drawn_at IS NULL OR g.drawn_at > :warm)
                  ORDER BY g.created_at DESC
                  LIMIT 50""",
            {"warm": db.iso(db.now() - COMMENT_WARM_AFTER_END)},
        ).fetchall()
    for giveaway in rows:
        get_comments(giveaway)  # refetches iff stale and writes the cache
    return len(rows)


# --------------------------------------------------------------------------- users


def upsert_user(conn: sqlite3.Connection, instance: str, account: mastodon.RemoteAccount) -> dict:
    """Create or refresh a user from their profile. The OAuth token is deliberately not kept."""
    ts = db.iso(db.now())
    acct = f"{account.username}@{instance}".lower()
    conn.execute(
        """INSERT INTO users (instance, remote_id, acct, username, display_name, avatar_url,
                              profile_url, account_created_at, created_at, last_login_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(instance, remote_id) DO UPDATE SET
             acct=excluded.acct, username=excluded.username, display_name=excluded.display_name,
             avatar_url=excluded.avatar_url, profile_url=excluded.profile_url,
             account_created_at=excluded.account_created_at,
             last_login_at=excluded.last_login_at""",
        (
            instance,
            account.remote_id,
            acct,
            account.username,
            account.display_name,
            account.avatar_url,
            account.profile_url,
            account.created_at,
            ts,
            ts,
        ),
    )
    return conn.execute(
        "SELECT * FROM users WHERE instance = ? AND remote_id = ?", (instance, account.remote_id)
    ).fetchone()


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def is_admin(user: dict | None) -> bool:
    return bool(user) and user["acct"] in settings.admins


# ----------------------------------------------------------------------- giveaways

GIVEAWAY_SELECT = """
SELECT g.*,
       u.acct AS owner_acct, u.display_name AS owner_name, u.avatar_url AS owner_avatar,
       u.profile_url AS owner_profile_url,
       (SELECT COUNT(*) FROM entries e WHERE e.giveaway_id = g.id) AS entry_count
FROM giveaways g
JOIN users u ON u.id = g.owner_id
"""


def _clean_text(value: str | None, limit: int, *, required: bool = False, name: str = "") -> str:
    value = (value or "").replace("\r\n", "\n").strip()
    if required and not value:
        raise ValidationError(f"{name} is required.")
    if len(value) > limit:
        raise ValidationError(f"{name} is too long (max {limit} characters).")
    return value


def parse_allowed_instances(raw: str | None) -> str:
    items: list[str] = []
    for chunk in (raw or "").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        items.append(mastodon.normalize_instance(chunk))
    return ",".join(dict.fromkeys(items))


def parse_hours(raw: str | None, *, name: str = "Duration") -> int:
    try:
        hours = int(float(raw or ""))
    except ValueError as exc:
        raise ValidationError(f"{name} must be a number of hours.") from exc
    if hours < 1 or hours > MAX_DURATION_HOURS:
        raise ValidationError(f"{name} must be between 1 hour and 90 days.")
    return hours


def parse_rewards(data: dict[str, str]) -> list[str]:
    """Read `winner_count` and its `reward_1..reward_N` fields off a posted form."""
    try:
        count = int(data.get("winner_count") or 1)
    except ValueError as exc:
        raise ValidationError("Number of winners must be a whole number.") from exc
    if count < 1 or count > MAX_WINNERS:
        raise ValidationError(f"Number of winners must be between 1 and {MAX_WINNERS}.")
    return [
        _clean_text(data.get(f"reward_{i}"), REWARD_MAX, required=True, name=f"Reward {i}")
        for i in range(1, count + 1)
    ]


@dataclass(slots=True)
class GiveawayForm:
    title: str
    rewards: list[str]
    conditions: str
    quest: str
    allowed_instances: str
    min_account_age_days: int
    listed: bool
    restart_if_unclaimed: bool
    hours: int

    @classmethod
    def from_form(cls, data: dict[str, str]) -> GiveawayForm:
        title = _clean_text(data.get("title"), TITLE_MAX, required=True, name="Title")
        try:
            min_age = int(data.get("min_account_age_days") or 0)
        except ValueError as exc:
            raise ValidationError("Minimum account age must be a whole number of days.") from exc
        if min_age < 0 or min_age > 3650:
            raise ValidationError("Minimum account age must be between 0 and 3650 days.")
        try:
            allowed = parse_allowed_instances(data.get("allowed_instances"))
        except mastodon.MastodonError as exc:
            raise ValidationError(f"Allowed servers: {exc}") from exc
        return cls(
            title=title,
            rewards=parse_rewards(data),
            conditions=_clean_text(data.get("conditions"), TEXT_MAX, name="Conditions"),
            quest=_clean_text(data.get("quest"), TEXT_MAX, name="Quest"),
            allowed_instances=allowed,
            min_account_age_days=min_age,
            listed=data.get("listed") == "on",
            restart_if_unclaimed=data.get("restart_if_unclaimed") == "on",
            hours=parse_hours(data.get("hours")),
        )


def suggested_post_text(title: str, quest: str) -> str:
    text = f"Giveaway: {title}"
    if quest:
        text += f" — {quest}"
    return text[:POST_MAX]


def neutralize_for_post(text: str) -> str:
    """Make host-written text safe to embed in a post from the *site's own* account.

    Mastodon turns ``@user@server`` into a mention, ``#tag`` into a hashtag and
    ``https://…`` into a link anywhere in a status, so a giveaway title could
    make the official account mention, tag or link to whatever the host wants.
    Full-width look-alikes keep the text readable without triggering any of it.
    """
    text = re.sub(r"https?://", "", text)
    return text.replace("@", "＠").replace("#", "＃")


def _insert_seats(conn: sqlite3.Connection, giveaway_id: int, rewards: list[str]) -> None:
    conn.executemany(
        "INSERT INTO winners (giveaway_id, seat, reward) VALUES (?, ?, ?)",
        [(giveaway_id, seat, reward) for seat, reward in enumerate(rewards, start=1)],
    )


def create_giveaway(conn: sqlite3.Connection, owner: dict, form: GiveawayForm) -> dict:
    now = db.now()
    slug = new_slug(conn)
    cur = conn.execute(
        """INSERT INTO giveaways (slug, owner_id, title, conditions, quest,
                                  allowed_instances, min_account_age_days, listed,
                                  restart_if_unclaimed, duration_hours, winner_count,
                                  created_at, ends_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            slug,
            owner["id"],
            form.title,
            form.conditions,
            form.quest,
            form.allowed_instances,
            form.min_account_age_days,
            int(form.listed),
            int(form.restart_if_unclaimed),
            form.hours,
            len(form.rewards),
            db.iso(now),
            db.iso(now + timedelta(hours=form.hours)),
        ),
    )
    _insert_seats(conn, cur.lastrowid, form.rewards)
    return get_giveaway(conn, slug)


def announce_on_mastodon(conn: sqlite3.Connection, giveaway: dict, text: str) -> dict:
    """Post the announcement from the site's own account and bind the status to the giveaway."""
    if not settings.announce_enabled:
        raise mastodon.MastodonError("No announcement account is configured (set GQ_ANNOUNCE_*).")
    url = settings.url(f"/{giveaway['slug']}")
    status = mastodon.post_status(
        settings.announce_instance, settings.announce_token, f"{text}\n\n{url}"
    )
    conn.execute(
        "UPDATE giveaways SET post_url = ?, post_id = ? WHERE id = ?",
        (status.get("url") or status.get("uri"), str(status.get("id", "")), giveaway["id"]),
    )
    return get_giveaway(conn, giveaway["slug"])


ANNOUNCE_RETRY = timedelta(minutes=30)  # min gap between auto-announce attempts for one giveaway


def announce_state(giveaway: dict) -> str:
    """How the Mastodon announcement stands, for the host/admin panel.

    ``announced`` (a post is bound), ``pending`` (auto-announce will post it
    within a loop tick), ``retrying`` (a previous auto-announce attempt failed
    and will be retried), ``unlisted`` (auto-announce skips it), ``manual``
    (auto-announce is off - use ``gq announce`` or paste a URL).
    """
    if giveaway["post_url"] or giveaway["post_id"]:
        return "announced"
    if not (settings.announce_enabled and settings.auto_announce):
        return "manual"
    if not giveaway["listed"]:
        return "unlisted"
    return "retrying" if giveaway["announce_attempted_at"] else "pending"


def announce_due() -> list[str]:
    """Announce listed giveaways the site hasn't posted yet. Returns the slugs announced.

    Runs from the background loop. Retries a failed giveaway no more than once
    per ``ANNOUNCE_RETRY`` so a broken token doesn't spin; ``gq announce`` is the
    manual override and ignores this.
    """
    if not (settings.announce_enabled and settings.auto_announce):
        return []
    with db.connect() as conn:
        rows = conn.execute(
            GIVEAWAY_SELECT
            + """ WHERE g.listed = 1 AND g.hidden = 0 AND g.drawn_at IS NULL
                    AND g.ends_at > :now
                    AND (g.post_id IS NULL OR g.post_id = '')
                    AND (g.post_url IS NULL OR g.post_url = '')
                    AND (g.announce_attempted_at IS NULL OR g.announce_attempted_at < :cutoff)
                  ORDER BY g.created_at""",
            {"now": db.iso(db.now()), "cutoff": db.iso(db.now() - ANNOUNCE_RETRY)},
        ).fetchall()
    announced: list[str] = []
    for giveaway in rows:
        with db.connect() as conn:
            conn.execute(
                "UPDATE giveaways SET announce_attempted_at = ? WHERE id = ?",
                (db.iso(db.now()), giveaway["id"]),
            )
            try:
                announce_on_mastodon(conn, giveaway, _auto_announce_text(giveaway))
            except mastodon.MastodonError as exc:
                log.warning("auto-announce failed for %s: %s", giveaway["slug"], exc)
                continue
        announced.append(giveaway["slug"])
    return announced


def _auto_announce_text(giveaway: dict) -> str:
    # Host-written title/quest → keep the official account from mentioning,
    # tagging or linking whatever the host typed (same rule as `gq announce`).
    return neutralize_for_post(suggested_post_text(giveaway["title"], giveaway["quest"]))


def update_giveaway(conn: sqlite3.Connection, giveaway: dict, data: dict[str, str]) -> dict:
    rewards = parse_rewards(data)
    conditions = _clean_text(data.get("conditions"), TEXT_MAX, name="Conditions")
    quest = _clean_text(data.get("quest"), TEXT_MAX, name="Quest")
    listed = data.get("listed") == "on"
    restart_if_unclaimed = data.get("restart_if_unclaimed") == "on"
    ends_at = giveaway["ends_at"]
    if data.get("hours", "").strip():
        hours = parse_hours(data.get("hours"), name="Ends in")
        ends_at = db.iso(db.now() + timedelta(hours=hours))
    post_url = _clean_text(data.get("post_url"), 500, name="Post URL")
    if post_url and not post_url.startswith("https://"):
        raise ValidationError("Post URL must start with https://")
    # Keep post_id in step with a hand-pasted URL so comments work without
    # `gq announce`, but only when the URL is a status on our own announcement
    # instance (the only server whose ids our token can query).
    post_id = (
        mastodon.status_id_from_url(post_url, settings.announce_instance) if post_url else None
    )
    conn.execute(
        """UPDATE giveaways
             SET conditions = ?, quest = ?, listed = ?, restart_if_unclaimed = ?,
                 ends_at = ?, post_url = ?, post_id = ?, winner_count = ?
           WHERE id = ?""",
        (
            conditions,
            quest,
            int(listed),
            int(restart_if_unclaimed),
            ends_at,
            post_url or None,
            post_id,
            len(rewards),
            giveaway["id"],
        ),
    )
    # Safe to replace wholesale: this only runs pre-draw (see routes/pages.py),
    # so no seat can have a user_id yet.
    conn.execute("DELETE FROM winners WHERE giveaway_id = ?", (giveaway["id"],))
    _insert_seats(conn, giveaway["id"], rewards)
    return get_giveaway(conn, giveaway["slug"])


def update_reward(conn: sqlite3.Connection, giveaway: dict, data: dict[str, str]) -> dict:
    """Correct one or more seats' reward text on an already-drawn giveaway.

    The reward is the one thing a host can still change after the draw: a typo in
    a code or a dead link would otherwise leave a winner with nothing and no fix
    (deleting and recreating loses the entrants and the other winners). Everything
    else stays frozen - changing the quest, the rules or the deadline after the
    fact would rewrite the terms people entered under. Seat count is fixed once
    drawn. Each winner sees their corrected text next time they load the page,
    claimed or not.
    """
    for winner in get_winners(conn, giveaway["id"]):
        field = f"reward_{winner['id']}"
        if field not in data:
            continue
        reward = _clean_text(data[field], REWARD_MAX, required=True, name="The reward")
        conn.execute("UPDATE winners SET reward = ? WHERE id = ?", (reward, winner["id"]))
    return get_giveaway(conn, giveaway["slug"])


def delete_giveaway(conn: sqlite3.Connection, giveaway_id: int) -> None:
    conn.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))


def get_giveaway(conn: sqlite3.Connection, slug: str) -> dict | None:
    return conn.execute(GIVEAWAY_SELECT + " WHERE g.slug = ?", (slug,)).fetchone()


WINNER_SELECT = """
SELECT w.*,
       u.acct AS user_acct, u.display_name AS user_name,
       u.avatar_url AS user_avatar, u.profile_url AS user_profile_url
FROM winners w
LEFT JOIN users u ON u.id = w.user_id
"""


def get_winners(conn: sqlite3.Connection, giveaway_id: int) -> list[dict]:
    """All seats for a giveaway, in display order."""
    return conn.execute(
        WINNER_SELECT + " WHERE w.giveaway_id = ? ORDER BY w.seat", (giveaway_id,)
    ).fetchall()


def get_winner_for_user(conn: sqlite3.Connection, giveaway_id: int, user_id: int) -> dict | None:
    """The seat this user holds in this giveaway, if any."""
    return conn.execute(
        WINNER_SELECT + " WHERE w.giveaway_id = ? AND w.user_id = ?", (giveaway_id, user_id)
    ).fetchone()


def status_of(giveaway: dict) -> str:
    if giveaway["drawn_at"]:
        return "ended"
    if db.parse_iso(giveaway["ends_at"]) <= db.now():
        return "drawing"  # past the deadline, winner not picked yet (background job pending)
    return "open"


SORTS = {
    "ending": "g.drawn_at IS NOT NULL, g.ends_at ASC",
    "newest": "g.created_at DESC",
    "popular": "entry_count DESC, g.created_at DESC",
}


def list_giveaways(
    conn: sqlite3.Connection,
    *,
    page: int = 1,
    sort: str = "ending",
    status: str = "open",
    q: str = "",
    owner_id: int | None = None,
    include_unlisted: bool = False,
) -> tuple[list[dict], int]:
    where = ["g.hidden = 0"]
    params: list = []
    if not include_unlisted:
        where.append("g.listed = 1")
    if owner_id is not None:
        where.append("g.owner_id = ?")
        params.append(owner_id)
    if status == "open":
        where.append("g.drawn_at IS NULL AND g.ends_at > ?")
        params.append(db.iso(db.now()))
    elif status == "ended":
        where.append("(g.drawn_at IS NOT NULL OR g.ends_at <= ?)")
        params.append(db.iso(db.now()))
    if q:
        where.append("(g.title LIKE ? OR g.quest LIKE ? OR u.acct LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    sql_where = " WHERE " + " AND ".join(where)
    order = SORTS.get(sort, SORTS["ending"])
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM giveaways g JOIN users u ON u.id = g.owner_id{sql_where}",
        params,
    ).fetchone()["n"]
    offset = max(page - 1, 0) * PAGE_SIZE
    rows = conn.execute(
        f"{GIVEAWAY_SELECT}{sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, offset],
    ).fetchall()
    return rows, total


def pagination_window(
    page: int, pages: int, *, edges: int = 1, around: int = 2
) -> list[int | None]:
    """Page numbers to render, with `None` marking a collapsed gap ("…")."""
    if pages <= 1:
        return [1]
    keep = {1, pages, page}
    keep.update(range(1, edges + 1))
    keep.update(range(pages - edges + 1, pages + 1))
    keep.update(range(max(page - around, 1), min(page + around, pages) + 1))
    result: list[int | None] = []
    for n in sorted(k for k in keep if 1 <= k <= pages):
        if result and result[-1] is not None and n - result[-1] > 1:
            result.append(None)
        result.append(n)
    return result


def listed_slugs(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        "SELECT slug, created_at, ends_at, drawn_at FROM giveaways WHERE listed = 1 AND hidden = 0"
        " ORDER BY created_at DESC"
    ).fetchall()


# ------------------------------------------------------------------------- entries


def get_entry(conn: sqlite3.Connection, giveaway_id: int, user_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM entries WHERE giveaway_id = ? AND user_id = ?", (giveaway_id, user_id)
    ).fetchone()


def eligibility_problem(giveaway: dict, user: dict) -> str | None:
    """Why this user can't enter, or None when they can."""
    if status_of(giveaway) != "open":
        return "This giveaway has ended."
    if user["id"] == giveaway["owner_id"]:
        return "You are hosting this giveaway."
    if user["banned"]:
        return "Your account is not allowed to participate."
    allowed = [i for i in giveaway["allowed_instances"].split(",") if i]
    if allowed and user["instance"] not in allowed:
        return "This giveaway is limited to accounts on: " + ", ".join(allowed)
    min_days = giveaway["min_account_age_days"]
    if min_days:
        created = db.parse_iso(user["account_created_at"])
        if created is None or created > db.now() - timedelta(days=min_days):
            return f"Your Mastodon account must be at least {min_days} days old to enter."
    return None


def enter_giveaway(conn: sqlite3.Connection, giveaway: dict, user: dict, agreed: bool) -> None:
    if not agreed:
        raise ValidationError("You need to confirm you have read the conditions.")
    problem = eligibility_problem(giveaway, user)
    if problem:
        raise ValidationError(problem)
    conn.execute(
        "INSERT OR IGNORE INTO entries (giveaway_id, user_id, created_at) VALUES (?, ?, ?)",
        (giveaway["id"], user["id"], db.iso(db.now())),
    )


def withdraw(conn: sqlite3.Connection, giveaway: dict, user: dict) -> None:
    if status_of(giveaway) != "open":
        raise ValidationError("This giveaway has ended.")
    conn.execute(
        "DELETE FROM entries WHERE giveaway_id = ? AND user_id = ?", (giveaway["id"], user["id"])
    )


# --------------------------------------------------------------------------- drawing


def _eligible_entrants(conn: sqlite3.Connection, giveaway_id: int) -> list[dict]:
    """This giveaway's non-banned entrants who don't already hold a seat in it."""
    held = {
        row["user_id"]
        for row in conn.execute(
            "SELECT user_id FROM winners WHERE giveaway_id = ? AND user_id IS NOT NULL",
            (giveaway_id,),
        ).fetchall()
    }
    return [
        row
        for row in conn.execute(
            "SELECT u.* FROM entries e JOIN users u ON u.id = e.user_id"
            " WHERE e.giveaway_id = ? AND u.banned = 0 ORDER BY e.id",
            (giveaway_id,),
        ).fetchall()
        if row["id"] not in held
    ]


def draw(conn: sqlite3.Connection, giveaway: dict) -> list[dict]:
    """Fill every still-open seat with a distinct entrant, CSPRNG, no repeats within
    this giveaway. Leftover seats stay winnerless if there aren't enough entrants
    to fill all of them. Returns the seats newly filled.

    If *no* entrant at all was eligible (nobody entered, or everybody who did
    already holds another seat) and `restart_if_unclaimed` is set, the giveaway
    is reopened for fresh entries instead of being marked ended with zero
    winners, the same way an unclaimed seat reopens it in `_restart_unclaimed`:
    `ends_at` is pushed out and `drawn_at` stays null, so `due_giveaways` picks
    it back up and this keeps happening indefinitely until someone actually
    wins a seat. A giveaway that manages to fill at least one seat still ends
    normally even with other seats left empty - only a total no-show restarts.
    """
    now = db.now()
    open_seats = conn.execute(
        "SELECT * FROM winners WHERE giveaway_id = ? AND user_id IS NULL ORDER BY seat",
        (giveaway["id"],),
    ).fetchall()
    entrants = _eligible_entrants(conn, giveaway["id"])
    chosen = secrets.SystemRandom().sample(entrants, k=min(len(entrants), len(open_seats)))
    filled_ids = []
    for seat, winner in zip(open_seats, chosen, strict=False):
        conn.execute(
            "UPDATE winners SET user_id = ?, drawn_at = ?, claim_deadline = ? WHERE id = ?",
            (winner["id"], db.iso(now), db.iso(now + CLAIM_WINDOW), seat["id"]),
        )
        filled_ids.append(seat["id"])
    if not filled_ids and open_seats and giveaway["restart_if_unclaimed"]:
        conn.execute(
            "UPDATE giveaways SET ends_at = ? WHERE id = ?",
            (db.iso(now + timedelta(hours=_reopen_hours(giveaway))), giveaway["id"]),
        )
        log.info("%s: no eligible entrants at the deadline, reopened", giveaway["slug"])
    else:
        conn.execute(
            "UPDATE giveaways SET drawn_at = ? WHERE id = ? AND drawn_at IS NULL",
            (db.iso(now), giveaway["id"]),
        )
    return [w for w in get_winners(conn, giveaway["id"]) if w["id"] in filled_ids]


def due_giveaways(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        GIVEAWAY_SELECT + " WHERE g.drawn_at IS NULL AND g.ends_at <= ? ORDER BY g.ends_at",
        (db.iso(db.now()),),
    ).fetchall()


def notify_winner(conn: sqlite3.Connection, giveaway: dict, winner: dict) -> bool:
    """DM one seat's winner from the site's own account.

    Best effort; the site is the source of truth. `winner` is a `winners` row
    (joined to its user via `user_acct` etc., see `WINNER_SELECT`).
    """
    if not winner["user_id"] or winner["winner_notified_at"]:
        return False
    if not settings.announce_enabled:
        return False
    url = settings.url(f"/{giveaway['slug']}")
    title = neutralize_for_post(giveaway["title"])
    days = CLAIM_WINDOW.days
    prize = f"1 of {giveaway['winner_count']} prizes" if giveaway["winner_count"] > 1 else "it"
    text = (
        f'@{winner["user_acct"]} you won {prize} in "{title}" on giveaway.quest! 🎉\n'
        f"Log in within {days} days to claim your reward: {url}"
    )
    try:
        mastodon.post_status(
            settings.announce_instance, settings.announce_token, text, visibility="direct"
        )
    except mastodon.MastodonError:
        return False
    conn.execute(
        "UPDATE winners SET winner_notified_at = ? WHERE id = ?",
        (db.iso(db.now()), winner["id"]),
    )
    return True


def draw_due() -> list[str]:
    """Draw every giveaway past its deadline. Each in its own transaction. Returns slugs drawn."""
    with db.connect() as conn:
        due = due_giveaways(conn)
    drawn: list[str] = []
    for giveaway in due:
        with db.connect() as conn:
            filled = draw(conn, giveaway)
            for winner in filled:
                notify_winner(conn, giveaway, winner)
        drawn.append(giveaway["slug"])
    return drawn


def mark_reward_viewed(conn: sqlite3.Connection, winner: dict) -> None:
    if not winner["reward_viewed_at"]:
        conn.execute(
            "UPDATE winners SET reward_viewed_at = ? WHERE id = ?",
            (db.iso(db.now()), winner["id"]),
        )


# --------------------------------------------------------------------------- claiming
#
# After the draw the winner sees only a "Claim reward" button, not the reward
# itself. They have `CLAIM_WINDOW` to claim; miss it and (if `restart_if_unclaimed`
# is set) `process_unclaimed` drops their entry and re-draws among whoever is left,
# reopening the giveaway for fresh entries only when nobody remains.


def reward_claim_status(giveaway: dict, winner: dict | None) -> str:
    """Where one seat's reward stands: ``none`` (no winner drawn for it), ``claimed``,
    ``waiting`` (winner drawn, still claimable) or ``unclaimed`` (window elapsed).

    ``unclaimed`` only happens with ``restart_if_unclaimed`` set - that is the
    signal `process_unclaimed` acts on. Without it the reward stays claimable
    indefinitely rather than hard-locking a winner who logs in late.
    """
    if not winner or not winner["user_id"]:
        return "none"
    if winner["claimed_at"]:
        return "claimed"
    if not giveaway["restart_if_unclaimed"]:
        return "waiting"
    deadline = db.parse_iso(winner["claim_deadline"])
    if deadline and deadline > db.now():
        return "waiting"
    return "unclaimed"


def claim_reward(conn: sqlite3.Connection, giveaway: dict, user: dict) -> dict:
    """Let the caller unlock their seat's reward. Raises ValidationError if they can't."""
    winner = get_winner_for_user(conn, giveaway["id"], user["id"])
    if not winner:
        raise ValidationError("You are not a winner of this giveaway.")
    if winner["claimed_at"]:
        raise ValidationError("You have already claimed this reward.")
    deadline_guard = "" if not giveaway["restart_if_unclaimed"] else " AND claim_deadline > :now"
    cur = conn.execute(
        f"""UPDATE winners SET claimed_at = :now
            WHERE id = :id AND user_id = :uid AND claimed_at IS NULL
                  AND drawn_at IS NOT NULL{deadline_guard}""",
        {"now": db.iso(db.now()), "id": winner["id"], "uid": user["id"]},
    )
    if cur.rowcount == 0:
        raise ValidationError("The claim window has closed - the reward is being re-drawn.")
    return get_giveaway(conn, giveaway["slug"])


def _reopen_hours(giveaway: dict) -> int:
    """Run length to reuse when reopening: the stored duration, else derived from
    the original created_at .. ends_at span (for pre-v5 giveaways)."""
    if giveaway["duration_hours"]:
        return int(giveaway["duration_hours"])
    start, end = db.parse_iso(giveaway["created_at"]), db.parse_iso(giveaway["ends_at"])
    if start and end:
        return max(1, round((end - start).total_seconds() / 3600))
    return 72


def _get_giveaway_by_id(conn: sqlite3.Connection, giveaway_id: int) -> dict | None:
    return conn.execute(GIVEAWAY_SELECT + " WHERE g.id = ?", (giveaway_id,)).fetchone()


def _get_winner(conn: sqlite3.Connection, winner_id: int) -> dict | None:
    return conn.execute(WINNER_SELECT + " WHERE w.id = ?", (winner_id,)).fetchone()


def _restart_unclaimed(conn: sqlite3.Connection, giveaway: dict, winner: dict) -> str:
    """Drop the no-show's entry, then re-draw this seat among the rest, or reopen
    the whole giveaway for fresh entries if nobody eligible is left.

    Returns ``"redrawn"`` or ``"reopened"``. Other seats in the same giveaway
    (already claimed or still waiting) are untouched either way.
    """
    conn.execute(
        "DELETE FROM entries WHERE giveaway_id = ? AND user_id = ?",
        (giveaway["id"], winner["user_id"]),
    )
    conn.execute(
        """UPDATE winners
             SET user_id = NULL, drawn_at = NULL, claim_deadline = NULL,
                 winner_notified_at = NULL, claimed_at = NULL,
                 unclaimed_count = unclaimed_count + 1
           WHERE id = ?""",
        (winner["id"],),
    )
    entrants = _eligible_entrants(conn, giveaway["id"])
    now = db.now()
    if entrants:
        replacement = secrets.choice(entrants)
        conn.execute(
            "UPDATE winners SET user_id = ?, drawn_at = ?, claim_deadline = ? WHERE id = ?",
            (replacement["id"], db.iso(now), db.iso(now + CLAIM_WINDOW), winner["id"]),
        )
        notify_winner(conn, giveaway, _get_winner(conn, winner["id"]))
        return "redrawn"
    conn.execute(
        "UPDATE giveaways SET drawn_at = NULL, ends_at = ? WHERE id = ?",
        (db.iso(now + timedelta(hours=_reopen_hours(giveaway))), giveaway["id"]),
    )
    return "reopened"


def process_unclaimed() -> list[str]:
    """Re-draw or reopen seats whose winner let the claim window lapse.

    Runs from the background loop (and `gq draw`). Each seat in its own
    transaction. Returns the slug of the giveaway for every seat handled (a
    slug can appear more than once if several of its seats needed handling in
    the same pass).
    """
    with db.connect() as conn:
        due = conn.execute(
            """SELECT w.id AS winner_id, w.giveaway_id
               FROM winners w JOIN giveaways g ON g.id = w.giveaway_id
               WHERE w.user_id IS NOT NULL AND w.claimed_at IS NULL
                     AND g.restart_if_unclaimed = 1
                     AND w.claim_deadline IS NOT NULL AND w.claim_deadline <= ?
               ORDER BY w.claim_deadline""",
            (db.iso(db.now()),),
        ).fetchall()
    handled: list[str] = []
    for row in due:
        with db.connect() as conn:
            giveaway = _get_giveaway_by_id(conn, row["giveaway_id"])
            winner = _get_winner(conn, row["winner_id"])
            if not giveaway or not winner or reward_claim_status(giveaway, winner) != "unclaimed":
                continue  # claimed (or already handled) between the SELECT and here
            outcome = _restart_unclaimed(conn, giveaway, winner)
        log.info("%s seat %s unclaimed: %s", giveaway["slug"], winner["seat"], outcome)
        handled.append(giveaway["slug"])
    return handled


def ended_without_winners(conn: sqlite3.Connection) -> list[dict]:
    """Ended giveaways that filled zero seats even though `restart_if_unclaimed`
    was set - the ones a pre-fix build of `draw()` ended for good instead of
    reopening (see its docstring). One-off repair target for `gq admin
    reopen-empty`; going forward `draw()` reopens these itself, so this list
    should only ever contain giveaways that ended before the fix shipped.
    """
    return conn.execute(
        GIVEAWAY_SELECT
        + """ WHERE g.drawn_at IS NOT NULL AND g.restart_if_unclaimed = 1
                AND NOT EXISTS (
                    SELECT 1 FROM winners w WHERE w.giveaway_id = g.id AND w.user_id IS NOT NULL
                )
              ORDER BY g.drawn_at"""
    ).fetchall()


def reopen_giveaway(conn: sqlite3.Connection, giveaway: dict) -> None:
    """Clear `drawn_at` and push `ends_at` out by `_reopen_hours`, same as the
    zero-entrant reopen `draw()` now does on its own. Used to repair giveaways
    from `ended_without_winners` (`gq admin reopen-empty`)."""
    conn.execute(
        "UPDATE giveaways SET drawn_at = NULL, ends_at = ? WHERE id = ?",
        (db.iso(db.now() + timedelta(hours=_reopen_hours(giveaway))), giveaway["id"]),
    )
