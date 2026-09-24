import json
import re
import sqlite3
from datetime import timedelta

import pytest

from giveaway_quest import db, mastodon, services
from tests.conftest import claim, create_giveaway, csrf, login_as, make_user

_LD_JSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


def _ld_objects(html: str) -> dict:
    """Structured-data blocks on a page, keyed by top-level @type (skips @graph blocks)."""
    out = {}
    for block in _LD_JSON.findall(html):
        obj = json.loads(block)
        if "@type" in obj:
            out[obj["@type"]] = obj
    return out


def test_schema_v1_to_v2_renames_secret_to_reward(tmp_path):
    # Hand-build a pre-v2 `giveaways` table (still named `secret`) so init_db()
    # has to run every migration up to the current version, v2's rename included.
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE giveaways (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            secret TEXT NOT NULL,
            conditions TEXT NOT NULL DEFAULT '',
            quest TEXT NOT NULL DEFAULT '',
            allowed_instances TEXT NOT NULL DEFAULT '',
            min_account_age_days INTEGER NOT NULL DEFAULT 0,
            listed INTEGER NOT NULL DEFAULT 1,
            hidden INTEGER NOT NULL DEFAULT 0,
            post_url TEXT,
            post_id TEXT,
            created_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            drawn_at TEXT,
            winner_id INTEGER,
            winner_notified_at TEXT,
            secret_viewed_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO giveaways (slug, owner_id, title, secret, created_at, ends_at)"
        " VALUES ('a-b-c', 1, 'T', 'KEY-123', '2026-01-01Z', '2026-02-01Z')"
    )
    conn.executescript("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    db.init_db(path)  # runs the v2..v6 migrations in one pass

    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
    assert "secret" not in cols and "secret_viewed_at" not in cols
    assert "reward" not in cols  # v6 moved it off giveaways onto `winners`
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    reward = conn.execute(
        "SELECT w.reward FROM winners w JOIN giveaways g ON g.id = w.giveaway_id"
        " WHERE g.slug = 'a-b-c'"
    ).fetchone()[0]
    assert reward == "KEY-123"
    conn.close()


def test_front_page_and_meta(client):
    assert client.get("/").status_code == 200
    robots = client.get("/robots.txt")
    assert "Allow: /" in robots.text and "Sitemap:" in robots.text
    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200 and "<urlset" in sitemap.text
    assert client.get("/does-not-exist").status_code == 404
    assert client.get("/new", follow_redirects=False).status_code == 302

    graphs = [json.loads(b) for b in _LD_JSON.findall(client.get("/").text)]
    assert len(graphs) == 1
    types = {node["@type"] for node in graphs[0]["@graph"]}
    assert types == {"Organization", "WebSite"}


def test_oauth_login_roundtrip(client, fake):
    fake.accounts["token-for-thecode"] = mastodon.RemoteAccount(
        remote_id="42",
        username="bob",
        display_name="Bob",
        avatar_url="",
        profile_url="https://example.social/@bob",
        created_at="2021-05-05T00:00:00Z",
    )
    resp = client.post(
        "/auth/login",
        data={
            "instance": "https://Example.Social/",
            "next": "/new",
            "_csrf_token": csrf(client, "/auth/login"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://example.social/oauth/authorize?")
    state = client.get_session_data()["oauth"]["state"]

    # wrong state is rejected
    bad = client.get(
        "/auth/callback", params={"code": "thecode", "state": "nope"}, follow_redirects=False
    )
    assert bad.headers["location"] == "/auth/login"
    assert "user_id" not in client.get_session_data()

    # redo login, then a correct callback logs in and lands on `next`
    client.post(
        "/auth/login",
        data={"instance": "example.social", "next": "/new", "_csrf_token": csrf(client)},
        follow_redirects=False,
    )
    state = client.get_session_data()["oauth"]["state"]
    ok = client.get(
        "/auth/callback", params={"code": "thecode", "state": state}, follow_redirects=False
    )
    assert ok.headers["location"] == "/new"
    assert client.get_session_data()["user_id"]
    page = client.get("/new")
    assert page.status_code == 200 and "@bob@example.social" in page.text


def test_create_announce_enter_draw_win(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client)
    assert "-" in slug
    assert not fake.posted  # the site never posts on create

    # the host gets a prefilled "Share on Mastodon" link
    page = client.get(f"/{slug}")
    assert "Share on Mastodon" in page.text
    assert "share.joinmastodon.org/#text=" in page.text

    # the giveaway page carries valid Event + BreadcrumbList structured data
    ld = _ld_objects(page.text)
    assert ld["Event"]["url"].endswith(f"/{slug}")
    assert ld["Event"]["organizer"]["identifier"] == "@host@example.social"
    assert [i["position"] for i in ld["BreadcrumbList"]["itemListElement"]] == [1, 2]

    # an admin announces it from the site's own account
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        services.announce_on_mastodon(conn, g, "Free key, pet a cat!")
    assert fake.posted[0]["instance"] == "botsrv.social"
    assert fake.posted[0]["text"].endswith(f"http://testserver/{slug}")

    page = client.get(f"/{slug}")
    assert page.status_code == 200
    assert "Psychonauts 2" in page.text and "pet a cat" in page.text
    assert 'property="og:title"' in page.text
    assert "AAAA-BBBB-CCCC" in page.text  # host can see their own code
    assert "You're hosting this" in page.text
    assert slug in client.get("/sitemap.xml").text
    assert slug in client.get("/").text

    # host cannot enter their own giveaway
    resp = client.post(
        f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client)}, follow_redirects=True
    )
    assert "You are hosting" in resp.text

    # participant enters (must tick the box)
    player = make_user("player@other.social")
    login_as(client, player)
    page = client.get(f"/{slug}")
    assert "AAAA-BBBB-CCCC" not in page.text
    resp = client.post(f"/{slug}/enter", data={"_csrf_token": csrf(client)}, follow_redirects=True)
    assert "confirm you have read" in resp.text
    resp = client.post(
        f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client)}, follow_redirects=True
    )
    assert "You're in" in resp.text
    assert "Withdraw entry" in resp.text

    # nothing happens before the deadline
    assert services.draw_due() == []

    # move the deadline into the past and draw
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET ends_at = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(minutes=1)), slug),
        )
    assert services.draw_due() == [slug]
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        seat = services.get_winners(conn, g["id"])[0]
    assert seat["user_id"] == player["id"] and g["drawn_at"] and seat["winner_notified_at"]
    assert seat["claim_deadline"] and not seat["claimed_at"]
    dm = fake.posted[-1]
    assert dm["visibility"] == "direct" and "@player@other.social" in dm["text"]
    assert "claim your reward" in dm["text"].lower()
    assert dm["instance"] == "botsrv.social"  # DM'd from the site account, not the host

    # the winner sees only a claim button until they claim
    page = client.get(f"/{slug}")
    assert "Claim reward" in page.text and "AAAA-BBBB-CCCC" not in page.text
    claim(client, slug)

    # after claiming, the code is revealed and the view is recorded
    page = client.get(f"/{slug}")
    assert "That's you" in page.text and "AAAA-BBBB-CCCC" in page.text
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        seat = services.get_winners(conn, g["id"])[0]
    assert seat["claimed_at"] and seat["reward_viewed_at"]

    # somebody else does not see the code
    login_as(client, make_user("loser@other.social"))
    page = client.get(f"/{slug}")
    assert "AAAA-BBBB-CCCC" not in page.text and "@player@other.social" in page.text


