"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { createAutomation } = require("./owner-phase-a-automation.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const { parseRunnerDeadline } = require("./owner-phase-a-deadline.cjs");

function need(value, message) {
  if (!value) throw Error(message);
}

function readPredecessor(file) {
  need(
    typeof file === "string" && path.isAbsolute(file),
    "Absolute predecessor path required",
  );
  need(fs.realpathSync(file) === file, "Canonical predecessor path required");
  const fd = fs.openSync(
    file,
    fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
  );
  try {
    const before = fs.fstatSync(fd);
    need(
      before.isFile() &&
        before.nlink === 1 &&
        before.size > 0 &&
        before.size <= 1024 * 1024 &&
        before.uid === process.getuid() &&
        !(before.mode & 0o077),
      "Private bounded predecessor file required",
    );
    const buffer = Buffer.alloc(1024 * 1024 + 1);
    let size = 0,
      read;
    do {
      read = fs.readSync(fd, buffer, size, buffer.length - size, null);
      size += read;
    } while (read && size < buffer.length);
    const bytes = buffer.subarray(0, size);
    const after = fs.fstatSync(fd);
    need(
      before.size === after.size &&
        before.mtimeMs === after.mtimeMs &&
        before.ctimeMs === after.ctimeMs &&
        bytes.length === before.size,
      "Predecessor changed during read",
    );
    const raw = bytes.toString("utf8");
    need(
      Buffer.from(raw, "utf8").equals(bytes),
      "Predecessor must be exact UTF-8",
    );
    return raw;
  } finally {
    fs.closeSync(fd);
  }
}

function writeStdout(line) {
  const bytes = Buffer.from(line);
  let offset = 0;
  while (offset < bytes.length) {
    const written = fs.writeSync(1, bytes, offset, bytes.length - offset);
    need(written > 0, "Output write failed");
    offset += written;
  }
}

async function runCli({
  argv = process.argv.slice(2),
  env = process.env,
  collectCheckpoint,
  readPredecessorEvidence,
  predecessorEvidencePin,
  observeDeployment,
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
      argv.length === 4 &&
        argv[0] === "--predecessor" &&
        argv[2] === "--output",
      "Expected --predecessor ABS_PATH --output ABS_DIRECTORY",
    );
    const deadline = parseRunnerDeadline(env.RUNNER_DEADLINE, 2700000);
    // No observer wire protocol or automatic approval is supplied by this slice.
    need(
      typeof collectCheckpoint === "function",
      "Reviewed observer collector is not linked",
    );
    need(
      typeof observeDeployment === "function",
      "Reviewed deployment observer is not linked",
    );
    const predecessorRaw = readPredecessor(argv[1]);
    const authenticatedFetch = createSessionFetch(
      env.RELAY_SESSION_SOCKET,
      env.RELAY_SESSION_CONTROL_SOCKET,
    );
    need(deadline > Date.now(), "Runner deadline elapsed during preparation");
    host = createAutomation({
      directory: argv[3],
      predecessorRaw,
      authenticatedFetch,
      collectCheckpoint,
      readPredecessorEvidence,
      predecessorEvidencePin,
      observeDeployment,
      renewalPrelude: (context) =>
        require("./owner-phase-a-renewal.cjs")
          .createRenewalPrelude(context)
          .run(),
      deadline,
      signal: controller.signal,
      maxDurationMs: deadline - Date.now(),
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
    // Do not leak injected transport/collector errors, paths, headers or credentials.
    emit({
      type: "final",
      ok: false,
      reason: "Automation unavailable or stopped; preserve durable evidence",
      lastDurableSnapshot: lastSnapshot,
    });
    return 1;
  } finally {
    signals.removeListener("SIGTERM", terminate);
    signals.removeListener("SIGINT", terminate);
  }
}

if (require.main === module) {
  runCli().then(
    (code) => {
      process.exitCode = code;
    },
    () => {
      process.exitCode = 1;
    },
  );
}
module.exports = { runCli, readPredecessor };
