# giveaway.quest

Litestar + sqlite web app; read `README.md` (run/deploy) and `NOTES.md`
(design decisions, gotchas) before changing things.

- Work inside the flake shell: `NIX_CONFIG="experimental-features = nix-command flakes" nix develop`.
- Tests: `uv run pytest -W error::DeprecationWarning`. Lint: `ruff check . && ruff format .`.
- Domain logic goes in `giveaway_quest/services.py`; handlers stay thin and are registered via Routers (never directly on the app, see NOTES.md).
- Never commit `.env`, `data/`, or the built `giveaway_quest/static/app.css`.
