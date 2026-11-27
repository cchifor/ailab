"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const a = require("./owner-phase-a.js");
const {
  createAutomation,
  createStore,
} = require("./owner-phase-a-automation.cjs");

// Synthetic original report, never the owner's data. Actual driver validates it.
function original() {
  const p = a.PROFILES.codex,
    now = a.EXPIRES - 10000;
  const ids = [1, 2, 3, 4].map(
    (n) => `${String(n).padStart(8, "0")}-1111-4111-8111-111111111111`,
  );
  const date = (n) => new Date(n).toISOString();
  const scope = {
    namespace: "default",
    principal: "key:" + p.key,
    route: "canary-pi-codex",
    since: now - 1000,
  };
  const first = {
    version: 1,
    source: a.STOPPED_SOURCE,
    origin: "http://127.0.0.1:8787",
    scope,
    requestedAt: now - 100,
    observedAt: now - 99,
    rows: [],
    fetchedRows: 0,
    totalAttempts: 0,
  };
  const session = "/api/agent-sessions/" + a.STOPPED_SESSION;
  return (
    JSON.stringify(
      {
        version: 1,
        relay: a.SOURCE,
        router: a.STOPPED_SOURCE,
        status: "stopped",
        reason: "Unexpected tool permission",
        completed: false,
        runs: [
          {
            provider: "codex",
            runId: p.run,
            effort: false,
            prompts: 1,
            deadline: now - 1,
            firstSnapshot: first,
            lastSnapshot: first,
          },
        ],
        evidence: [
          {
            kind: "launch",
            at: date(now - 80),
            id: ids[0],
            operationId: ids[1],
            sessionId: a.STOPPED_SESSION,
            profileId: p.id,
            revision: 1,
            journalId: a.HOLDER,
            holderId: ids[2],
            recovery: false,
          },
          {
            kind: "pre-launch",
            marker: {
              version: 1,
              provider: "codex",
              profileId: p.id,
              runId: p.run,
              phase: "baseline",
              state: "observed",
              phaseChecksPass: true,
              observedUtc: date(now),
              commands: {
                denied: a.markerCommand("codex", p.run, "denied"),
                effects: a.markerCommand("codex", p.run, "effects"),
              },
              files: {
                denied: { state: "absent" },
                effects: { state: "absent" },
              },
            },
          },
          { kind: "cleanup", status: "closed-and-released", at: date(now) },
        ],
        operations: [
          {
            status: "received",
            path: "/api/agent-launches",
            body: { operationId: ids[1] },
          },
          {
            status: "received",
            kind: "lease-acquisition",
            path: session + "/control",
          },
          {
            status: "received",
            path:
              "/api/agent-permissions/" + a.STOPPED_PERMISSION + "/decision",
            body: { outcome: { outcome: "cancelled" } },
          },
          ...["prompt", "cancel", "close"].map((kind, i) => ({
            status: "received",
            path: session + "/commands",
            body: {
              kind,
              generation: 1,
              operationId: ids[i],
              ...(kind === "prompt"
                ? {
                    text: "Reply exactly READY. Do not use tools or modify files.",
                  }
                : {}),
            },
          })),
        ],
      },
      null,
      2,
    ) + "\n"
  );
}
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "owner-automation-"));
  fs.chmodSync(root, 0o700);
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return path.join(root, "run");
}
function setup(t, overrides = {}) {
  const directory = fixture(t),
    predecessorRaw = original();
  assert.equal(a.stoppedPredecessor(predecessorRaw).run.prompts, 1);
  return {
    directory,
    predecessorRaw,
    authenticatedFetch: async () => {
      throw Error("Unexpected HTTP");
    },
    collectCheckpoint: async () => ({}),
    maxDurationMs: 1000,
    cleanupTimeoutMs: 100,
    ...overrides,
  };
}
function deployment() {
  return {
    confirmMetadataReview: true,
    reportDownloaded: true,
    operatorVerified: true,
    deploymentVerified: true,
    observedUtc: new Date().toISOString(),
    hubSource: a.RUN_SOURCE,
  };
}

