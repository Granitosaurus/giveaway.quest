"""HTML pages: front page, giveaway page, create/edit/delete, enter/withdraw, my stuff."""

from __future__ import annotations

from typing import Annotated

from litestar import Request, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.exceptions import NotFoundException, PermissionDeniedException
from litestar.params import Body, FromPath, FromQuery
from litestar.response import Redirect, Template

from .. import db, mastodon, services
from ..config import settings
from ..slugs import SLUG_RE
from ..web import flash, render, require_login, write_rate_limit

Form = Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)]


def _load(slug: str, user: NamedDependency[dict | None], *, for_owner: bool = False) -> dict:
    if not SLUG_RE.match(slug):
        raise NotFoundException()
    with db.connect() as conn:
        giveaway = services.get_giveaway(conn, slug)
    if giveaway is None:
        raise NotFoundException()
    is_owner = bool(user) and user["id"] == giveaway["owner_id"]
    admin = services.is_admin(user)
    if giveaway["hidden"] and not (is_owner or admin):
        raise NotFoundException()
    if for_owner and not (is_owner or admin):
        raise PermissionDeniedException("Only the host can do that.")
    return giveaway


def _winner_count(data: dict[str, str]) -> int:
    """Best-effort winner_count for re-rendering a form after a validation error
    (real parsing/validation happens in services.parse_rewards)."""
    try:
        return max(1, min(int(data.get("winner_count") or 1), services.MAX_WINNERS))
    except ValueError:
        return 1


def _posted_rewards(data: dict[str, str]) -> list[str]:
    return [data.get(f"reward_{i}", "") for i in range(1, services.MAX_WINNERS + 1)]


