# 2026-09-28 — dev-worker web terminal: close the open paths, then add file paste

Two PRs, in this order. PR 1 closes a live hole and does not depend on PR 2; PR 2 adds a second
write endpoint on the same origin and must not ship before PR 1.

## Why

The operator asked for a way to paste pictures, documents and other files into Claude Code / Codex
running in tmux on the dev-workers, including from the web terminal (`https://dwN.chifor.me`). The
investigation turned up a larger problem first.

### Measured 2026-09-28

- `curl -k https://192.168.0.8/` from the workstation → 200 ttyd index; `/token` → 200. No
  authentication at all on the direct path, and ufw allows 443 from 192.168.0.0/24, a LAN shared
  with cloudlab (its CI runner VMs run arbitrary jobs). ttyd drops into c4's `main` tmux session;
  c4 is a passwordless sudoer. **Any LAN host has root on every dev-worker.**
- ttyd runs without `-O`/`-c`; its first WebSocket frame carries an empty AuthToken, which it accepts.
- The dw Access apps leave `same_site_cookie_attribute` unset (tfstate: null) — Cloudflare's default
  is `None` — and have no binding cookie. With no Origin check anywhere, a page on any site the
  operator visits while holding an 8h Access session can open `wss://dwN.chifor.me/ws` with the
  cookie attached (CSWSH). Not PoC'd in a browser.
- Consumers of worker :443 today: the in-cluster cloudflared (edge/cloudflared, both pods on
  talos-cp1; connections arrive at the worker from the NODE IP 192.168.0.41 — Cilium masquerade),
  the Tailscale subnet router (`ailab-subnet`, a pod on talos-cp3; also arrives from a node IP), the
  workstation (.86) and other LAN devices. Nothing in the repo calls it programmatically.
- Paste behaviour of the agents (throwaway `tmux -L` server on dw1, `paste-buffer -p`):
  Claude Code 2.1.283 and codex 0.153.4 both turn a single bracketed-pasted image path into
  `[Image #1]`; for a paste holding two paths Claude attaches each image while **codex attaches
  nothing**. PDF paths stay text in both. So: one path per bracketed paste.
- ttyd 1.7.7's frontend exposes `window.term`; Ctrl+V is turned into `\x16` and cancelled (no
  browser paste); ttyd's trzsz drag-and-drop types `trz\r` into the focused pane (unusable when an
  agent owns the pane).

### Why not a source-IP allow-list

Every legitimate non-workstation path (tunnel, Tailscale) arrives from a Talos node IP, which is
also the source address of every pod in the cluster. An allow-list cannot tell them apart, and it
would break DHCP LAN devices. Only authentication closes the hole without breaking a path.

## PR 1 — authenticate every path to the web terminal

One Caddy site per worker serving the IP, the hostname **and** `dwN.chifor.me`, inside a single
`route` (written order — `respond` would otherwise sort after `handle` and never run):

1. **Origin gate.** WebSocket upgrades (and, in PR 2, `/_dw/upload`) must carry exactly one of the
   worker's own origins; a missing or foreign Origin is 403. Any other request carrying a foreign
   Origin is 403 too. Page navigations carry no Origin and pass.
2. **Tunnel path.** A request carrying `Cf-Access-Jwt-Assertion` goes through `forward_auth` to
   `dw-access-verify` on 127.0.0.1:7682: RS256 only, signature against the team JWKS
   (`https://chifor.cloudflareaccess.com/cdn-cgi/access/certs`), `aud` = THIS worker's Access app
   AUD, exact `iss`, `exp` required, 30 s leeway. 403 = invalid token, 503 = no signing keys
   available. A forged header is 403 even alongside a valid LAN cookie.
