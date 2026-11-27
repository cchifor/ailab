"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

function need(value, message) {
  if (!value) throw Error(message);
}

function createSessionFetch(socketPath) {
  need(
    typeof socketPath === "string" && path.isAbsolute(socketPath),
    "Absolute session socket required",
  );
  const stat = fs.lstatSync(socketPath);
  need(
    stat.isSocket() && !stat.isSymbolicLink(),
    "Session endpoint must be a Unix socket",
  );
  return function sessionFetch(target, init = {}) {
    need(
      typeof target === "string" &&
        /^\/api\/[A-Za-z0-9/_-]+(?:\?[^#\s\\]*)?$/.test(target) &&
        !target.includes("//") &&
        !/%(?:2f|2e)/i.test(target),
      "Relative Relay API path required",
    );
    const method = init.method || "GET";
    need(
      ["GET", "POST", "DELETE"].includes(method),
      "Unsupported Relay method",
    );
    const supplied = new Headers(init.headers);
    for (const [key, value] of supplied) {
      need(
        key === "content-type" && value === "application/json",
        "Only JSON content header allowed",
      );
    }
    need(
      init.body === undefined || typeof init.body === "string",
      "String JSON body required",
    );
    const body = Buffer.from(init.body || "", "utf8");
    need(
      body.length <= 65536 && (method === "POST" || !body.length),
      "Invalid request body bound",
    );
    if (method === "POST") {
      let value;
      try {
        value = JSON.parse(body.toString("utf8"));
      } catch {
        throw Error("JSON object body required");
      }
      need(
        value && typeof value === "object" && !Array.isArray(value),
        "JSON object body required",
      );
    }
    init.signal?.throwIfAborted();
    return new Promise((resolve, reject) => {
      let response,
        settled = false;
      const done = (error, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        init.signal?.removeEventListener("abort", abort);
        if (error) {
          response?.destroy();
          request.destroy();
          reject(Error("Session socket request failed"));
        } else resolve(value);
      };
      const request = http.request(
        {
          socketPath,
          path: target,
          method,
          agent: false,
          headers: {
            "content-type": "application/json",
            "content-length": body.length,
          },
        },
        (incoming) => {
          response = incoming;
          let size = 0;
          const chunks = [];
          incoming.on("data", (chunk) => {
            size += chunk.length;
            if (size > 2 * 1024 * 1024) done(Error("Response bound"));
            else chunks.push(chunk);
          });
          incoming.on("aborted", () => done(Error("Incomplete response")));
          incoming.on("close", () => {
            if (!incoming.complete) done(Error("Incomplete response"));
          });
          incoming.on("error", () => done(Error("Response failed")));
          incoming.on("end", () => {
            const text = Buffer.concat(chunks).toString("utf8");
            const status = incoming.statusCode;
            // Never expose Set-Cookie or follow Location; the broker owns authentication.
            done(null, {
              ok: status >= 200 && status < 300,
              status,
              headers: new Headers({
                "content-type": incoming.headers["content-type"] || "",
              }),
              text: async () => text,
            });
          });
        },
      );
      const timer = setTimeout(() => done(Error("Request timeout")), 30000);
      const abort = () => done(Error("Request aborted"));
      request.on("error", () => done(Error("Socket failed")));
      init.signal?.addEventListener("abort", abort, { once: true });
      if (init.signal?.aborted) abort();
      else request.end(body);
    });
  };
}

module.exports = { createSessionFetch };
