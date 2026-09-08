"""Litestar application factory."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
from litestar import Litestar, Request
from litestar.config.csrf import CSRFConfig
from litestar.di import Provide
from litestar.exceptions import HTTPException, NotAuthorizedException
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.response import Redirect, Response
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from litestar.types import ASGIApp, Message, Receive, Scope, Send

from . import db, services
from .config import settings
from .routes import auth, meta, pages
from .web import current_user, render

log = logging.getLogger("giveaway_quest")
HERE = Path(__file__).parent
LOOP_INTERVAL_SECONDS = 30


async def background_loop() -> None:
    """Draw due winners and auto-announce listed giveaways. Runs for the life of the process."""
    while True:
        try:
            for slug in await anyio.to_thread.run_sync(services.draw_due):
                log.info("drew winner for %s", slug)
            for slug in await anyio.to_thread.run_sync(services.announce_due):
                log.info("announced %s", slug)
            await anyio.to_thread.run_sync(services.refresh_comment_threads)
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("background loop failed")
        await asyncio.sleep(LOOP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: Litestar) -> AsyncIterator[None]:
    db.init_db()
    task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        task.cancel()


def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    if isinstance(exc, NotAuthorizedException):
        return Redirect(f"/auth/login?next={request.url.path}")
    try:
        user = current_user(request)
    except Exception:  # noqa: BLE001
        user = None
    return render(
        request,
        "error.html.jinja",
        status_code=exc.status_code,
        user=user,
        code=exc.status_code,
        detail=exc.detail,
    )


def _configure_jinja(engine: JinjaTemplateEngine) -> None:
    env = engine.engine

    def dt(value: str | None) -> datetime | None:
        return db.parse_iso(value)

    def human(value: str | None) -> str:
        parsed = db.parse_iso(value)
        return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else ""

    def remaining(value: str | None) -> str:
        parsed = db.parse_iso(value)
        if not parsed:
            return ""
        delta = parsed - datetime.now(UTC)
        seconds = int(delta.total_seconds())
        if seconds <= 0:
            return "ended"
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        if days:
            return f"{days}d {hours}h left"
        if hours:
            return f"{hours}h {minutes}m left"
        return f"{minutes}m left"

    env.filters.update(dt=dt, human=human, remaining=remaining, markdown=services.render_markdown)
    env.globals.update(status_of=services.status_of)


session_config = CookieBackendConfig(
    secret=settings.session_secret,
    secure=settings.base_url.startswith("https://"),
    max_age=60 * 60 * 24 * 30,
)

# All JS/CSS is served from /static (no inline scripts, styles or event
# handlers in the templates), which is what lets the CSP be this strict.
# img-src allows any https host because avatars come from users' instances.
# form-action needs https: because browsers apply it to the redirect after
# POST /auth/login, which goes to the user's Mastodon server.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' https: data:",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self' https:",
        "frame-ancestors 'none'",
    ]
)


def security_headers() -> dict[str, str]:
    headers = {
        "content-security-policy": CONTENT_SECURITY_POLICY,
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=()",
    }
    if settings.base_url.startswith("https://"):
        headers["strict-transport-security"] = "max-age=31536000; includeSubDomains"
    return headers


def security_headers_middleware(app: ASGIApp) -> ASGIApp:
    """Add the security headers to *every* HTTP response.

    A plain ASGI wrapper rather than Litestar's ``response_headers`` because the
    latter is skipped for responses built by exception handlers (404/403/429
    pages), and those must be covered too.
    """
    extra = [(k.encode(), v.encode()) for k, v in security_headers().items()]

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in extra if k not in present)
                message["headers"] = headers
            await send(message)

        await app(scope, receive, send_with_headers)

    return middleware


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[
            # Handlers live in Routers on purpose: registering a sync_to_thread handler directly
            # on the app when GET and POST share a path makes Litestar 2.24 wrap it twice.
            pages.router,
            auth.router,
            meta.robots,
            meta.sitemap,
            create_static_files_router(path="/static", directories=[HERE / "static"]),
        ],
        template_config=TemplateConfig(
            directory=HERE / "templates",
            engine=JinjaTemplateEngine,
            engine_callback=_configure_jinja,
        ),
        middleware=[security_headers_middleware, session_config.middleware],
        csrf_config=CSRFConfig(
            secret=settings.secret_key, cookie_secure=settings.base_url.startswith("https://")
        ),
        dependencies={"user": Provide(current_user, sync_to_thread=True)},
        exception_handlers={HTTPException: http_exception_handler},
        lifespan=[lifespan],
        debug=settings.debug,
        openapi_config=None,  # public site, no API docs
    )


app = create_app()
