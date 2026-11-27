"use strict";

const fs = require("node:fs");
const { readPredecessor: readPrivateUtf8 } = require("./owner-phase-a-cli.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const { parseRunnerDeadline } = require("./owner-phase-a-deadline.cjs");
const {
  createClaudePhaseCAutomation,
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

async function runPhaseCCli({
  argv = process.argv.slice(2),
  env = process.env,
  collectCheckpoint,
  claudePorts,
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
        argv[2] === "--phase-b-report" &&
        argv[4] === "--output",
      "Expected --inputs ABS_PATH --phase-b-report ABS_PATH --output ABS_DIRECTORY",
    );
    need(
      typeof collectCheckpoint === "function" && claudePorts,
      "Reviewed Claude observers and service adapters are not linked",
    );
    const deadline = parseRunnerDeadline(env.RUNNER_DEADLINE, 900000);
    const phaseInputs = JSON.parse(readPrivateUtf8(argv[1]));
    need(
      phaseInputs &&
        typeof phaseInputs === "object" &&
        !Array.isArray(phaseInputs) &&
        !Object.hasOwn(phaseInputs, "phaseBReportRaw"),
      "Completed Phase B must come from its separate private report",
    );
    phaseInputs.phaseBReportRaw = readPrivateUtf8(argv[3]);
    const authenticatedFetch = createSessionFetch(
      env.RELAY_SESSION_SOCKET,
      env.RELAY_SESSION_CONTROL_SOCKET,
    );
    need(deadline > Date.now(), "Runner deadline elapsed during preparation");
    host = createClaudePhaseCAutomation({
      directory: argv[5],
      phaseInputs,
      authenticatedFetch,
      collectCheckpoint,
      claudePorts,
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
        "Warm Claude automation unavailable or stopped; preserve durable evidence",
      lastDurableSnapshot: lastSnapshot,
    });
    return 1;
  } finally {
    signals.removeListener("SIGTERM", terminate);
    signals.removeListener("SIGINT", terminate);
  }
}

if (require.main === module)
  runPhaseCCli().then(
    (code) => {
      process.exitCode = code;
    },
    () => {
      process.exitCode = 1;
    },
  );
module.exports = { runPhaseCCli };
