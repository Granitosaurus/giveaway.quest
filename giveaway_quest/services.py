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

COMMENT_TTL = timedelta(minutes=5)
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
       (SELECT COUNT(*) FROM entries e WHERE e.giveaway_id = g.id) AS entry_count,
       w.acct AS winner_acct, w.display_name AS winner_name, w.profile_url AS winner_profile_url
FROM giveaways g
JOIN users u ON u.id = g.owner_id
LEFT JOIN users w ON w.id = g.winner_id
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


@dataclass(slots=True)
class GiveawayForm:
    title: str
    reward: str
    conditions: str
    quest: str
    allowed_instances: str
    min_account_age_days: int
    listed: bool
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
            reward=_clean_text(data.get("reward"), REWARD_MAX, required=True, name="The reward"),
            conditions=_clean_text(data.get("conditions"), TEXT_MAX, name="Conditions"),
            quest=_clean_text(data.get("quest"), TEXT_MAX, name="Quest"),
            allowed_instances=allowed,
            min_account_age_days=min_age,
            listed=data.get("listed") == "on",
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


def create_giveaway(conn: sqlite3.Connection, owner: dict, form: GiveawayForm) -> dict:
    now = db.now()
    slug = new_slug(conn)
    conn.execute(
        """INSERT INTO giveaways (slug, owner_id, title, reward, conditions, quest,
                                  allowed_instances, min_account_age_days, listed,
                                  created_at, ends_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            slug,
            owner["id"],
            form.title,
            form.reward,
            form.conditions,
            form.quest,
            form.allowed_instances,
            form.min_account_age_days,
            int(form.listed),
            db.iso(now),
            db.iso(now + timedelta(hours=form.hours)),
        ),
    )
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
    conditions = _clean_text(data.get("conditions"), TEXT_MAX, name="Conditions")
    quest = _clean_text(data.get("quest"), TEXT_MAX, name="Quest")
    listed = data.get("listed") == "on"
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
             SET conditions = ?, quest = ?, listed = ?, ends_at = ?, post_url = ?, post_id = ?
           WHERE id = ?""",
        (conditions, quest, int(listed), ends_at, post_url or None, post_id, giveaway["id"]),
    )
    return get_giveaway(conn, giveaway["slug"])


def delete_giveaway(conn: sqlite3.Connection, giveaway_id: int) -> None:
    conn.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))


def get_giveaway(conn: sqlite3.Connection, slug: str) -> dict | None:
    return conn.execute(GIVEAWAY_SELECT + " WHERE g.slug = ?", (slug,)).fetchone()


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


def draw(conn: sqlite3.Connection, giveaway: dict) -> dict | None:
    """Pick a winner uniformly at random with a CSPRNG. Returns the winning user or None."""
    entrants = conn.execute(
        "SELECT u.* FROM entries e JOIN users u ON u.id = e.user_id"
        " WHERE e.giveaway_id = ? AND u.banned = 0 ORDER BY e.id",
        (giveaway["id"],),
    ).fetchall()
    winner = secrets.choice(entrants) if entrants else None
    conn.execute(
        "UPDATE giveaways SET drawn_at = ?, winner_id = ? WHERE id = ? AND drawn_at IS NULL",
        (db.iso(db.now()), winner["id"] if winner else None, giveaway["id"]),
    )
    return winner


def due_giveaways(conn: sqlite3.Connection) -> list[dict]:
    return conn.execute(
        GIVEAWAY_SELECT + " WHERE g.drawn_at IS NULL AND g.ends_at <= ? ORDER BY g.ends_at",
        (db.iso(db.now()),),
    ).fetchall()


def notify_winner(conn: sqlite3.Connection, giveaway: dict) -> bool:
    """DM the winner from the site's own account. Best effort; the site is the source of truth."""
    if not giveaway["winner_id"] or giveaway["winner_notified_at"]:
        return False
    if not settings.announce_enabled:
        return False
    winner = get_user(conn, giveaway["winner_id"])
    if not winner:
        return False
    url = settings.url(f"/{giveaway['slug']}")
    title = neutralize_for_post(giveaway["title"])
    text = (
        f'@{winner["acct"]} you won "{title}" on giveaway.quest! 🎉\nLog in to see your code: {url}'
    )
    try:
        mastodon.post_status(
            settings.announce_instance, settings.announce_token, text, visibility="direct"
        )
    except mastodon.MastodonError:
        return False
    conn.execute(
        "UPDATE giveaways SET winner_notified_at = ? WHERE id = ?",
        (db.iso(db.now()), giveaway["id"]),
    )
    return True


def draw_due() -> list[str]:
    """Draw every giveaway past its deadline. Each in its own transaction. Returns slugs drawn."""
    with db.connect() as conn:
        due = due_giveaways(conn)
    drawn: list[str] = []
    for giveaway in due:
        with db.connect() as conn:
            draw(conn, giveaway)
            fresh = get_giveaway(conn, giveaway["slug"])
            notify_winner(conn, fresh)
        drawn.append(giveaway["slug"])
    return drawn


def mark_reward_viewed(conn: sqlite3.Connection, giveaway: dict) -> None:
    if not giveaway["reward_viewed_at"]:
        conn.execute(
            "UPDATE giveaways SET reward_viewed_at = ? WHERE id = ?",
            (db.iso(db.now()), giveaway["id"]),
        )
