# Enabled conductor activation — authorized production allowance

The user explicitly requested activation in the existing DSH configuration and separately approved **40 production calls / 40M conservatively reserved tokens**. This supersedes the earlier configured-disabled rollout proposal. It is not a monetary ceiling, not a refund of acceptance reservations, and not authorization to create a task automatically.

## Human interface

Once the reviewed deployment is active, use the existing GUI's human slash-command surface:

- `/conductor status` — show run identifiers, states and reservations.
- `/conductor start <task>` — start the first production task against `cchifor/dsh-team-conductor`.
- `/conductor pause <id>` — request a safe pause; in-flight work must settle.
- `/conductor resume <id>` — resume without acknowledging uncertain interruption.
- `/conductor resume-ack <id>` — explicitly acknowledge interruption **after inspecting and reconciling uncertain effects**. No refund occurs.

These commands are not model-facing tools and must be invoked from a top-level human session. Activation itself creates no task.

## Budget boundary

The published package's admission limit is per run. The deployment therefore admits only **one persisted production run** through its command surface, including failed or completed runs. Concurrent starts are excluded until creation settles, even if the UI stops awaiting the result. Persisted history is checked again on every subsequent start, including after restart. Another run requires new operator authorization and a reviewed allowance change; deleting SQLite or history is never a budget reset procedure.

The configuration pins 40 calls, 40M tokens and 1M conservative tokens per call on each existing route. The previous 118 native acceptance calls remain in their separate preserved histories; none belong to this new production allowance.

## Explicit artifact ownership decision

The user approved storing the independently reviewed private release bytes in an **operator-provisioned immutable ConfigMap**, rather than embedding an opaque binary in this configuration PR. GitOps retains the checksum, provenance, installer and read-only projected references; automated reviewers are not asked to approve unseen binary contents. The package remains `private:true`, and the installer gets no forge credential.

Before external activation merge, an operator downloads the existing private v0.1.0 release through the approved credential helper and verifies its published SHA256, then runs:

```sh
python3 scripts/provision-dsh-conductor-artifact.py /path/to/dsh-team-conductor-0.1.0.tgz --dry-run
python3 scripts/provision-dsh-conductor-artifact.py /path/to/dsh-team-conductor-0.1.0.tgz
```

The helper bounds descriptor reads, rejects symlinks/nonregular files, checks the exact 54,302 bytes and published hash, and publishes that same verified buffer. It refuses mismatching existing objects; it never patches/replaces one. Repeat invocation verifies without mutation. Only the artifact key is projected into `/conductor-release`; the existing staging helper independently verifies it again before modifying ps2. ConfigMap name: `dsh-conductor-artifact-7a97432202131d79`, namespace `dsh`.

**Recovery tradeoff:** this persistent artifact object is not recreated by Flux. If deleted or lost with the cluster, an operator must restore it from the private release with the same helper before rollout. Keep it and ps1 across rollback. Do not attach it to an ephemeral Job or delete it to recover a model budget.

## Activation gates

- Verify the production database is absent or empty before enabling automatic polling. Initial inspection found both `/dsh-home/team-conductor/state.sqlite` and its parent absent.
- Keep one coordinator, Recreate deployment and preserved ps1 closure. Create the SQLite parent without removing any existing state.
- Verify enabled, idle startup with the actual installed package and real forge/validator preflight. Use a standalone inference-veto fixture, never a global veto/appExit in the live GUI.
- Install the human command and readiness modules beside the authoritative profile, not through an ephemeral live-only edit.
- Clear only the readiness marker on both initial boot and main-container restart. Independent verification must bind it to Pod UID and require its timestamp to be no older than the current container's startedAt.
- Obtain external review/CI/merge via the approved private fork, with restart-surviving observation and rollback handling armed first. No self-merge or protection change.

Status: implementation/validation in progress. The existing GUI has not yet been rolled or activated.
