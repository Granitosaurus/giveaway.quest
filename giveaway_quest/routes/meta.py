"""Crawler-facing endpoints: robots.txt and a sitemap of listed giveaways."""

from __future__ import annotations

from xml.sax.saxutils import escape

from litestar import Response, get

from .. import db, services
from ..config import settings


@get("/robots.txt", sync_to_thread=False)
def robots() -> Response:
    body = (
        "User-agent: *\nAllow: /\nDisallow: /auth/\nDisallow: /mine\n\n"
        f"Sitemap: {settings.url('/sitemap.xml')}\n"
    )
    return Response(body, media_type="text/plain")


@get("/sitemap.xml", sync_to_thread=True)
def sitemap() -> Response:
    with db.connect() as conn:
        rows = services.listed_slugs(conn)
    urls = [f"<url><loc>{escape(settings.url('/'))}</loc><changefreq>hourly</changefreq></url>"]
    for row in rows:
        lastmod = (row["drawn_at"] or row["created_at"])[:10]
        urls.append(
            f"<url><loc>{escape(settings.url('/' + row['slug']))}</loc>"
            f"<lastmod>{lastmod}</lastmod></url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(body, media_type="application/xml")
