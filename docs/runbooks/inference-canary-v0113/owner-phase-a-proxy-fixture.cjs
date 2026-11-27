"use strict";
// Reuse the qualified stateful upstream and collectors unchanged. This adapter
// supplies synthetic authentication/provenance omitted by the direct-host case.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const {
  createFixture: createDirectFixture,
} = require("./owner-phase-a-automation-fixture.cjs");
const { createSessionFetch } = require("./owner-phase-a-socket.cjs");
const ACTOR = "11111111-1111-4111-8111-111111111111";
const OLD_OPERATION = "22222222-2222-4222-8222-222222222222";
const FOREIGN = "33333333-3333-4333-8333-333333333333";
const COOKIE = "relay_session=synthetic-proxy-session-only";
const PROXY_SOCKET = "/run/relay-session/proxy.sock";
const CONTROL_SOCKET = "/run/relay-session/control.sock";

function request(socketPath, method, target, body) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? "" : JSON.stringify(body);
    const req = http.request(
      {
        socketPath,
        method,
        path: target,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(payload),
        },
      },
      (res) => {
        let raw = "";
        res.on("data", (b) => {
          raw += b;
          if (raw.length > 2 * 1024 * 1024)
            req.destroy(Error("fixture response bound"));
        });
        res.on("error", reject);
        res.on("end", () =>
          resolve({ status: res.statusCode, value: JSON.parse(raw) }),
        );
      },
    );
    req.on("error", reject);
    req.setTimeout(30000, () => req.destroy(Error("fixture request timeout")));
    req.end(payload);
  });
}

