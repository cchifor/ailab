# Pinned Relay connector service adoption

`ansible/relay-connectors.yml` reconciles an **already-enrolled** worker's existing
tmux connector identity and user service. It is opt-in and is not included by the
worker/site playbooks or the daily fleet converge. It does not enroll a host,
rewrite its identity/token/service settings, approve access, enable ACP launches
or start an agent. Relay's R0 rollout remains incomplete until the actual worker
inventory, logout/reboot and fresh hub heartbeat checks pass.

## Release and inventory

The default release is the v0.9.0 connector manifest from Relay release
[v0.9.0](https://git.chifor.me/cchifor/relay/releases/tag/v0.9.0) (release
workflow run 120163, 2026-11-04). The upgraded hub serves the same manifest at
`https://relay.chifor.me/downloads/connector/v0.9.0/manifest.json`:

| Field | Pin |
|---|---|
| Source | `e71399f29c68d1a19cb6594dad4c1d125624b14e` |
| Linux amd64 SHA-256 | `402f8d36bfa24469c92617937b009ddbfa656aeba62bf7bfd202a31f8b0e97d1` |
| Linux arm64 SHA-256 | `d675fc3ccf1a94ee1324affefc77c5e9568ade78d193d9a160d9bd61489f532a` |
| relay-bus `SKILL.md` SHA-256 | `a515392d9352a1bf82a46981b1b4ab52a0b788a1033178862a9f37c0e93f0fe2` |
| relay-bus `relay-bus.py` SHA-256 | `e86983b9a879e425a6cc6cf56f895d4bf71bf14b6edb8606122582c318cd3986` |

These pins were checked on 2026-11-04 against four sources, and all agree:
- the release's `manifest.json`;
- `SHA256SUMS`;
- the downloaded files;
- for the skill, the tagged `packages/agent-skill` sources.

`deployment-images-120163.json` names the same source commit and records the hub image digest
(`sha256:d16ff16eeaa99e23ff0144c8a7bf47d0201acb33940fa81b46d7ad4d3327f5cc`).

**What v0.9.0 changes.** v0.8.1 was a version on Relay's main branch and was never tagged; v0.9.0 carries it. The
full plan is `docs/plans/2026-11-03-agent-bus-skill/plan.md` in the relay repository.

- **Terminal Chat is gone.** The connector no longer reads agents' transcripts, so the hub needs no
  `RELAY_TERMINAL_CHAT`.
