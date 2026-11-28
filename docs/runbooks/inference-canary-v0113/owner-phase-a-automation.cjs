"use strict";

// Transport and evidence collection are injected. This module grants no authority.
const fs = require("node:fs");
const path = require("node:path");
const { createHash, webcrypto } = require("node:crypto");
const driver = require("./owner-phase-a.js");

function need(value, message) {
  if (!value) throw new Error(message);
}
function privateDirectory(directory) {
  const stat = fs.lstatSync(directory);
  need(
    stat.isDirectory() && !stat.isSymbolicLink(),
    "Private directory required",
  );
  if (process.getuid) {
    need(
      stat.uid === process.getuid() && !(stat.mode & 0o077),
      "Private owned directory required",
    );
  }
}
function syncDirectory(directory) {
  // Linux is the execution target; Windows does not expose directory fsync.
  if (process.platform === "win32") return;
  const fd = fs.openSync(directory, fs.constants.O_RDONLY);
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}
function writeExclusive(file, content) {
  const fd = fs.openSync(
    file,
    fs.constants.O_WRONLY |
      fs.constants.O_CREAT |
      fs.constants.O_EXCL |
      (fs.constants.O_NOFOLLOW || 0),
    0o600,
  );
  try {
    fs.writeFileSync(fd, content);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  syncDirectory(path.dirname(file));
}

function createStore(directory, predecessorRaw, onSnapshot) {
  return createRunStore(
    directory,
    "predecessor",
    predecessorRaw,
    onSnapshot,
    () => predecessorRaw,
  );
}

// Only the named entries below choose the immutable input and runner.
// There is no caller-supplied runner, start method or input filename.
function createRunStore(directory, inputName, inputRaw, onSnapshot, load) {
  need(
    onSnapshot === undefined || typeof onSnapshot === "function",
    "Snapshot callback must be a function",
  );
  need(path.isAbsolute(directory), "Absolute store path required");
  const parent = path.dirname(directory);
  need(fs.realpathSync(parent) === parent, "Canonical store parent required");
  privateDirectory(parent);
  // Refuse any old/partial operation. Neither stale locks nor state are removed.
  fs.mkdirSync(directory, { mode: 0o700 });
  privateDirectory(directory);
  // Persist the new directory entry, not only files inside it, before dispatch.
  syncDirectory(parent);
  writeExclusive(
    path.join(directory, "run.lock"),
    JSON.stringify({ pid: process.pid }) + "\n",
  );
  writeExclusive(path.join(directory, `${inputName}.json`), inputRaw);
  writeExclusive(
    path.join(directory, `${inputName}.sha256`),
    createHash("sha256").update(inputRaw).digest("hex") + "\n",
  );
  let sequence = 0;
  return {
    load,
    save(state) {
      const content = JSON.stringify(state);
      need(
        Buffer.byteLength(content) <= 8 * 1024 * 1024,
        "State exceeds bound",
      );
      const next = sequence + 1;
      writeExclusive(
        path.join(directory, `state-${String(next).padStart(6, "0")}.json`),
        content + "\n",
      );
      sequence = next;
      if (onSnapshot) {
        const result = onSnapshot(JSON.parse(content));
        need(
          !result || typeof result.then !== "function",
          "Snapshot callback must be synchronous",
        );
      }
    },
  };
}

function abortable(promise, signal) {
  if (signal.aborted) return Promise.reject(new Error("Automation cancelled"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error("Automation cancelled"));
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve(promise)
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", abort));
  });
}

function createAutomation(options) {
  const { predecessorRaw } = options;
  need(
    typeof predecessorRaw === "string" &&
      Buffer.byteLength(predecessorRaw) <= 1024 * 1024,
    "Bounded original raw report required",
  );
  driver.stoppedPredecessor(predecessorRaw); // No partial successor or fresh-start reset.
  return createHost(options, {
    store: (directory, onSnapshot) =>
      createStore(directory, predecessorRaw, onSnapshot),
    runner: (env) => new driver.Runner(env),
    start: (runner) => runner.replaceStopped(),
  });
}

