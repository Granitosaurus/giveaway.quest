"""Comments = the reply thread of the giveaway's Mastodon announcement post."""

from datetime import timedelta

from giveaway_quest import db, services
from tests.conftest import create_giveaway, csrf, login_as, make_user


def reply(
    sid,
    parent,
    *,
    content="<p>nice one</p>",
    acct="carol@other.social",
    visibility="public",
    created="2026-09-08T10:00:00.000Z",
):
    user, _, domain = acct.partition("@")
    domain = domain or "botsrv.social"
    return {
        "id": str(sid),
        "in_reply_to_id": str(parent),
        "visibility": visibility,
        "created_at": created,
        "url": f"https://{domain}/@{user}/{sid}",
        "content": content,
        "account": {
            "acct": acct,
            "username": user,
            "display_name": user.title(),
            "url": f"https://{domain}/@{user}",
            "avatar_static": f"https://{domain}/{user}.png",
        },
    }


def announce(slug: str) -> dict:
    with db.connect() as conn:
        g = services.get_giveaway(conn, slug)
        return services.announce_on_mastodon(conn, g, "Free stuff!")


def host_giveaway(client, **overrides) -> str:
    login_as(client, make_user("host@example.social"))
    return create_giveaway(client, **overrides)


def test_thread_renders_nested_and_in_order(client, fake):
    slug = host_giveaway(client)
    g = announce(slug)
    fake.contexts[g["post_id"]] = [
        reply(10, g["post_id"], content="<p>first</p>", created="2026-09-08T10:00:00Z"),
        reply(11, 10, content="<p>a nested reply</p>", created="2026-09-08T10:05:00Z"),
        reply(12, 11, content="<p>deeper still</p>", created="2026-09-08T10:10:00Z"),
    ]
    page = client.get(f"/{slug}").text

    assert "Comments" in page and "Reply on Mastodon" in page
    assert page.index("first") < page.index("a nested reply") < page.index("deeper still")
    assert "sm:ml-6" in page and "sm:ml-12" in page  # depth 1 and 2 are indented
    assert "https://other.social/@carol" in page  # author links out to their profile


def test_comment_html_is_sanitized(client, fake):
    slug = host_giveaway(client)
    g = announce(slug)
    fake.contexts[g["post_id"]] = [
        reply(
            10,
            g["post_id"],
            content='<p>hello <a href="javascript:evil()">x</a></p><script>alert(1)</script>',
        )
    ]
    page = client.get(f"/{slug}").text

    assert "hello" in page
    assert "<script>alert(1)</script>" not in page
    assert "javascript:evil" not in page
    assert 'rel="nofollow noopener noreferrer ugc"' in page  # nh3 rewrites the safe link


def test_non_public_and_banned_replies_are_hidden(client, fake):
    slug = host_giveaway(client)
    g = announce(slug)
    make_user("eve@other.social")
    with db.connect() as conn:
        conn.execute("UPDATE users SET banned = 1 WHERE acct = ?", ("eve@other.social",))
    fake.contexts[g["post_id"]] = [
        reply(10, g["post_id"], content="<p>public and fine</p>"),
        reply(11, g["post_id"], content="<p>followers only</p>", visibility="private"),
        reply(12, g["post_id"], content="<p>from a banned user</p>", acct="eve@other.social"),
    ]
    page = client.get(f"/{slug}").text

    assert "public and fine" in page
    assert "followers only" not in page
    assert "from a banned user" not in page


def test_thread_is_cached(client, fake):
    slug = host_giveaway(client)
    g = announce(slug)
    fake.contexts[g["post_id"]] = [reply(10, g["post_id"])]

    client.get(f"/{slug}")
    client.get(f"/{slug}")
    assert fake.context_calls == 1  # second view is served from the cache


def test_stale_cache_is_served_when_the_refresh_fails(client, fake):
    slug = host_giveaway(client)
    g = announce(slug)
    fake.contexts[g["post_id"]] = [reply(10, g["post_id"], content="<p>cached comment</p>")]
    client.get(f"/{slug}")  # populates the cache

    with db.connect() as conn:
        conn.execute(
            "UPDATE comment_threads SET fetched_at = ?",
            (db.iso(db.now() - timedelta(hours=1)),),
        )
    fake.fail_context = True
    page = client.get(f"/{slug}").text

    assert fake.context_calls == 2  # it tried to refresh
    assert "cached comment" in page  # and fell back to the last good copy
    assert "couldn't be loaded" not in page


def test_comments_unavailable_on_first_fetch_failure(client, fake):
    slug = host_giveaway(client)
    announce(slug)
    fake.fail_context = True
    page = client.get(f"/{slug}").text
    assert "couldn't be loaded" in page


def test_no_comment_section_without_an_announcement(client, fake):
    slug = host_giveaway(client)
    page = client.get(f"/{slug}").text
    assert 'id="comments"' not in page
    assert "Reply on Mastodon" not in page
    assert fake.context_calls == 0


def test_pasted_announcement_url_enables_comments(client, fake):
    slug = host_giveaway(client)
    login_as(client, make_user("host@example.social"))
    fake.contexts["777"] = [reply(10, 777, content="<p>found via pasted url</p>")]
    client.post(
        f"/{slug}/edit",
        data={
            "quest": "pet a cat",
            "conditions": "",
            "hours": "48",
            "listed": "on",
            "post_url": "https://botsrv.social/@giveaway/777",
            "_csrf_token": csrf(client, f"/{slug}/edit"),
        },
        follow_redirects=False,
    )
    with db.connect() as conn:
        assert services.get_giveaway(conn, slug)["post_id"] == "777"
    assert "found via pasted url" in client.get(f"/{slug}").text
