# Plan — bound the image COUNT, not just the bytes

**Date:** 2026-09-10 · **Status:** DRAFT · **Repo:** `cchifor/ailab`
**Origin:** the surviving finding on PR #613, which merged with it open and advisory.

---

## 1. The defect

dsh re-sends every image in a conversation on every turn. `maxRequestImageBytes: 8388608` bounds
the **bytes** of that payload; **nothing bounds the count**. Attachments here measure 2 KB–100 KB
with a 40 KB median, so 65 images is ~2.6 MB — far under 8 MiB — and the backend rejects it:

```
400  At most 64 image(s) may be provided in one prompt
```

The server cap bounds what is **accepted**. It does not bound what the client **accumulates**.
The turn fails, and no amount of prompting prevents it.

## 2. The decisive fact: the machinery exists, unwired

`@deepseek-ai/dsh-llm` `lib/index.js:742-747` already implements count offload:

```js
const excessCount = policy.maxImages === void 0 ? 0 : Math.max(0, lengths.length - policy.maxImages);
const countQuantum = policy.countQuantum ?? 1;
const removeCount = excessCount === 0 ? 0 : Math.ceil(excessCount / countQuantum) * countQuantum;
```

`RequestImageOffloadPolicy` declares `maxImages` — *"omission leaves count unbounded"*.

The **adapter** never passes it. `@deepseek-ai/dsh-llm-pi-ai`: no count field in the profile schema
(`lib/index.js:1011-1013`); both `offloadRequestImagesWithPolicy` call sites (`:1288`, `:1296`)
pass `maxBytes` and `byteQuantum: 1` only; `grep -rn maxImages` returns nothing.

**This is not a missing feature. It is unwired plumbing**, and that is what makes it cheap.

## 3. Recommendation

Wire the existing helper through the adapter. Two profile fields, one pass-through at each of two
call sites:

```ts
maxRequestImages: z.number().step(1).min(1).optional(),
requestImageCountQuantum: z.number().step(1).min(1).optional(),
```

```js
...maxRequestImages === void 0 ? {} : { maxImages: maxRequestImages },
...requestImageCountQuantum === void 0 ? {} : { countQuantum: requestImageCountQuantum },
```

Configure **64 images, quantum 8.**

### The quantum is the part worth arguing about

The first draft of this plan proposed `countQuantum: 1`, reasoning only that it matched the
existing `byteQuantum: 1`. That was cargo-culting, and the codex cross-review corrected it.

Quantisation exists so the removed prefix stays **byte-stable** and the serving engine's prefix
cache survives. At quantum 1 the omission boundary moves with **every additional image once the
cap is exceeded**, invalidating reuse from that changed position onward — not the whole cache
every turn, which is how both drafts first overstated it, but enough to defeat the property the
front-only design exists for. At quantum 8 the boundary is sticky:

| images in history | transmitted |
|---:|---:|
| 63 | 63 |
| 64 | 64 |
| 65 | 57 |
| 72 | 64 |
| 73 | 57 |

The cost is at most 7 images omitted earlier than strictly necessary. The benefit is a boundary
that changes once every 8 images instead of every turn. Worth it.

## 4. Delivery — and where the two plans diverged

The cross-review recommended building the patch against the pinned versions and **baking it into a
digest-pinned image**. This plan does not, and the honest reason is narrower than the first draft's.

**dsh publishes no container image** — `install-job.yaml` exists precisely because of that. Building
one means owning and maintaining a second artifact, its registry, and its rebuild cadence for a
package that already installs cleanly. That is an *operational* argument, and it is the real one.

The first draft also argued the image would "carry the LiteLLM credential path". The review is right
that this does not hold: credentials are injected at runtime, and a credential *pathname* is not a
credential. The npm install already sits inside that trust boundary. Argument withdrawn.

So: **apply the patch in the install Job**, on the RWX volume, before the `.installed` marker —
reusing egress confined to the Job, a versioned prefix, and verify-then-mark.