function createClaudePhaseBAutomation(options) {
  const b = require("./owner-phase-b.js");
  const phaseInputs = b.inputs(options.phaseInputs, Date.now());
  const attemptFence = {};
  for (const name of ["inspect", "arm", "advance", "close", "readOperation"]) {
    need(
      typeof options.attemptFence?.[name] === "function",
      "Fixed signed attempt-fence adapter required",
    );
    attemptFence[name] = options.attemptFence[name];
  }
  Object.freeze(attemptFence);
  need(
    typeof options.phaseAReportSha256 === "string" &&
      /^[a-f0-9]{64}$/.test(options.phaseAReportSha256),
    "Phase A report hash required; its report is never imported or reset",
  );
  for (const field of [
    "predecessorRaw",
    "readPredecessorEvidence",
    "predecessorEvidencePin",
    "renewalPrelude",
    "observeDeployment",
  ])
    need(
      !Object.hasOwn(options, field),
      "Phase A bootstrap is not a Phase B operation",
    );
  const maxDurationMs = options.maxDurationMs ?? 1800000;
  need(
    Number.isInteger(maxDurationMs) &&
      maxDurationMs > 0 &&
      maxDurationMs <= 1800000,
    "Phase B host deadline exceeds thirty minutes",
  );
  need(
    options.deadline === undefined || Number.isSafeInteger(options.deadline),
    "Phase B absolute deadline invalid",
  );
  const inputRaw = JSON.stringify({
    kind: "claude-phase-b-inputs",
    version: 1,
    phaseAReportSha256: options.phaseAReportSha256,
    inputs: phaseInputs,
  });
  return createHost(
    {
      ...options,
      maxDurationMs,
      deadline: Math.min(
        options.deadline ?? Infinity,
        phaseInputs.window.expiresAt,
        Date.now() + maxDurationMs,
      ),
    },
    {
      store: (directory, onSnapshot) =>
        createRunStore(
          directory,
          "phase-b-inputs",
          inputRaw,
          onSnapshot,
          () => null,
        ),
      runner: (env) => b.createClaudePhaseBRunner(env, phaseInputs),
      start: (runner) => runner.start(),
      attemptFence,
    },
  );
}

const CLAUDE_C_PORTS = Object.freeze({
  readiness: ["read"],
  process: ["start", "next", "stop"],
  settings: ["read", "replace"],
  fence: ["arm", "inspect", "advance", "close"],
});
const CLAUDE_C_CLEANUP = new Set([
  "process.stop",
  "settings.read",
  "settings.replace",
  "fence.inspect",
  "fence.close",
]);

function createClaudePhaseCAutomation(options) {
  const c = require("./owner-phase-c.cjs");
  // Keep the exact completed B report string inside the private immutable input.
  // Its schema and hash do not independently authenticate the earlier run.
  const raw = JSON.stringify(options.phaseInputs);
  need(
    typeof raw === "string" && Buffer.byteLength(raw) <= 2 * 1024 * 1024,
    "Bounded Phase C inputs required",
  );
  const phaseInputs = JSON.parse(raw);
  const checked = c.inputs(phaseInputs, Date.now());
  for (const field of [
    "predecessorRaw",
    "readPredecessorEvidence",
    "predecessorEvidencePin",
    "renewalPrelude",
    "observeDeployment",
  ])
    need(
      !Object.hasOwn(options, field),
      "Phase A bootstrap is not a Phase C operation",
    );
  const maxDurationMs = options.maxDurationMs ?? 900000;
  need(
    Number.isInteger(maxDurationMs) &&
      maxDurationMs > 0 &&
      maxDurationMs <= 900000,
    "Phase C host deadline exceeds fifteen minutes",
  );
  need(
    options.deadline === undefined || Number.isSafeInteger(options.deadline),
    "Phase C absolute deadline invalid",
  );
  const supplied = options.claudePorts;
  need(
    supplied &&
      typeof supplied === "object" &&
      Object.keys(supplied).length === Object.keys(CLAUDE_C_PORTS).length,
    "Fixed Claude service ports required",
  );
  const callbacks = {};
  for (const [group, names] of Object.entries(CLAUDE_C_PORTS)) {
    need(
      supplied[group] && Object.keys(supplied[group]).length === names.length,
      "Fixed Claude service methods required",
    );
    callbacks[group] = {};
    for (const name of names) {
      need(
        typeof supplied[group][name] === "function",
        "Claude service callback missing",
      );
      callbacks[group][name] = supplied[group][name];
    }
    Object.freeze(callbacks[group]);
  }
  Object.freeze(callbacks);
  return createHost(
    {
      ...options,
      maxDurationMs,
      deadline: Math.min(
        options.deadline ?? Infinity,
        checked.window.expiresAt,
        Date.now() + maxDurationMs,
      ),
    },
    {
      store: (directory, onSnapshot) =>
        createRunStore(
          directory,
          "phase-c-inputs",
          raw,
          onSnapshot,
          () => null,
        ),
      claudePorts: callbacks,
      runner: (env, ports) =>
        c.createClaudePhaseCRunner(env, JSON.parse(raw), ports),
      start: (runner) => runner.start(),
    },
  );
}

