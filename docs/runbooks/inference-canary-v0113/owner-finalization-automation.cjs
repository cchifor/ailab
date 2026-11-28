"use strict";
const fs = require("node:fs");
const path = require("node:path");
const core = require("./owner-finalization.cjs");
const { abortable } = require("./owner-phase-a-automation.cjs");

function need(v, message) {
  if (!v) throw Error(message);
}
function directory(dir) {
  const s = fs.lstatSync(dir);
  need(
    s.isDirectory() &&
      !s.isSymbolicLink() &&
      (!process.getuid || (s.uid === process.getuid() && !(s.mode & 0o077))),
    "Private owned directory required",
  );
}
function sync(dir) {
  if (process.platform === "win32") return;
  const fd = fs.openSync(dir, fs.constants.O_RDONLY);
  try {
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}
function write(file, raw) {
  const fd = fs.openSync(
    file,
    fs.constants.O_WRONLY |
      fs.constants.O_CREAT |
      fs.constants.O_EXCL |
      (fs.constants.O_NOFOLLOW || 0),
    0o600,
  );
  try {
    fs.writeFileSync(fd, raw);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  sync(path.dirname(file));
}
function createStore(destination, reports, manifest) {
  need(
    path.isAbsolute(destination) && path.normalize(destination) === destination,
    "Absolute normalized output required",
  );
  const parent = path.dirname(destination);
  need(fs.realpathSync(parent) === parent, "Canonical output parent required");
  directory(parent);
  fs.mkdirSync(destination, { mode: 0o700 });
  sync(parent);
  directory(destination);
  write(
    path.join(destination, "run.lock"),
    JSON.stringify({ pid: process.pid }) + "\n",
  );
  write(
    path.join(destination, "manifest.json"),
    JSON.stringify(manifest) + "\n",
  );
  for (const name of core.reportArtifactNames(manifest)) {
    write(path.join(destination, name + ".json"), reports[name]);
    write(
      path.join(destination, name + ".sha256"),
      core.sha(reports[name]) + "\n",
    );
  }
  let sequence = 0,
    sealed = false;
  return {
    seal() {
      sealed = true;
    },
    save(state) {
      need(!sealed, "Finalization evidence store sealed");
      const raw = JSON.stringify(state) + "\n";
      need(
        Buffer.byteLength(raw) <= 8 * 1024 * 1024,
        "Evidence state exceeds bound",
      );
      write(
        path.join(
          destination,
          `state-${String(sequence + 1).padStart(6, "0")}.json`,
        ),
        raw,
      );
      sequence++;
    },
  };
}

function createFinalizationAutomation(options) {
  need(
    typeof options.authenticatedFetch === "function",
    "Same-session transport required",
  );
  core.qualified(options.observer); // Refuse before creating the output directory.
  const reports = structuredClone(options.reports),
    manifest = structuredClone(options.manifest);
  const originalFetch = options.authenticatedFetch,
    originalObserve = options.observer.observe;
  const pending = new Set(),
    work = new AbortController(),
    hard = new AbortController();
  let store,
    runner,
    runPromise,
    stopped = false,
    used = false,
    timer,
    stopTimer,
    lastSnapshot;
  const track = (promise) => {
    const task = Promise.resolve(promise);
    pending.add(task);
    task.then(
      () => pending.delete(task),
      () => pending.delete(task),
    );
    return task;
  };
  runner = core.createFinalization({
    reports,
    manifest,
    observer: {
      qualificationSha256: options.observer.qualificationSha256,
      observe(context) {
        const signal = AbortSignal.any([
          context.signal,
          work.signal,
          hard.signal,
        ]);
        signal.throwIfAborted();
        return abortable(
          track(
            Promise.resolve().then(() => {
              signal.throwIfAborted();
              return originalObserve(Object.freeze({ ...context, signal }));
            }),
          ),
          signal,
        );
      },
    },
    authenticatedFetch(target, init) {
      const signal = AbortSignal.any([init.signal, hard.signal]);
      signal.throwIfAborted();
      return abortable(
        track(
          Promise.resolve().then(() => {
            signal.throwIfAborted();
            if (init.method === "POST" && stopped)
              throw core.failure("dispatch-blocked");
            return originalFetch(target, { ...init, signal });
          }),
        ),
        signal,
      ).then((response) => ({
        ok: response.ok,
        status: response.status,
        headers: response.headers,
        text: () =>
          abortable(
            track(Promise.resolve().then(() => response.text())),
            signal,
          ),
      }));
    },
    save(state) {
      store.save(state);
      lastSnapshot = structuredClone(state);
    },
  });
  store = createStore(options.directory, reports, manifest);
  const stop = () => {
    if (stopped) return;
    stopped = true;
    work.abort();
    runner.stop();
    stopTimer = setTimeout(
      () => {
        runner.abort();
        hard.abort();
      },
      Math.max(1, Math.min(120000, manifest.cleanupDeadline - Date.now())),
    );
  };
  options.signal?.addEventListener("abort", stop, { once: true });
  return {
    stop,
    async run() {
      need(!used, "Finalization host is single-use");
      used = true;
      if (options.signal?.aborted) stop();
      timer = setTimeout(
        stop,
        Math.max(1, manifest.cleanupDeadline - Date.now()),
      );
      runPromise = runner.run();
      try {
        const result = await abortable(runPromise, hard.signal);
        if (stopped) throw core.failure("host-stopped");
        if (pending.size !== 0) throw core.failure("callbacks-unresolved");
        return result;
      } catch (error) {
        stop();
        let drained = true;
        try {
          await abortable(
            Promise.allSettled([runPromise, ...pending]),
            hard.signal,
          );
        } catch {
          drained = false;
        }
        if (!drained || pending.size !== 0)
          throw core.failure("callbacks-unresolved");
        throw core.redact(error);
      } finally {
        clearTimeout(timer);
        clearTimeout(stopTimer);
        options.signal?.removeEventListener("abort", stop);
        work.abort();
        hard.abort();
        runner.abort();
        store.seal();
      }
    },
    snapshot() {
      return structuredClone(lastSnapshot);
    },
  };
}
module.exports = { createFinalizationAutomation };