def test_eligibility_rules(client):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(
        client, allowed_instances="fosstodon.org, mastodon.social", min_account_age_days="30"
    )

    newbie = make_user("newbie@fosstodon.org", created_at=db.iso(db.now() - timedelta(days=3)))
    login_as(client, newbie)
    assert "at least 30 days old" in client.get(f"/{slug}").text

    outsider = make_user("out@elsewhere.social")
    login_as(client, outsider)
    assert "limited to accounts on: fosstodon.org, mastodon.social" in client.get(f"/{slug}").text

    veteran = make_user("vet@mastodon.social")
    login_as(client, veteran)
    resp = client.post(
        f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client)}, follow_redirects=True
    )
    assert "You're in" in resp.text
    resp = client.post(
        f"/{slug}/withdraw", data={"_csrf_token": csrf(client)}, follow_redirects=True
    )
    assert "entry was removed" in resp.text


def test_announce_failure_leaves_giveaway_unannounced(client, fake):
    login_as(client, make_user())
    slug = create_giveaway(client)
    fake.fail_post = True
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        with pytest.raises(mastodon.MastodonError):
            services.announce_on_mastodon(conn, g, "hi")
    with db.connect() as conn:
        assert services.get_giveaway(conn, slug)["post_url"] is None

    # the background pass also records the failure without binding a post
    assert services.announce_due() == []
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert g["post_url"] is None and g["post_id"] is None and g["announce_attempted_at"]
    assert "hasn't gone through yet" in client.get(f"/{slug}").text


