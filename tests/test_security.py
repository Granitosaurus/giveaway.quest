"""Regression tests for the 2026-09-06 security review (see security-review-2026-09-06.md)."""

import sqlite3

import pytest

from giveaway_quest import db, mastodon, services
from giveaway_quest.config import settings
from tests.conftest import create_giveaway, csrf, login_as, make_user

PROFILE = {
    "id": 7,
    "username": "mallory",
    "display_name": "Mallory",
    "url": "https://evil.example/@mallory",
    "avatar_static": "https://evil.example/a.png",
    "created_at": "2020-01-01T00:00:00Z",
}


def test_remote_profile_urls_must_be_https():
    """A hostile instance controls `url`/`avatar`; javascript: there would be stored XSS."""
    bad = {**PROFILE, "url": "javascript:alert(1)", "avatar_static": "data:text/html,x"}
    acct = mastodon.account_from_json("evil.example", bad)
    assert acct.profile_url == "https://evil.example/@mallory"
    assert acct.avatar_url == ""
    good = mastodon.account_from_json("evil.example", PROFILE)
    assert good.profile_url == "https://evil.example/@mallory"
    assert good.avatar_url == "https://evil.example/a.png"


@pytest.mark.parametrize("username", ["admin@fosstodon.org", "", "a b", 12, "x" * 65])
def test_remote_username_is_validated(username):
    with pytest.raises(mastodon.MastodonError):
        mastodon.account_from_json("evil.example", {**PROFILE, "username": username})


def test_login_token_is_revoked_and_never_stored(client, fake):
    fake.accounts["token-for-c"] = mastodon.account_from_json("example.social", PROFILE)
    client.post(
        "/auth/login",
        data={"instance": "example.social", "_csrf_token": csrf(client, "/auth/login")},
        follow_redirects=False,
    )
    state = client.get_session_data()["oauth"]["state"]
    client.get("/auth/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert fake.revoked == ["token-for-c"]
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert "access_token" not in columns


def test_migration_drops_stored_tokens(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute("ALTER TABLE users ADD COLUMN access_token TEXT")
        conn.execute(
            "INSERT INTO users (instance, remote_id, acct, username, access_token, created_at,"
            " last_login_at) VALUES ('x.y', '1', 'a@x.y', 'a', 'tok', 't', 't')"
        )
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "access_token" not in columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT acct FROM users").fetchone()[0] == "a@x.y"


def test_security_headers_and_no_inline_script(client):
    resp = client.get("/")
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "<script>" not in resp.text and "onsubmit=" not in resp.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/theme.js").status_code == 200
    # error pages carry the headers too
    assert "content-security-policy" in client.get("/nope").headers


def test_official_account_posts_cannot_mention_or_link(client, fake):
    host = make_user("host@example.social")
    login_as(client, host)
    slug = create_giveaway(client, title="@victim@other.social win #free https://spam.example")
    entrant = make_user("bob@example.social")
    login_as(client, entrant)
    client.post(f"/{slug}/enter", data={"agree": "on", "_csrf_token": csrf(client, f"/{slug}")})
    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET ends_at = '2000-01-01T00:00:00Z' WHERE slug = ?", (slug,)
        )
    services.draw_due()
    dm = fake.posted[-1]
    assert dm["visibility"] == "direct"
    assert dm["text"].startswith("@bob@example.social ")
    body = dm["text"].split(" ", 1)[1]
    assert "@victim" not in body and "#free" not in body and "https://spam" not in body
    assert "＠victim＠other.social" in body
    assert settings.url(f"/{slug}") in dm["text"]


def test_neutralize_for_post():
    assert services.neutralize_for_post("hi @a@b.c #x http://e.f/g") == "hi ＠a＠b.c ＃x e.f/g"