function createClaudeActiveDrainAutomation(options) {
  const drain = require("./owner-active-drain.cjs");
  // Preserve the exact predecessor report privately. A parsed report or its
  // hash does not independently authenticate the preceding execution.
  const raw = JSON.stringify(options.phaseInputs);
  need(
    typeof raw === "string" && Buffer.byteLength(raw) <= 2 * 1024 * 1024,
    "Bounded active-drain inputs required",
  );
  const now = Date.now();
  const checked = drain.inputs(JSON.parse(raw), now);
  for (const field of [
    "predecessorRaw",
    "readPredecessorEvidence",
    "predecessorEvidencePin",
    "renewalPrelude",
    "observeDeployment",
  ])
    need(
      !Object.hasOwn(options, field),
      "Phase A bootstrap is not an active-drain operation",
    );
  // No default window: the caller supplies an existing absolute expiry. A
  // smaller caller budget refuses rather than silently extending that budget.
  const deadline = options.deadline;
  need(
    Number.isSafeInteger(deadline) &&
      deadline > now &&
      deadline <= checked.window.expiresAt &&
      deadline - now <= 2700000,
    "Active-drain absolute deadline outside the supplied window or host bound",
  );
  const remaining = deadline - now;
  const callerBudget = options.maxDurationMs ?? remaining;
  need(
    Number.isSafeInteger(callerBudget) &&
      callerBudget > 0 &&
      callerBudget <= 2700000 &&
      remaining <= callerBudget,
    "Active-drain deadline exceeds caller budget",
  );
  const supplied = options.claudePorts;
  need(
    supplied &&
      typeof supplied === "object" &&
      Object.keys(supplied).length === Object.keys(CLAUDE_C_PORTS).length,
    "Fixed Claude service ports required",
  );
  const callbacks = {};
  for (const [group, names] of Object.entries(CLAUDE_C_PORTS)) {
    need(
      supplied[group] && Object.keys(supplied[group]).length === names.length,
      "Fixed Claude service methods required",
    );
    callbacks[group] = {};
    for (const name of names) {
      need(
        typeof supplied[group][name] === "function",
        "Claude service callback missing",
      );
      callbacks[group][name] = supplied[group][name];
    }
    Object.freeze(callbacks[group]);
  }
  Object.freeze(callbacks);
  return createHost(
    { ...options, deadline, maxDurationMs: remaining },
    {
      store: (directory, onSnapshot) =>
        createRunStore(
          directory,
          "active-drain-inputs",
          raw,
          onSnapshot,
          () => null,
        ),
      claudePorts: callbacks,
      runner: (env, ports) =>
        drain.createClaudeActiveDrainRunner(
          { ...env, executionDeadline: deadline },
          JSON.parse(raw),
          ports,
        ),
      start: (runner) => runner.start(),
    },
  );
}

