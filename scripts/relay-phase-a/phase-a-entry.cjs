"use strict";
// phase-a-entry.cjs: the Runner entrypoint of an automated phase A session (plans/2026-11-27-relay-phase-a-observer-plan.md,
// section 1). It links the inference session's pinned owner-phase-a-cli.cjs to the observer client and nothing else:
//   - RUNNER_DEADLINE from the broker is epoch SECONDS (a decimal); runCli requires a 13-digit epoch-ms string;
//   - the predecessor report is the read-only staged copy, and the output directory is created by the CLI under the
//     broker-staged private /state/run;
//   - predecessorEvidencePin is the reviewed pin file shipped in the bundle (absent: undefined);
//   - the CLI's return code is the process exit code.
const fs = require("node:fs");
const path = require("node:path");

const PREDECESSOR = "/predecessor/report.json";
const OUTPUT = "/state/run/phase-a";

function deadlineMs(env) {
  const raw = env.RUNNER_DEADLINE;
  if (typeof raw !== "string" || !/^[0-9]{10}(\.[0-9]{1,9})?$/.test(raw)) {
    throw new Error("RUNNER_DEADLINE (epoch seconds) required");
  }
  const ms = Math.floor(Number(raw) * 1000);
  if (!Number.isSafeInteger(ms) || String(ms).length !== 13) throw new Error("RUNNER_DEADLINE out of range");
  return String(ms);
}

function loadPin(dir) {
  const file = path.join(dir, "predecessor-pin.json");
  if (!fs.existsSync(file)) return undefined;
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

async function main({ env = process.env, dir = __dirname, cli, client } = {}) {
  const { runCli } = cli || require(path.join(dir, "owner-phase-a-cli.cjs"));
  const { createObserverClient } = client || require(path.join(dir, "observer-client.cjs"));
  const socket = env.RELAY_OBSERVER_SOCKET;
  if (typeof socket !== "string" || !socket.startsWith("/")) throw new Error("RELAY_OBSERVER_SOCKET required");
  const observer = createObserverClient(socket);
  return runCli({
    argv: ["--predecessor", PREDECESSOR, "--output", OUTPUT],
    env: { ...env, RUNNER_DEADLINE: deadlineMs(env) },
    collectCheckpoint: observer.collectCheckpoint,
    observeDeployment: observer.observeDeployment,
    readPredecessorEvidence: observer.readPredecessorEvidence,
    predecessorEvidencePin: loadPin(dir),
  });
}

if (require.main === module) {
  main().then(
    (code) => {
      process.exitCode = Number.isInteger(code) ? code : 1;
    },
    () => {
      process.exitCode = 1; // never print the error: it may carry paths or host details
    },
  );
}
module.exports = { main, deadlineMs, PREDECESSOR, OUTPUT };
