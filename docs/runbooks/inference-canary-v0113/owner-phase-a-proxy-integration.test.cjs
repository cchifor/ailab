"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { EventEmitter } = require("node:events");
const { runCli } = require("./owner-phase-a-cli.cjs");
const { createFixture } = require("./owner-phase-a-proxy-fixture.cjs");

test(
  "actual CLI and Runner complete phase A through pinned operator proxy with real heartbeat and logout",
  { timeout: 360000 },
  async (t) => {
    const directory = fs.mkdtempSync(
      path.join(os.tmpdir(), "relay-host-positive-"),
    );
    fs.chmodSync(directory, 0o700);
    const outputDirectory = path.join(directory, "durable");
    const f = await createFixture(directory, outputDirectory);
    const reportFile = path.join(directory, "original.json");
    fs.writeFileSync(reportFile, f.raw, { mode: 0o600 });
    const reportHash = createHash("sha256").update(f.raw).digest("hex");
    const observations = [];
    const records = [],
      signals = new EventEmitter();
    const stop = () => signals.emit("SIGTERM");
    t.signal.addEventListener("abort", stop, { once: true });
    const started = Date.now();
    let result;
    try {
      await f.initialRefusals();
      result = runCli({
        argv: ["--predecessor", reportFile, "--output", outputDirectory],
        env: {
          RELAY_SESSION_SOCKET: f.socketPath,
          RUNNER_DEADLINE: String(f.deadline),
        },
        signals,
        collectCheckpoint: async (options) => {
          const value = await f.collectCheckpoint(options);
          observations.push(
            JSON.parse(JSON.stringify({ context: options.context, value })),
          );
          return value;
        },
        observeDeployment: f.observeDeployment,
        predecessorEvidencePin: f.projectionPin,
        readPredecessorEvidence: f.readPredecessorEvidence,
        writeLine: (line) => {
          const record = JSON.parse(line);
          records.push(record);
          if (record.type === "snapshot" && record.state.runs?.length) {
            const phases = record.state.runs
              .map(
                (r) =>
                  r.provider +
                  (r.effort ? ":effort" : ":lifecycle") +
                  ":" +
                  (r.status || "running"),
              )
              .join(",");
            if (phases !== records.lastProgress) {
              records.lastProgress = phases;
              process.stdout.write("SYNTHETIC_PHASE " + phases + "\n");
            }
          }
        },
      });
      const code = await result;
      assert.deepEqual(f.failures, [], "synthetic server assertions");
      assert.equal(code, 0, JSON.stringify(records.at(-1)));
      const final = records.at(-1);
      assert.equal(final.type, "final");
      assert.equal(final.ok, true);
      const state = final.state;
      assert.equal(state.status, "phase-a-passed");
      assert.equal(state.phaseACompleted, true);
      assert.equal(
        state.completed,
        false,
        "phase A is not whole-plan completion",
      );
      assert.equal(state.predecessor.raw, f.raw);
      assert.equal(state.replacement.carriedPrompts, 1);
      assert.equal(state.replacement.carriedAttempts, 2);
      assert.equal(state.replacement.originalDeadline, f.prior.run.deadline);
      assert.deepEqual(
        state.runs.map((r) => [r.provider, r.effort, r.prompts]),
        [
          ["codex", false, 12],
          ["qwen", false, 11],
          ["codex", true, 4],
        ],
      );
      assert.equal(state.runs[0].carriedAttempts, 2);
      assert.equal(
        state.evidence.filter(
          (e) => e.kind === "operator-predecessor-projection",
        ).length,
        1,
      );
      const phase = f.phase();
      assert.equal(phase.launches.length, 5);
      assert.equal(phase.launches.filter((l) => l.resume_from).length, 2);
      assert(
        [...phase.sessions.values()].every(
          (s) => s.state === "closed" && s.lease === null,
        ),
      );
      assert.equal(
        phase.effects.size,
        2,
        "only lifecycle phases request markers; effort never does",
      );
      assert([...phase.effects.values()].every((count) => count === 1));
      assert.equal(phase.permissions.size, 4);
      assert.equal(
        [...phase.permissions.values()].filter(
          (q) => q.decision.optionId === "no",
        ).length,
        2,
      );
      assert.equal(
        [...phase.permissions.values()].filter(
          (q) => q.decision.optionId === "yes",
        ).length,
        2,
      );
      assert.equal(f.rotations.length, 2);
      assert.equal(new Set(f.rotations.map((r) => r.operationId)).size, 2);
      assert(f.heartbeats.length > 0);
      assert(
        f.heartbeats[0].at - started >= 19500,
        "real interval, not a fixture-invoked renew",
      );
      assert(f.heartbeats.every((h) => phase.sessions.has(h.sessionId)));
      assert(
        f.requests.every((r) => !r.pathname.endsWith("/revoke")),
        "final all-generation revocation is separately gated",
      );
      assert(
        !f.requests.some((r) =>
          r.pathname.includes(f.prior.launch.sessionId + "/operations/"),
        ),
      );
      assert(
        f.requests
          .filter((r) => r.pathname.includes(f.prior.launch.sessionId))
          .every((r) => r.method === "GET"),
      );
      assert.equal(
        fs.readFileSync(path.join(outputDirectory, "predecessor.json"), "utf8"),
        f.raw,
      );
      assert.equal(
        fs.readFileSync(
          path.join(outputDirectory, "predecessor.sha256"),
          "utf8",
        ),
        reportHash + "\n",
      );
      const snapshots = fs
        .readdirSync(outputDirectory)
        .filter((n) => /^state-\d+\.json$/.test(n))
        .sort();
      assert.equal(
        snapshots.length,
        records.filter((r) => r.type === "snapshot").length,
      );
      assert(snapshots.length > 50);
      for (const name of snapshots)
        assert.equal(
          fs.statSync(path.join(outputDirectory, name)).mode & 0o777,
          0o600,
        );
      await f.finalRefusals();
      assert.equal(signals.listenerCount("SIGTERM"), 0);
      assert.equal(signals.listenerCount("SIGINT"), 0);
      process.stdout.write(
        JSON.stringify({
          syntheticQualification: true,
          elapsedMs: Date.now() - started,
          launches: 5,
          resumes: 2,
          phases: 3,
          rotations: 2,
          heartbeatRequests: f.heartbeats.length,
          snapshots: snapshots.length,
          scope:
            "actual CLI/host/Runner/proxy main+Upstream; synthetic login, Unix upstream, deployment and observers; no real JIT/TLS/provider/holder proof",
        }) + "\n",
      );
    } finally {
      stop();
      await result?.catch(() => {});
      t.signal.removeEventListener("abort", stop);
      let closeError;
      try {
        await f.close();
      } catch (e) {
        closeError = e;
      }
      // Preserve failed fixtures when an evidence directory is supplied by the
      // isolated runner. Only test-created synthetic files are copied.
      if (process.env.SYNTHETIC_HOST_EVIDENCE) {
        const target = process.env.SYNTHETIC_HOST_EVIDENCE;
        fs.mkdirSync(target, { recursive: true, mode: 0o700 });
        fs.writeFileSync(
          path.join(target, "returned-observations.json"),
          JSON.stringify(observations, null, 2),
        );
        fs.writeFileSync(
          path.join(target, "http-trace.json"),
          JSON.stringify(f.requests, null, 2),
        );
        fs.writeFileSync(
          path.join(target, "records.jsonl"),
          records.map((r) => JSON.stringify(r)).join("\n") + "\n",
        );
        fs.writeFileSync(
          path.join(target, "fixture-failures.json"),
          JSON.stringify(f.failures),
        );
        if (fs.existsSync(outputDirectory))
          fs.cpSync(outputDirectory, path.join(target, "durable"), {
            recursive: true,
            errorOnExist: true,
            force: false,
          });
      }
      fs.rmSync(directory, { recursive: true });
      if (closeError) throw closeError;
    }
  },
);
