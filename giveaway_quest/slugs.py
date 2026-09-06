"""Friendly random ids like `blue-jelly-cat`."""

from __future__ import annotations

import re
import sqlite3

from coolname import generate_slug

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def new_slug(conn: sqlite3.Connection, words: int = 3) -> str:
    for _ in range(50):
        slug = generate_slug(words)
        if "-of-" in slug or slug.count("-") != words - 1:
            continue  # keep the short `blue-jelly-cat` shape, skip `x-of-y` forms
        taken = conn.execute("SELECT 1 FROM giveaways WHERE slug = ?", (slug,)).fetchone()
        if not taken:
            return slug
    # Astronomically unlikely, but never loop forever.
    return generate_slug(4)
