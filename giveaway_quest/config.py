"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import __version__

load_dotenv()

SITE_NAME = "giveaway.quest"
# The only scope we ask a user for: read their public profile. The site never
# posts, follows or messages as a logged-in user. Announcements and winner DMs
# go out from the site's own account (GQ_ANNOUNCE_* below).
MASTODON_SCOPES = "read:accounts"


@dataclass(slots=True)
class Settings:
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    debug: bool = False
    admins: frozenset[str] = frozenset()
    announce_instance: str = ""  # host of the site's own Mastodon account
    announce_token: str = ""  # its access token (needs write:statuses + read:statuses)
    # Pull replies to the announcement post and show them as comments on the
    # giveaway page. Needs an announcement account (GQ_ANNOUNCE_*) whose token
    # also carries `read:statuses`. Set GQ_COMMENTS=0 to turn the feature off.
    comments_enabled: bool = True
    # Post an announcement from the site's own account for every listed giveaway
    # automatically (a background pass), which is also what opens its comment
    # thread. Needs GQ_ANNOUNCE_*. Set GQ_AUTO_ANNOUNCE=0 to keep `gq announce`
    # the only path.
    auto_announce: bool = True
    # Per-client-IP cap on the expensive/abusable POSTs (login start, create),
    # requests per minute per path. 0 disables (tests).
    rate_limit: int = 10
    # Shown in the footer to confirm a deploy actually replaced the running
    # container. Defaults to the package version; set GQ_VERSION (e.g. to a git
    # short hash) in the image/env to override once git history exists.
    version: str = __version__

    @property
    def db_path(self) -> Path:
        return self.data_dir / "giveaway.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def session_secret(self) -> bytes:
        # The cookie session backend wants exactly 16/24/32 bytes.
        return hashlib.sha256(self.secret_key.encode()).digest()

    @property
    def host(self) -> str:
        return self.base_url.split("://", 1)[-1].rstrip("/")

    @property
    def announce_enabled(self) -> bool:
        return bool(self.announce_instance and self.announce_token)

    def url(self, path: str = "/") -> str:
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    admins = {a.strip().lstrip("@").lower() for a in os.environ.get("GQ_ADMINS", "").split(",")}
    return Settings(
        base_url=os.environ.get("GQ_BASE_URL", "http://localhost:8000").rstrip("/"),
        secret_key=os.environ.get("GQ_SECRET_KEY", "change-me"),
        data_dir=Path(os.environ.get("GQ_DATA_DIR", "./data")).expanduser(),
        debug=_truthy(os.environ.get("GQ_DEBUG")),
        admins=frozenset(a for a in admins if a),
        announce_instance=os.environ.get("GQ_ANNOUNCE_INSTANCE", "").strip().lower(),
        announce_token=os.environ.get("GQ_ANNOUNCE_TOKEN", "").strip(),
        comments_enabled=_truthy(os.environ.get("GQ_COMMENTS", "1")),
        auto_announce=_truthy(os.environ.get("GQ_AUTO_ANNOUNCE", "1")),
        rate_limit=int(os.environ.get("GQ_RATE_LIMIT", "10") or 0),
        version=os.environ.get("GQ_VERSION", "").strip() or __version__,
    )


settings = load_settings()
