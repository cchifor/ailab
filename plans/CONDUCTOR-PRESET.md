# Team Conductor preset integration

## Scope and safety

Add a discoverable **Team Conductor (0.1.0)** preset (`team-dsh-conductor`) as a control/help front door to the already-deployed host service. Do not replace the legacy `Conductor` preset: it is an ordinary delegating coding-agent composition, not the released orchestration service.

The new composition contributes only a scoped persona. It mounts no conductor instance, model-facing conductor tool, coding tools, delegation tools, credential provider or persistence service. Selection and mounting create no production task. Human `/conductor` commands continue to use the existing host registry and service, whose admission guard and durable ledger remain authoritative. Ordinary help-chat inference uses the selected session model and is outside the conductor run ledger; the preset does not pin or change model routes.

The persona explains status/start/pause/resume, fixed repository, one-run 40-call/40M-reserved-token allowance, and explicit human start. It must not claim status or progress that the user has not obtained from the command. `resume-ack` is an explicit interrupted-stage acknowledgement, never a refund.

## Implementation

1. Add paired `team-dsh-conductor.preset.yml` and `team-dsh-conductor.agent.cordis.yml` to the existing GitOps ConfigMap generator.
2. Reuse the existing seed-settings projection into the read-only, system-trust `/dsh-teams` root. Do not edit the live preset tree or add an alternate server.
3. Keep existing presets and the default selection unchanged. No package, artifact, credential, model, budget or validator changes are needed; backend preset data uses the existing GUI picker, so no frontend rebuild is required.

## Usage after deployment

Refresh the existing GUI and select **Team Conductor (0.1.0)** for a new top-level session (not the legacy **Conductor** preset). Enter `/conductor status` to check the service without inference. Selection alone does not start work. To intentionally consume the one approved production-run admission, enter `/conductor start <task>`; use `/conductor pause <run-id>` and `/conductor resume <run-id>` for that run. Ordinary messages are help-chat on the selected session model, not automatically submitted conductor tasks, and their inference is outside the conductor ledger.

## Validation and review gates

- Inspect and internally review the actual DSH discovery/mount API and scoped persona behavior.
- Unit-test metadata, paired projection, composition safety and preserved existing presets/configuration.
- Validate discovery and mount against the installed DSH SDK. Create only a disposable fixture session, invoke real `/conductor status`, assert unchanged singleton and zero task/model calls; prohibit inference in the fixture.
- Render GitOps and server-dry-run affected resources. Obtain two independent exact-head external approvals and CI, then external merge only. Check the live production ledger before merge: if a human has started an active run, coordinate a safe pause/rollout window rather than silently interrupting it.
- Observe the existing GUI rollout and verify actual preset roster/discovery. Ask the human to select the preset and run `/conductor status`; do not start a production task.

Draft implemented; internal peer reviews are pending. Four preset contract tests and the full DSH suite passed (174 cases:173pass/1skip). A real installed-SDK fixture discovered and mounted the system-trust preset, shared the existing durable store and executed human /conductor status with zero inference and zero runs. Initial fixture failures came from comparing context-bound Cordis service proxies, not a preset mount defect; underlying store identity is the correct singleton assertion. The rendered ConfigMap and Deployment passed server dry-run; no live profile was modified. Deployment requires a new externally reviewed and merged PR.