test("exclusive store preserves original bytes and refuses reuse or partial directories", (t) => {
  const dir = fixture(t),
    raw = original(),
    store = createStore(dir, raw);
  store.save({ operation: "sending" });
  store.save({ operation: "uncertain" });
  assert.equal(
    fs.readFileSync(path.join(dir, "predecessor.json"), "utf8"),
    raw,
  );
  assert.equal(
    fs.readFileSync(path.join(dir, "predecessor.sha256"), "utf8").trim(),
    createHash("sha256").update(raw).digest("hex"),
  );
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(dir, "state-000001.json"))),
    { operation: "sending" },
  );
  assert.throws(() => createStore(dir, raw), /EEXIST/);
  const partial = path.join(path.dirname(dir), "partial");
  fs.mkdirSync(partial);
  assert.throws(() => createStore(partial, raw), /EEXIST/);
});

test("save failure never overwrites the preceding durable snapshot", (t) => {
  const dir = fixture(t),
    store = createStore(dir, original());
  store.save({ first: true });
  fs.writeFileSync(path.join(dir, "state-000002.json"), "collision");
  assert.throws(() => store.save({ second: true }), /EEXIST/);
  assert.equal(
    fs.readFileSync(path.join(dir, "state-000002.json"), "utf8"),
    "collision",
  );
});

test(
  "parent directory fsync precedes file writes and its failure permits no HTTP",
  { skip: process.platform === "win32" },
  (t) => {
    const o = setup(t),
      parent = path.dirname(o.directory);
    const open = fs.openSync,
      close = fs.closeSync,
      sync = fs.fsyncSync;
    const descriptors = new Map();
    let parentSynced = false,
      fileOpened = false,
      calls = 0;
    o.authenticatedFetch = async () => {
      calls++;
    };
    fs.openSync = (file, ...args) => {
      if (file !== parent) {
        fileOpened = true;
        assert.equal(parentSynced, true);
      }
      const fd = open(file, ...args);
      descriptors.set(fd, file);
      return fd;
    };
    fs.closeSync = (fd) => {
      descriptors.delete(fd);
      return close(fd);
    };
    fs.fsyncSync = (fd) => {
      if (descriptors.get(fd) === parent)
        throw Error("Injected parent fsync failure");
      return sync(fd);
    };
    try {
      assert.throws(() => createAutomation(o), /parent fsync failure/);
    } finally {
      fs.openSync = open;
      fs.closeSync = close;
      fs.fsyncSync = sync;
    }
    assert.equal(fileOpened, false);
    assert.equal(calls, 0);
    assert.equal(fs.existsSync(o.directory), true);
    assert.throws(() => createAutomation(o), /EEXIST/);
    // Now record successful ordering in a separate fresh store.
    const next = path.join(parent, "next");
    fs.openSync = (file, ...args) => {
      if (file !== parent) assert.equal(parentSynced, true);
      const fd = open(file, ...args);
      descriptors.set(fd, file);
      return fd;
    };
    fs.fsyncSync = (fd) => {
      if (descriptors.get(fd) === parent) parentSynced = true;
      return sync(fd);
    };
    fs.closeSync = (fd) => {
      descriptors.delete(fd);
      return close(fd);
    };
    try {
      createStore(next, original());
    } finally {
      fs.openSync = open;
      fs.closeSync = close;
      fs.fsyncSync = sync;
    }
    assert.equal(parentSynced, true);
  },
);

test("actual Runner mutation persists sending then uncertain, and save failure prevents POST", async (t) => {
  const dir = fixture(t),
    store = createStore(dir, original());
  let calls = 0;
  const runner = new a.Runner({
    ...store,
    origin: a.ORIGIN,
    crypto: require("node:crypto").webcrypto,
    fetch: async () => {
      calls++;
      const saved = JSON.parse(
        fs.readFileSync(path.join(dir, "state-000001.json")),
      );
      assert.equal(saved.operations[0].status, "sending");
      assert.equal(
        saved.operations[0].body.operationId,
        "11111111-1111-4111-8111-111111111111",
      );
      throw Error("Synthetic response lost after dispatch");
    },
  });
  runner.effectiveExpiry = Date.now() + 10000; // Offline metadata fixture only.
  const body = {
    operationId: "11111111-1111-4111-8111-111111111111",
    revision: 2,
  };
  await assert.rejects(
    runner.mutate("/api/router-credentials/fixture/revoke", body),
    /response lost/,
  );
  assert.equal(calls, 1);
  const saved = JSON.parse(
    fs.readFileSync(path.join(dir, "state-000002.json")),
  );
  assert.equal(saved.operations[0].status, "uncertain");
  fs.writeFileSync(path.join(dir, "state-000003.json"), "collision");
  await assert.rejects(
    runner.mutate("/api/router-credentials/fixture/revoke", body),
    /EEXIST/,
  );
  assert.equal(calls, 1);
});