def test_login_page_is_read_only(client):
    page = " ".join(client.get("/auth/login").text.split())
    assert "never posts, follows, or sends messages as you" in page


def test_logged_out_visitor_gets_a_share_link(client):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client)
    client.set_session_data({})  # log out
    page = client.get(f"/{slug}").text
    assert "share.joinmastodon.org/#text=" in page and "Share on Mastodon" in page


def test_validation_errors(client):
    login_as(client, make_user())
    resp = client.post(
        "/new",
        data={
            "title": "",
            "winner_count": "1",
            "reward_1": "x",
            "hours": "1",
            "_csrf_token": csrf(client, "/new"),
        },
    )
    assert resp.status_code == 422 and "Title is required" in resp.text
    resp = client.post(
        "/new",
        data={
            "title": "t",
            "winner_count": "1",
            "reward_1": "x",
            "hours": "99999",
            "_csrf_token": csrf(client),
        },
    )
    assert resp.status_code == 422 and "between 1 hour and 90 days" in resp.text
    resp = client.post(
        "/new",
        data={
            "title": "t",
            "winner_count": "1",
            "reward_1": "x",
            "hours": "1",
            "allowed_instances": "not a host",
            "_csrf_token": csrf(client),
        },
    )
    assert resp.status_code == 422 and "Allowed servers" in resp.text
    resp = client.post(
        "/new",
        data={
            "title": "t",
            "winner_count": "1",
            "hours": "1",
            "_csrf_token": csrf(client),
        },
    )
    assert resp.status_code == 422 and "Reward 1 is required" in resp.text
    resp = client.post(
        "/new",
        data={
            "title": "t",
            "winner_count": "99",
            "reward_1": "x",
            "hours": "1",
            "_csrf_token": csrf(client),
        },
    )
    assert resp.status_code == 422 and "Number of winners must be between 1 and 10" in resp.text


def test_edit_delete_and_unlisted(client):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client)
    other = make_user("other@example.social")

    login_as(client, other)
    assert client.get(f"/{slug}/edit").status_code == 403
    assert client.post(f"/{slug}/delete", data={"_csrf_token": csrf(client)}).status_code == 403

    login_as(client, host)
    resp = client.post(
        f"/{slug}/edit",
        data={
            "winner_count": "1",
            "reward_1": "UPDATED-CODE",
            "quest": "hug a dog",
            "conditions": "",
            "hours": "1",
            "post_url": "https://example.social/@host/1",
            "_csrf_token": csrf(client),
        },
        follow_redirects=True,
    )
    assert "Giveaway updated" in resp.text and "hug a dog" in resp.text
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        seat = services.get_winners(conn, g["id"])[0]
    assert db.parse_iso(g["ends_at"]) < db.now() + timedelta(hours=1, minutes=1)
    assert g["listed"] == 0  # checkbox not sent -> unlisted
    assert seat["reward"] == "UPDATED-CODE"  # reward is editable pre-draw too
    assert slug not in client.get("/").text
    assert slug not in client.get("/sitemap.xml").text
    assert client.get(f"/{slug}").status_code == 200  # still reachable by link
    assert slug in client.get("/mine").text

    resp = client.post(
        f"/{slug}/delete", data={"_csrf_token": csrf(client)}, follow_redirects=False
    )
    assert resp.headers["location"] == "/mine"
    assert client.get(f"/{slug}").status_code == 404


