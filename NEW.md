<!--
Add a bullet point per notable change as you work. `uv run invoke release`
turns everything below this comment into the GitHub release notes for the
next tag, then empties this file back to just this comment.
-->

- Giveaways that get zero entries now reopen for a fixed 24 hours instead of
  re-running their full original duration (so a 30-day giveaway no longer
  silently restarts for another 30 days).
- Added an "Auto-extend" option on the create/edit forms (on by default) to
  turn that reopen-on-no-entries behavior off per giveaway.
- `uv run invoke release` now cuts a GitHub release from `NEW.md` automatically.