test("partial successor refused before directory creation, collector or HTTP", (t) => {
  let calls = 0;
  const o = setup(t, {
    predecessorRaw: JSON.stringify({
      predecessor: { raw: original() },
      status: "running",
    }),
    authenticatedFetch: async () => {
      calls++;
    },
    collectCheckpoint: async () => {
      calls++;
    },
  });
  assert.throws(() => createAutomation(o));
  assert.equal(calls, 0);
  assert.equal(fs.existsSync(o.directory), false);
});

test("actual Runner rejects invalid machine confirmation with zero HTTP and retains original", async (t) => {
  let calls = 0;
  const o = setup(t, {
    authenticatedFetch: async () => {
      calls++;
    },
  });
  const host = createAutomation(o);
  await assert.rejects(host.run(), /preserve store/);
  assert.equal(calls, 0);
  assert.equal(
    fs.readFileSync(path.join(o.directory, "predecessor.json"), "utf8"),
    o.predecessorRaw,
  );
  await assert.rejects(host.run(), /single-use/);
});

test("actual Runner saves before first authenticated GET; transport error is not retried", async (t) => {
  let calls = 0;
  const o = setup(t, { collectCheckpoint: async () => deployment() });
  o.authenticatedFetch = async (url, init) => {
    calls++;
    assert.equal(url, "/api/router-credentials");
    assert.equal(init.method, "GET");
    const saved = JSON.parse(
      fs.readFileSync(path.join(o.directory, "state-000001.json")),
    );
    assert.equal(saved.status, "running");
    assert.equal(saved.predecessor.raw, o.predecessorRaw);
    throw Error("Transport unavailable");
  };
  await assert.rejects(createAutomation(o).run(), /preserve store/);
  assert.equal(calls, 1);
});

test("execution timeout aborts collector and permits no late confirmation or HTTP", async (t) => {
  let calls = 0,
    collectorSignal,
    answer;
  const o = setup(t, {
    maxDurationMs: 30,
    authenticatedFetch: async () => {
      calls++;
    },
    collectCheckpoint: ({ signal }) => {
      collectorSignal = signal;
      return new Promise((r) => {
        answer = r;
      });
    },
  });
  await assert.rejects(createAutomation(o).run(), /preserve store/);
  assert.equal(collectorSignal.aborted, true);
  answer(deployment());
  await new Promise((r) => setImmediate(r));
  assert.equal(calls, 0);
});

test("abort drains cooperative authenticated request before returning", async (t) => {
  const controller = new AbortController();
  let drained = false;
  const o = setup(t, {
    signal: controller.signal,
    collectCheckpoint: async () => deployment(),
    authenticatedFetch: (url, { signal }) =>
      new Promise((resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => {
            drained = true;
            reject(Error("aborted"));
          },
          { once: true },
        );
        queueMicrotask(() => controller.abort());
      }),
  });
  await assert.rejects(createAutomation(o).run(), /preserve store/);
  assert.equal(drained, true);
});

test("uncooperative transport ends bounded with retained unresolved cleanup and no retry", async (t) => {
  let calls = 0;
  const o = setup(t, {
    maxDurationMs: 30,
    cleanupTimeoutMs: 30,
    collectCheckpoint: async () => deployment(),
    authenticatedFetch: () => {
      calls++;
      return new Promise(() => {});
    },
  });
  await assert.rejects(createAutomation(o).run(), /cleanup unresolved/);
  assert.equal(calls, 1);
  const states = fs
    .readdirSync(o.directory)
    .filter((n) => n.startsWith("state-"));
  const last = JSON.parse(
    fs.readFileSync(path.join(o.directory, states.sort().at(-1))),
  );
  assert.equal(last.automationHost.state, "cleanup-unresolved");
  assert.equal(last.automationHost.outstandingTransports, 1);
  assert.equal(fs.existsSync(path.join(o.directory, "run.lock")), true);
});

