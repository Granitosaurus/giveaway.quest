 I want to talk about @security-review-2026-09-06.md - what are my biggest risks with this setup realistically speaking? I host some backups etc on this same machine and I want to ensure no one manages to escalate access and escape the server but to me this seems extremely unlikely because I'm using cloudflare so not even my direct IP is known?
Read security-review-2026-09-06.md (201 lines)

  Searched for 1 pattern, ran 3 shell commands

Good news up front: I dug into the actual setup on the box, and your instinct that a full "escalate + escape" compromise is unlikely is basically right — but not for the reason you gave. Let me reframe it.

The Cloudflare point buys you less than you think

Hiding your origin IP defends against exactly one class of attack: someone scanning/attacking your home IP directly — L3/4 DDoS on your connection, hitting other ports, poking services that aren't giveaway.quest. That's real value, keep it.

But it does nothing against the attack that actually matters here: someone attacking the app through giveaway.quest. The tunnel is a permanent, authenticated pipe from Cloudflare straight into your app container. An attacker doesn't need your IP — they need a bug in the Litestar app, a malicious Mastodon instance, or a poisoned dependency. The front door is wide open by design; it's just narrow (one HTTP app).

So the honest threat model is: "what happens if the giveaway.quest app gets remote code execution?" Everything else is secondary.

Walking that chain on your actual box

1. RCE in the app → you're now running as UID 10001 inside a rootless-Podman user namespace, as an unprivileged fake-user. Rootless Podman + non-root container is doing a lot of work here.
2. Reach the tailnet / backups? The gq-rootless-egress cgroup firewall rule (H1) blocks the container from reaching 100.64.0.0/10, RFC1918, and loopback. I verified it's live in configuration.nix. So Windmill (:8001) and CloakBrowser (:8091) are unreachable from a compromised app. This is your single most important control and it's in place.
3. Escape the container / namespace → needs a Linux kernel LPE (unprivileged user namespace → root). This is the "escape the server" scenario. It's not exotic — user-namespace LPEs land a few times a year — but it requires the attacker to already have step 1.
4. Read your other backups → I checked: ~/backups/restic-laptop is a restic repo, encrypted at rest, and the password isn't on homek14 (this box is the receiving end of the laptop's backup). So even code exec as dex gets an attacker an encrypted blob they can't open unless they also own the source laptop. That's a genuinely good outcome.

Your realistic risks, ranked

1. Supply chain (review item #4). This is your most likely RCE vector by far. cloudflared:latest,                 TAILWIND_VERSION=latest, unpinned pip insaries fetched by curl with no checksum. Acompromised upstream or a MITM on a build fetch runs attacker code in your image or build. Everything above assumes the app is honest; this is how it stops being honest. Pin everything, verify sha256, pin cloudflared to a di2. The giveaway.quest sqlite backups are plaveaway-quest-dev-backup-... and the hourlysnapshots). Unlike the restic repo, these spill giveaway codes and OAuth client secrets to any app-RCE-as-de   Review items #2 and #11. Lower impact thaelete the stale dev copy now, and get thesnapshots into an rclone crypt remote.                                                                      3. Kernel currency. Your entire containment acker doesn't have a kernel LPE." Make surenixos-rebuild switch / channel bumps are actually happening on a schedule, not drifting. A hardware watchdog   doesn't help if you're 8 months behind on4. Container hardening not done (review item-privileges:true, read_only: true + tmpfs.   Cheap defense-in-depth that raises the ba helps, but no-new-privileges in particular is worth the one line.                                                                                         5. CloakBrowser is an unauthenticated CDP enart = false, so mostly dormant). If you everstart it and a tailnet device is compromised, that's full unauthenticated browser control — CDP can read/wri   files in the container and make arbitraryst risk, but it's the weakest thing on thetailnet. Set AUTH_TOKEN before you rely on it.                                                              What I'd actually do this week- Delete ~/giveaway-quest-dev-backup-2026-09ns sitting in plaintext).- Pin the Dockerfile supply chain + cloudflaction for the effort.- Add the three security_opt/cap_drop/read_o.- Confirm your NixOS update cadence and thatord is backed up somewhere other than the  laptop (otherwise a laptop loss = unrecoveoblem but a real one).- Off-site encrypted copy of the giveaway.quREMOTE).The stuff you were worried about — someone pnto your backups and off the box — isgenuinely well-defended by the rootless + egtic combination. The gap is the supply chaingetting you to RCE in the first place, and aopies that don't need to exist.

