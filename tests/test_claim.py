"""The winner must claim the reward within CLAIM_WINDOW; unclaimed rewards are re-drawn."""

import sqlite3
from datetime import timedelta

from giveaway_quest import db, services
from tests.conftest import claim, create_giveaway, csrf, login_as, make_user


def _run_to_draw(client, slug: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET ends_at = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(minutes=1)), slug),
        )
    services.draw_due()


def _expire_claim(slug: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET claim_deadline = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(minutes=1)), slug),
        )


def _enter(client, slug: str, acct: str) -> dict:
    user = make_user(acct)
    login_as(client, user)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    return user


def test_claim_is_required_and_owner_bound(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client)
    player = _enter(client, slug, "player@other.social")
    _run_to_draw(client, slug)

    # a non-winner cannot claim
    login_as(client, make_user("nosy@other.social"))
    resp = client.post(
        f"/{slug}/claim", data={"_csrf_token": csrf(client, f"/{slug}")}, follow_redirects=True
    )
    assert "not the winner" in resp.text
    with db.connect() as conn:
        assert not services.get_giveaway(conn, slug)["claimed_at"]

    # the winner claims once; a second claim is rejected
    login_as(client, player)
    claim(client, slug)
    resp = client.post(
        f"/{slug}/claim", data={"_csrf_token": csrf(client, f"/{slug}")}, follow_redirects=True
    )
    assert "already claimed" in resp.text


def test_unclaimed_reward_is_redrawn_among_remaining(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client)
    a = _enter(client, slug, "a@other.social")
    b = _enter(client, slug, "b@other.social")
    _run_to_draw(client, slug)

    with db.connect() as conn:
        first = services.get_giveaway(conn, slug)["winner_id"]
    assert first in (a["id"], b["id"])

    _expire_claim(slug)
    posts_before = len(fake.posted)
    assert services.process_unclaimed() == [slug]

    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        entrant_ids = [
            r["user_id"]
            for r in conn.execute("SELECT user_id FROM entries WHERE giveaway_id = ?", (g["id"],))
        ]
    other = b["id"] if first == a["id"] else a["id"]
    assert g["winner_id"] == other  # the only entrant left
    assert first not in entrant_ids  # the no-show's entry was dropped
    assert g["drawn_at"] and g["claim_deadline"] and not g["claimed_at"]
    assert g["winner_notified_at"] and g["unclaimed_count"] == 1
    assert len(fake.posted) == posts_before + 1  # the replacement winner was DM'd
    assert services.status_of(g) == "ended"


def test_unclaimed_reopens_when_no_entrants_left(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, hours="48")
    player = _enter(client, slug, "solo@other.social")
    _run_to_draw(client, slug)
    _expire_claim(slug)

    assert services.process_unclaimed() == [slug]
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert g["winner_id"] is None and g["drawn_at"] is None and g["claim_deadline"] is None
    assert g["unclaimed_count"] == 1
    assert services.status_of(g) == "open"
    assert db.parse_iso(g["ends_at"]) > db.now() + timedelta(hours=47)

    # the giveaway runs again and can be won on a second pass
    login_as(client, player)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    _run_to_draw(client, slug)
    with db.connect() as conn:
        assert services.get_giveaway(conn, slug)["winner_id"] == player["id"]


def test_restart_disabled_leaves_it_claimable_indefinitely(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, restart_if_unclaimed="")
    player = _enter(client, slug, "player@other.social")
    _run_to_draw(client, slug)
    _expire_claim(slug)

    assert services.process_unclaimed() == []  # nothing to do without the flag
    with db.connect() as conn:
        assert services.reward_claim_status(services.get_giveaway(conn, slug)) == "waiting"

    # the late winner can still claim
    login_as(client, player)
    claim(client, slug)
    page = client.get(f"/{slug}").text
    assert "AAAA-BBBB-CCCC" in page


def test_claim_after_window_is_rejected_when_restart_enabled(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client)
    player = _enter(client, slug, "player@other.social")
    _run_to_draw(client, slug)
    _expire_claim(slug)

    login_as(client, player)
    resp = client.post(
        f"/{slug}/claim", data={"_csrf_token": csrf(client, f"/{slug}")}, follow_redirects=True
    )
    assert "claim window has closed" in resp.text
    assert "You didn't claim in time" in client.get(f"/{slug}").text


def test_schema_v5_grandfathers_drawn_giveaways(tmp_path):
    path = tmp_path / "old.sqlite3"
    db.init_db(path)
    conn = sqlite3.connect(path)
    v5_cols = (
        "restart_if_unclaimed",
        "duration_hours",
        "claim_deadline",
        "claimed_at",
        "unclaimed_count",
    )
    for col in v5_cols:
        conn.execute(f"ALTER TABLE giveaways DROP COLUMN {col}")
    conn.executescript("PRAGMA user_version = 4;")
    conn.execute(
        "INSERT INTO giveaways (slug, owner_id, title, reward, created_at, ends_at, drawn_at,"
        " winner_id, reward_viewed_at) VALUES"
        " ('a-b-c', 1, 'T', 'KEY', '2026-01-01Z', '2026-01-08Z', '2026-01-08Z', 5, '2026-01-09Z')"
    )
    conn.commit()
    conn.close()

    db.init_db(path)  # runs the v5 migration

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    g = conn.execute("SELECT * FROM giveaways WHERE slug = 'a-b-c'").fetchone()
    assert g["claimed_at"] == "2026-01-09Z"  # already-drawn winner is grandfathered in
    assert g["restart_if_unclaimed"] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
