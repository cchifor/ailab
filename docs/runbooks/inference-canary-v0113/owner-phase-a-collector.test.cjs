"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { setImmediate: nextTurn } = require("node:timers/promises");
const { createFixture } = require("./owner-phase-a-automation-fixture.cjs");

test("actual synthetic collector rechecks its wall-clock boundary after an early wake", async (t) => {
  const directory = fs.mkdtempSync(
    path.join(os.tmpdir(), "relay-collector-clock-"),
  );
  fs.chmodSync(directory, 0o700);
  const f = await createFixture(
    directory,
    path.join(directory, "unused-output"),
  );
  const controller = new AbortController();
  let pending;
  try {
    // Negative clock experiment only: this does not claim the unretained
    // observation in the full-run failure had this exact clock interleaving.
    const now = Date.now(),
      notBefore = now + 10;
    t.mock.timers.enable({ apis: ["Date"], now });
    let settled = false;
    pending = f
      .collectCheckpoint({
        context: Object.freeze({
          version: 1,
          kind: "renewal-deployment",
          notBefore,
        }),
        signal: controller.signal,
      })
      .then((value) => {
        settled = true;
        return value;
      });
    // The duration timer can finish while wall time is still before the
    // required boundary. No HTTP, Runner, or collector function is replaced.
    t.mock.timers.setTime(now - 100);
    const wakeBy = process.hrtime.bigint() + 50000000n;
    while (process.hrtime.bigint() < wakeBy) await nextTurn();
    assert.equal(f.requests.length, 0);
    assert.equal(
      settled,
      false,
      "must not emit an observation before notBefore",
    );
    t.mock.timers.setTime(notBefore);
    const observed = await pending;
    assert(Date.parse(observed.observedUtc) >= notBefore);
    assert.equal(
      observed.observedUtc,
      new Date(Date.now()).toISOString(),
      "timestamp is the actual fixture wall clock",
    );
    assert.equal(f.requests.length, 0);
  } finally {
    controller.abort();
    await pending?.catch(() => {});
    t.mock.timers.reset();
    await f.close();
    fs.rmSync(directory, { recursive: true });
  }
});

test("actual collector boundary wait remains abortable without HTTP or a fabricated observation", async (t) => {
  const directory = fs.mkdtempSync(
    path.join(os.tmpdir(), "relay-collector-abort-"),
  );
  fs.chmodSync(directory, 0o700);
  const f = await createFixture(
    directory,
    path.join(directory, "unused-output"),
  );
  const controller = new AbortController();
  let pending;
  try {
    const now = Date.now();
    t.mock.timers.enable({ apis: ["Date"], now });
    pending = f.collectCheckpoint({
      context: Object.freeze({
        version: 1,
        kind: "renewal-deployment",
        notBefore: now + 10,
      }),
      signal: controller.signal,
    });
    const rejected = assert.rejects(pending, { name: "AbortError" });
    t.mock.timers.setTime(now - 100);
    const wakeBy = process.hrtime.bigint() + 50000000n;
    while (process.hrtime.bigint() < wakeBy) await nextTurn();
    const abortAt = process.hrtime.bigint();
    controller.abort();
    await rejected;
    assert(
      process.hrtime.bigint() - abortAt < 1000000000n,
      "abort does not wait for the wall clock",
    );
    assert.equal(f.requests.length, 0);
  } finally {
    controller.abort();
    await pending?.catch(() => {});
    t.mock.timers.reset();
    await f.close();
    fs.rmSync(directory, { recursive: true });
  }
});
