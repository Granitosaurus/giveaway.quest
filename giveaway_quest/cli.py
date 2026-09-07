"""`gq` command line: run the server, moderate content, draw winners, back up the database."""

from __future__ import annotations

import sys
from pathlib import Path

from cyclopts import App

from . import db, mastodon, services
from .config import settings

app = App(name="gq", help="giveaway.quest administration.")
admin = App(name="admin", help="Moderation: list, hide, delete giveaways; ban users.")
app.command(admin)


@app.command
def serve(
    host: str = "127.0.0.1",
    port: int = 8012,
    reload: bool = False,
    debug: bool = False,
    workers: int = 1,
) -> None:
    """Run the web server (uvicorn)."""
    import os

    import uvicorn

    if debug:
        # Reload/multi-worker modes re-import config.py in a fresh subprocess,
        # so flip the env var too, not just the already-loaded settings object.
        os.environ["GQ_DEBUG"] = "1"
        settings.debug = True

    db.init_db()
    uvicorn.run(
        "giveaway_quest.app:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers,
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload_dirs=[str(Path(__file__).parent)] if reload else None,
    )


@app.command
def init_db() -> None:
    """Create the database and tables if missing."""
    db.init_db()
    print(f"database ready at {settings.db_path}")


@app.command
def draw() -> None:
    """Draw winners for all giveaways past their deadline (the server does this automatically)."""
    db.init_db()
    drawn = services.draw_due()
    print(f"drew {len(drawn)} giveaway(s): {', '.join(drawn) or '-'}")


@app.command
def announce(slug: str, text: str | None = None) -> None:
    """Post a giveaway announcement from the site's own Mastodon account (GQ_ANNOUNCE_*)."""
    db.init_db()
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        if not g:
            sys.exit("not found")
        # The default text is host-written; keep the official account from
        # mentioning/tagging/linking whatever the host typed. `--text` is yours.
        body = text or services.neutralize_for_post(
            services.suggested_post_text(g["title"], g["quest"])
        )
        try:
            g = services.announce_on_mastodon(conn, g, body)
        except mastodon.MastodonError as exc:
            sys.exit(str(exc))
    print(g["post_url"])


@app.command
def backup(dest: Path | None = None, keep: int = 48) -> None:
    """Snapshot the database into the backups directory, keeping the newest KEEP files."""
    if not settings.db_path.exists():
        sys.exit(f"no database at {settings.db_path}")
    path = db.backup(dest, keep=keep)
    print(path)


def _fmt(g: dict) -> str:
    flags = "".join(
        [
            "H" if g["hidden"] else "-",
            "L" if g["listed"] else "-",
            "D" if g["drawn_at"] else "-",
        ]
    )
    return (
        f"{g['slug']:<32} {flags} ends={g['ends_at']} entries={g['entry_count']:<4} "
        f"host={g['owner_acct']} :: {g['title'][:50]}"
    )


@admin.command(name="list")
def list_(all: bool = False, hidden: bool = False, q: str = "", limit: int = 50) -> None:
    """List giveaways. Flags column: H=hidden by admin, L=listed on front page, D=drawn."""
    where = ["1=1"]
    params: list = []
    if hidden:
        where.append("g.hidden = 1")
    elif not all:
        where.append("g.hidden = 0 AND g.drawn_at IS NULL")
    if q:
        where.append("(g.title LIKE ? OR g.slug LIKE ? OR u.acct LIKE ?)")
        params += [f"%{q}%"] * 3
    with db.connect() as conn:
        rows = conn.execute(
            services.GIVEAWAY_SELECT
            + " WHERE "
            + " AND ".join(where)
            + " ORDER BY g.created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
    for g in rows:
        print(_fmt(g))
    print(f"({len(rows)} shown)")


@admin.command
def show(slug: str) -> None:
    """Print everything about one giveaway, including the secret and entrants."""
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        if not g:
            sys.exit("not found")
        entrants = conn.execute(
            "SELECT u.acct, e.created_at FROM entries e JOIN users u ON u.id = e.user_id"
            " WHERE e.giveaway_id = ? ORDER BY e.id",
            (g["id"],),
        ).fetchall()
    for key, value in g.items():
        print(f"{key:>22}: {value}")
    print(f"{'entrants':>22}: {len(entrants)}")
    for e in entrants:
        print(f"{'':>24}{e['acct']}  ({e['created_at']})")


def _set_hidden(slug: str, hidden: bool) -> None:
    with db.connect() as conn:
        cur = conn.execute("UPDATE giveaways SET hidden = ? WHERE slug = ?", (int(hidden), slug))
        if cur.rowcount == 0:
            sys.exit("not found")
    print(f"{slug}: hidden={hidden}")


@admin.command
def hide(slug: str) -> None:
    """Hide a giveaway from everyone except its host (soft moderation)."""
    _set_hidden(slug, True)


@admin.command
def unhide(slug: str) -> None:
    """Undo `hide`."""
    _set_hidden(slug, False)


@admin.command
def delete(slug: str, yes: bool = False) -> None:
    """Permanently delete a giveaway and its entries."""
    if not yes:
        sys.exit("refusing without --yes")
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM giveaways WHERE slug = ?", (slug,))
        if cur.rowcount == 0:
            sys.exit("not found")
    print(f"{slug}: deleted")


@admin.command
def ban(acct: str, unban: bool = False) -> None:
    """Ban (or --unban) a user by acct (user@instance). Banned users can't log in or win."""
    acct = acct.lstrip("@").lower()
    with db.connect() as conn:
        cur = conn.execute("UPDATE users SET banned = ? WHERE acct = ?", (int(not unban), acct))
        if cur.rowcount == 0:
            sys.exit("no such user (they need to have logged in at least once)")
    print(f"{acct}: banned={not unban}")


@admin.command
def users(limit: int = 50) -> None:
    """List known users."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT acct, banned, account_created_at, last_login_at,"
            " (SELECT COUNT(*) FROM giveaways g WHERE g.owner_id = users.id) AS hosted,"
            " (SELECT COUNT(*) FROM entries e WHERE e.user_id = users.id) AS entries"
            " FROM users ORDER BY last_login_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    for u in rows:
        flag = "BANNED " if u["banned"] else ""
        print(
            f"{u['acct']:<40} {flag}hosted={u['hosted']} entries={u['entries']}"
            f" last_login={u['last_login_at']}"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
