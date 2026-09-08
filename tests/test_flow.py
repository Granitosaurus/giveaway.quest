import json
import re
import sqlite3
from datetime import timedelta

import pytest

from giveaway_quest import db, mastodon, services
from tests.conftest import create_giveaway, csrf, login_as, make_user

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
    path = tmp_path / "old.sqlite3"
    db.init_db(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        "ALTER TABLE giveaways RENAME COLUMN reward TO secret;"
        "ALTER TABLE giveaways RENAME COLUMN reward_viewed_at TO secret_viewed_at;"
        "PRAGMA user_version = 1;"
    )
    conn.execute(
        "INSERT INTO giveaways (slug, owner_id, title, secret, created_at, ends_at)"
        " VALUES ('a-b-c', 1, 'T', 'KEY-123', '2026-01-01Z', '2026-02-01Z')"
    )
    conn.commit()
    conn.close()

    db.init_db(path)  # runs the v2 migration

    conn = sqlite3.connect(path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(giveaways)")}
    assert "reward" in cols and "reward_viewed_at" in cols
    assert "secret" not in cols and "secret_viewed_at" not in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT reward FROM giveaways").fetchone()[0] == "KEY-123"
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
    assert g["winner_id"] == player["id"] and g["drawn_at"] and g["winner_notified_at"]
    dm = fake.posted[-1]
    assert dm["visibility"] == "direct" and "@player@other.social" in dm["text"]
    assert dm["instance"] == "botsrv.social"  # DM'd from the site account, not the host

    # winner sees the code, and the view is recorded
    page = client.get(f"/{slug}")
    assert "That's you" in page.text and "AAAA-BBBB-CCCC" in page.text
    with db.connect() as conn:
        assert services.get_giveaway(conn, slug)["reward_viewed_at"]

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
        "/new", data={"title": "", "reward": "x", "hours": "1", "_csrf_token": csrf(client, "/new")}
    )
    assert resp.status_code == 422 and "Title is required" in resp.text
    resp = client.post(
        "/new", data={"title": "t", "reward": "x", "hours": "99999", "_csrf_token": csrf(client)}
    )
    assert resp.status_code == 422 and "between 1 hour and 90 days" in resp.text
    resp = client.post(
        "/new",
        data={
            "title": "t",
            "reward": "x",
            "hours": "1",
            "allowed_instances": "not a host",
            "_csrf_token": csrf(client),
        },
    )
    assert resp.status_code == 422 and "Allowed servers" in resp.text


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
    assert db.parse_iso(g["ends_at"]) < db.now() + timedelta(hours=1, minutes=1)
    assert g["listed"] == 0  # checkbox not sent -> unlisted
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
    resp = client.post("/new", data={"title": "t", "reward": "x", "hours": "1"})
    assert resp.status_code == 403


def test_reward_is_rendered_markdown_for_the_winner(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(
        client,
        reward="**A shiny sword!**\n\nRedeem `AAAA-BBBB-CCCC` at https://example.com",
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

    page = client.get(f"/{slug}").text
    assert "<strong>A shiny sword!</strong>" in page
    assert "<code>AAAA-BBBB-CCCC</code>" in page
    assert "Here is your reward" in page


def test_md_preview_endpoint(client):
    login_as(client, make_user())
    resp = client.post(
        "/md-preview",
        data={"text": "# Hi\n\n*there*", "_csrf_token": csrf(client, "/new")},
    )
    assert resp.status_code in (200, 201)
    assert "<h1>Hi</h1>" in resp.text and "<em>there</em>" in resp.text