function createHost(options, entry) {
  const {
    directory,
    authenticatedFetch,
    collectCheckpoint,
    signal,
    maxDurationMs = 2700000,
    cleanupTimeoutMs = 120000,
    onSnapshot,
    readPredecessorEvidence,
    predecessorEvidencePin,
    renewalPrelude,
    observeDeployment,
    deadline,
  } = options;
  need(
    typeof authenticatedFetch === "function" &&
      typeof collectCheckpoint === "function",
    "Authenticated transport and checkpoint collector required",
  );
  need(
    Number.isInteger(maxDurationMs) &&
      maxDurationMs > 0 &&
      maxDurationMs <= 2700000 &&
      Number.isInteger(cleanupTimeoutMs) &&
      cleanupTimeoutMs > 0 &&
      cleanupTimeoutMs <= 120000,
    "Bounded execution and cleanup deadlines required",
  );
  need(!signal?.aborted, "Automation already cancelled");
  need(
    renewalPrelude === undefined || typeof renewalPrelude === "function",
    "Renewal prelude must be a function",
  );
  need(
    observeDeployment === undefined || typeof observeDeployment === "function",
    "Deployment observer must be a function",
  );
  need(
    deadline === undefined ||
      (Number.isSafeInteger(deadline) &&
        deadline > Date.now() &&
        deadline - Date.now() <= 2700000),
    "Absolute execution deadline outside bound",
  );
  need(
    readPredecessorEvidence === undefined ||
      typeof readPredecessorEvidence === "function",
    "Predecessor evidence reader must be a function",
  );
  const store = entry.store(directory, onSnapshot);
  const active = new AbortController(),
    cleanup = new AbortController();
  const transports = new Set();
  const collectors = new Set();
  let checkpoint,
    runner,
    stopping,
    started = false,
    finished = false;
  let heartbeat, executionTimer, cleanupTimer, runPromise;
  const track = (set, task) => {
    set.add(task);
    task.then(
      () => set.delete(task),
      () => set.delete(task),
    );
    return task;
  };
  const failure = () => {
    if (!stopping && !finished) void stop();
  };
  const env = {
    origin: driver.ORIGIN,
    crypto: webcrypto,
    load: store.load,
    predecessorEvidencePin,
    ...(readPredecessorEvidence
      ? {
          async readPredecessorEvidence(context) {
            need(
              started && !finished && !stopping,
              "Predecessor evidence reader is closed",
            );
            const combined = AbortSignal.any([active.signal, context.signal]);
            combined.throwIfAborted();
            try {
              const value = await abortable(
                track(
                  collectors,
                  Promise.resolve().then(() => {
                    combined.throwIfAborted();
                    return readPredecessorEvidence(
                      Object.freeze({
                        ...context,
                        signal: combined,
                      }),
                    );
                  }),
                ),
                combined,
              );
              combined.throwIfAborted();
              return value;
            } catch (error) {
              failure();
              throw error;
            }
          },
        }
      : {}),
    save(state) {
      try {
        store.save(state);
        // Runner records its own failure before entering internal cleanup.
        // Start our cleanup deadline now, not only when that cleanup returns.
        if (state.status === "stopped") queueMicrotask(failure);
      } catch (error) {
        queueMicrotask(failure);
        throw error;
      }
    },
    async fetch(url, init) {
      need(started && !finished, "Automation transport is closed");
      need(
        typeof url === "string" &&
          url.startsWith("/api/") &&
          !url.includes("\\") &&
          !url.includes("#"),
        "Relative Relay API path required",
      );
      const phaseSignal = stopping ? cleanup.signal : active.signal;
      const combined = AbortSignal.any([
        phaseSignal,
        ...(init.signal ? [init.signal] : []),
      ]);
      combined.throwIfAborted();
      // Caller preserves ONE authenticated session. No auth headers or cookies are generated here.
      const request = track(
        transports,
        Promise.resolve().then(async () => {
          combined.throwIfAborted();
          const response = await authenticatedFetch(url, {
            ...init,
            signal: combined,
          });
          combined.throwIfAborted();
          // Include body consumption in the drain boundary, not just headers.
          const text = await response.text();
          combined.throwIfAborted();
          need(text.length <= 2 * 1024 * 1024, "Oversized API response");
          return {
            ok: response.ok,
            status: response.status,
            headers: response.headers,
            text: async () => text,
          };
        }),
      );
      try {
        const response = await abortable(request, combined);
        if (!response.ok) failure();
        return response;
      } catch (error) {
        failure();
        throw error;
      }
    },
    async ask(name, instructions, validate, timeout, context) {
      need(
        !stopping && !checkpoint && Number.isFinite(timeout) && timeout > 0,
        "Checkpoint unavailable",
      );
      const local = new AbortController();
      checkpoint = local;
      const timer = setTimeout(() => local.abort(), timeout);
      const combined = AbortSignal.any([local.signal, active.signal]);
      try {
        const value = await abortable(
          track(
            collectors,
            Promise.resolve().then(() =>
              collectCheckpoint({
                name,
                instructions,
                deadline: Date.now() + timeout,
                signal: combined,
                context,
              }),
            ),
          ),
          combined,
        );
        combined.throwIfAborted();
        return validate(value); // Never turn an absent/invalid observation into approval.
      } catch (error) {
        failure();
        throw error;
      } finally {
        clearTimeout(timer);
        local.abort();
        if (checkpoint === local) checkpoint = null;
      }
    },
    cancelCheckpoint() {
      checkpoint?.abort();
    },
  };
  let claudePorts;
  if (entry.claudePorts) {
    claudePorts = {
      signal: active.signal,
      cleanupSignal: cleanup.signal,
      monotonicNs: () => process.hrtime.bigint(),
    };
    for (const [group, names] of Object.entries(CLAUDE_C_PORTS)) {
      claudePorts[group] = {};
      for (const name of names) {
        const callback = entry.claudePorts[group][name];
        claudePorts[group][name] = async (context) => {
          need(started && !finished, "Claude service transport is closed");
          need(
            context && context.signal instanceof AbortSignal,
            "Claude service cancellation signal required",
          );
          const cleanupCall = context.signal === cleanup.signal;
          need(
            !cleanupCall || CLAUDE_C_CLEANUP.has(`${group}.${name}`),
            "Claude method is not available for cleanup",
          );
          const combined = cleanupCall
            ? cleanup.signal
            : AbortSignal.any([active.signal, context.signal]);
          combined.throwIfAborted();
          const operation = track(
            collectors,
            Promise.resolve().then(() => {
              combined.throwIfAborted();
              // The adapter resolves only after consuming and projecting its
              // bounded response, including any observer shutdown acknowledgement.
              return callback({ ...context, signal: combined });
            }),
          );
          return abortable(operation, combined);
        };
      }
      Object.freeze(claudePorts[group]);
    }
    Object.freeze(claudePorts);
  }
  if (entry.attemptFence) {
    // The named B entry has already fixed this absolute deadline. Store setup
    // and later phase transitions cannot extend its signing authority.
    env.executionDeadline = deadline;
    const wrapped = {};
    for (const [name, callback] of Object.entries(entry.attemptFence)) {
      wrapped[name] = async (context) => {
        need(started && !finished, "Attempt-fence adapter is closed");
        need(
          context &&
            context.signal instanceof AbortSignal &&
            typeof context.cleanup === "boolean",
          "Attempt-fence cancellation context required",
        );
        need(
          !context.cleanup ||
            ["inspect", "close", "readOperation"].includes(name),
          "Attempt-fence method is not available for cleanup",
        );
        const combined = AbortSignal.any([
          context.cleanup ? cleanup.signal : active.signal,
          context.signal,
        ]);
        combined.throwIfAborted();
        const task = track(
          collectors,
          Promise.resolve().then(() => {
            combined.throwIfAborted();
            // The adapter signs these exact bytes and resolves only after its
            // bounded response has been consumed and projected to plain JSON.
            return callback(Object.freeze({ ...context, signal: combined }));
          }),
        );
        const value = await abortable(task, combined);
        combined.throwIfAborted();
        return value;
      };
    }
    env.attemptFence = Object.freeze(wrapped);
  }
  runner = entry.runner(env, claudePorts);
  const stop = () => {
    if (finished) return Promise.resolve();
    if (stopping) return stopping;
    // Establish cleanup mode before cancelling an in-flight request.
    stopping = Promise.resolve();
    clearInterval(heartbeat);
    active.abort();
    checkpoint?.abort();
    clearTimeout(cleanupTimer);
    cleanupTimer = setTimeout(() => cleanup.abort(), cleanupTimeoutMs);
    stopping = abortable(
      (async () => {
        await runner.stop();
        await runPromise?.catch(() => {});
        // No Runner cleanup can introduce a new transport after its full run settles.
        await Promise.allSettled([...transports, ...collectors]);
      })(),
      cleanup.signal,
    )
      .catch(() => {
        store.save({
          automationHost: {
            state: "cleanup-unresolved",
            outstandingTransports: transports.size,
            outstandingCollectors: collectors.size,
          },
          runner: runner.state,
        });
        throw new Error("Cleanup unresolved");
      })
      .finally(() => {
        clearTimeout(cleanupTimer);
        cleanup.abort();
      });
    // A caller may be waiting on run(), not stop(). Keep rejection handled there.
    stopping.catch(() => {});
    return stopping;
  };
  return {
    stop,
    async run() {
      need(!started && !finished && !stopping, "Automation is single-use");
      started = true;
      const runAbsoluteDeadline = Math.min(
        deadline ?? Infinity,
        Date.now() + maxDurationMs,
      );
      const cancel = () => {
        void stop();
      };
      signal?.addEventListener("abort", cancel, { once: true });
      if (signal?.aborted) cancel();
      executionTimer = setTimeout(
        cancel,
        Math.max(0, runAbsoluteDeadline - Date.now()),
      );
      heartbeat = setInterval(() => {
        runner.renew().catch(() => {
          void stop();
        });
      }, 20000);
      try {
        need(
          runAbsoluteDeadline > Date.now(),
          "Execution deadline elapsed before dispatch",
        );
        runPromise = (async () => {
          active.signal.throwIfAborted();
          if (renewalPrelude) {
            await renewalPrelude({
              authenticatedFetch: (url, init) => {
                active.signal.throwIfAborted();
                return env.fetch(url, init);
              },
              persist: (state) =>
                store.save({ renewal: state, runner: runner.state }),
              deadline: runAbsoluteDeadline,
              signal: active.signal,
              ...(observeDeployment
                ? {
                    observeDeployment: async (context) => {
                      const combined = AbortSignal.any([
                        active.signal,
                        context.signal,
                      ]);
                      combined.throwIfAborted();
                      const value = await abortable(
                        track(
                          collectors,
                          Promise.resolve().then(() => {
                            combined.throwIfAborted();
                            return observeDeployment({
                              ...context,
                              signal: combined,
                            });
                          }),
                        ),
                        combined,
                      );
                      combined.throwIfAborted();
                      return value;
                    },
                  }
                : {}),
            });
          }
          active.signal.throwIfAborted();
          return entry.start(runner);
        })();
        await abortable(runPromise, active.signal);
        need(!stopping, "Automation stopped");
        clearInterval(heartbeat);
        clearTimeout(cleanupTimer);
        cleanupTimer = setTimeout(() => cleanup.abort(), cleanupTimeoutMs);
        await abortable(
          Promise.allSettled([...transports, ...collectors]),
          cleanup.signal,
        );
        active.signal.throwIfAborted();
        need(!stopping, "Automation stopped");
        clearTimeout(cleanupTimer);
        return JSON.parse(JSON.stringify(runner.state));
      } catch (error) {
        try {
          await stop();
        } catch {
          throw new Error(
            "Automation stopped; cleanup unresolved; preserve store and inspect",
          );
        }
        throw new Error(
          "Automation stopped; preserve store and inspect recorded receipts",
        );
      } finally {
        finished = true;
        clearInterval(heartbeat);
        clearTimeout(executionTimer);
        clearTimeout(cleanupTimer);
        signal?.removeEventListener("abort", cancel);
        active.abort();
        cleanup.abort();
      }
    },
  };
}

module.exports = {
  createAutomation,
  createClaudePhaseBAutomation,
  createClaudePhaseCAutomation,
  createClaudeActiveDrainAutomation,
  createStore,
  abortable,
};
