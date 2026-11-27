"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createHash } = require("node:crypto");
const { EventEmitter } = require("node:events");
const a = require("./owner-phase-a.js");
const b = require("./owner-phase-b.js");
const { runPhaseBCli } = require("./owner-phase-b-cli.cjs");
const { createFixture } = require("./owner-phase-b-automation-fixture.cjs");
const hash = (value) => createHash("sha256").update(value).digest("hex");

test(
  "actual named B CLI and host complete Claude lifecycle and effort over Unix HTTP with real heartbeat",
  { timeout: 300000 },
  async (t) => {
    const directory = fs.mkdtempSync(
      path.join(os.tmpdir(), "relay-b-positive-"),
    );
    fs.chmodSync(directory, 0o700);
    const output = path.join(directory, "durable");
    const f = await createFixture(directory, output);
    const inputsPath = path.join(directory, "inputs.json");
    const aReportPath = path.join(directory, "phase-a-report.json");
    const inputsRaw = JSON.stringify(f.inputs, null, 2) + "\n";
    fs.writeFileSync(inputsPath, inputsRaw, { mode: 0o600 });
    fs.writeFileSync(aReportPath, f.phaseARaw, { mode: 0o600 });
    const aHash = hash(f.phaseARaw),
      originalProfiles = JSON.stringify(a.PROFILES);
    const observations = [];
    const records = [],
      signals = new EventEmitter();
    const stop = () => signals.emit("SIGTERM");
    t.signal.addEventListener("abort", stop, { once: true });
    const started = Date.now();
    let result;
    try {
      result = runPhaseBCli({
        argv: [
          "--inputs",
          inputsPath,
          "--phase-a-report",
          aReportPath,
          "--output",
          output,
        ],
        env: {
          RELAY_SESSION_SOCKET: f.socketPath,
          RUNNER_DEADLINE: String(started + 280000),
        },
        signals,
        collectCheckpoint: async (options) => {
          const value = await f.collectCheckpoint(options);
          // Keep the exact returned observation even if the real validator refuses it.
          observations.push(
            JSON.parse(JSON.stringify({ context: options.context, value })),
          );
          return value;
        },
        writeLine: (line) => {
          const record = JSON.parse(line);
          records.push(record);
          if (record.type === "snapshot" && record.state.runs?.length) {
            const phase = record.state.runs
              .map(
                (r) =>
                  `${r.provider}:${r.effort ? "effort" : "lifecycle"}:${r.status || "running"}`,
              )
              .join(",");
            if (phase !== records.lastProgress) {
              records.lastProgress = phase;
              process.stdout.write("SYNTHETIC_B_PHASE " + phase + "\n");
            }
          }
        },
      });
      const code = await result;
      assert.deepEqual(f.failures, []);
      assert.equal(code, 0, JSON.stringify(records.at(-1)));
      const final = records.at(-1),
        state = final.state;
      assert.equal(final.type, "final");
      assert.equal(final.ok, true);
      assert.equal(state.kind, "claude-phase-b");
      assert.equal(state.status, "phase-b-passed");
      assert.equal(state.phaseBCompleted, true);
      assert.equal(state.completed, false);
      assert.equal(state.phaseACompleted, undefined);
      assert.equal(state.successorCredentials, undefined);
      assert.equal(JSON.stringify(a.PROFILES), originalProfiles);
      assert.deepEqual(
        state.runs.map((r) => [r.provider, r.effort, r.prompts]),
        [
          ["claude", false, 11],
          ["claude", true, 4],
        ],
      );
      assert(
        state.runs.every(
          (r) => r.status === "passed-with-live-wire-unobserved",
        ),
      );
      for (const run of state.runs) {
        assert.equal(run.reservations.length, run.prompts);
        assert(
          run.reservations.every(
            (r) => r.state === "settled" && r.attempts <= r.maximum,
          ),
        );
        assert(
          run.lastSnapshot.totalAttempts - run.firstSnapshot.totalAttempts <=
            (run.effort ? 6 : 20),
        );
      }
      assert.equal(f.launches.length, 3);
      const launches = [...f.launches].reverse();
      assert.equal(launches.filter((l) => l.resume_from).length, 1);
      assert.equal(launches[1].resume_from, launches[0].session_id);
      assert.equal(
        launches[1].receipt.result.nativeSessionId,
        launches[0].receipt.result.nativeSessionId,
      );
      assert.notEqual(
        launches[2].receipt.result.nativeSessionId,
        launches[1].receipt.result.nativeSessionId,
      );
      assert(
        [...f.sessions.values()].every(
          (s) => s.state === "closed" && s.lease === null,
        ),
      );
      assert.equal(f.effects.size, 1);
      assert.equal(f.effects.get(f.inputs.lifecycleRunId), 1);
      assert.equal(f.effects.has(f.inputs.effortRunId), false);
      assert.equal(f.permissions.size, 2);
      assert.equal(
        [...f.permissions.values()].filter((p) => p.decision.optionId === "no")
          .length,
        1,
      );
      assert.equal(
        [...f.permissions.values()].filter((p) => p.decision.optionId === "yes")
          .length,
        1,
      );
      const prompts = f.calls
        .filter((c) => c.body?.kind === "prompt")
        .map((c) => c.body.text);
      assert.equal(prompts.length, 15);
      assert.equal(prompts[0], b.TEXT.ready);
      assert.equal(prompts[9], "/relay-reconcile confirmed");
      assert.deepEqual(prompts.slice(-4), [
        b.TEXT.low,
        b.TEXT.highProbe,
        "/relay-reconcile confirmed",
        b.TEXT.high,
      ]);
      assert(f.calls.every((c) => !/\/(rotate|revoke)$/.test(c.path)));
      assert(
        f.calls
          .filter((c) => c.path.startsWith("/api/router-credentials"))
          .every((c) => c.method === "GET"),
      );
      assert(
        f.calls
          .filter((c) => c.path.endsWith("/resume"))
          .every((c) => c.body.acknowledgeUnknownToolEffects === undefined),
      );
      const branchEvidence = state.evidence.filter(
        (e) => e.kind === "claude-branch-observation",
      );
      assert(
        branchEvidence.length >= 5,
        "recovery and effort retirement require scoped branch observations",
      );
      assert(
        f.checkpoints.some(
          (c) => c.kind === "claude-branch" && c.retiredBranch,
        ),
      );
      assert.equal(
        f.checkpoints.filter((c) => c.kind === "claude-readiness").length,
        1,
      );
      const refusal = [...f.commands.values()].find((c) => c.refusal);
      assert(refusal, "exact configuration fence must be exercised");
      assert.equal(refusal.response, a.CONFIGURATION_CHANGED_NOTICE);
      assert(
        state.runs[1].reservations.some(
          (r) => r.maximum === 0 && r.attempts === 0,
        ),
      );
      assert(f.heartbeats.length > 0);
      assert(
        f.heartbeats[0].at - started >= 19500,
        "actual 20s timer, no manual renew",
      );
      assert(f.heartbeats.every((h) => f.sessions.has(h.sessionId)));
      assert.equal(fs.readFileSync(aReportPath, "utf8"), f.phaseARaw);
      assert.equal(fs.readFileSync(inputsPath, "utf8"), inputsRaw);
      const savedInput = fs.readFileSync(
        path.join(output, "phase-b-inputs.json"),
      );
      assert.equal(JSON.parse(savedInput).phaseAReportSha256, aHash);
      assert.deepEqual(JSON.parse(savedInput).inputs, f.inputs);
      assert.equal(
        fs.readFileSync(path.join(output, "phase-b-inputs.sha256"), "utf8"),
        hash(savedInput) + "\n",
      );
      const snapshots = fs
        .readdirSync(output)
        .filter((n) => /^state-\d+\.json$/.test(n))
        .sort();
      const emitted = records.filter((r) => r.type === "snapshot");
      assert.equal(snapshots.length, emitted.length);
      assert(snapshots.length > 50);
      for (let i = 0; i < snapshots.length; i++) {
        const file = path.join(output, snapshots[i]);
        assert.equal(fs.statSync(file).mode & 0o777, 0o600);
        assert.deepEqual(
          JSON.parse(fs.readFileSync(file, "utf8")),
          emitted[i].state,
        );
      }
      assert.equal(signals.listenerCount("SIGTERM"), 0);
      assert.equal(signals.listenerCount("SIGINT"), 0);
      process.stdout.write(
        JSON.stringify({
          syntheticQualification: true,
          elapsedMs: Date.now() - started,
          launches: 3,
          resumes: 1,
          phases: 2,
          prompts: [11, 4],
          markerEffects: 1,
          heartbeatRequests: f.heartbeats.length,
          snapshots: snapshots.length,
          scope:
            "actual B CLI/host/ClaudeRunner; synthetic HTTP/readiness/branch/marker/trajectory and completed-A structure; no JIT/provider/holder/live proof; reservations are not a hard upstream fence",
        }) + "\n",
      );
    } finally {
      stop();
      await result?.catch(() => {});
      t.signal.removeEventListener("abort", stop);
      await f.close();
      if (process.env.SYNTHETIC_B_HOST_EVIDENCE) {
        const target = process.env.SYNTHETIC_B_HOST_EVIDENCE;
        fs.mkdirSync(target, { recursive: true, mode: 0o700 });
        for (const [name, value] of Object.entries({
          "http-trace.json": f.calls,
          "returned-observations.json": observations,
          "checkpoints.json": f.checkpoints,
          "fixture-failures.json": f.failures,
          "heartbeat.json": f.heartbeats,
        }))
          fs.writeFileSync(
            path.join(target, name),
            JSON.stringify(value, null, 2),
          );
        fs.writeFileSync(
          path.join(target, "records.jsonl"),
          records.map((r) => JSON.stringify(r)).join("\n") + "\n",
        );
        fs.copyFileSync(
          aReportPath,
          path.join(target, "synthetic-phase-a-report.json"),
        );
        fs.copyFileSync(inputsPath, path.join(target, "inputs.json"));
        if (fs.existsSync(output))
          fs.cpSync(output, path.join(target, "durable"), {
            recursive: true,
            errorOnExist: true,
            force: false,
          });
      }
      fs.rmSync(directory, { recursive: true });
    }
  },
);
