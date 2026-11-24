# Relay Pi canary holder on dev-worker-1

The operator assigned connector `b3b1be1a-275c-4a2e-b515-428b39c43171`
on `dev-worker-1` (execution user `c4`) for the inference-session canary.
The 2026-11-24 read-only preflight found Linux x64, Docker 29.8.0, Python
3.12.3, cgroup v2, linger enabled, no holder service, 25 GiB free on `/`
and 31 GiB free on `/workspace`. The existing tmux connector stays separate.

## Release and profile inputs

Use the published v0.11.3 source, application image and control-plane manifest
after Relay #648 merges and its normal release qualification succeeds. Pin the
image digest, source commit, holder executable hash, connector hash/version and
both inner manifest digests in a reviewed inventory change. Download/join the
`holder` and `pi-linux-x64` assets on the deployment controller using the existing
Gitea identity. Verify the joined archive SHA256 against the reviewed release
manifest. Version-2 manifests may reference an earlier release for an unchanged
component: follow its `release`, `builtFrom` and ordered `assets` fields.
No worker downloads, mutable tags or dependency installation are needed.

The application image contains the compiled upstream `install-holder-release`,
`install-pi-runtime` and `render-holder-service` programs and their dependencies.
The role runs these in network-disabled, read-only containers with only the
required source/destination mounts. Host Node 20 is never used. The installed
Pi runtime carries its own pinned Node and communication bridge; a separate
agent-tools runtime is not necessary for this Pi-only canary.

The owner must approve the exact profiles before activation. Prepare one profile
per protocol, naming them `pi-canary-claude`, `pi-canary-codex` and
`pi-canary-qwen`, with these fixed choices:

| Protocol | Route | Model/account | Limits |
| --- | --- | --- | --- |
| `anthropic-messages` | Dedicated no-fallback Claude route from #1971 | Claude / sonnet | 200000 context, 1024 output, 120s request |
| `openai-responses` | `canary-pi-codex` | codex-2 / gpt-6-astra | 200000 context, 1024 output, 120s request |
| `openai-chat` | `canary-pi-qwen` | One fixed qwen3.8-27b target | 32768 context, 1024 output, 120s request |

These limits are proposed canary limits, not provider capacity claims. Restrict
each workspace root to `/home/c4/.local/state/relay-holder/workspaces/<profile-id>`
and its credential home to `.../relay-holder/native-homes/<profile-id>`. The role
validates and creates these private per-profile directories. The existing
`/workspace` ancestry is group-writable; this canary deliberately uses the
private home filesystem instead. Use
new profile IDs and native homes, exact installed Pi entrypoint hashes and
mandatory Relay communication. Do not point a canary at another agent's worktree.
Resolve tenant, router connection and binding IDs through the operator's normal
Relay flow. Use Relay-managed `lrk_` bindings and the existing router renderer
role/approved credential delivery path; never mint a substitute key or put a
token in inventory, profile JSON, receipts or command arguments. The role does
not grant launch admission, create bindings or approve its own profiles.

## Initial installation

`ansible/relay-holders.yml` is opt-in and is not imported into fleet playbooks.
Provide these variables in the reviewed per-host release inputs:

- `relay_holder_enabled: true`, initially `relay_holder_start: false`.
- `relay_holder_image`, `relay_holder_source_commit` and
  `relay_holder_binary_sha256` from the qualified release.
- `relay_holder_archives`: exactly two maps with `component` (`holder` or
  `pi-linux-x64`), controller-local `source`, archive `sha256` and
  `manifestSha256`. The `pi-linux-x64` entry also requires `nodeVersion` in
  `major.minor.patch` form, copied from the reviewed manifest's `node` field.
  The role compares that version with the authenticated manifest and verifies
  the packaged Node digest before execution. The holder archive needs no Node
  version pin; the compiled upstream installer's own runtime checks still apply.
- `relay_holder_profiles_source` and the independently approved exact file
  `relay_holder_profiles_sha256`.
- `relay_holder_connector_source`, `relay_holder_connector_sha256` and
  `relay_holder_connector_version` from the same approved connector release.

Run from the Ansible directory, restricted to the assigned worker:

```sh
ansible-playbook relay-holders.yml --limit dev-worker-1 -e @/private/reviewed-holder-inputs.yml
```

Check mode is refused because it cannot verify or render the installed closure.
The role verifies pinned archive bytes before bounded extraction, invokes the
upstream immutable installers, and retains nonsecret receipts in
`/opt/relay-holder/transfer`. The installer re-verifies every byte on repeated
convergence. It preserves damaged or different existing installations for
inspection. Existing profile/unit bytes must match; an implicit upgrade fails.

The upstream service renderer validates the holder digest and private profile
schema before the role verifies both units with `systemd-analyze --user verify`.
The generated holder uses its existing cgroup limits and qualification mode.
The separate `relay-holder-control-plane.service` uses the already-enrolled
connector identity, reconciles only this holder and does not replace the tmux
service. Startup rejects shadowed units and unreviewed drop-ins.

The reconciliation client's journal is retained under
`SyslogIdentifier=relay-holder-control-plane`, with rate limiting. Its continuous
loop in Relay `connector/src/agent_control.rs::run` prints only the fixed
`Holder control reconciliation failed; retrying with durable command identities`
message on cycle errors, discarding HTTP/IPC error bodies. Startup failures are
local filesystem/identity/URL/TLS setup errors; inspect their handling again when
repinning the connector. The same function calls `load(state_path)` on each cycle
and never calls `save` or writes the identity file. Its only local write is its
exclusive `connector-control.lock` under holder state. Sharing the enrolled
identity with the tmux connector depends on this read-only control-plane behavior;
recheck it for future releases.

After owner approval of profiles and successful managed binding installation,
set `relay_holder_start: true` through reviewed inventory and converge. Require
both user services active, the expected holder incarnation/profile digest in
Relay, healthy managed-router admission, and no secrets in the reconciliation
journal. Read that journal successfully from service start; a failed read is not
a clean result. The upstream holder deliberately discards raw subprocess output;
inspect its minimized durable journal and systemd state for holder failures. A repeat must
not restart either service or modify published artifacts.

## Live acceptance and rollback

Follow [the inference rollout](relay-inference-sessions-20261124.md), including
approval denial/allow, one tool effect, native history, branch/compact,
cancellation, reconciliation and restart. Qualify cold Claude first, then warm
retention, process release during approvals, idle expiry and warm-to-cold drain.
Router-only canaries do not establish this paired acceptance.

This role bootstraps services; it does not perform an in-place upgrade. A different
unit or profile requires the upstream persistent admission pause, drain and
sealed holder service-upgrade procedure. Stop new launches and drain before
stopping the two canary services. Preserve the holder journal, native histories,
receipts and installed digest directories. Keep the existing tmux connector
running. Never overwrite the journal with a pre-canary backup or silently change
the transport for signed/encrypted history. Resume only after explicit effect
reconciliation and compatible profile/release approval.