async function createFixture(directory, outputDirectory) {
  assert.equal(
    fs.existsSync(PROXY_SOCKET),
    false,
    "requires fresh private /run/relay-session tmpfs",
  );
  assert.equal(fs.statSync(path.dirname(PROXY_SOCKET)).mode & 0o777, 0o700);
  assert.equal(fs.existsSync(CONTROL_SOCKET), false);
  const f = await createDirectFixture(directory, outputDirectory);
  const upstreamPath = path.join(directory, "proxy-upstream.sock");
  const trace = [],
    metadata = new Map(),
    failures = [];
  let loggedOut = false,
    logoutCount = 0,
    meAfterLogout = 0;
  const historical = [
    {
      id: FOREIGN,
      actor_id: ACTOR,
      operation_id: OLD_OPERATION,
      created_at: "2026-01-01T00:00:00.000Z",
      session_id: FOREIGN,
    },
    {
      id: "44444444-4444-4444-8444-444444444444",
      actor_id: FOREIGN,
      operation_id: "55555555-5555-4555-8555-555555555555",
      created_at: "2026-01-01T00:00:00.000Z",
      session_id: FOREIGN,
    },
  ];
  function enrich(row) {
    const meta = metadata.get(row.operation_id);
    assert(meta, "launch must originate in an actual forwarded POST");
    return {
      ...row,
      actor_id: ACTOR,
      created_at: meta.createdAt,
      resume_generation: meta.resumeGeneration,
    };
  }
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (b) => {
      raw += b;
      if (raw.length > 65536) req.destroy();
    });
    req.on("end", async () => {
      try {
        assert.equal(req.headers.cookie, COOKIE);
        assert.equal(req.headers.authorization, undefined);
        assert.equal(req.headers.host, "relay.chifor.me");
        if (req.method !== "GET")
          assert.equal(req.headers.origin, "https://relay.chifor.me");
        const body = raw ? JSON.parse(raw) : undefined;
        trace.push({ method: req.method, path: req.url, body }); // no cookie/header recording
        let result;
        if (req.url === "/api/auth/logout") {
          assert.equal(req.method, "POST");
          assert.deepEqual(body, {});
          assert.equal(loggedOut, false);
          loggedOut = true;
          logoutCount++;
          result = { status: 200, value: { ok: true } };
        } else if (req.url === "/api/auth/me") {
          assert(loggedOut);
          assert.equal(req.method, "GET");
          meAfterLogout++;
          result = { status: 401, value: { error: "synthetic logged out" } };
        } else {
          assert.equal(loggedOut, false);
          if (
            req.method === "POST" &&
            (req.url === "/api/agent-launches" || req.url.endsWith("/resume"))
          ) {
            assert(!metadata.has(body.operationId));
            metadata.set(body.operationId, {
              createdAt: new Date().toISOString(),
              resumeGeneration: req.url.endsWith("/resume")
                ? body.generation
                : null,
            });
          }
          if (
            req.method === "GET" &&
            req.url === "/api/agent-launches" &&
            !f.phase()?.launches.length
          ) {
            result = { status: 200, value: historical };
          } else {
            result = await request(f.socketPath, req.method, req.url, body);
            if (req.url === "/api/agent-launches" && req.method === "GET")
              result.value = [...result.value.map(enrich), ...historical];
            else if (
              req.method === "POST" &&
              (req.url === "/api/agent-launches" || req.url.endsWith("/resume"))
            )
              result.value = enrich(result.value);
          }
        }
        res.writeHead(result.status, {
          "Content-Type": "application/json",
          Connection: "close",
        });
        res.end(JSON.stringify(result.value));
      } catch (e) {
        failures.push(e.stack);
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "synthetic upstream assertion" }));
      }
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(upstreamPath, resolve);
  });
  fs.chmodSync(upstreamPath, 0o600);
  const deadline = Date.now() + 340000;
  const child = spawn(
    "python3",
    [path.join(__dirname, "owner-phase-a-proxy-bootstrap.py"), upstreamPath],
    {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    },
  );
  let stdout = "",
    stderr = "";
  child.stdout.on("data", (b) => {
    stdout += b;
  });
  child.stderr.on("data", (b) => {
    stderr += b;
  });
  const exited = once(child, "exit");
  child.stdin.end(
    "synthetic-no-credential\n" +
      JSON.stringify({
        username: "synthetic-proxy-operator",
        runner_deadline: deadline / 1000,
        cleanup_deadline: deadline / 1000 + 120,
      }) +
      "\n",
  );
  let ready;
  try {
    const readyEnd = Date.now() + 10000;
    while (
      !stdout.includes("READY ") &&
      child.exitCode === null &&
      Date.now() < readyEnd
    )
      await new Promise((r) => setTimeout(r, 20));
    assert(stdout.startsWith("READY "), "proxy main startup: " + stderr);
    ready = JSON.parse(stdout.trim().slice(6));
    assert.equal(ready.actor, ACTOR);
    assert.equal(ready.socket, PROXY_SOCKET);
    assert.equal(ready.controlSocket, CONTROL_SOCKET);
    assert.equal(trace[0].path, "/api/agent-launches");
    assert.equal(fs.statSync(PROXY_SOCKET).mode & 0o777, 0o600);
    assert.equal(fs.statSync(CONTROL_SOCKET).mode & 0o777, 0o600);
  } catch (error) {
    child.kill("SIGTERM");
    const forced = setTimeout(() => child.kill("SIGKILL"), 15000);
    try {
      await exited;
    } catch {
    } finally {
      clearTimeout(forced);
    }
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
    await f.close();
    if (process.env.SYNTHETIC_HOST_EVIDENCE) {
      fs.mkdirSync(process.env.SYNTHETIC_HOST_EVIDENCE, {
        recursive: true,
        mode: 0o700,
      });
      fs.writeFileSync(
        path.join(
          process.env.SYNTHETIC_HOST_EVIDENCE,
          "proxy-startup-failure.json",
        ),
        JSON.stringify(
          { stdout, stderr, trace, failures, reason: error.message },
          null,
          2,
        ),
        { mode: 0o600 },
      );
    }
    throw error;
  }
  const fetch = createSessionFetch(PROXY_SOCKET, CONTROL_SOCKET);
  const refusals = [];
  async function refused(method, target, body) {
    const before = trace.length;
    const response = await fetch(target, {
      method,
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    assert.equal(response.status, 403, await response.text());
    assert.equal(trace.length, before, "refusal must not dispatch upstream");
    refusals.push({
      method,
      target,
      status: response.status,
      upstreamCalls: 0,
    });
  }
  async function initialRefusals() {
    await refused("GET", "/api/agent-sessions/" + FOREIGN);
    await refused("GET", "/api/router-credentials/keys/" + FOREIGN);
    const launch = {
      operationId: OLD_OPERATION,
      profileId: "1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
      revision: 1,
      cwd: "/home/c4/.local/state/relay-holder/workspaces/1d9a7d6e-4b26-49fd-9053-7b0a22e1baae",
    };
    await refused("POST", "/api/agent-launches", launch);
    await refused("POST", "/api/agent-launches", {
      ...launch,
      operationId: "66666666-6666-4666-8666-666666666666",
      profileId: FOREIGN,
    });
  }
  async function finalRefusals() {
    assert.deepEqual(failures, []);
    assert.equal(metadata.size, 5);
    const rotation = f.rotations[0];
    await refused(
      "POST",
      "/api/router-credentials/a0f57684-747f-4c3e-8d90-e684b7262590/rotate",
      { operationId: "77777777-7777-4777-8777-777777777777", revision: 1 },
    );
    assert(rotation);
    const leases = trace.filter(
      (r) =>
        r.method === "POST" && r.path.endsWith("/control") && r.body.leaseId,
    );
    assert(leases.length > 0);
    const other = f
      .phase()
      .launches.find((l) => !leases[0].path.includes(l.session_id));
    await refused(
      "POST",
      `/api/agent-sessions/${other.session_id}/control`,
      leases[0].body,
    );
  }
  return {
    ...f,
    socketPath: PROXY_SOCKET,
    controlSocketPath: CONTROL_SOCKET,
    deadline,
    initialRefusals,
    finalRefusals,
    close: async () => {
      child.kill("SIGTERM");
      let timer;
      try {
        const [code, signal] = await Promise.race([
          exited,
          new Promise((_, reject) => {
            timer = setTimeout(() => {
              child.kill("SIGKILL");
              reject(Error("proxy shutdown exceeded 15s"));
            }, 15000);
          }),
        ]);
        assert.equal(signal, null);
        assert.equal(code, 0, stderr);
        assert.equal(logoutCount, 1);
        assert.equal(meAfterLogout, 1);
        assert.equal(fs.existsSync(PROXY_SOCKET), false);
        assert.equal(fs.existsSync(CONTROL_SOCKET), false);
        assert.deepEqual(failures, []);
      } finally {
        clearTimeout(timer);
        if (process.env.SYNTHETIC_HOST_EVIDENCE) {
          const dir = process.env.SYNTHETIC_HOST_EVIDENCE;
          fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
          fs.writeFileSync(
            path.join(dir, "proxy-evidence.json"),
            JSON.stringify(
              {
                ready,
                trace,
                refusals,
                logoutCount,
                meAfterLogout,
                failures,
                stdout,
                stderr,
              },
              null,
              2,
            ),
            { mode: 0o600 },
          );
        }
        server.closeAllConnections();
        await new Promise((r) => server.close(r));
        await f.close();
      }
    },
  };
}
module.exports = { createFixture };
