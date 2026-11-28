"use strict";
const fs = require("node:fs");
const { readPredecessor: readPrivateUtf8 } = require("./owner-phase-a-cli.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const { parseRunnerDeadline } = require("./owner-phase-a-deadline.cjs");
const {
  createFinalizationAutomation,
} = require("./owner-finalization-automation.cjs");
const { qualified, diagnostic, failure } = require("./owner-finalization.cjs");
function need(v, m) {
  if (!v) throw failure("cli-input-invalid");
}
function output(fd, line) {
  const raw = Buffer.from(line);
  for (let n = 0; n < raw.length;) {
    const count = fs.writeSync(fd, raw, n, raw.length - n);
    need(count > 0, "Output failed");
    n += count;
  }
}
async function runFinalizationCli({
  argv = process.argv.slice(2),
  env = process.env,
  observer,
  writeLine = (line) => output(1, line),
  writeError = (line) => output(2, line),
  signals = process,
} = {}) {
  let host,
    stage = "qualification";
  const controller = new AbortController();
  const stop = () => {
    controller.abort();
    host?.stop();
  };
  try {
    need(process.platform === "linux", "Linux execution required");
    qualified(observer);
    stage = "arguments";
    need(
      argv.length === 12 &&
        [
          "--manifest",
          "--phase-a",
          "--phase-b",
          "--phase-b-inputs",
          "--phase-c",
          "--output",
        ].every((v, i) => argv[i * 2] === v),
      "Expected fixed manifest/A/B/B-inputs/C/output paths",
    );
    stage = "deadline";
    const deadline = parseRunnerDeadline(env.RUNNER_DEADLINE, 900000);
    stage = "private-inputs";
    const manifest = JSON.parse(readPrivateUtf8(argv[1]));
    need(
      manifest.cleanupDeadline === deadline,
      "Cleanup deadline differs from operator runner deadline",
    );
    const reports = {
      a: readPrivateUtf8(argv[3]),
      b: readPrivateUtf8(argv[5]),
      bInputs: readPrivateUtf8(argv[7]),
      c: readPrivateUtf8(argv[9]),
    };
    stage = "host-preparation";
    host = createFinalizationAutomation({
      directory: argv[11],
      reports,
      manifest,
      observer,
      authenticatedFetch: createSessionFetch(
        env.RELAY_SESSION_SOCKET,
        env.RELAY_SESSION_CONTROL_SOCKET,
      ),
      signal: controller.signal,
    });
    signals.on("SIGTERM", stop);
    signals.on("SIGINT", stop);
    stage = "finalization";
    const result = await host.run();
    writeLine(
      JSON.stringify({
        type: "final",
        ok: true,
        status: result.status,
        completed: false,
        reportPins: result.reportPins,
      }) + "\n",
    );
    return 0;
  } catch (error) {
    const snapshot = host?.snapshot(),
      reason = diagnostic(error);
    writeError(reason.reason + "\n");
    writeLine(
      JSON.stringify({
        type: "final",
        ok: false,
        status: snapshot ? "finalization-unresolved" : "preparation-refused",
        snapshotStatus: snapshot?.status ?? null,
        stage: snapshot?.stage ?? stage,
        reason,
        intents: snapshot?.intents?.length ?? 0,
        unconfirmedIntents:
          snapshot?.intents?.filter((i) => i.status !== "confirmed").length ??
          0,
        potentiallyDispatchedIntents:
          snapshot?.intents?.filter((i) => i.status !== "not-dispatched")
            .length ?? 0,
        completed: false,
      }) + "\n",
    );
    return 1;
  } finally {
    signals.removeListener("SIGTERM", stop);
    signals.removeListener("SIGINT", stop);
  }
}
if (require.main === module)
  runFinalizationCli().then(
    (code) => {
      process.exitCode = code;
    },
    () => {
      process.exitCode = 1;
    },
  );
module.exports = { runFinalizationCli };
