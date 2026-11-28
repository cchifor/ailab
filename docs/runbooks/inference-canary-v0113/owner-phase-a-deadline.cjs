"use strict";

// The operator orchestrator exports decimal epoch seconds; direct callers may
// supply the original integer epoch milliseconds. Normalize once, never round
// a submillisecond expiry up or infer a duration from either representation.
function parseRunnerDeadline(value, maximumMs, now = Date.now()) {
  let deadline;
  if (typeof value === "string" && /^[0-9]{13}$/.test(value)) {
    deadline = Number(value);
  } else if (
    typeof value === "string" &&
    /^[0-9]{10}(?:\.[0-9]{1,9})?$/.test(value)
  ) {
    const [seconds, fraction = ""] = value.split(".");
    deadline = Number(
      BigInt(seconds) * 1000n + BigInt((fraction + "000").slice(0, 3)),
    );
  }
  if (
    !Number.isSafeInteger(now) ||
    !Number.isSafeInteger(maximumMs) ||
    maximumMs <= 0 ||
    maximumMs > 2700000 ||
    !Number.isSafeInteger(deadline) ||
    deadline <= now ||
    deadline - now > maximumMs
  ) {
    throw Error("Runner absolute deadline format or bound invalid");
  }
  return deadline;
}

module.exports = { parseRunnerDeadline };