@get("/", sync_to_thread=True)
def index(
    request: Request,
    user: NamedDependency[dict | None],
    page: FromQuery[int] = 1,
    sort: FromQuery[str] = "ending",
    status: FromQuery[str] = "open",
    q: FromQuery[str] = "",
) -> Template:
    page = max(page, 1)
    q = q.strip()[:80]
    if sort not in services.SORTS:
        sort = "ending"
    if status not in {"open", "ended", "all"}:
        status = "open"
    with db.connect() as conn:
        giveaways, total = services.list_giveaways(conn, page=page, sort=sort, status=status, q=q)
    pages = max((total + services.PAGE_SIZE - 1) // services.PAGE_SIZE, 1)
    return render(
        request,
        "index.html.jinja",
        user=user,
        giveaways=giveaways,
        total=total,
        page=page,
        pages=pages,
        page_numbers=services.pagination_window(page, pages),
        sort=sort,
        status=status,
        q=q,
    )


@get("/new", guards=[require_login], sync_to_thread=True)
def new_form(request: Request, user: NamedDependency[dict]) -> Template:
    return render(
        request,
        "new.html.jinja",
        user=user,
        form={"hours": 72, "listed": "on", "restart_if_unclaimed": "on"},
        rewards=[],
        count=1,
        max_winners=services.MAX_WINNERS,
    )


@post("/new", guards=[require_login], sync_to_thread=True, middleware=write_rate_limit)
def create(request: Request, user: NamedDependency[dict], data: Form) -> Template | Redirect:
    try:
        form = services.GiveawayForm.from_form(data)
    except services.ValidationError as exc:
        flash(request, str(exc), "error")
        return render(
            request,
            "new.html.jinja",
            status_code=422,
            user=user,
            form=data,
            rewards=_posted_rewards(data),
            count=_winner_count(data),
            max_winners=services.MAX_WINNERS,
        )
    with db.connect() as conn:
        giveaway = services.create_giveaway(conn, user, form)
    flash(request, "Giveaway created. Share the link so people can enter!", "success")
    return Redirect(f"/{giveaway['slug']}")


@post("/md-preview", guards=[require_login], sync_to_thread=True)
def md_preview(data: Form) -> Template:
    text = (data.get("text") or "")[: services.TEXT_MAX]
    return Template("_md_preview.html.jinja", context={"html": services.render_markdown(text)})


@get("/mine", guards=[require_login], sync_to_thread=True)
def mine(request: Request, user: NamedDependency[dict]) -> Template:
    with db.connect() as conn:
        hosting, _ = services.list_giveaways(
            conn, status="all", sort="newest", owner_id=user["id"], include_unlisted=True
        )
        entered = conn.execute(
            services.GIVEAWAY_SELECT
            + " JOIN entries e ON e.giveaway_id = g.id WHERE e.user_id = ? AND g.hidden = 0"
            " ORDER BY g.ends_at DESC",
            (user["id"],),
        ).fetchall()
    return render(request, "mine.html.jinja", user=user, hosting=hosting, entered=entered)


@get("/{slug:str}", sync_to_thread=True)
def giveaway_page(
    request: Request, slug: FromPath[str], user: NamedDependency[dict | None]
) -> Template:
    giveaway = _load(slug, user)
    status = services.status_of(giveaway)
    is_owner = bool(user) and user["id"] == giveaway["owner_id"]
    with db.connect() as conn:
        winners = services.get_winners(conn, giveaway["id"])
        entry = problem = None
        if user and not is_owner:
            entry = services.get_entry(conn, giveaway["id"], user["id"])
            if not entry:
                problem = services.eligibility_problem(giveaway, user)
        my_seat = next((w for w in winners if user and w["user_id"] == user["id"]), None)
        claim_status = services.reward_claim_status(giveaway, my_seat)
        if my_seat and claim_status == "claimed":
            services.mark_reward_viewed(conn, my_seat)
    share_url = settings.url(f"/{giveaway['slug']}")
    suggested = services.suggested_post_text(giveaway["title"], giveaway["quest"])
    toot_text = f"{suggested}\n\n{share_url}"
    return render(
        request,
        "giveaway.html.jinja",
        user=user,
        g=giveaway,
        status=status,
        is_owner=is_owner,
        winners=winners,
        my_seat=my_seat,
        claim_status=claim_status,
        entry=entry,
        problem=problem,
        share_url=share_url,
        toot_text=toot_text,
        share_mastodon_url=mastodon.share_url(toot_text),
        comments=services.get_comments(giveaway),
        announce_state=services.announce_state(giveaway),
    )


@post("/{slug:str}/enter", guards=[require_login], sync_to_thread=True)
def enter(
    request: Request, slug: FromPath[str], user: NamedDependency[dict], data: Form
) -> Redirect:
    giveaway = _load(slug, user)
    try:
        with db.connect() as conn:
            services.enter_giveaway(conn, giveaway, user, agreed=data.get("agree") == "on")
        flash(request, "You're in! Good luck. 🍀", "success")
    except services.ValidationError as exc:
        flash(request, str(exc), "error")
    return Redirect(f"/{slug}")


@post("/{slug:str}/withdraw", guards=[require_login], sync_to_thread=True)
def withdraw(request: Request, slug: FromPath[str], user: NamedDependency[dict]) -> Redirect:
    giveaway = _load(slug, user)
    try:
        with db.connect() as conn:
            services.withdraw(conn, giveaway, user)
        flash(request, "Your entry was removed.", "info")
    except services.ValidationError as exc:
        flash(request, str(exc), "error")
    return Redirect(f"/{slug}")


@post("/{slug:str}/claim", guards=[require_login], sync_to_thread=True)
def claim(request: Request, slug: FromPath[str], user: NamedDependency[dict]) -> Redirect:
    giveaway = _load(slug, user)
    try:
        with db.connect() as conn:
            services.claim_reward(conn, giveaway, user)
        flash(request, "Reward claimed - here it is. 🎁", "success")
    except services.ValidationError as exc:
        flash(request, str(exc), "error")
    return Redirect(f"/{slug}")


@get("/{slug:str}/edit", guards=[require_login], sync_to_thread=True)
def edit_form(request: Request, slug: FromPath[str], user: NamedDependency[dict]) -> Template:
    giveaway = _load(slug, user, for_owner=True)
    with db.connect() as conn:
        winners = services.get_winners(conn, giveaway["id"])
    return render(
        request,
        "edit.html.jinja",
        user=user,
        g=giveaway,
        winners=winners,
        rewards=[w["reward"] for w in winners],
        count=giveaway["winner_count"],
        max_winners=services.MAX_WINNERS,
        status=services.status_of(giveaway),
    )


@post("/{slug:str}/edit", guards=[require_login], sync_to_thread=True)
def edit(
    request: Request, slug: FromPath[str], user: NamedDependency[dict], data: Form
) -> Template | Redirect:
    giveaway = _load(slug, user, for_owner=True)
    # Once drawn, the reward is the only field still editable (see
    # services.update_reward) so the host can fix a bad code or link.
    drawn = bool(giveaway["drawn_at"])
    try:
        with db.connect() as conn:
            if drawn:
                services.update_reward(conn, giveaway, data)
            else:
                services.update_giveaway(conn, giveaway, data)
    except services.ValidationError as exc:
        flash(request, str(exc), "error")
        with db.connect() as conn:
            winners = services.get_winners(conn, giveaway["id"])
        if drawn:
            # reflect the just-posted (possibly invalid) per-seat edits
            winners = [{**w, "reward": data.get(f"reward_{w['id']}", w["reward"])} for w in winners]
            rewards, count = [], giveaway["winner_count"]
        else:
            rewards, count = _posted_rewards(data), _winner_count(data)
        return render(
            request,
            "edit.html.jinja",
            status_code=422,
            user=user,
            g={**giveaway, **data},
            winners=winners,
            rewards=rewards,
            count=count,
            max_winners=services.MAX_WINNERS,
            status=services.status_of(giveaway),
        )
    flash(request, "Reward updated." if drawn else "Giveaway updated.", "success")
    return Redirect(f"/{slug}")


@post("/{slug:str}/delete", guards=[require_login], sync_to_thread=True)
def delete(request: Request, slug: FromPath[str], user: NamedDependency[dict]) -> Redirect:
    giveaway = _load(slug, user, for_owner=True)
    with db.connect() as conn:
        services.delete_giveaway(conn, giveaway["id"])
    flash(
        request,
        "Giveaway deleted. The Mastodon post (if any) is still up; delete it there.",
        "info",
    )
    return Redirect("/mine")


router = Router(
    path="",
    route_handlers=[
        index,
        new_form,
        create,
        md_preview,
        mine,
        giveaway_page,
        enter,
        withdraw,
        claim,
        edit_form,
        edit,
        delete,
    ],
)