**What that does NOT inherit, stated rather than glossed:** a digest identifies a built artifact; a
versioned directory plus a marker identify a *label*, unless the marker is tied to verified
contents. And a pinned top-level package does not freeze its transitive closure. Recovering the
difference needs a committed lockfile, a frozen install (`npm ci` semantics), and hashes of both the
patch and its output recorded in the marker. Fresh staging also still depends on the registry being
reachable, where a tested image would not — an existing estate tradeoff, but not an equivalence.

Sequence:

1. **File it upstream first**, with the evidence in §2. It is a small, obviously-correct patch to
   a package that is already `-alpha`; it may well land before we need the local delta.
2. **Local patch in the install Job** as the bridge: a pinned, reviewed diff applied to
   `node_modules/@deepseek-ai/dsh-llm-pi-ai` after install, verified, then marked. Five conditions,
   all from the cross-review, none optional:

   - **Patch identity must reach the install PREFIX, the marker, and the runtime's selection** —
     not just the Job name. Otherwise a new Job finds an old `.installed` and skips patching; and
     rebuilding the *same* prefix would rewrite files under a running process. A patch bump must
     select a **new** install directory and roll the pod, exactly as `DSH_VERSION`/`DSH_BUILD` do.
     A `DSH_PATCHSET` input carried through the existing `replacements` block is the shape.
   - **Verify-then-mark does not cover concurrent or interrupted writers.** Kubernetes permits
     duplicate Job execution, and a retry can meet half-patched files. Stage in an isolated
     directory and publish by rename; never edit a completed prefix.
   - **"Applied and verified" needs a definition**: assert the expected package version, assert
     pristine hashes of the target files before patching, apply *after* the final npm operation,
     and verify the result.
   - **Verify the adapter dsh ACTUALLY RESOLVES**, not a top-level copy of it. A hoisted tree can
     hold a nested second copy; patching the wrong one yields a successful install with the defect
     intact. Resolve from the same anchor dsh uses and hash that file.
   - **The runtime must not be able to rewrite the patched tree.** Already true and worth keeping:
     `/app` is mounted `readOnly: true` in the `dsh` container and in both init containers that
     touch it — verified in `deployment.yaml`. Writable state lives on separate volumes.
3. **Retire the local delta** when a released version carries the field and passes §5.

## 5. Acceptance — what must be true, not "it works"

Largely the cross-review's, which was sharper than the first draft's:

- **Count bounds independently of bytes.** With the byte budget inactive, grow a history of 2 KB
  images and assert the transmitted count against the table in §3. Include **repeated attachment
  ids** and **images nested in tool results** — the bound is on *occurrences*, not unique files,
  and a naive implementation counts the latter.
- **Both bounds together.** Byte-only and combined-overflow fixtures both stay within 8 MiB.
- **Semantics.** Retained images are the **newest**; placeholders name the omitted attachments;
  text, tool ordering and durable history are untouched. A conversation that previously failed
  resumes through the real gateway and backend.
- **Determinism and prefix stability.** Identical history produces identical projected content
  across retries and restarts, and between count thresholds appending an image leaves the previous
  projected prefix unchanged. **This is the test that catches a tail-pruning implementation**,
  which would pass every "it completed" check.
- **Exercise the ADAPTER, not the helper.** A helper-level test passes *today*, against unpatched
  code, because the helper was never the broken part. Drive both adapter call paths through real
  profile parsing, using the installed patched package, and inspect the payload actually handed to
  the provider.
- **Assert the retained set, not just the ceilings.** Count ≤ 64 **and** bytes ≤ 8 MiB together,
  plus the expected retained suffix with count dominating and with bytes dominating. Upper-bound
  checks alone accept an implementation that prunes far too much.
- **Vary the numbers.** A hardcoded 64/quantum-8 passes every boundary fixture above. Test a second
  cap/quantum pair, and test both fields *omitted* — which must reproduce today's behaviour exactly.
