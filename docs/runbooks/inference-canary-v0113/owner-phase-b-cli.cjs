"use strict";

const fs = require("node:fs");
const { createHash } = require("node:crypto");
const { readPredecessor: readPrivateUtf8 } = require("./owner-phase-a-cli.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const {
  createClaudePhaseBAutomation,
} = require("./owner-phase-a-automation.cjs");

function need(value, message) {
  if (!value) throw Error(message);
}
function writeStdout(line) {
  const bytes = Buffer.from(line);
  for (let offset = 0; offset < bytes.length;) {
    const written = fs.writeSync(1, bytes, offset, bytes.length - offset);
    need(written > 0, "Output write failed");
    offset += written;
  }
}

// This proves the retained report's structure, not its origin or live validity.
// The operator binds it to the completed A execution in the reviewed bundle.
function completedPhaseA(raw) {
  const report = JSON.parse(raw);
  need(
    report?.version === 1 &&
      report.phaseACompleted === true &&
      ["phase-a-passed", "phase-a-and-grants-closed"].includes(report.status) &&
      Array.isArray(report.runs) &&
      report.runs.length === 3,
    "Completed Phase A report required",
  );
  const expected = [
    ["codex", false],
    ["qwen", false],
    ["codex", true],
  ];
  for (let i = 0; i < expected.length; i++) {
    const run = report.runs[i];
    need(
      run.provider === expected[i][0] &&
        run.effort === expected[i][1] &&
        run.status === "passed-with-live-wire-unobserved",
      "Phase A workflow is incomplete",
    );
  }
  need(
    report.replacement?.carriedPrompts === 1 &&
      report.replacement.carriedAttempts === 2 &&
      typeof report.predecessor?.raw === "string",
    "Phase A original accounting missing",
  );
  return createHash("sha256").update(raw).digest("hex");
}

async function runPhaseBCli({
  argv = process.argv.slice(2),
  env = process.env,
  collectCheckpoint,
  writeLine = writeStdout,
  signals = process,
} = {}) {
  let host,
    lastSnapshot = null;
  const controller = new AbortController();
  const terminate = () => {
    controller.abort();
    if (host) void host.stop().catch(() => {});
  };
  const emit = (record) => writeLine(JSON.stringify(record) + "\n");
  try {
    need(process.platform === "linux", "Linux execution required");
    need(
      argv.length === 6 &&
        argv[0] === "--inputs" &&
        argv[2] === "--phase-a-report" &&
        argv[4] === "--output",
      "Expected --inputs ABS_PATH --phase-a-report ABS_PATH --output ABS_DIRECTORY",
    );
    need(
      typeof collectCheckpoint === "function",
      "Reviewed Claude observer is not linked",
    );
    need(
      typeof env.RUNNER_DEADLINE === "string" &&
        /^[0-9]{13}$/.test(env.RUNNER_DEADLINE),
      "RUNNER_DEADLINE epoch milliseconds required",
    );
    const deadline = Number(env.RUNNER_DEADLINE);
    need(
      Number.isSafeInteger(deadline) &&
        deadline > Date.now() &&
        deadline - Date.now() <= 1800000,
      "Phase B deadline outside bound",
    );
    const phaseInputs = JSON.parse(readPrivateUtf8(argv[1]));
    const phaseAReportSha256 = completedPhaseA(readPrivateUtf8(argv[3]));
    const authenticatedFetch = createSessionFetch(env.RELAY_SESSION_SOCKET);
    host = createClaudePhaseBAutomation({
      directory: argv[5],
      phaseInputs,
      phaseAReportSha256,
      authenticatedFetch,
      collectCheckpoint,
      deadline,
      maxDurationMs: deadline - Date.now(),
      signal: controller.signal,
      onSnapshot: (state) => {
        lastSnapshot = state;
        emit({ type: "snapshot", state });
      },
    });
    signals.on("SIGTERM", terminate);
    signals.on("SIGINT", terminate);
    const state = await host.run();
    emit({ type: "final", ok: true, state });
    return 0;
  } catch {
    emit({
      type: "final",
      ok: false,
      reason:
        "Claude automation unavailable or stopped; preserve durable evidence",
      lastDurableSnapshot: lastSnapshot,
    });
    return 1;
  } finally {
    signals.removeListener("SIGTERM", terminate);
    signals.removeListener("SIGINT", terminate);
  }
}

if (require.main === module)
  runPhaseBCli().then(
    (code) => {
      process.exitCode = code;
    },
    () => {
      process.exitCode = 1;
    },
  );
module.exports = { runPhaseBCli, completedPhaseA };
