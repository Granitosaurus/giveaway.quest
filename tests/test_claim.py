"""Each seat has its own CLAIM_WINDOW; unclaimed seats are independently re-drawn."""

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


def _expire_claim(slug: str, seat: int | None = None) -> None:
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        sql = "UPDATE winners SET claim_deadline = ? WHERE giveaway_id = ?"
        params: list = [db.iso(db.now() - timedelta(minutes=1)), g["id"]]
        if seat is not None:
            sql += " AND seat = ?"
            params.append(seat)
        conn.execute(sql, params)


def _enter(client, slug: str, acct: str) -> dict:
    user = make_user(acct)
    login_as(client, user)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    return user


def _seats(slug: str) -> list[dict]:
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        return services.get_winners(conn, g["id"])


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
    assert "not a winner" in resp.text
    assert not _seats(slug)[0]["claimed_at"]

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

    first = _seats(slug)[0]["user_id"]
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
    seat = _seats(slug)[0]
    other = b["id"] if first == a["id"] else a["id"]
    assert seat["user_id"] == other  # the only entrant left
    assert first not in entrant_ids  # the no-show's entry was dropped
    assert g["drawn_at"] and seat["claim_deadline"] and not seat["claimed_at"]
    assert seat["winner_notified_at"] and seat["unclaimed_count"] == 1
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
    seat = _seats(slug)[0]
    assert seat["user_id"] is None and g["drawn_at"] is None and seat["claim_deadline"] is None
    assert seat["unclaimed_count"] == 1
    assert services.status_of(g) == "open"
    assert db.parse_iso(g["ends_at"]) > db.now() + timedelta(hours=47)

    # the giveaway runs again and can be won on a second pass
    login_as(client, player)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    _run_to_draw(client, slug)
    assert _seats(slug)[0]["user_id"] == player["id"]


def test_restart_disabled_leaves_it_claimable_indefinitely(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, restart_if_unclaimed="")
    player = _enter(client, slug, "player@other.social")
    _run_to_draw(client, slug)
    _expire_claim(slug)

    assert services.process_unclaimed() == []  # nothing to do without the flag
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert services.reward_claim_status(g, _seats(slug)[0]) == "waiting"

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


def test_draw_picks_distinct_winners_with_no_repeats(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, winner_count="3", reward_1="AAA", reward_2="BBB", reward_3="CCC")
    entrants = {_enter(client, slug, f"p{i}@other.social")["id"] for i in range(4)}
    _run_to_draw(client, slug)

    seats = _seats(slug)
    winner_ids = [s["user_id"] for s in seats]
    assert len(winner_ids) == 3
    assert len(set(winner_ids)) == 3  # no repeats
    assert set(winner_ids) <= entrants
    rewards = {s["reward"] for s in seats}
    assert rewards == {"AAA", "BBB", "CCC"}


def test_draw_partially_fills_seats_when_short_on_entrants(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, winner_count="3", reward_1="A", reward_2="B", reward_3="C")
    _enter(client, slug, "solo@other.social")
    _run_to_draw(client, slug)

    seats = _seats(slug)
    filled = [s for s in seats if s["user_id"]]
    assert len(filled) == 1
    assert len(seats) == 3
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert services.status_of(g) == "ended"


def test_no_show_only_affects_its_own_seat(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, winner_count="2", reward_1="A", reward_2="B")
    a = _enter(client, slug, "a@other.social")
    b = _enter(client, slug, "b@other.social")
    c = _enter(client, slug, "c@other.social")
    _run_to_draw(client, slug)

    seats = _seats(slug)
    winner_ids = {s["user_id"] for s in seats}
    assert len(winner_ids) == 2 and winner_ids <= {a["id"], b["id"], c["id"]}
    leftover_id = ({a["id"], b["id"], c["id"]} - winner_ids).pop()
    no_show_seat, kept_seat = seats[0], seats[1]
    kept_before = dict(kept_seat)

    _expire_claim(slug, seat=no_show_seat["seat"])
    assert services.process_unclaimed() == [slug]

    seats_after = {s["seat"]: s for s in _seats(slug)}
    # the untouched seat's winner, claim window and claim status are unchanged
    kept_after = seats_after[kept_seat["seat"]]
    assert kept_after["user_id"] == kept_before["user_id"]
    assert kept_after["claim_deadline"] == kept_before["claim_deadline"]
    # the no-show's seat was re-drawn to the only remaining eligible entrant
    redrawn = seats_after[no_show_seat["seat"]]
    assert redrawn["user_id"] == leftover_id
    assert redrawn["user_id"] != kept_after["user_id"]


