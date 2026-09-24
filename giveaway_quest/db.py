"""Thin sqlite layer: one connection per unit of work, plain SQL, rows as dicts."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS mastodon_apps (
    instance      TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    scopes        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id                 INTEGER PRIMARY KEY,
    instance           TEXT NOT NULL,
    remote_id          TEXT NOT NULL,
    acct               TEXT NOT NULL,   -- username@instance, lower-cased
    username           TEXT NOT NULL,
    display_name       TEXT NOT NULL DEFAULT '',
    avatar_url         TEXT NOT NULL DEFAULT '',
    profile_url        TEXT NOT NULL DEFAULT '',
    account_created_at TEXT,            -- when the Mastodon account was created
    banned             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    last_login_at      TEXT NOT NULL,
    UNIQUE (instance, remote_id)
);

CREATE TABLE IF NOT EXISTS giveaways (
    id                   INTEGER PRIMARY KEY,
    slug                 TEXT NOT NULL UNIQUE,
    owner_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    conditions           TEXT NOT NULL DEFAULT '',
    quest                TEXT NOT NULL DEFAULT '',
    allowed_instances    TEXT NOT NULL DEFAULT '', -- comma separated, '' = everyone
    min_account_age_days INTEGER NOT NULL DEFAULT 0,
    listed               INTEGER NOT NULL DEFAULT 1, -- show on the front page / sitemap
    hidden               INTEGER NOT NULL DEFAULT 0, -- admin moderation
    restart_if_unclaimed INTEGER NOT NULL DEFAULT 1, -- re-draw/reopen seats a winner never claims
    duration_hours       INTEGER,                    -- original run length, reused when reopening
    winner_count         INTEGER NOT NULL DEFAULT 1, -- number of seats/rewards, see `winners`
    post_url             TEXT,
    post_id              TEXT,
    announce_attempted_at TEXT,               -- last auto-announce try (for retry spacing)
    created_at           TEXT NOT NULL,
    ends_at              TEXT NOT NULL,
    drawn_at             TEXT                -- last time the background job resolved open seats
);
CREATE INDEX IF NOT EXISTS giveaways_ends_at ON giveaways (ends_at);
CREATE INDEX IF NOT EXISTS giveaways_owner ON giveaways (owner_id);

-- One row per reward/seat. `winner_count` seats are created (user_id NULL) when
-- the giveaway is made; draw() fills in user_id/drawn_at/claim_deadline for
-- whichever seats are still open. Claim/notify/reopen state all live here so
-- each seat's no-show only affects its own seat, see NOTES.md.
CREATE TABLE IF NOT EXISTS winners (
    id                 INTEGER PRIMARY KEY,
    giveaway_id        INTEGER NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    seat               INTEGER NOT NULL,          -- 1..winner_count, stable display order
    reward             TEXT NOT NULL,             -- host Markdown, this seat's code
    user_id            INTEGER REFERENCES users(id) ON DELETE SET NULL,
    drawn_at           TEXT,
    winner_notified_at TEXT,
    claim_deadline     TEXT,               -- winner must claim by here (drawn_at + CLAIM_WINDOW)
    claimed_at         TEXT,               -- when the winner claimed and unlocked the reward
    reward_viewed_at   TEXT,
    unclaimed_count    INTEGER NOT NULL DEFAULT 0, -- times this seat was re-drawn/reopened
    UNIQUE (giveaway_id, seat)
);
CREATE INDEX IF NOT EXISTS winners_giveaway ON winners (giveaway_id);
CREATE INDEX IF NOT EXISTS winners_user ON winners (user_id);

CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY,
    giveaway_id INTEGER NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    UNIQUE (giveaway_id, user_id)
);
CREATE INDEX IF NOT EXISTS entries_giveaway ON entries (giveaway_id);

-- Cached reply thread of the Mastodon announcement post, shown as comments.
-- `payload` is the normalised, already-sanitised comment list as JSON; refreshed
-- when older than services.COMMENT_TTL. Nothing here is authoritative.
CREATE TABLE IF NOT EXISTS comment_threads (
    giveaway_id INTEGER PRIMARY KEY REFERENCES giveaways(id) ON DELETE CASCADE,
    status_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Stable, sortable UTC timestamp format used everywhere in the database."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


SCHEMA_VERSION = 6


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """Bring an existing database from `version` up to SCHEMA_VERSION, one step at a time."""
    if version < 1:
        # v1: the login token was stored but never used; stop keeping it (2026-09-06 review).
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "access_token" in columns:
            conn.execute("ALTER TABLE users DROP COLUMN access_token")
    if version < 2:
        # v2: the "code being given away" became a host-written Markdown "reward".
        columns = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
        if "secret" in columns:
            conn.execute("ALTER TABLE giveaways RENAME COLUMN secret TO reward")
        if "secret_viewed_at" in columns:
            conn.execute("ALTER TABLE giveaways RENAME COLUMN secret_viewed_at TO reward_viewed_at")
    if version < 3:
        # v3: added the `comment_threads` cache table. It is created by the
        # `CREATE TABLE IF NOT EXISTS` in SCHEMA above, so there is nothing to do
        # here but record the bump.
        pass
    if version < 4:
        # v4: auto-announce needs to remember the last attempt to space retries.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
        if "announce_attempted_at" not in columns:
            conn.execute("ALTER TABLE giveaways ADD COLUMN announce_attempted_at TEXT")
    if version < 5:
        # v5: the winner now has a fixed window to *claim* the reward before it is
        # re-drawn (services.CLAIM_WINDOW). Add the columns, then grandfather every
        # already-drawn giveaway as claimed so the deploy doesn't re-draw historic
        # winners or hide rewards they can already see.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
        adds = {
            "restart_if_unclaimed": "INTEGER NOT NULL DEFAULT 1",
            "duration_hours": "INTEGER",
            "claim_deadline": "TEXT",
            "claimed_at": "TEXT",
            "unclaimed_count": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, decl in adds.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE giveaways ADD COLUMN {name} {decl}")
        if "reward_viewed_at" in columns:
            # Only a real pre-v5 giveaways table has this column (a fresh install
            # jumping straight from 0 to SCHEMA_VERSION never did - it's dropped
            # again by v6 below).
            conn.execute(
                "UPDATE giveaways SET claimed_at = COALESCE(reward_viewed_at, drawn_at)"
                " WHERE drawn_at IS NOT NULL AND claimed_at IS NULL"
            )
    if version < 6:
        # v6: multiple winners per giveaway. `winners` (created by the CREATE TABLE
        # IF NOT EXISTS above) gets one seat-1 row per existing giveaway, carrying
        # over its single winner/reward, then the now-redundant columns are dropped
        # from `giveaways`. `winner_count` defaults to 1, matching the single seat
        # just created, so old giveaways behave exactly as before the migration.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
        if "winner_count" not in columns:
            conn.execute("ALTER TABLE giveaways ADD COLUMN winner_count INTEGER NOT NULL DEFAULT 1")
        if "reward" in columns:
            conn.execute(
                """INSERT INTO winners (giveaway_id, seat, reward, user_id, drawn_at,
                                        winner_notified_at, claim_deadline, claimed_at,
                                        reward_viewed_at, unclaimed_count)
                   SELECT id, 1, reward, winner_id, drawn_at, winner_notified_at,
                          claim_deadline, claimed_at, reward_viewed_at, unclaimed_count
                   FROM giveaways"""
            )
        # Re-check: a fresh install jumping straight from version 0 never had
        # `reward` etc., but v5 above unconditionally (re-)adds claim_deadline/
        # claimed_at/unclaimed_count, so those still need dropping here too.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
        for column in (
            "reward",
            "winner_id",
            "winner_notified_at",
            "claim_deadline",
            "claimed_at",
            "unclaimed_count",
            "reward_viewed_at",
        ):
            if column in columns:
                conn.execute(f"ALTER TABLE giveaways DROP COLUMN {column}")


def init_db(path: Path | None = None) -> None:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            _migrate(conn, version)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection for one unit of work. Commits on success, rolls back on error."""
    conn = sqlite3.connect(path or settings.db_path, timeout=10, isolation_level=None)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def backup(dest_dir: Path | None = None, keep: int = 48) -> Path:
    """Consistent online backup via sqlite's backup API. Keeps the newest `keep` files."""
    dest_dir = dest_dir or settings.backup_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    dest = dest_dir / f"giveaway-{stamp}.sqlite3"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(dest_dir.glob("giveaway-*.sqlite3"))
    for old in backups[:-keep] if keep > 0 else []:
        old.unlink()
    return dest
