"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseRunnerDeadline } = require("./owner-phase-a-deadline.cjs");
const now = 1795816200000;

test("operator decimal seconds and direct milliseconds describe the same expiry", () => {
  assert.equal(
    parseRunnerDeadline("1795816201.234567", 60000, now),
    now + 1234,
  );
  assert.equal(parseRunnerDeadline("1795816201234", 60000, now), now + 1234);
  assert.equal(parseRunnerDeadline("1795816201", 60000, now), now + 1000);
});

test("fractional seconds are truncated before the expiry bound is checked", () => {
  assert.equal(
    parseRunnerDeadline("1795816201.234999999", 1234, now),
    now + 1234,
  );
  assert.throws(() => parseRunnerDeadline("1795816200.000999", 1000, now));
  assert.throws(() => parseRunnerDeadline("1795816201.235000", 1234, now));
});

test("expired, oversized and ambiguous deadline representations fail closed", () => {
  for (const value of [
    undefined,
    null,
    1795816201,
    "",
    " 1795816201",
    "1795816201 ",
    "+1795816201",
    "1795816201e0",
    "1795816201.",
    "1795816201.1234567890",
    "1795816201234.0",
    "1795816200",
    "1795816260.001",
    "Infinity",
    "NaN",
  ])
    assert.throws(() => parseRunnerDeadline(value, 60000, now));
  for (const limit of [0, -1, 2700001, NaN, Infinity, 1.5])
    assert.throws(() => parseRunnerDeadline("1795816201", limit, now));
});
