<!--
Add a bullet point per notable change as you work. `uv run invoke release`
turns everything below this comment into the GitHub release notes for the
next tag, then empties this file back to just this comment.
-->
- Security fix: uv 0.13.4 in the image build and CI (GHSA-hq85-5cpj-mr62,
  compromised emSign root CA in uv's bundled trust store).
- `invoke redeploy` now also recreates cloudflared when docker-compose.yml
  pins a different image than the one running.
