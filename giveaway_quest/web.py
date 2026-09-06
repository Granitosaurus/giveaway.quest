"""Shared helpers for route handlers: current user, flash messages, rendering."""

from __future__ import annotations

from typing import Any

from litestar import Request
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler
from litestar.middleware.rate_limit import RateLimitConfig
from litestar.response import Template

from . import db, services
from .config import SITE_NAME, settings


def _client_key(request: Request) -> str:
    # uvicorn runs with proxy_headers=True, so `client.host` is the address
    # cloudflared/Cloudflare forwarded, not the tunnel's. Keyed per path so the
    # login and create budgets are independent.
    host = request.client.host if request.client else "unknown"
    return f"{request.url.path}:{host}"


# Attach with `middleware=write_rate_limit` on the handlers that do outbound
# requests or create rows for anyone who asks (login start, giveaway create).
write_rate_limit = (
    [
        RateLimitConfig(
            rate_limit=("minute", settings.rate_limit), identifier_for_request=_client_key
        ).middleware
    ]
    if settings.rate_limit > 0
    else []
)


def current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    with db.connect() as conn:
        user = services.get_user(conn, int(user_id))
    if user is None or user["banned"]:
        request.session.pop("user_id", None)
        return None
    return user


def require_login(connection: ASGIConnection, _: BaseRouteHandler) -> None:
    if not connection.session.get("user_id"):
        raise NotAuthorizedException("Please log in first.")


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session.setdefault("flash", []).append({"message": message, "kind": kind})


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Template:
    flashes = request.session.pop("flash", []) if "session" in request.scope else []
    context.setdefault("user", None)
    return Template(
        template,
        status_code=status_code,
        context={
            "site_name": SITE_NAME,
            "base_url": settings.base_url,
            "version": settings.version,
            "flashes": flashes,
            "is_admin": services.is_admin(context.get("user")),
            "path": request.url.path,
            **context,
        },
    )