def test_reopen_leaves_other_seats_untouched(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client, winner_count="2", reward_1="A", reward_2="B", hours="48")
    a = _enter(client, slug, "a@other.social")
    b = _enter(client, slug, "b@other.social")
    _run_to_draw(client, slug)

    seats = _seats(slug)
    no_show_seat, kept_seat = seats[0], seats[1]
    kept_before = dict(kept_seat)

    _expire_claim(slug, seat=no_show_seat["seat"])
    assert services.process_unclaimed() == [slug]

    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert services.status_of(g) == "open"  # reopened for fresh entries

    seats_after = {s["seat"]: s for s in _seats(slug)}
    reopened = seats_after[no_show_seat["seat"]]
    assert reopened["user_id"] is None and reopened["claim_deadline"] is None
    kept_after = seats_after[kept_seat["seat"]]
    assert kept_after["user_id"] == kept_before["user_id"]
    assert kept_after["claim_deadline"] == kept_before["claim_deadline"]

    # a fresh entrant fills the reopened seat; the other seat's winner is untouched
    fresh = a if kept_before["user_id"] == b["id"] else b
    login_as(client, fresh)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    third = _enter(client, slug, "third@other.social")
    _run_to_draw(client, slug)

    seats_final = {s["seat"]: s for s in _seats(slug)}
    assert seats_final[kept_seat["seat"]]["user_id"] == kept_before["user_id"]
    assert seats_final[no_show_seat["seat"]]["user_id"] in (fresh["id"], third["id"])


def test_pre_draw_edit_changes_winner_count(client, fake):
    login_as(client, make_user("host@example.social"))
    slug = create_giveaway(client)  # winner_count=1
    resp = client.post(
        f"/{slug}/edit",
        data={
            "winner_count": "3",
            "reward_1": "X1",
            "reward_2": "X2",
            "reward_3": "X3",
            "quest": "pet a cat",
            "conditions": "",
            "listed": "on",
            "restart_if_unclaimed": "on",
            "_csrf_token": csrf(client, f"/{slug}/edit"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert g["winner_count"] == 3
    seats = _seats(slug)
    assert [s["reward"] for s in seats] == ["X1", "X2", "X3"]


def test_post_draw_edit_changes_one_seat_only(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client, winner_count="2", reward_1="A", reward_2="B")
    _enter(client, slug, "a@other.social")
    _enter(client, slug, "b@other.social")
    _run_to_draw(client, slug)

    seats = _seats(slug)
    target = seats[0]
    login_as(client, host)
    resp = client.post(
        f"/{slug}/edit",
        data={
            f"reward_{target['id']}": "A-fixed",
            "_csrf_token": csrf(client, f"/{slug}/edit"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text
    seats_after = {s["id"]: s for s in _seats(slug)}
    assert seats_after[target["id"]]["reward"] == "A-fixed"
    other = seats[1]
    assert seats_after[other["id"]]["reward"] == other["reward"]


def test_schema_migrates_old_single_winner_giveaways_into_winners_table(tmp_path):
    # Hand-build a pre-v5 `giveaways` table (no restart_if_unclaimed/winner_count/
    # winners table yet) so init_db() has to run the v5 *and* v6 migrations.
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE giveaways (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            reward TEXT NOT NULL,
            conditions TEXT NOT NULL DEFAULT '',
            quest TEXT NOT NULL DEFAULT '',
            allowed_instances TEXT NOT NULL DEFAULT '',
            min_account_age_days INTEGER NOT NULL DEFAULT 0,
            listed INTEGER NOT NULL DEFAULT 1,
            hidden INTEGER NOT NULL DEFAULT 0,
            post_url TEXT,
            post_id TEXT,
            announce_attempted_at TEXT,
            created_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            drawn_at TEXT,
            winner_id INTEGER,
            winner_notified_at TEXT,
            reward_viewed_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO giveaways (slug, owner_id, title, reward, created_at, ends_at, drawn_at,"
        " winner_id, reward_viewed_at) VALUES"
        " ('a-b-c', 1, 'T', 'KEY', '2026-01-01Z', '2026-01-08Z', '2026-01-08Z', 5, '2026-01-09Z')"
    )
    conn.executescript("PRAGMA user_version = 4;")
    conn.commit()
    conn.close()

    db.init_db(path)  # runs the v5 and v6 migrations in one pass

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    g = conn.execute("SELECT * FROM giveaways WHERE slug = 'a-b-c'").fetchone()
    assert g["winner_count"] == 1
    assert g["restart_if_unclaimed"] == 1
    assert "reward" not in g.keys() and "winner_id" not in g.keys()

    w = conn.execute("SELECT * FROM winners WHERE giveaway_id = ?", (g["id"],)).fetchone()
    assert w["seat"] == 1 and w["reward"] == "KEY" and w["user_id"] == 5
    assert w["claimed_at"] == "2026-01-09Z"  # already-drawn winner is grandfathered in
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