3. **LAN/Tailscale path.** No JWT header → Caddy `basic_auth` (user `c4`, one fleet credential,
   bcrypt from SOPS). An authenticated response sets `dw_lan=<secret>; Secure; HttpOnly;
   SameSite=Strict`, and a request presenting that cookie skips the Basic prompt. The cookie is what
   keeps Safari/iOS working: WebKit does not send cached Basic credentials on the WebSocket
   handshake (ttyd#1437, teslamate#952), so Basic alone would break the terminal there.
4. Dispatch to ttyd unchanged.

Selecting the gate by the header's presence (not by Host) means cloudflared needs **no change**:
it keeps sending `Host: 192.168.0.N`, the request still carries the JWT, and the per-worker
rollout is independent of the shared Flux ConfigMap. No outage window, no lockstep.

Prototype validated on dw1 (throwaway Caddy on :18443, dummy backends, isolated storage): 25/25
checks — LAN 401/wrong password/200 + cookie issued, no cookie on 401, cookie reuse, wrong cookie
401, WS own-origin proxied with the Upgrade header intact, WS foreign/sibling/missing Origin 403,
foreign-Origin GET 403, tunnel good/forged JWT 200/403, forged JWT + valid cookie 403, tunnel host
without credentials 401, validator down 502.

`dw-access-verify` — stdlib `http.server` + python3-jwt 2.7.0 + cryptography (already on the
workers). Own JWKS cache: last good key set kept up to 24 h; refresh at most every 30 s (on an
unknown `kid` or when older than 5 min); 5 s fetch timeout; one refresh at a time. systemd:
`DynamicUser`, `ProtectSystem=strict`, `NoNewPrivileges`, loopback only, `Restart=always`.

Also in PR 1:
- `same_site_cookie_attribute = "lax"` on the four dw Access apps (tofu, applied by hand).
- `dev_worker_web_access_aud` map (non-secret; values from the cloudflare module's new
  `dev_worker_access_aud` output). A wrong/missing AUD fails the play before Caddy is touched.
- The Caddyfile becomes 0640 root:caddy (it now holds the bcrypt hash + cookie secret); the role
  runs `caddy validate` before reload.
- A converge-time self-check against the worker's own `https://<ip>/`: no credentials → 401, a
  forged JWT → 403, a foreign-Origin WebSocket → 403, the LAN login → 200 + cookie. The play fails
  otherwise.
- Gatus: one endpoint per worker expecting 401 from `https://192.168.0.N/`, so a regression that
  reopens the path alerts.
- Unit tests for the validator (valid, wrong aud/iss, expired, `alg=none`, HS256 key confusion,
  unknown kid → refresh, JWKS down with and without a warm cache), wired into
  `.gitea/workflows/dev-worker-scripts.yaml` with `REQUIRE_PYJWT=1` (jobs run on the runner hosts,
  which carry pyjwt 2.7.0).
- Runbook + threat model rewrite.

### Rollout (PR 1)

SSH (22) is never touched; the ansible run itself proves SSH + sudo on each host before it changes
anything. A Caddy reload drops open web terminals (the browser reconnects to the same tmux session)
— and drops any socket opened before the gate existed, which is wanted.

1. Mint the LAN credential + cookie secret into `ansible/secrets/dev-worker.sops.yaml` (done in the
   PR; round-tripped).
2. `dev-worker-4` first, from the PR branch: `ansible-playbook dev-workers.yml -l dev-worker-4
   -t web-gate`. The converge self-check (sent to the worker's own IP, exactly as a LAN browser)
   must pass. Then by hand: `https://192.168.0.11/` in Chrome/Edge and in Safari/iOS (one Basic
   prompt, terminal connects, reload, reconnect after sleep); the same URL from an off-LAN device
   over Tailscale; `https://dw4.chifor.me` (Access login only, no Basic prompt — proves the JWT
   reaches the WebSocket upgrade; `journalctl -u dw-access-verify` shows `allow` for `/ws`); and
   `systemctl stop dw-access-verify` → the tunnel path answers 502, LAN unaffected → start again.
3. dev-worker-1..3 the same way.
4. Merge. The daily 06:35 fleet converge runs `main`, so a worker gated from the branch reverts to
   the old open Caddyfile at the next 06:35 if the PR is not merged by then — merge the same day.
   Merging also ships the Gatus probes, which expect 401 and would page against an ungated worker:
   another reason to converge all four first.
5. `tofu -chdir=kubernetes/infra/cloudflare apply` for `same_site_cookie_attribute = "lax"`.

**No rollback to the old Caddyfile** — it is an open root shell. Containment instead:
`systemctl stop caddy` on the affected worker (web terminal off, SSH on), fix, re-run `-t web-gate`.

**The `dw_lan` cookie** is a bearer credential equal to the password and never expires server-side
(`Max-Age` only bounds how long the browser keeps it). It is revoked by rotating the cookie secret —
always together with the password (runbook).

## PR 2 — paste files into the agent from the browser and from SSH

- `dw-upload` (stdlib Python, `User=c4`, 127.0.0.1:7683), behind the PR 1 gate at `/_dw/upload`:
  `PUT` with a raw body; one bounded `Content-Length` required (no chunked), 64 MiB per file
  (Caddy `request_body max_size 64MiB` matches), directory quota 2 GiB + file-count cap under a lock,
  name sanitised + stamped, `O_EXCL` 0600 write and a no-replace publish (`link`) into
  `/workspace/c4/pastes/` (0700, tmpfiles 14 d). Returns `{path}`.
- ttyd `-I` index = the pinned ttyd's own index (extracted at converge time, sha256-pinned, never
  committed — 730 KB on one line) + `<script src="/_dw/paste.js">`.
- `paste.js`: `term.attachCustomKeyEventHandler` lets plain Ctrl+V reach the browser (loses ^V
  literal-next in the web terminal only); capture-phase `paste` and `drop` → upload; a paperclip file
  picker (the reliable path on iOS); uploads serialised; each finished upload → `term.paste(path)`
  then `term.paste(" ")` separately; never `\r`; auto-paste only for uploads that finish quickly,
  otherwise a "Paste path" button (focus may have moved); `fetch(..., {redirect: "manual"})` so an
  Access expiry reads as "session expired, reload".
- `poppler-utils` + `pandoc` so both agents can read PDF/DOCX.
- `scripts/dw-paste.ps1`: any file type, several files, a named tmux buffer per file; paste stays a
  deliberate `prefix+]` (the "active pane" is ambiguous with several clients attached).
- Runbook: Claude Remote Control as the zero-infra Claude-only path (claude.ai/code + phone app
  attach photos/files natively; transcripts incl. screenshots are stored at Anthropic).
- A paste regression script (throwaway `tmux -L`, asserts `[Image #1]` in both agents) to run after
  agent upgrades — Claude self-updates and codex is a floor pin.

### PR 2 as built (deviations from the above)

- **Caddy serves the terminal page** (ttyd's own index, spliced on the host at converge, sanity-checked
  for `window.term`) instead of `ttyd -I`: ttyd is never restarted, and nothing 730 KB passes through
  Ansible templating. (A ttyd restart would not have killed tmux — its server lives in
  claude-dashboard.service — but there is no reason to take it.)
- **dw-paste.ps1 never auto-pastes**: one automatic tmux buffer per file, loaded in reverse so
  `prefix+]` pastes the first and `prefix+=` picks the rest (Codex's plan-review finding: named buffers
  are not what `prefix+]` pastes, and "the most recent client's pane" is ambiguous).
- Browser coverage is `tests/e2e-web-paste.sh`: a throwaway copy of the real stack driven by Chromium
  (14 checks), manual on a worker — the CI runners have no Caddy/ttyd/Chromium.

## Follow-ups (not in these PRs)

- cloudflared → Caddy still uses `noTLSVerify`: an active LAN attacker could intercept the Access
  JWT (a bearer token for up to 8 h). Fix: `originServerName: dwN.chifor.me` + `caPool` with the four
  workers' Caddy root CAs.
- The edge cloudflared runs `cloudflare/cloudflared:latest` (unpinned).

## Cross-validation

Design reviewed by Fable 5.1 (twice) and Codex gpt-6-astra (twice, reviewer-2 seat). Accepted: the
security-first ordering, origin-side JWT validation, header-presence gate selection (Fable),
explicit `route` ordering, JWKS outage policy and 403/503 split, strict upload framing and quota,
exact 64 MiB, per-upload paste targeting caution, browser end-to-end coverage (Codex); from the
plan review: self-check against the worker's own IP, containment instead of rollback, the cookie's
real (rotation-bound) lifetime, Tailscale + validator-down canary steps (Codex). Deferred to PR 2
(its own review): named-buffer selection in dw-paste.ps1, upload framing/quota acceptance tests.
Rejected from the plan review: an authenticated Gatus probe (it would copy the LAN credential into
the cluster; the converge self-check covers "the terminal works"). Rejected:
ttyd `-O` as the Origin gate (it compares Origin to Host, which cloudflared rewrites to the IP);
mTLS for the LAN path (browsers send no SNI for IP literals, so it cannot be scoped to the IP site);
Authelia for the LAN path (needs a new URL and makes LAN access depend on the internet); a
multi-value `header Origin a b c` line (a parse error on Caddy 2.11.4).
