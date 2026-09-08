"""Auto-announcing listed giveaways from the site's own account (opens the comment thread)."""

from datetime import timedelta

from giveaway_quest import db, services
from tests.conftest import create_giveaway, login_as, make_user


def a_giveaway(client, **overrides) -> str:
    login_as(client, make_user("host@example.social"))
    return create_giveaway(client, **overrides)


def test_listed_giveaway_is_auto_announced(client, fake):
    slug = a_giveaway(client)
    assert not fake.posted  # nothing on create itself

    assert services.announce_due() == [slug]
    assert fake.posted[0]["instance"] == "botsrv.social"
    assert fake.posted[0]["text"].endswith(f"http://testserver/{slug}")
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert g["post_id"] and g["post_url"]
    assert services.announce_state(g) == "announced"

    assert services.announce_due() == []  # already done, not posted again


def test_unlisted_giveaway_is_not_announced(client, fake):
    slug = a_giveaway(client, listed="")
    assert services.announce_due() == []
    assert not fake.posted
    with db.connect() as conn:
        assert services.announce_state(services.get_giveaway(conn, slug)) == "unlisted"


def test_failed_announce_is_recorded_and_retried_with_spacing(client, fake):
    slug = a_giveaway(client)
    fake.fail_post = True

    assert services.announce_due() == []
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
    assert g["announce_attempted_at"] and not g["post_id"]
    assert services.announce_state(g) == "retrying"

    assert services.announce_due() == []  # within the retry window, not tried again
    assert not fake.posted

    with db.connect() as conn:
        conn.execute(
            "UPDATE giveaways SET announce_attempted_at = ? WHERE slug = ?",
            (db.iso(db.now() - timedelta(hours=2)), slug),
        )
    fake.fail_post = False
    assert services.announce_due() == [slug]


def test_auto_announce_can_be_disabled(client, fake, monkeypatch):
    slug = a_giveaway(client)
    monkeypatch.setattr(services.settings, "auto_announce", False)
    assert services.announce_due() == []
    assert not fake.posted
    with db.connect() as conn:
        assert services.announce_state(services.get_giveaway(conn, slug)) == "manual"


def test_auto_announced_thread_becomes_comments(client, fake):
    slug = a_giveaway(client)
    services.announce_due()
    with db.connect() as conn:
        sid = services.get_giveaway(conn, slug)["post_id"]
    fake.contexts[sid] = [
        {
            "id": "9",
            "in_reply_to_id": sid,
            "visibility": "public",
            "created_at": "2026-09-08T12:00:00Z",
            "url": "https://m.example/@x/9",
            "content": "<p>auto thread works</p>",
            "account": {
                "acct": "x@m.example",
                "username": "x",
                "display_name": "X",
                "url": "https://m.example/@x",
                "avatar_static": "https://m.example/x.png",
            },
        }
    ]
    assert "auto thread works" in client.get(f"/{slug}").text
