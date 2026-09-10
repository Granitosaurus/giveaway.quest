import os
import re
from collections.abc import Iterator

import pytest

os.environ["GQ_DATA_DIR"] = ""  # replaced per test below
os.environ["GQ_BASE_URL"] = "http://testserver"
os.environ["GQ_SECRET_KEY"] = "test-secret"
os.environ["GQ_ADMINS"] = "admin@example.social"
os.environ["GQ_ANNOUNCE_INSTANCE"] = "botsrv.social"
os.environ["GQ_ANNOUNCE_TOKEN"] = "bot-token"
os.environ["GQ_RATE_LIMIT"] = "0"  # one app for the whole run; limits would carry across tests

from litestar.testing import TestClient  # noqa: E402

from giveaway_quest import db, mastodon, services  # noqa: E402
from giveaway_quest.app import app, session_config  # noqa: E402
from giveaway_quest.config import settings  # noqa: E402


class FakeMastodon:
    """Stands in for every network call the app makes to a Mastodon server."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.accounts: dict[str, mastodon.RemoteAccount] = {}
        self.revoked: list[str] = []
        self.fail_post = False
        # status_id -> list of `descendants` status dicts for fetch_context
        self.contexts: dict[str, list[dict]] = {}
        self.context_calls = 0
        self.fail_context = False

    def get_or_register_app(self, conn, instance):
        return {
            "instance": instance,
            "client_id": "cid",
            "client_secret": "sec",
            "redirect_uri": settings.url("/auth/callback"),
            "scopes": "read:accounts",
        }

    def exchange_code(self, app, code):
        return f"token-for-{code}"

    def verify_credentials(self, instance, token):
        return self.accounts[token]

    def revoke_token(self, app, token):
        self.revoked.append(token)

    def post_status(self, instance, token, text, *, visibility="public"):
        if self.fail_post:
            raise mastodon.MastodonError("boom")
        self.posted.append({"instance": instance, "text": text, "visibility": visibility})
        return {"id": str(len(self.posted)), "url": f"https://{instance}/@x/{len(self.posted)}"}

    def fetch_context(self, instance, token, status_id):
        self.context_calls += 1
        if self.fail_context:
            raise mastodon.MastodonError("thread unavailable")
        return {"ancestors": [], "descendants": self.contexts.get(str(status_id), [])}


@pytest.fixture
def fake(monkeypatch) -> FakeMastodon:
    fm = FakeMastodon()
    for name in (
        "get_or_register_app",
        "exchange_code",
        "verify_credentials",
        "revoke_token",
        "post_status",
        "fetch_context",
    ):
        monkeypatch.setattr(mastodon, name, getattr(fm, name))
    return fm


@pytest.fixture
def client(tmp_path, monkeypatch, fake) -> Iterator[TestClient]:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    db.init_db()
    with TestClient(app=app, session_config=session_config) as c:
        yield c


def make_user(acct: str = "alice@example.social", created_at: str = "2020-01-01T00:00:00Z") -> dict:
    username, instance = acct.split("@")
    account = mastodon.RemoteAccount(
        remote_id=str(abs(hash(acct)) % 10**8),
        username=username,
        display_name=username.title(),
        avatar_url="",
        profile_url=f"https://{instance}/@{username}",
        created_at=created_at,
    )
    with db.connect() as conn:
        return services.upsert_user(conn, instance, account)


def login_as(client: TestClient, user: dict) -> None:
    client.set_session_data({"user_id": user["id"]})


def csrf(client: TestClient, path: str = "/") -> str:
    resp = client.get(path)
    match = re.search(r'name="_csrf_token" value="([^"]+)"', resp.text)
    return match.group(1) if match else client.cookies.get("csrftoken", "")


def claim(client: TestClient, slug: str) -> None:
    """Winner claims the reward (unlocks the code). Caller must be logged in as the winner."""
    resp = client.post(
        f"/{slug}/claim", data={"_csrf_token": csrf(client, f"/{slug}")}, follow_redirects=True
    )
    assert resp.status_code == 200, resp.text


def create_giveaway(client: TestClient, **overrides) -> str:
    form = {
        "title": "Psychonauts 2",
        "reward": "AAAA-BBBB-CCCC",
        "quest": "pet a cat",
        "conditions": "US only",
        "hours": "48",
        "listed": "on",
        "restart_if_unclaimed": "on",
        "_csrf_token": csrf(client, "/new"),
    }
    form.update(overrides)
    resp = client.post("/new", data=form, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    return resp.headers["location"].strip("/")
