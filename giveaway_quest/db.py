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
    reward               TEXT NOT NULL,          -- host Markdown, winner-only
    conditions           TEXT NOT NULL DEFAULT '',
    quest                TEXT NOT NULL DEFAULT '',
    allowed_instances    TEXT NOT NULL DEFAULT '', -- comma separated, '' = everyone
    min_account_age_days INTEGER NOT NULL DEFAULT 0,
    listed               INTEGER NOT NULL DEFAULT 1, -- show on the front page / sitemap
    hidden               INTEGER NOT NULL DEFAULT 0, -- admin moderation
    post_url             TEXT,
    post_id              TEXT,
    created_at           TEXT NOT NULL,
    ends_at              TEXT NOT NULL,
    drawn_at             TEXT,
    winner_id            INTEGER REFERENCES users(id) ON DELETE SET NULL,
    winner_notified_at   TEXT,
    reward_viewed_at     TEXT
);
CREATE INDEX IF NOT EXISTS giveaways_ends_at ON giveaways (ends_at);
CREATE INDEX IF NOT EXISTS giveaways_owner ON giveaways (owner_id);

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


SCHEMA_VERSION = 3


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
