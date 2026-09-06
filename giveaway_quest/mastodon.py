"""Minimal Mastodon OAuth + API client (sync httpx; handlers run in a thread)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx

from . import db
from .config import MASTODON_SCOPES, SITE_NAME, settings

INSTANCE_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
# Mastodon usernames are [A-Za-z0-9_]; anything else came from a server we
# don't trust and must not end up in an `acct` (which admins ban by) or a DM.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
TIMEOUT = httpx.Timeout(15.0)


class MastodonError(Exception):
    pass


def normalize_instance(raw: str) -> str:
    """Normalise user input like 'https://fosstodon.org/' or '@user@fosstodon.org' to a host."""
    value = raw.strip().lower()
    value = re.sub(r"^https?://", "", value)
    if "@" in value:
        value = value.rsplit("@", 1)[-1]
    value = value.split("/", 1)[0].strip(". ")
    if not INSTANCE_RE.match(value):
        raise MastodonError("That does not look like a Mastodon server name.")
    return value


def redirect_uri() -> str:
    return settings.url("/auth/callback")


def share_url(text: str) -> str:
    """Link to Mastodon's official share page, pre-filled. No token or JS needed.

    Mastodon's old per-instance ``/share`` intent is deprecated and breaks when the
    text contains a URL (mastodon#33681); share.joinmastodon.org is the official
    replacement and works regardless of which server the user is on.
    """
    return f"https://share.joinmastodon.org/#text={quote(text)}"


def get_or_register_app(conn: sqlite3.Connection, instance: str) -> dict:
    """Dynamic client registration: one OAuth app per instance, cached in the db."""
    uri = redirect_uri()
    app = conn.execute("SELECT * FROM mastodon_apps WHERE instance = ?", (instance,)).fetchone()
    if app and app["redirect_uri"] == uri and app["scopes"] == MASTODON_SCOPES:
        return app
    try:
        resp = httpx.post(
            f"https://{instance}/api/v1/apps",
            data={
                "client_name": SITE_NAME,
                "redirect_uris": uri,
                "scopes": MASTODON_SCOPES,
                "website": settings.base_url,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        client_id, client_secret = data["client_id"], data["client_secret"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise MastodonError(f"Could not register with {instance}: {exc}") from exc
    conn.execute(
        """INSERT INTO mastodon_apps
             (instance, client_id, client_secret, redirect_uri, scopes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(instance) DO UPDATE SET client_id=excluded.client_id,
             client_secret=excluded.client_secret, redirect_uri=excluded.redirect_uri,
             scopes=excluded.scopes, created_at=excluded.created_at""",
        (instance, client_id, client_secret, uri, MASTODON_SCOPES, db.iso(db.now())),
    )
    return conn.execute("SELECT * FROM mastodon_apps WHERE instance = ?", (instance,)).fetchone()


def authorize_url(app: dict, state: str) -> str:
    query = urlencode(
        {
            "client_id": app["client_id"],
            "redirect_uri": app["redirect_uri"],
            "response_type": "code",
            "scope": app["scopes"],
            "state": state,
        }
    )
    return f"https://{app['instance']}/oauth/authorize?{query}"


def exchange_code(app: dict, code: str) -> str:
    try:
        resp = httpx.post(
            f"https://{app['instance']}/oauth/token",
            data={
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
                "redirect_uri": app["redirect_uri"],
                "grant_type": "authorization_code",
                "code": code,
                "scope": app["scopes"],
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise MastodonError(f"Login with {app['instance']} failed: {exc}") from exc


@dataclass(slots=True)
class RemoteAccount:
    remote_id: str
    username: str
    display_name: str
    avatar_url: str
    profile_url: str
    created_at: str | None


def safe_https_url(value: object, fallback: str = "") -> str:
    """Only ever put ``https://`` URLs from a remote server into ``href``/``src``.

    Anyone can run a Mastodon-compatible server, so its profile/avatar URLs are
    attacker-controlled; a ``javascript:`` URL here would be stored XSS.
    """
    if isinstance(value, str) and value.startswith("https://") and len(value) <= 2048:
        return value
    return fallback


def account_from_json(instance: str, data: dict) -> RemoteAccount:
    username = data["username"]
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise MastodonError(f"{instance} returned an invalid username.")
    display_name = data.get("display_name")
    created_at = data.get("created_at")
    return RemoteAccount(
        remote_id=str(data["id"]),
        username=username,
        display_name=(display_name if isinstance(display_name, str) else "")[:200] or username,
        avatar_url=safe_https_url(data.get("avatar_static") or data.get("avatar")),
        profile_url=safe_https_url(data.get("url"), f"https://{instance}/@{username}"),
        created_at=created_at if isinstance(created_at, str) else None,
    )


def verify_credentials(instance: str, token: str) -> RemoteAccount:
    try:
        resp = httpx.get(
            f"https://{instance}/api/v1/accounts/verify_credentials",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return account_from_json(instance, resp.json())
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise MastodonError(f"Could not read your profile from {instance}: {exc}") from exc


def revoke_token(app: dict, token: str) -> None:
    """Best effort: the token has done its one job (reading the profile), so drop it server-side.

    We never store it, so a failure here only leaves an unused token on the
    user's Mastodon "Authorized apps" page.
    """
    try:
        httpx.post(
            f"https://{app['instance']}/oauth/revoke",
            data={
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
                "token": token,
            },
            timeout=TIMEOUT,
        )
    except httpx.HTTPError:
        pass


def post_status(instance: str, token: str, text: str, *, visibility: str = "public") -> dict:
    """Post a status from the site's own account. Returns the created status ('id' and 'url')."""
    try:
        resp = httpx.post(
            f"https://{instance}/api/v1/statuses",
            headers={"Authorization": f"Bearer {token}"},
            data={"status": text, "visibility": visibility},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MastodonError(f"Posting to {instance} failed: {exc}") from exc