- **Upgrade an EXISTING volume, not just a fresh one.** This is the trap that already bit #613:
  `settings.yaml` is seeded only when absent, so adding `maxRequestImages` to `settings.seed.yaml`
  reaches a fresh volume and **nothing else**. It must go through the `reconcile-provider.js` path
  that rewrites the `litellm` provider block on every boot. Test the upgrade, and test rollback —
  old code meeting the new fields.

## 6. Rejected, with reasons

| Option | Why not |
|---|---|
| **Lower `maxRequestImageBytes` as a count proxy** | Cannot express a count bound. A guarantee needs `64 × smallest image` = 128 KB, which offloads almost every real screenshot; at the median, 100 × 2 KB images sit at 200 KB — under budget, over count. Smaller images make it *worse*. Lowers probability, does not close the hole, and regresses the screenshot case the byte budget was tuned for. |
| **Raise `MM_IMAGES`** | Moves the wall. vLLM profiles the encoder at the largest accepted image and that peak comes out of the KV pool; the documented ceiling is `max_model_len / 2048`. Costs concurrency, leaves accumulation unbounded, and is per-route — the client still would not know the limit. |
| **Prune in the LiteLLM gateway** | Wrong layer. It would duplicate dsh's attachment, placeholder and quantisation semantics in a shared gateway, and dsh would still transmit the full payload to it. Breaks the front-only stability property. |
| **A replacement adapter or provider-wrapper plugin** | Expands the trusted surface — in a pod that executes model-authored code — to supply plumbing that already exists one layer down. Rejected by both plans independently. |
| **Tail pruning / one-at-a-time trimming / conversation reset** | Tail pruning discards the most recent evidence; trimming one at a time moves the boundary every image; a reset loses continuity. All are worse than the quantised front-only offload already implemented. |

## 7. Traps

- **The fields do nothing on unpatched `0.1.5-alpha.2`.** Setting `maxRequestImages` today is inert
  — and, because the profile schema is a closed schemastery object, may be *rejected* outright,
  which would fail provider resolution rather than being ignored. Verify which before shipping the
  setting ahead of the patch. Ship the patch first, or the setting breaks the provider.
- **A setting added to the seed does not reach an existing deployment.** See the acceptance item
  above; this is the same failure mode as #613's `AGENTS.md`, and this repo now has exactly one
  correct path for it (`reconcile-provider.js`, invoked per provider).
- **Count occurrences, not files.** The same attachment referenced twice is two occurrences.
- **Quantisation buys cache reuse between boundary changes**, not uninterrupted hits.
- **Byte-driven pruning can still move the boundary** independently of the count bound; the two
  interact and the acceptance fixtures must exercise both.
- **This bounds what is transmitted, not what is stored.** Durable history, text context and disk
  are unaffected, and no setting makes more than 64 images simultaneously visible to the model.

## 8. Not doing

Raising the byte budget, lowering it as a proxy, or touching `MM_IMAGES`. None bound the count and
two cost something real.

---

## Appendix — how this plan was produced

Two independent plans, then a cross-review, all with `codex exec -s read-only -m gpt-6-astra`.

**Codex's plan corrected mine on three points**, all adopted: `countQuantum: 8` rather than 1 (my
draft said 1 only because `byteQuantum` is 1 — cargo-culting, not reasoning); acceptance framed
around *occurrences* rather than files, with nested tool-result images and repeated attachment ids;
and the determinism/prefix-stability requirement as the test that catches tail-pruning.

**The cross-review of the merged plan then**: accepted the delivery override as defensible but
rejected the claim that the Job "inherits every property" of an image, and supplied the five
delivery conditions in §4; withdrew my credential argument as not holding; narrowed its **own**
earlier quantum-1 claim, which I had repeated; and added four acceptance counterexamples — including
the existing-volume upgrade case, which is the failure this repo has already shipped once.

Nothing was carried over a reviewer's objection.