test("response body is part of auth drain; hung body is explicitly unresolved", async (t) => {
  let calls = 0,
    bodyStarted = false;
  const o = setup(t, {
    maxDurationMs: 30,
    cleanupTimeoutMs: 30,
    collectCheckpoint: async () => deployment(),
    authenticatedFetch: async () => {
      calls++;
      return {
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        text: () => {
          bodyStarted = true;
          return new Promise(() => {});
        },
      };
    },
  });
  await assert.rejects(createAutomation(o).run(), /cleanup unresolved/);
  assert.equal(calls, 1);
  assert.equal(bodyStarted, true);
  const files = fs
    .readdirSync(o.directory)
    .filter((n) => n.startsWith("state-"))
    .sort();
  const last = JSON.parse(
    fs.readFileSync(path.join(o.directory, files.at(-1))),
  );
  assert.equal(last.automationHost.outstandingTransports, 1);
});

test("uncooperative collector is retained as unresolved; late value cannot dispatch", async (t) => {
  let answer,
    calls = 0;
  const o = setup(t, {
    maxDurationMs: 30,
    cleanupTimeoutMs: 30,
    collectCheckpoint: () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
    authenticatedFetch: async () => {
      calls++;
    },
  });
  await assert.rejects(createAutomation(o).run(), /cleanup unresolved/);
  const files = fs
    .readdirSync(o.directory)
    .filter((n) => n.startsWith("state-"))
    .sort();
  const last = JSON.parse(
    fs.readFileSync(path.join(o.directory, files.at(-1))),
  );
  assert.equal(last.automationHost.outstandingCollectors, 1);
  answer(deployment());
  await new Promise((r) => setImmediate(r));
  assert.equal(calls, 0);
});

test("heartbeat rejection aborts actual Runner checkpoint and clears future renewals", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
  // Negative fault injection only: actual Runner owns the pending flow and cleanup.
  const renew = t.mock.method(a.Runner.prototype, "renew", async () => {
    throw Error("Synthetic lease renewal failure");
  });
  let calls = 0,
    collectorSignal;
  const o = setup(t, {
    maxDurationMs: 2700000,
    collectCheckpoint: ({ signal }) => {
      collectorSignal = signal;
      return new Promise((resolve, reject) => {
        signal.addEventListener(
          "abort",
          () => reject(Error("Checkpoint aborted")),
          { once: true },
        );
      });
    },
    authenticatedFetch: async () => {
      calls++;
    },
  });
  const host = createAutomation(o);
  const rejected = assert.rejects(host.run(), /preserve store/);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(collectorSignal.aborted, false);
  t.mock.timers.tick(19999);
  assert.equal(renew.mock.callCount(), 0);
  t.mock.timers.tick(1);
  await rejected;
  assert.equal(renew.mock.callCount(), 1);
  assert.equal(collectorSignal.aborted, true);
  assert.equal(calls, 0);
  const states = fs
    .readdirSync(o.directory)
    .filter((name) => name.startsWith("state-"))
    .sort();
  assert.ok(states.length > 0);
  const last = JSON.parse(
    fs.readFileSync(path.join(o.directory, states.at(-1))),
  );
  assert.equal(last.status, "stopped");
  assert.equal(
    fs.readFileSync(path.join(o.directory, "predecessor.json"), "utf8"),
    o.predecessorRaw,
  );
  await assert.rejects(host.run(), /single-use/);
  t.mock.timers.tick(2700000);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(renew.mock.callCount(), 1);
  assert.equal(calls, 0);
});

test("host ceiling accepts three-phase budget but does not accept unbounded deadlines", (t) => {
  const o = setup(t, { maxDurationMs: 2700000 });
  createAutomation(o);
  for (const maxDurationMs of [Infinity, NaN, 2700001, 0, -1]) {
    assert.throws(
      () => createAutomation({ ...o, maxDurationMs }),
      /Bounded execution/,
    );
  }
});
