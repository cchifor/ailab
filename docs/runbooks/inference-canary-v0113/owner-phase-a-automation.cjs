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

function createStore(directory, predecessorRaw) {
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
  writeExclusive(path.join(directory, "predecessor.json"), predecessorRaw);
  writeExclusive(
    path.join(directory, "predecessor.sha256"),
    createHash("sha256").update(predecessorRaw).digest("hex") + "\n",
  );
  let sequence = 0;
  return {
    load: () => predecessorRaw,
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
  const {
    directory,
    predecessorRaw,
    authenticatedFetch,
    collectCheckpoint,
    signal,
    maxDurationMs = 2700000,
    cleanupTimeoutMs = 120000,
  } = options;
  need(
    typeof predecessorRaw === "string" &&
      Buffer.byteLength(predecessorRaw) <= 1024 * 1024,
    "Bounded original raw report required",
  );
  driver.stoppedPredecessor(predecessorRaw); // No partial successor or fresh-start reset.
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
  const store = createStore(directory, predecessorRaw);
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
    async ask(name, instructions, validate, timeout) {
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
  runner = new driver.Runner(env);
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
      const cancel = () => {
        void stop();
      };
      signal?.addEventListener("abort", cancel, { once: true });
      if (signal?.aborted) cancel();
      executionTimer = setTimeout(cancel, maxDurationMs);
      heartbeat = setInterval(() => {
        runner.renew().catch(() => {
          void stop();
        });
      }, 20000);
      try {
        runPromise = runner.replaceStopped();
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

module.exports = { createAutomation, createStore, abortable };