def test_admin_hide_and_backup(client, tmp_path):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client)
    with db.connect() as conn:
        conn.execute("UPDATE giveaways SET hidden = 1 WHERE slug = ?", (slug,))
    assert client.get("/").text.count(slug) == 0
    assert client.get(f"/{slug}").status_code == 200  # owner still sees it
    login_as(client, make_user("someone@x.social"))
    assert client.get(f"/{slug}").status_code == 404
    login_as(client, make_user("admin@example.social"))
    assert client.get(f"/{slug}").status_code == 200

    path = db.backup(tmp_path / "backups", keep=1)
    assert path.exists() and path.stat().st_size > 0


def test_csrf_required(client):
    login_as(client, make_user())
    resp = client.post(
        "/new", data={"title": "t", "winner_count": "1", "reward_1": "x", "hours": "1"}
    )
    assert resp.status_code == 403


def test_reward_is_rendered_markdown_for_the_winner(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(
        client,
        winner_count="1",
        reward_1="**A shiny sword!**\n\nRedeem `AAAA-BBBB-CCCC` at https://example.com",
    )

    player = make_user("player@other.social")
    login_as(client, player)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client)})
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET ends_at = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(minutes=1)), slug),
        )
    assert services.draw_due() == [slug]
    claim(client, slug)

    page = client.get(f"/{slug}").text
    assert "<strong>A shiny sword!</strong>" in page
    assert "<code>AAAA-BBBB-CCCC</code>" in page
    assert "Here is your reward" in page


def test_reward_is_the_only_field_editable_after_the_draw(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client, quest="pet a cat", winner_count="1", reward_1="WRONG-CODE")

    player = make_user("player@other.social")
    login_as(client, player)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client)})
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET ends_at = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(minutes=1)), slug),
        )
    assert services.draw_due() == [slug]
    with db.connect() as conn:
        before = services.get_giveaway(conn, slug)
        before_seat = services.get_winners(conn, before["id"])[0]

    login_as(client, host)
    assert "Edit rewards" in client.get(f"/{slug}").text  # host panel button, post-draw
    assert "Edit rewards" in client.get(f"/{slug}/edit").text

    # an empty reward is still rejected
    resp = client.post(
        f"/{slug}/edit",
        data={f"reward_{before_seat['id']}": "  ", "_csrf_token": csrf(client, f"/{slug}/edit")},
    )
    assert resp.status_code == 422 and "The reward" in resp.text

    # the reward is corrected; everything else sent along is ignored
    resp = client.post(
        f"/{slug}/edit",
        data={
            f"reward_{before_seat['id']}": "RIGHT-CODE",
            "quest": "hug a dog",
            "hours": "500",
            "listed": "on",
            "_csrf_token": csrf(client, f"/{slug}/edit"),
        },
        follow_redirects=True,
    )
    assert "Reward updated" in resp.text
    with db.connect() as conn:
        after = services.get_giveaway(conn, slug)
        after_seat = services.get_winners(conn, after["id"])[0]
    assert after_seat["reward"] == "RIGHT-CODE"
    for field in ("quest", "ends_at", "listed"):
        assert after[field] == before[field]
    for field in ("user_id", "drawn_at", "claim_deadline"):
        assert after_seat[field] == before_seat[field]

    # the winner sees the corrected reward
    login_as(client, player)
    claim(client, slug)
    assert "RIGHT-CODE" in client.get(f"/{slug}").text

    # and nobody else can touch it
    login_as(client, make_user("nosy@other.social"))
    resp = client.post(
        f"/{slug}/edit",
        data={f"reward_{before_seat['id']}": "STOLEN", "_csrf_token": csrf(client)},
    )
    assert resp.status_code == 403


def test_md_preview_endpoint(client):
    login_as(client, make_user())
    resp = client.post(
        "/md-preview",
        data={"text": "# Hi\n\n*there*", "_csrf_token": csrf(client, "/new")},
    )
    assert resp.status_code in (200, 201)
    assert "<h1>Hi</h1>" in resp.text and "<em>there</em>" in resp.text