- **The tmux messaging path is gone.**
  - Migration 055 closes tmux sessions and revokes their principals.
  - The connector keeps `mcp` and `inbox-hint` only as hidden stubs that exit 0, so a hook that is
    still loaded never blocks a prompt. The new preflight
    ([Claude Code messaging](#claude-code-messaging)) still refuses this pin while the old hook is
    installed.
- **Agents join the bus with a key** through the `relay-bus` skill ([The relay-bus skill](#the-relay-bus-skill)).
  - Migration 056 adds agent keys and their requests.
  - Keys are issued by device grant (an admin approves the agent's code) or created in the console. Both live
    under Settings > Agent communication > Agent keys.
- **Connector hardening.** The connector:
  - streams only watched panes, fairly, over slow links;
  - isolates a pane's or a command's failure from the rest;
  - posts discovery only when it changed;
  - pastes bulk input through one tmux buffer;
  - reports health in the heartbeat and `relay-connector status --json`.
- **The connector needs tmux 3.2 or later**, because every tmux command passes `-N`, so the connector never
  starts a tmux server. The dev-workers run tmux 3.4.
- **The unit changes:**
  - `Type=notify` with `WatchdogSec=60`;
  - `Restart=always`;
  - limits `MemoryMax=1G`, `TasksMax=1024` and `LimitNOFILE=8192`, sized by a soak with 500 agents and
    64 Full tmux views.

  Changing the unit makes the role restart the connector service. That restart never touches the agents'
  tmux servers: `KillMode=control-group` covers only the connector and its own tmux clients, as
  qualified under a real `systemd --user`.

**Roll out in this order**, and stop at any step whose check fails. No agent or tmux session is ever restarted.

1. **Confirm the old hook is gone.** Check that dev-worker-1, -2 and -3 converged with
   `relay_connector_claude_messaging: absent`, with no `inbox-hint` entry in `~/.claude/settings.json` and no
   `relay` MCP server. Verified on 2026-11-04 for all four workers.
2. **Deploy the hub.** Flux applies the hub. The `migrate` init container takes its pre-migration dump and runs
   055 and 056, each committed separately. Check:
   - the schema is 56;
   - the pod is Ready;
   - `/agent/skill.md` answers with `text/markdown`, and `/agent/x` answers 404, not HTML.

   If 056 fails, 055 stays committed. At that point the database is at schema 55, 055 has already
   closed the tmux sessions and revoked their principals, and the old v0.8.0 pod keeps serving a
   schema its restore fence does not accept. Do not deploy v0.8.0 again. Either fix forward by keeping
   the rollout stopped and rerunning the unchanged migrations under their lock, or do the full restore
   under "A real hub rollback" below.
3. **Smoke the v0.9 hub with the v0.8 connectors** still running: Agent pane, Full tmux view, input and uploads.
4. **Converge the connector one worker at a time.** Start with dev-worker-1, then -2, -3 and -4. On each, check
   that `relay-connector status --json` is fresh and connected and that the host's health appears in
   Agents > host details.
5. **Apply the skill role** (`just relay-agent-skill`) on each worker.

**Rolling back.** Forward fixes are the default. v0.8.0's restore fence needs exactly schema 54, so a v0.8.0 hub
cannot run against schema 56.
- **Connectors** can go back to v0.8.0 on their own: repin `relay_connector_release` and converge. The
  unit template follows the pin, so a v0.8.0 pin gets back its `Type=simple` unit, without the watchdog or
  the limits; v0.8.0 never sends `READY=1`. A v0.8 connector works against the v0.9 hub, as qualified in
  both directions. Keep `relay_connector_claude_messaging: absent`.
- **The skill hook** stays inert against an old hub. `relay_agent_skill_state: absent` removes it and the skill
  files.
- **A real hub rollback:**
  1. Stop issuance with `RELAY_AGENT_KEY_ISSUANCE=0`.
  2. Revoke every key, then restart the hub.
  3. Restore the pre-migration dump with the v0.8.0 image, using v0.8.0's own recovery tooling.

  Data written since the deploy is lost.

**v0.8.0** (2026-11-03, run 118693) added the Conversations page, the tmux messaging path and read-only terminal
Chat; v0.9.0 removes the last two. Its pins, for the rollbacks above:

| Field | v0.8.0 pin |
|---|---|
| Source | `04960c3b7267b5d9e5f82f1ad0f42451c22321b7` |
| Linux amd64 SHA-256 | `072fe6c4db6ff88eabb50ba27cca2c7657fb896af7601fcdc3a035a32c5126c9` |
| Linux arm64 SHA-256 | `965d47866d48ff598f7b1444011a84d2c1a14e0c0a290fba1056b3804a3e0ae4` |
| Hub image | `registry.chifor.me/relay/control-plane@sha256:f2c3e1be224cdc54c4371af1b5d4d81937da9b5b5d22678f8e337c024daa6aef` |

**What v0.7.7 changed for connectors** (v0.7.4 and v0.7.5 changed only the hub
image; v0.7.6 was tagged but never published, so v0.7.7 carries it):

- **Agent messaging with communication only**
  ([Relay #166](https://git.chifor.me/cchifor/relay/pulls/166)). Until now, the
  connector's agent-control loop called `/question-replies` on every cycle. That route
  exists only with `RELAY_AGENT_CONTROL_PLANE`, so the cycle aborted on a 404 and never
  reached `/end`, and ended sessions stayed open. A v0.7.7 hub advertises
  `controlPlane: false`, and the v0.7.7 connector then skips the control-plane calls
  but still posts `/end`. An older connector keeps the old behaviour against a v0.7.7
  hub; nothing regresses.
- **UTF-8 tmux output whatever the locale**
  ([Relay #168](https://git.chifor.me/cchifor/relay/pulls/168)). Every tmux call passes
  `-u`. The dev-workers' services already run with `LANG=C.UTF-8`, so this only
  matters on hosts without a locale.
- **Hub only:** dependency-safe message retention
  ([Relay #167](https://git.chifor.me/cchifor/relay/pulls/167), migration 052). Row
  deletion stays off unless `RELAY_MESSAGE_ROW_RETENTION=1`, which this estate does not
  set; bodies are still cleared after 30 days as before.

Rolling the hub back from v0.7.7 to v0.7.5 is an image revert. Migration 052 only adds
a table and indexes. The v0.7.5 migrator applies just its own unrecorded files, and
nothing in v0.7.5 reads the new objects.

Earlier releases, for reference:

[Relay #161](https://git.chifor.me/cchifor/relay/pulls/161) describes what v0.7.3 adds;
[Relay #159](https://git.chifor.me/cchifor/relay/pulls/159) describes v0.7.2, which it
builds on. In summary:

- **Identity across tmux server restarts (v0.7.2).** A reboot reattaches each
  restored pane to its existing agent. Agents whose panes are gone are removed.
- **Server-scoped tombstones (v0.7.2, schema 51).** A deleted agent's tombstone
  matches only its own tmux server instance and is pruned once that server ends.
- **Pane observer restart (v0.7.2)** and **byte-exact pane stream (v0.7.1).**
- **Missed scans and per-socket scan states (v0.7.3).** An online agent whose pane
  the server still lists survives 20 s of missed scans. A socket whose tmux listing
  failed leaves its agents as they were; four failures in a row report it absent.
- **Explicit panes, client address, dead hosts and "Last seen" (v0.7.3).**
  - Explicitly registered panes stay observed.
  - Rate limits key on the hub's `RELAY_CLIENT_ADDRESS_HEADER` (`cf-connecting-ip`). The
    pending access-request cap is then 20 per client address. The global ceiling of 500
    pending requests across all addresses still applies, and IPv6 addresses are not grouped
    by prefix.
  - A silent host is terminated after 40 s.
  - A reconnect leaves offline agents' last-seen times alone.

Rolling the hub back is only an image revert, with no database restore and no connector
downgrade. v0.7.3 adds no schema change, and a v0.7.2 hub drops the connector's new `scans`
field like any other unknown discovery field. An earlier hub also ignores
`RELAY_CLIENT_ADDRESS_HEADER`, so after a revert Relay's request logs again show
cloudflared's pod address as `remoteAddress`, and rate limits are shared again. For a rollback
to v0.7.1 (schema 51 stays),
each of these was checked on 2026-10-15 against an isolated database:

- v0.7.1's migrator, which knows migrations up to 050, exits cleanly against a
  schema-51 database.
- v0.7.1's tombstone, deletion and broker tests pass on schema 51.
- v0.7.1's real-tmux end-to-end suite passes with the 0.7.2 connector. The v0.7.1
  hub ignores the new start-time, position and server-report fields.

The connector role only changes the content-addressed binary pin. Its service
unit, identity checks and activation gates are unchanged.

Obtain the versioned binary on the controller from that reviewed release and
compare the manifest/source/digest. Pass its local absolute path; workers never
build, fetch `latest` or replace a global binary. SHA-256 binds the reviewed
bytes, not an independent publisher signature. Future upgrades change the
release object through review, including both architecture hashes and source.

Reconcile the current `dev_workers` inventory first. Do not assume the historical
count of four. For each selected worker, record its execution user, architecture,
connector host UUID, state path, service name, selected sockets and linger
status. Keep tokens in their existing private identity file. Use nonsecret
host-scoped variables with this shape:

```yaml
relay_connector_enabled: true
relay_connector_manage_service: false
relay_connector_user: reviewed_execution_user
relay_connector_arch: amd64
relay_connector_source: /absolute/controller/release/relay-connector-linux-amd64
relay_connector_host_id: reviewed-existing-host-uuid
relay_connector_state_file: /absolute/worker/private/connector.json
relay_connector_name: Existing reviewed service name
relay_connector_sockets: ['']  # Must exactly match existing service configuration.
```

The example identity/path values must be replaced, not copied into inventory.
Home and primary group default to the existing passwd/group records, including
nonstandard account homes and group names; explicit overrides are for reviewed
custom layouts. The role resolves these facts before validating paths.
The service filename is `relay-connector-<host UUID>.service`; it matches Relay's
installer convention. The existing state must be a regular single-link 0600
file owned by that execution user, with the exact origin, explicit approved phase, account,
name and sockets. A missing or mismatched identity fails; use the normal reviewed
enrollment flow separately for a new installation. Phase-less legacy state is
unsupported by this role; establish approval through the existing connector/hub
workflow before adoption rather than inferring it from a missing field. Do not copy a worker token
into Ansible variables, logs or operator handoff files.

## Current inventory (2026-10-14)

Each dev-worker has its pin in `ansible/host_vars/dev-worker-N.yml`. The pins record each
identity's `service` block as read back on the host. The owner approved all four account-scope
identities in Relay on 2026-10-14 at 13:33 UTC, before the pins merged. Each identity's local
`phase` was `approved` when the pin was written, so the role's phase check passes. The installed
version is whatever the role last applied (`/opt/relay/connector/applied-<UUID>.json`), not this table.

| Worker | Installation UUID | State file | Sockets | Origin of the installation |
|---|---|---|---|---|
| dev-worker-1 | `b3b1be1a-…` | `~/.config/relay-connector/connector.json` | `['']` (default) | migrated from the v0.1.0 pane runner `relay-connector.service`; that unit file was removed |
| dev-worker-2 | `14123362-…` | `~/.local/state/relay/f765a05a957da5b4/connector.json` | `['']` (default) | new 2026-10-14 |
| dev-worker-3 | `47405392-…` | `~/.local/state/relay/relay.chifor.me/connector.json` | `['']` (default) | migrated from the v0.2.2 `join` runner in tmux session `relay-chifor` (removed) |
| dev-worker-4 | `a7366b7a-…` | `~/.local/state/relay/f765a05a957da5b4/connector.json` | `['/tmp/tmux-1000/default']` | service mode since 2026-10-04. Its install recorded the default socket's explicit path |

dev-worker-1 and dev-worker-3 kept their original state paths: `service install --state` on the
existing identity keeps the UUID and credential, and only the scope change needed a fresh approval.
A migration is finished only when no legacy runner is running and nothing can start one on that
state file again. For a legacy unit, run these as the execution user, in order:

1. `systemctl --user disable --now <legacy>.service`;
2. confirm that `systemctl --user is-active <legacy>.service` prints `inactive` and that
   `pgrep -af relay-connector` lists only the new `serve` process. Deleting or disabling a unit
   does not stop a runner that is already running;
3. delete the unit file and run `systemctl --user daemon-reload`. A disabled unit can still be
   started.

Then restart the new `relay-connector-<UUID>.service`, so it is the one holding the state lock.
For a detached tmux session that runs the connector (the pre-v0.3.0 workflow), stop the connector
process inside it, check the pane is an idle shell, and kill the session. tmux-continuum restores
a leftover session after every reboot as an empty shell, so also check that the next resurrect save
no longer lists it.

The controller binary lives in the WSL converge clone's sibling directory,
`~/.ailab-converge/artifacts/relay-connector/v<version>/relay-connector-linux-amd64`, fetched on
Windows (WSL has no internet) and checked against the pinned SHA-256. Pass it with
`-e relay_connector_source=…`; it is not committed.

## Converge one worker

On the existing Ansible control node, use its normal reviewed inventory:

```sh
cd ansible
ansible-playbook relay-connectors.yml --limit <reviewed-worker> -e @<host-vars-file> --check --diff
ansible-playbook relay-connectors.yml --limit <reviewed-worker> -e @<host-vars-file> --diff
```

Staging requires a reachable user manager and the selected connector service
inactive/dead with no pending job. It never stops a running service for you.
For an intentional online upgrade, review the reconnect window, set
`relay_connector_manage_service: true` and rerun. Linger must already be enabled
by the worker account bootstrap; the role checks it and never silently grants it.
Only the selected service is enabled/restarted. Tmux sessions, other connectors,
router renderers and credentials are preserved.

The role verifies controller and installed hashes, native architecture and
`--version`; installs root-owned content-addressed artifacts; and uses the
current connector's unit hardening and `serve` command. Network readiness uses
the connector's reconnection loop and service restart policy, not a system-only
network target in the user manager. The effective unit must
be the managed file without drop-in overrides. An applied nonsecret unit revision
is written only after successful service activation. It makes a later activation
of staged files and a retry after interrupted restart converge correctly.
Unchanged managed reruns do not restart the connector. `--check` never reloads,
enables or starts services and does not record an applied revision.

Confirm `systemctl --user is-enabled/status` as the execution user, the installed
hash/version, `loginctl show-user <uid> --property=Linger`, and a fresh Relay hub
heartbeat under the same host UUID with the expected account, sockets, version
and linger metadata. Check actual terminal reconnect and logout/reboot
persistence in the rollout window. The applied revision is a convergence receipt,
not proof of a live authenticated heartbeat or production acceptance.

Rollback repins a previously reviewed compatible binary/source and reapplies;
never restore an old identity file or clear hub blocks. Keep the previous binary
until rollback qualification completes, then delete obsolete artifacts. Full
decommission uses the existing host revoke/drain and service uninstall workflow;
turning this role off only disables reconciliation.

## Claude Code messaging

**Being retired (2026-11-03).** Relay is replacing this path with a skill that any agent can load,
authenticated by a per-agent key that an admin approves. Relay v0.9.0 removes the connector's
messaging path. It keeps `mcp` and `inbox-hint` only as hidden, deprecated stubs that exit 0 (`mcp`
prints one line to stderr). A hook or MCP entry that is still loaded therefore never sees clap's
exit code 2 for an unknown subcommand, and Claude Code blocks a prompt when a `UserPromptSubmit`
hook exits 2. The stubs stay until every Claude session that was running when the hook was
installed has exited. Even so, dev-worker-1, -2 and -3 are set to `absent`, and every worker must
converge with `absent` **before** it gets a v0.9.0 or later connector, so that no stale entry
outlives the stubs. No agent had enrolled through this path, so nothing is lost. The section below
describes the role as it stands until the cleanup after v0.9.0 removes it.

The role enforces that order (`tasks/legacy_hook_preflight.yml`). A `relay_connector_release` pin
of 0.9.0 or later is refused, before anything on the worker changes and whatever
`relay_connector_claude_messaging` says, while the user's `~/.claude/settings.json` still holds the
old hook. It is also refused when that file cannot be read or parsed, because then nobody can tell.
The match is the role's own: only the literal `/…/relay-connector --state /… inbox-hint` entry. The
relay-bus skill's hook (`… relay-bus.py inbox --hook …`, [below](#the-relay-bus-skill)) never trips
it. Converging `absent` and the repin in the same run is refused too: converge `absent` with the
current pin first, check, then repin. `present` with a 0.9.0 or later pin fails as well.

From relay-connector 0.8.0, the Claude Code agents in a worker's tmux can take part in Relay
messaging. `relay_connector_claude_messaging` (`ansible/roles/relay_connector/tasks/claude.yml`)
sets up two things for the execution user. Both point at the pinned, content-addressed binary:

- **The `relay` MCP server.** It runs `relay-connector --state <state file> mcp` over stdio and is
  registered at user scope with the user's own CLI, `claude mcp add-json --scope user relay`. The
  CLI writes `~/.claude.json`. The role only reads that file, because running sessions rewrite it.
  The role does not use `/etc/claude-code/managed-mcp.json`: that file is exclusive and would turn
  off every MCP server the user added.
- **A `UserPromptSubmit` hook** in `~/.claude/settings.json`. It runs
  `relay-connector --state <state file> inbox-hint` before each prompt, with a 2 s timeout
  (`relay_connector_claude_hook_timeout`). `inbox-hint` prints hook JSON, answers within 300 ms
  and never fails the prompt. The role replaces only Relay's entry
  (`files/claude_relay_hook.py`): an entry whose command is exactly
  `/…/relay-connector --state /… inbox-hint`. A command that only mentions Relay, or runs it as
  part of a longer shell command, belongs to the user and stays, like every other key and hook.

A repin moves both to the new binary in the same run: the old MCP entry is removed and added
again, and exactly one Relay hook entry remains.

**Order and failure handling.** Before anything on the worker changes (binary, unit, service),
the role reads `~/.claude.json`, works out the MCP change, starts the CLI as the user
(`claude --version`, with the same environment the change uses) when that change needs it, and
dry-runs the `settings.json` merge
(`tasks/claude_preflight.yml`). The dry run also proves the user can write the change: a
read-only `~/.claude` or `settings.json` fails here. After the binary is verified, the role
changes the MCP server and then writes the hook, as one unit. If any of those steps fails, it puts
the MCP server back through the CLI as it was before the run (it removes a server this run added
and adds back a definition this run removed) and stops with `settings.json` unchanged.

**What a failed run can leave behind.**

- The Claude Code steps run last, so the release steps before them have already happened: the new
  binary and unit are in place and, with `relay_connector_manage_service: true`, the connector
  has restarted on the new release. The MCP server and hook keep pointing at the previous binary,
  which stays installed, until a run succeeds.
- If putting the MCP server back fails as well, the run says so. Check `claude mcp get relay` as
  the user and add the previous definition back with `claude mcp add-json --scope user relay`.
- Claude Code takes no lock on `settings.json`. The helper re-reads it just before its rename and
  redoes the merge if anything changed, but an edit that lands inside that last instant can still
  be lost. The estate's own two mergers, this helper and the relay-bus skill role's, never lose
  each other's change. Each one writes only under an exclusive `flock` on
  `~/.claude/.settings.json.lock` (0600, an empty file that stays), held from its read through its
  rename. A report (`--check`) or a run with nothing to change takes no lock and creates none.
  Whenever a change is due, `--check` included, a lock that could not be taken is refused first: an
  existing lock that is not the user's regular file, readable and writable, or a missing one in a
  directory the user cannot create it in. Both roles' preflight dry runs therefore stop on it before
  anything changes. The dev_worker role's status-line task does not take the lock.

**Turn it on** per worker in `ansible/host_vars/dev-worker-N.yml`, in the same change as a
reviewed 0.8.0 or later `relay_connector_release` pin:

```yaml
relay_connector_claude_messaging: present
```

In this estate dev-worker-1, -2 and -3 ran `present` with the v0.8.0 pin from 2026-11-03 and are
now `absent` (see above). dev-worker-4 runs Codex, which could not join this way, and stays
`unmanaged`.

With an older pin, `present` fails before anything is written. Claude Code must be installed for
the user at `~/.npm-global/bin/claude` (`relay_connector_claude_bin`) whenever the MCP server has to
change; `absent` with nothing registered works without it. Converge with `--check --diff` first;
it reports both changes without making them.

The Claude Code messaging tasks restart nothing: not the connector service, not tmux, not an
agent. A release repin applied in the same run follows the role's normal rules, so with
`relay_connector_manage_service: true` it restarts the managed connector service (and only that).
A Claude session loads its MCP servers and hooks when it starts, so sessions that are already
running join only once they are started again. To confirm, run `claude mcp get relay` as the user and look for the
hook under `/hooks` in a new session.

**Turn it off** with `relay_connector_claude_messaging: absent` and converge. The role removes the
`relay` server and the hook and keeps everything else. `unmanaged`, the default, leaves whatever
is there alone, so going straight from `present` to `unmanaged` leaves both in place.

The run fails before it changes anything if `~/.claude.json` or `settings.json` is not a regular file
(a FIFO, a device, a socket or a directory, there or behind its symlink), if either does not parse,
if `~/.claude.json` is not an object whose `mcpServers` (and `mcpServers.relay`) are objects, or if
the `UserPromptSubmit` hooks in `settings.json` are not in the documented shape. When the hook has
to change, it also fails if a rewrite could not keep the file's metadata: another owner (a symlink
target included), a group the user is not in, a POSIX ACL on the file, or a default ACL on
`~/.claude` that a new file would inherit. A `settings.json` that is already converged is left
alone whatever its owner or ACLs, so it never blocks an unrelated run. Repair the file by hand;
the role never overwrites a file it could not read. The rewrite keeps the mode
and the group. If the file's content or metadata changes between the role's read and its write,
every check and the merge are redone from the new state (up to three times).

Only the exact command the role writes counts as Relay's hook: the literal
`/…/relay-connector --state /… inbox-hint`, single-spaced, with no quoting or shell syntax. A
variant with `;`, `&&`, `|`, redirection, `$(...)` or backticks is left alone as the user's.

## The relay-bus skill

From Relay v0.9.0, any agent on a dev-worker (Claude Code or Codex; the Qwen-based agents run
through Codex) joins Relay's message bus through the **relay-bus skill**, with an agent key that a
person approves. `ansible/roles/relay_agent_skill` installs it for the agent user (`c4`). It is
applied by its own playbook, `ansible/relay-agent-skill.yml` (`just relay-agent-skill`), never by
`site.yml`, `just dev-workers` or the daily fleet converge. A host changes only with
`relay_agent_skill_enabled: true`, which no host sets yet.

**What `present` installs** (`relay_agent_skill_state`, default `present`):

- `~/.claude/skills/relay-bus/` and `~/.codex/skills/relay-bus/`, each with `SKILL.md` (0644) and
  `relay-bus.py` (0755), owned by the user. They are the release's two skill assets,
  `relay-bus-SKILL.md` (installed under its own name, `SKILL.md`) and `relay-bus.py`. Each sha256 is
  pinned in `relay_agent_skill_release`. The role checks the controller copy before anything on the
  worker changes, the helper checks the bytes it received before it writes them, and the role checks
  the installed file once more.
- `~/.config/relay` and `~/.config/relay/agents`, owned by the user with mode 0700. The role corrects
  an existing mode, but not recursively. `relay-bus.py` refuses either directory otherwise.
- `~/.config/relay/bus.json`, mode 0600: `{"origin": "https://relay.chifor.me"}`
  (`relay_agent_skill_origin`, the hub's `RELAY_URL`).
- One Claude Code `UserPromptSubmit` hook in `~/.claude/settings.json`, with a 3 s timeout
  (`relay_agent_skill_claude_hook_timeout`):

  ```sh
  command -v python3 >/dev/null 2>&1 && python3 "$HOME/.claude/skills/relay-bus/relay-bus.py" inbox --hook 2>/dev/null; exit 0
  ```

  It fails open. Without `python3` or without the helper it prints nothing and exits 0. Python
  exits 2 for a missing script before the helper's own guards run, hence the trailing `exit 0`.
  `inbox --hook` prints the "N unread Relay messages" context only when messages wait. Without a
  key, or on a 401, 429, timeout or an old hub's HTML 404, it prints nothing and exits 0.
  `files/claude_skill_hook.py` merges the hook with the same rules as the connector's helper above,
  under the same lock. Only an entry with exactly this command is the role's; every other hook
  stays. That includes an old `inbox-hint` entry, which only the connector role removes.

**Every write runs as the user, never as root,** and none follows a link. All of the skill's file
changes go through one helper, `files/skill_files.py`, run as the user:

- It reaches every path one component at a time from the home directory. Each directory is opened
  with `O_NOFOLLOW` relative to its parent's descriptor, and each change is made relative to those
  descriptors: create a directory, set a mode, write a fresh temporary file, rename it.
- The paths are `~/.config`, `~/.config/relay`, `agents/`, `~/.claude`, `~/.claude/skills`,
  `~/.codex`, `~/.codex/skills`, both `relay-bus` directories, both trees' skill files and
  `bus.json`. Each must be absent, or a real directory (a regular file for the files) owned by the
  user.
- A symlink, a directory where a file goes, or another user's path is refused, and the run changes
  nothing. The role does not repair ownership as root: fix such a path by hand.
- `validate.yml` dry-runs the helper before anything changes, in check mode too. The helper itself
  checks everything in a first pass before its first change. That pass also proves each change can
  be made: every directory it would create an entry in, remove from or set a mode on must be
  writable for the user, or be one the run itself gives an owner-writable mode, and not on a
  read-only file system. A link that appears later, even on an ancestor, meets the same no-follow
  operations and stops the run instead of being followed.
- For `absent`, both dry runs (the removal and the hook merge) run before the hook is removed. A
  refusal leaves the hook and every file in place.
- The skill files reach the helper as JSON on its stdin, and it checks each one against its pinned
  sha256 before writing.

The role never reads or writes a key file, and it restarts nothing. Claude Code and Codex load
skills, and Claude Code loads hooks, when a session starts, so a running agent picks the skill up in
its next session. Codex has no prompt hook: a Codex agent checks its inbox at turn boundaries, as
`SKILL.md` tells it. `present` also needs `python3` 3.10 or later for the user. The role checks the
`python3` that a privileged run resolves (`/usr/bin/python3` on the workers). The hook resolves
`python3` from the agent's own PATH.

**Release pins (TODO at release).** The defaults hold placeholders that fail validation, so nothing
installs until the deploy PR sets them from the v0.9.0 release:

```yaml
relay_agent_skill_release:
  version: 0.9.0
  sha256:
    relay-bus-SKILL.md: <manifest.json agentSkill.files[asset=relay-bus-SKILL.md].sha256>
    relay-bus.py: <manifest.json agentSkill.files[asset=relay-bus.py].sha256>
```

Check each value against the release's `SHA256SUMS` and the downloaded bytes. Fetch both assets on
Windows from `https://git.chifor.me/cchifor/relay/releases/download/v<version>/<asset>`, as for the
connector binary: the repository is private and WSL has no internet. Put them in
`~/.ailab-converge/artifacts/relay-agent-skill/v<version>/` and pass that directory as
`-e relay_agent_skill_source_dir=…`; it is not committed. Workers never download the skill. The hub's
`/agent/relay-bus.py` serves the same bytes as the release asset, which is a useful cross-check.
`/agent/skill.md` does not: the hub fills its origin section in, so never pin that copy.

**Order.** Each step has a stop condition. Re-check the live facts before each one.

1. **Absent convergence.** On dev-worker-1, -2 and -3, `relay_connector_claude_messaging: absent` has
   converged: `claude mcp get relay` finds no server, `settings.json` has no `inbox-hint` hook, and
   a rerun reports `changed=0`. The connector role refuses a 0.9.0 pin until this holds (above).
2. **Hub.** Flux deploys hub v0.9.0. Its migrations run, the schema is 56 and the pod is healthy.
3. **Connectors,** one host at a time: dev-worker-1, a smoke test, then dev-worker-2, -3 and -4.
4. **The skill role,** one host at a time. In the deploy PR, set the pins and
   `relay_agent_skill_enabled: true`. Then:

   ```sh
   cd ansible
   ansible-playbook relay-agent-skill.yml --limit dev-worker-1 -e relay_agent_skill_source_dir=<dir> --check --diff
   ansible-playbook relay-agent-skill.yml --limit dev-worker-1 -e relay_agent_skill_source_dir=<dir> --diff
   ansible-playbook relay-agent-skill.yml --limit dev-worker-1 -e relay_agent_skill_source_dir=<dir>  # changed=0
   ```

   `just relay-agent-skill --limit dev-worker-1 -e relay_agent_skill_source_dir=<dir> --check --diff`
   runs the same. Verify on the host, as the user: both skill directories hold the pinned bytes
   (`sha256sum`), `stat -c '%a %U' ~/.config/relay ~/.config/relay/agents` prints `700 c4` twice,
   `bus.json` names the origin, and `relay-bus.py whoami` exits 4 (not connected) until a key is
   approved.
5. No agent or tmux session is restarted.

**Approving an agent.** In its next session, an agent asked to join Relay runs
`relay-bus.py connect --name … --purpose … --harness claude-code|codex`. The helper prints a code
such as `BCDF-GHJK`, and the agent asks its person to approve it. A Relay administrator opens
**Settings → Agent communication → Agent keys**, enters the code, checks the name, purpose, host and
harness the request shows, and approves or denies it. The helper waits up to an hour. On approval it
stores the key in `~/.config/relay/agents/<identity>.key` (0600) and activates it. The key never
enters the agent's transcript. An administrator can also create a key in the console. A person then
installs it on the host as the user with `relay-bus.py install-key [--identity <id>]`, which reads it
from stdin; never paste a key to an agent. Several agents under one user (one per tmux window, say)
each need their own `RELAY_BUS_IDENTITY`; without it they share `default`.

**Trust boundary.** Every process running as the user can read that user's key files and act as
those agents, including authorized Relay terminal control. Admin credentials never live there. To
cut an agent off, revoke its key in Relay. `relay-bus.py disconnect` revokes the agent's own key and
deletes the file. Deleting a key file does not revoke the key.

**Rollback.** Set `relay_agent_skill_state: absent` and converge. The role removes the hook first.
Then the same helper, as the user, removes the two files it installed from each `relay-bus`
directory, and the directory once that leaves it empty. Anything else there stays, and so does the
directory. It walks from `~/.claude` and `~/.codex` down through no-follow directory descriptors. It
refuses a symlink, a non-regular file or another user's path there, in its dry run before the hook
changes and again when it removes. It keeps `~/.config/relay`: `bus.json` is harmless, and the
keys belong to the agents and the people who approved them. Revoke them in Relay when agents should
leave the bus. `absent` needs no pins, controller copy or Python. The hook is inert against an older
hub: a v0.8 hub has no `/agent/v1/inbox`, and the hook stays silent on whatever it answers instead
(its HTML page, a 404 or a redirect, which the helper never follows). A hub or connector rollback
therefore does not have to remove the skill first. `relay_agent_skill_enabled:
false` only stops managing the host and leaves everything in place.

## Qualification

The dedicated workflow runs
`scripts/tests/integration/test_relay_connectors.py` with Ansible 2.20.0 and
PyYAML 6.0.3. It uses disposable files and strict local `systemctl`/`loginctl`
fixtures, never host service commands, sudo or production credentials. Cases
cover disabled mode, checksum/architecture rejection, identity preservation,
private state, unapproved/cross-account inputs, staging safety, linger, drop-in
denial, interrupted activation, idempotence, check mode and offline unit parsing.
For Claude Code messaging, a strict fake `claude` CLI on `PATH` keeps `~/.claude.json`. The cases
cover the 0.8.0 gate before any write, registration and the hook alongside the user's own settings,
a repin, `absent`, check mode, unparsable files before a release change, `unmanaged`, a CLI the
user cannot run, and failed MCP changes with their restore. Unit tests cover
`claude_relay_hook.py` on its own. The 0.9.0 preflight cases refuse the old literal hook, alone or
beside the skill's hook, for 0.9.0 and 0.10.0 pins, including `absent` in the same run and an
unparsable file. They pass the skill's hook alone, no settings file, and the old hook under a 0.8.0
pin. A unit test pins the version comparison's edge cases. A settings lock the hook merge could not
take (a directory, a symlink, a read-only file) fails the Claude Code preflight, for `present` and
`absent` in apply and check mode, before the binary, unit or service changes. A FIFO as
`settings.json` (to the 0.9.0 preflight) or as `~/.claude.json` (to the Claude Code preflight) is
refused at once, never opened blocking, with nothing changed. The helpers open every user path they
read with `O_NONBLOCK` and check it is a regular file before reading. Subprocess tests with a 10 s
timeout cover a FIFO as `settings.json` (directly and behind its symlink), as the lock and as a skill
file. Set `TMPDIR` under `/workspace` for local runs. Real estate convergence and logout/reboot
evidence are operator acceptance work.

The same workflow runs `scripts/tests/integration/test_relay_agent_skill.py` for the skill role,
with real Ansible against a disposable home:

- disabled mode;
- the placeholder pins, mismatched, missing or linked release bytes, and bad inputs, all failing
  before any write;
- an install beside the user's own hooks and the old `inbox-hint` entry, with the modes, owners and
  bytes checked;
- a rerun with `changed=0`, and a new release;
- a symlink at each managed directory, from `~/.config` and `~/.claude` down, and a directory or
  link where a skill file or `bus.json` goes, refused in apply and check mode with nothing changed
  inside or outside the home;
- a path swapped for a symlink after validation (`~/.config`, `~/.config/relay`,
  `~/.claude/skills` or a skill file): the install refuses it and changes nothing, and the link's
  target stays untouched;
- Python older than 3.10 or broken, and unparsable settings;
- check mode, and `absent` keeping `~/.config/relay` and the key untouched. `absent` refuses links
  on the way (also when swapped in after its dry run) and keeps other files in a skill directory;
- a static check that `present.yml` and `absent.yml` each run as one block as the user, and that
  nothing in them writes but the helpers. A fixture without root cannot show that by running;
- read-only directories in apply and check mode, refused before the hook or any file changes:
  - for `absent`: a skill directory, or the `skills/` it would be removed from;
  - for `present`: a `skills/` or `~/.config` that a missing directory would be created in.

  A skill directory the role gives its own mode is repaired instead;
- a settings lock the hook merge could not take (a directory, a symlink, a read-only file), for
  `present` and `absent` in apply and check mode, refused before any change.

Unit tests cover `claude_skill_hook.py` and `skill_files.py`, including a link swapped in between the
helper's checks and its writes, and a change the user could not make. They also run the hook command
itself through `/bin/sh` with the helper or `python3` missing. Both settings helpers carry the same
lock code. Check mode refuses an unusable lock and never creates one. A regression holds writer 1
inside its rename, with the lock, and only then starts writer 2. It checks through explicit
interprocess events, in both orders, that writer 2 cannot take the lock until writer 1 has renamed.
The result keeps the skill's hook and drops the old one. Without the flock, writer 2 gets the lock
at once and the test fails.
With `RELAY_SKILL_RELEASE_DIR` naming a directory that holds a release's
two skill assets, one more case installs them and checks that the real `relay-bus.py` accepts what the
role wrote. Run it once against the v0.9.0 assets before applying the role.
