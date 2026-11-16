# Researcher installation: where each I5 value comes from

Step I5 of `docs/runbooks/relay-researcher-installation.md` commits the two files that
`scripts/relay-researcher-bundle.py import` wrote from the I4 bundle:
`kubernetes/apps/relay-researcher/pins/pins.yaml` and `kubernetes/apps/relay-researcher/agent/config.json`.
Neither is hand-edited. `config.json` is the importer's canonical compact JSON, byte for byte. This
table ties every filled value to its source, so the policy can be audited without re-running the
tools.

Pinned inputs (`docs/relay-workers/researcher-binding-inputs.json`, `release`): Gitea release 2142
(v0.10.3, source `c0fe50f030eac65d78a113e42797c5fe8c395233`), with these asset sha256s:

- `worker-image-worker-runtime-133788.json`: `269954687b5d25c344d744a8c9a682dcd9beba602be33fe781adc644f5001598`
- `worker-image-infrastructure-agent-133788.json`: `cc119a6ac87a3554103a2471605277881793dc9701abdaeebe86914f86a94434`
- `worker-runtime-manifest-133788.json`: `593e34b69b85abf4c0429ec563c97c73d74f39eb4ad0b68abab97eb659aa3d61`

| File / field | Value | Source |
| --- | --- | --- |
| `pins.yaml` `installationId`, `config.json` `identity.installationId` | `b380ff7f-0716-478d-8b01-3d39f30e750b` | I3: Relay's approval receipt (`POST /api/infrastructure/installations`, 201, `approved`); the inputs file's `installation.installationId` |
| `config.json` `identity.installationPublicKey` | `6D2Cma_Oa4KiaDtOnpmiiztuBhEKObNtEqFNJl-FBas` | I3: the public half of the key in OpenBao `af/relay-infrastructure/installation-key`. Its fingerprint `25070c63dada58d3` (sha256 of the key text, first 16 hex) equals the approval's |
| `pins.yaml` `agentImage` | `registry.chifor.me/relay/infrastructure-agent@sha256:ba4cdd9c…a4ef` | agent receipt `publication.image` |
| `pins.yaml` `sourceCommit` | `c0fe50f0…5233` | `sourceCommit` of both worker receipts, which equals release v0.10.3 |
| `config.json` `policy.allowedRuntimeDigests` | `d758093f…6407` | runtime receipt `runtime.runtimeDigest`; inputs `release.runtimeDigest` |
| `config.json` `policy.installation.allowedImageDigests` | `39aed234…9b6d` | The digest of the runtime IMAGE: runtime receipt `publication.image` / `publication.manifestSha256`, which equals inputs `release.runtimeImage`. Set by the generator as `[imageDigest(runtimeImage)]` (`scripts/relay-researcher-binding.mts`). The registry manifest readback at I1 hashed to this value |
| `config.json` `policy.installation.allowedHarnessAdapters` | `["codex"]` | The sealed researcher template's `definition.harness.adapter` (generator, same file). The template comes from the runtime manifest, whose `adapterName` and `holderProfiles.workerTemplates[0].definition.session.harness` are both `codex`. The v0.10.3 runtime image ships the Codex adapter only, which is how the owner's model decision (gpt-6-luna) runs |
| `config.json` `policy.allowedTemplates` | `4da6f919-d660-472c-b503-4d8849fa60df` rev 1, `abffc97e…f195` | generator `summary.json` `template` (templateId/revision: owner decision D3) |
| `config.json` `policy.installation.resourceCeiling`, `maxLeaseSeconds`, `maxLifetimeSeconds`, `allowedEgressDestinations` | 2000m / 4096 MiB / 10240 MiB scratch / 512 processes, 900 s, 28800 s, `[]` | owner decision G4 ("Template and ceilings" in the runbook), from `researcher-installation-definition.json` |
| `config.json` `policy.installation.allowedRegistries` | `["registry.chifor.me"]` | `researcher-installation-definition.json` |
| `config.json` `policy.podTiers` | `sandboxed-pod` / `relay-gvisor` | owner decision D2, from the binding's placements |

Reproduce it: the I4 commands in the runbook. The generator's output equals
`docs/relay-workers/researcher-i4/` (`diff -r` empty), and `import` from the resulting bundle rewrites
these two files with no diff.
