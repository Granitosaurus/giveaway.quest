"""Login with Mastodon (OAuth 2 authorization code flow with dynamic app registration)."""

from __future__ import annotations

import secrets
from typing import Annotated

from litestar import Request, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromQuery, QueryParameter
from litestar.response import Redirect, Template

from .. import db, mastodon, services
from ..web import flash, render, write_rate_limit

Form = Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)]


def _safe_next(value: str | None) -> str:
    """Only allow local, absolute paths as post-login destinations."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@get("/login", sync_to_thread=True)
def login_form(
    request: Request, user: NamedDependency[dict | None], next: FromQuery[str | None] = None
) -> Template | Redirect:
    if user:
        return Redirect(_safe_next(next))
    return render(
        request,
        "login.html.jinja",
        user=user,
        next=_safe_next(next),
        last_instance=request.session.get("last_instance", ""),
    )


@post("/login", sync_to_thread=True, middleware=write_rate_limit)
def login_start(request: Request, data: Form) -> Redirect:
    next_url = _safe_next(data.get("next"))
    try:
        instance = mastodon.normalize_instance(data.get("instance", ""))
        with db.connect() as conn:
            app = mastodon.get_or_register_app(conn, instance)
    except mastodon.MastodonError as exc:
        flash(request, str(exc), "error")
        return Redirect(f"/auth/login?next={next_url}")
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "instance": instance, "next": next_url}
    request.session["last_instance"] = instance
    return Redirect(mastodon.authorize_url(app, state))


@get("/callback", sync_to_thread=True)
def login_callback(
    request: Request,
    code: FromQuery[str | None] = None,
    oauth_state: Annotated[str | None, QueryParameter(name="state")] = None,
    error: FromQuery[str | None] = None,
) -> Redirect:
    pending = request.session.pop("oauth", None)
    if (
        error
        or not pending
        or not code
        or not oauth_state
        or not secrets.compare_digest(oauth_state, pending["state"])
    ):
        flash(request, "Login was cancelled or the login link expired. Please try again.", "error")
        return Redirect("/auth/login")
    instance = pending["instance"]
    try:
        with db.connect() as conn:
            app = mastodon.get_or_register_app(conn, instance)
            token = mastodon.exchange_code(app, code)
            account = mastodon.verify_credentials(instance, token)
            # The token has served its only purpose (reading the profile): revoke it
            # and never store it, so the database and backups hold nothing usable.
            mastodon.revoke_token(app, token)
            user = services.upsert_user(conn, instance, account)
    except mastodon.MastodonError as exc:
        flash(request, str(exc), "error")
        return Redirect("/auth/login")
    if user["banned"]:
        flash(request, "This account is not allowed to use giveaway.quest.", "error")
        return Redirect("/")
    request.session["user_id"] = user["id"]
    flash(request, f"Welcome, {user['display_name']}!", "success")
    return Redirect(_safe_next(pending.get("next")))


@post("/logout", sync_to_thread=True)
def logout(request: Request) -> Redirect:
    request.session.pop("user_id", None)
    flash(request, "Logged out.", "info")
    return Redirect("/")


router = Router(path="/auth", route_handlers=[login_form, login_start, login_callback, logout])
