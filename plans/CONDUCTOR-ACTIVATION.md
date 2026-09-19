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

## Activation gates

- Verify the production database is absent or empty before enabling automatic polling. Initial inspection found both `/dsh-home/team-conductor/state.sqlite` and its parent absent.
- Keep one coordinator, Recreate deployment and preserved ps1 closure. Create the SQLite parent without removing any existing state.
- Verify enabled, idle startup with the actual installed package and real forge/validator preflight. Use a standalone inference-veto fixture, never a global veto/appExit in the live GUI.
- Install the human command and readiness modules beside the authoritative profile, not through an ephemeral live-only edit.
- Clear only the readiness marker on both initial boot and main-container restart. Independent verification must bind it to Pod UID and require its timestamp to be no older than the current container's startedAt.
- Obtain external review/CI/merge via the approved private fork, with restart-surviving observation and rollback handling armed first. No self-merge or protection change.

Status: implementation/validation in progress. The existing GUI has not yet been rolled or activated.
