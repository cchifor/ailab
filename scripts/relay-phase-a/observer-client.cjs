"use strict";
// observer-client.cjs: the phase A Runner's side of the observer (plans/2026-11-27-relay-phase-a-observer-plan.md,
// sections 1-2). It implements the three integration callbacks of owner-phase-a-cli.cjs over the gate's Unix socket.
// It holds no credentials, opens no network, never evaluates `instructions` (only its SHA-256 travels) and returns the
// host's value unchanged: the driver's own validator decides. Any refusal, malformed reply, abort or deadline throws.
const crypto = require("node:crypto");
const net = require("node:net");

const MAX_REPLY = 512 * 1024;
const CODES = new Set(["refused", "failed", "deadline", "unsupported", "observer-down", "cancelled", "busy"]);

function sha256(text) {
  return crypto.createHash("sha256").update(String(text ?? ""), "utf8").digest("hex");
}

// JSON-safe copy without the AbortSignal (or any function/symbol); refuses cycles and non-finite numbers.
function plain(value, depth = 0) {
  if (depth > 32) throw new Error("Observer context too deep");
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("Observer context has a non-finite number");
    return value;
  }
  if (Array.isArray(value)) return value.map((v) => plain(v, depth + 1));
  if (typeof value === "object") {
    if (typeof AbortSignal !== "undefined" && value instanceof AbortSignal) return undefined;
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (k === "signal" || typeof v === "function" || typeof v === "symbol" || v === undefined) continue;
      const p = plain(v, depth + 1);
      if (p !== undefined) out[k] = p;
    }
    return out;
  }
  return undefined;
}

function createObserverClient(socketPath, { connect = net.createConnection } = {}) {
  if (typeof socketPath !== "string" || !socketPath.startsWith("/")) {
    throw new Error("Observer socket path required");
  }

  function call(envelope, signal, deadline) {
    return new Promise((resolve, reject) => {
      if (!Number.isFinite(deadline) || deadline <= Date.now()) {
        reject(new Error("Observer deadline elapsed"));
        return;
      }
      if (signal?.aborted) {
        reject(new Error("Observer call aborted"));
        return;
      }
      let done = false,
        size = 0;
      const chunks = [];
      const sock = connect(socketPath);
      const finish = (error, value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        signal?.removeEventListener?.("abort", onAbort);
        sock.destroy();
        if (error) reject(error);
        else resolve(value);
      };
      const onAbort = () => finish(new Error("Observer call aborted"));
      const timer = setTimeout(() => finish(new Error("Observer deadline elapsed")), deadline - Date.now());
      signal?.addEventListener?.("abort", onAbort, { once: true });
      sock.on("error", () => finish(new Error("Observer unavailable")));
      sock.on("connect", () => sock.write(JSON.stringify(envelope) + "\n"));
      sock.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_REPLY + 1) return finish(new Error("Observer reply too large"));
        chunks.push(chunk);
        const buf = Buffer.concat(chunks);
        const nl = buf.indexOf(10);
        if (nl === -1) return undefined;
        if (nl !== buf.length - 1) return finish(new Error("Observer reply framing"));
        let reply;
        try {
          reply = JSON.parse(buf.subarray(0, nl).toString("utf8"));
        } catch {
          return finish(new Error("Observer reply malformed"));
        }
        if (!reply || typeof reply !== "object" || reply.v !== 1 || typeof reply.ok !== "boolean") {
          return finish(new Error("Observer reply shape"));
        }
        if (reply.ok) {
          if (!Object.prototype.hasOwnProperty.call(reply, "value")) return finish(new Error("Observer reply shape"));
          return finish(null, reply.value);
        }
        // Only a fixed word ever travels; never host error text.
        return finish(new Error("Observer " + (CODES.has(reply.code) ? reply.code : "refused")));
      });
      sock.on("end", () => finish(new Error("Observer closed without a reply")));
    });
  }

  return {
    collectCheckpoint({ name, instructions, deadline, signal, context }) {
      const ctx = plain(context);
      if (!ctx || typeof ctx.kind !== "string") return Promise.reject(new Error("Checkpoint context required"));
      return call(
        {
          v: 1,
          call: "checkpoint",
          kind: ctx.kind,
          name: String(name ?? ""),
          instructionsSha256: sha256(instructions),
          deadline: Math.floor(deadline),
          context: ctx,
        },
        signal,
        deadline,
      );
    },
    observeDeployment(context) {
      const { deadline, signal } = context || {};
      return call({ v: 1, call: "deployment", deadline: Math.floor(deadline), context: plain(context) }, signal, deadline);
    },
    readPredecessorEvidence(context) {
      const { deadline, signal } = context || {};
      return call(
        { v: 1, call: "predecessor", deadline: Math.floor(deadline), context: plain(context) },
        signal,
        deadline,
      ).then((value) => {
        if (typeof value !== "string") throw new Error("Observer predecessor projection must be a string");
        return value;
      });
    },
  };
}

module.exports = { createObserverClient, plain, sha256 };
