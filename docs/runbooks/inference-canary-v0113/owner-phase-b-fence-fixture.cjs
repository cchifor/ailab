"use strict";
// TEST ONLY. Compile exact source-pin substitutions; no method/prototype overrides.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const { createHash } = require("node:crypto");
const f = require("./owner-phase-b-fence.cjs");
const SOURCE = "02d698236b6336daee0d5581547636f7cf2b36f3";
const OLD = "4e4bb34e2f968f9450aabe9cf228d3e70c8bab71";
const sha = (v) => createHash("sha256").update(v).digest("hex");
function sourceFixture() {
  const proofs = [];
  function compile(name, substitutions, dependency) {
    const filename = path.join(__dirname, name),
      before = fs.readFileSync(filename, "utf8");
    let after = before;
    for (const [old, next] of substitutions) {
      assert.equal(
        after.split(old).length,
        2,
        "Exactly one pin occurrence required",
      );
      after = after.replace(old, next);
    }
    proofs.push({
      file: name,
      beforeSha256: sha(before),
      afterSha256: sha(after),
      substitutions,
    });
    const mod = new Module(filename, module);
    mod.filename = filename;
    mod.paths = Module._nodeModulePaths(__dirname);
    const originalRequire = mod.require.bind(mod);
    mod.require = (name) =>
      name === "./owner-phase-a.js" && dependency
        ? dependency
        : originalRequire(name);
    mod._compile(after, filename);
    return mod.exports;
  }
  const a = compile("owner-phase-a.js", [[`"${OLD}"`, `"${SOURCE}"`]]);
  const b = compile(
    "owner-phase-b.js",
    [
      [`"${OLD}"`, `"${SOURCE}"`],
      [
        "const QUALIFIED_FENCE_ROUTER = null;",
        `const QUALIFIED_FENCE_ROUTER = "${SOURCE}";`,
      ],
    ],
    a,
  );
  return {
    a,
    b,
    proof: {
      kind: "synthetic-source-pin-compilation-only",
      qualifiedLiveBundle: false,
      source: SOURCE,
      files: proofs,
    },
  };
}
// Synthetic transport with the exact 79316b29 contract and committed operation
// response shape. It supplies no fake Runner success or browser HTTP overrides.
function transport({
  now,
  keyId,
  tenantId,
  saves,
  usage,
  source = SOURCE,
  mutate,
  lose,
  fail,
  observeCall,
}) {
  const created = now() - 1000,
    deploymentId = "11111111-1111-4111-8111-999999999999";
  let current = null;
  const operations = new Map(),
    calls = [];
  const clone = (x) => structuredClone(x);
  function counters() {
    return {
      ...{
        attempts: 0,
        tokenUsageUnknown: 0,
        openRequests: 0,
        staleOpenRequests: 0,
        unattributed: 0,
      },
      ...usage(),
      coveredFrom: created,
    };
  }
  function envelope(response) {
    const value = {
      routerSource: source,
      observedAt: now(),
      projectionSha256: f.digest(response),
      response: clone(response),
    };
    return mutate ? mutate(value, calls.at(-1)) || value : value;
  }
  async function call(method, request) {
    assert(Object.isFrozen(request));
    const body = JSON.parse(request.raw);
    assert.equal(request.bodySha256, sha(request.raw));
    assert.equal(request.requestDigest, f.binding(request.raw));
    assert(request.signal instanceof AbortSignal);
    calls.push({ method, body: clone(body), request });
    observeCall?.(calls.at(-1));
    if (["arm", "advance", "close"].includes(method)) {
      const intent =
        saves.at(-1)?.attemptFence?.operations?.at(-1) ||
        saves.at(-1)?.operations?.at(-1);
      assert.equal(
        intent?.raw,
        request.raw,
        "exact intent must be durable before signer sees request",
      );
      assert.equal(intent?.status, "sending");
      assert(!operations.has(body.operationId), "mutation was resent");
    }
    if (fail?.(method, body)) throw Error("synthetic transport failure");
    if (method === "readOperation") {
      assert.equal(body.action, "operation");
      assert(operations.has(body.targetOperationId));
      return envelope(operations.get(body.targetOperationId));
    }
    const counts = counters();
    if (method === "inspect") {
      return envelope({
        version: 1,
        action: "attempt-fence-inspect",
        operationId: body.operationId,
        observedAt: now(),
        deploymentId,
        keyId,
        keyCreatedAt: created,
        marker: current
          ? { fenceId: current.plan.fenceId, revision: current.revision }
          : null,
        fence: clone(current),
        ...counts,
      });
    }
    assert.equal(body.expectedAttempts, counts.attempts);
    assert.equal(body.expectedUnknownUsage, counts.tokenUsageUnknown);
    assert.equal(body.expectedFenceRevision, current?.revision || 0);
    // The actual Router captures one timestamp for the fence and its atomic
    // operation receipt. Separate reads can straddle a millisecond boundary.
    const committedAt = now();
    if (method === "arm") {
      const {
        version,
        action,
        operationId,
        keyId: _key,
        expectedAttempts,
        expectedUnknownUsage,
        expectedFenceRevision,
        ...plan
      } = body;
      if (current) {
        assert(current.closedAt);
        assert.equal(plan.predecessorFenceId, current.plan.fenceId);
      }
      current = {
        v: 1,
        keyId,
        issuer: "synthetic-relay-issuer",
        tenant: tenantId,
        plan,
        revision: expectedFenceRevision + 1,
        activeRow: 0,
        baselineAttempts: counts.attempts,
        rowStartAttempts: counts.attempts,
        baselineTokenUsageUnknown: counts.tokenUsageUnknown,
        rowStartTokenUsageUnknown: counts.tokenUsageUnknown,
        completed: [],
        createdAt: committedAt,
      };
    } else if (method === "advance") {
      assert.equal(body.fenceId, current.plan.fenceId);
      const row = current.plan.rows[current.activeRow],
        used = counts.attempts - current.rowStartAttempts;
      assert.equal(body.completedRowId, row.id);
      assert.equal(body.nextRowId, current.plan.rows[current.activeRow + 1].id);
      current.completed.push({
        rowId: row.id,
        used,
        retiredUnused: row.limit - used,
        terminalEvidenceSha256: body.terminalEvidenceSha256,
      });
      current.activeRow++;
      current.rowStartAttempts = counts.attempts;
      current.rowStartTokenUsageUnknown = counts.tokenUsageUnknown;
      current.revision++;
    } else {
      assert.equal(method, "close");
      current.closedAt = committedAt;
      current.closedAttempts = counts.attempts;
      current.closedTokenUsageUnknown = counts.tokenUsageUnknown;
      current.terminalEvidenceSha256 = body.terminalEvidenceSha256;
      current.revision++;
    }
    const receipt = {
      version: 1,
      action: body.action,
      operationId: body.operationId,
      // The HTTP route passes digest(raw) to RelayKeyService.execute. JWT
      // binding(raw) authenticates the request separately; it is not this field.
      requestDigest: request.bodySha256,
      committedAt,
      fence: clone(current),
      ...counts,
    };
    operations.set(body.operationId, clone(receipt));
    if (lose?.(method, body)) throw Error("synthetic lost committed response");
    return envelope(receipt);
  }
  return {
    port: Object.fromEntries(
      ["inspect", "arm", "advance", "close", "readOperation"].map((k) => [
        k,
        (r) => call(k, r),
      ]),
    ),
    calls,
    current: () => clone(current),
    deploymentId,
    operations,
  };
}
module.exports = { sourceFixture, transport, SOURCE };
