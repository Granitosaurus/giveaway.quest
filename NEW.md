<!--
Add a bullet point per notable change as you work. `uv run invoke release`
turns everything below this comment into the GitHub release notes for the
next tag, then empties this file back to just this comment.
-->

- Supply-chain hardening: Renovate for dependency updates (7-day release
  cooldown), CI with tests, pip-audit and a Trivy image scan; the Dockerfile
  now pins base images by digest, verifies the Tailwind/daisyUI downloads by
  sha256, installs with `uv sync --locked` and ships without pip;
  `cloudflared` is pinned instead of `:latest`.
- Security fixes: certifi 2026.10.19 (PYSEC-2026-4320), multidict 6.9.1
  (PYSEC-2026-4244).
