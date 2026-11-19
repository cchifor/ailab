#!/usr/bin/env python3
"""Compare reviewed File B with a fresh, read-only ledger readback; optionally
qualify it with the Router's authenticated worker-budget-preflight.

Python stdlib; psql is needed only for direct ledger connections.

Default (zero-write): no Router request is made. Verification exits 2 for
acknowledged ledger-only evidence (--skip-router-preflight-unavailable; never
signable) and 1 for any failure, including the unacknowledged Router half.

--router-preflight (opt-in; owner decision 2026-11-19): exactly ONE Router
request, signed as Relay's reconciler with a key read from an inherited pipe.
The Router durably writes that delegation's nonce to its replay set for the
issuer/tenant scope (one of 12 readiness slots until it expires). Never
retried. Exit 0 only when the ledger passes AND the Router proof is valid
(signingReady: true); any failure exits 1.

Utilities, which verify nothing and exit 0: --print-ledger-sql,
--print-router-ledger-sql, --print-router-store-script. --router-slots reports
the scope's readiness slots from a Router store readback (zero-write; exit 0
for a valid readback, 1 otherwise).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import time
import urllib.parse
import uuid

MAX_SAFE = 9007199254740991
MAX_BYTES = 16 * 1024 * 1024
FRESH_MS = 15000
DIGEST = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

# Relay d1c77f876606976362d70f42973033d31ac913c6:
# deploy/worker-budget-ledger/001-authority.sql:32-36 (complete table, no filter).
# 004-preflight-errors.sql:9 calls gate, 001-authority.sql:60-66 takes FOR SHARE:
# no persisted writes, but that row lock cannot run in a READ ONLY transaction.
# We call neither function; no authority/witness/allocation side effects.
LEDGER_SQL = """BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';
SET LOCAL search_path = pg_catalog;
SELECT jsonb_build_object(
  'version', 1,
  'readOnly', current_setting('transaction_read_only') = 'on',
  'observedAt', floor(extract(epoch FROM statement_timestamp()) * 1000)::bigint,
  'qualifications', COALESCE(jsonb_agg(jsonb_build_object(
    'deployment_digest', deployment_digest, 'digest', digest, 'policy', policy)
    ORDER BY deployment_digest, digest), '[]'::jsonb))
FROM worker_budget.qualifications;
ROLLBACK;
"""

# --router-preflight only: the same snapshot plus the authority row, whose
# recovery_epoch the Router's runtime snapshot carries (001-authority.sql:14-19;
# llm-router 5a5181b7 worker-budget-authority/index.ts:60-63, runtime.ts:104-108)
# and which its preflight requires (api-keys/meter.ts:121-125). A plain SELECT
# in the same READ ONLY snapshot: no lock, no function; witness_digest unread.
ROUTER_LEDGER_SQL = """BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';
SET LOCAL search_path = pg_catalog;
SELECT jsonb_build_object(
  'version', 1,
  'readOnly', current_setting('transaction_read_only') = 'on',
  'observedAt', floor(extract(epoch FROM statement_timestamp()) * 1000)::bigint,
  'qualifications', COALESCE(jsonb_agg(jsonb_build_object(
    'deployment_digest', deployment_digest, 'digest', digest, 'policy', policy)
    ORDER BY deployment_digest, digest), '[]'::jsonb),
  'authority', (SELECT COALESCE(jsonb_agg(jsonb_build_object(
    'instance_id', instance_id, 'recovery_epoch', recovery_epoch, 'ready', ready)), '[]'::jsonb)
    FROM worker_budget.authority))
FROM worker_budget.qualifications;
ROLLBACK;
"""

# The Router's replay sets live in its SQLite store (ailab router.yaml:156-157,
# ROUTER_DATABASE=/data/router.sqlite; llm-router 5a5181b7
# adapters/sqlite/sqlite-worker.mjs:38 `kv(ns, key, value, updated_at)`). The
# relay-keys plugin keeps one row per issuer/tenant scope in namespace
# relay-key-nonces (relay-keys/service.ts:11,39-49): a JSON array of
# {nonce, exp, readiness?}. This fixed program, piped into `node -` in the
# Router container, opens the file read-only (never creating it) with
# query_only, reads every scope of that one namespace, and prints only each
# entry's exp, readiness tag and the SHA-256 of its nonce. No interpolation.
ROUTER_STORE_SCRIPT = """'use strict';
// relay-worker-budget-file-b-check: fixed read-only Router replay-set readback, version 1.
const { DatabaseSync } = require('node:sqlite');
const { createHash } = require('node:crypto');
const sha = (value) => createHash('sha256').update(value).digest('hex');
let output;
try {
  const db = new DatabaseSync('/data/router.sqlite', { readOnly: true });
  try {
    db.exec('PRAGMA query_only = 1');
    db.exec('BEGIN');
    const readOnly = db.prepare('PRAGMA query_only').get().query_only === 1;
    const rows = db.prepare("SELECT key, value FROM kv WHERE ns = 'relay-key-nonces' ORDER BY key").all();
    const observedAt = Date.now();
    db.exec('ROLLBACK');
    const scopes = rows.map((row) => ({ key: row.key, entries: JSON.parse(row.value).map((n) => ({
      exp: Number.isSafeInteger(n && n.exp) ? n.exp : null,
      readiness: !!n && n.readiness === true,
      nonceSha256: n && typeof n.nonce === 'string' ? sha(n.nonce) : null })) }));
    output = JSON.stringify({ version: 1, readOnly, observedAt, namespace: 'relay-key-nonces', scopes });
  } finally { db.close(); }
} catch {
  process.stderr.write('router store readback failed; diagnostics suppressed\\n');
  process.exit(1);
}
process.stdout.write(output + '\\n');
"""

# llm-router 5a5181b7:packages/plugins/worker-budget-authority/runtime.ts:9-30.
HASH_FIELDS = {"digest", "routerReleaseDigest", "adapterDigest", "evidenceDigest"}
INTEGER_FIELDS = {"providerGeneration", "maximumInputTokens", "maximumOutputTokens", "qualificationExpiresAt"}
TEXT_LIMITS = {"accountId": 128, "deploymentId": 128, "model": 256}
POLICY_FIELDS = HASH_FIELDS | INTEGER_FIELDS | TEXT_LIMITS.keys() | {
    "version", "provider", "baseUrl", "transport", "outputIncludesReasoning", "usageIncludesAllTokens",
}
ROUTER_GAP = (
    "Router preflight not run: this zero-write mode makes no Router request, because "
    "POST /register/relay-keys action worker-budget-preflight (llm-router 5a5181b7) is authenticated and "
    "non-allocating but durably consumes a nonce (relay-keys/index.ts:23-25; service.ts:34-50). "
    "The owner accepted that one write on 2026-11-19: --router-preflight performs the check. "
    "--skip-router-preflight-unavailable acknowledges ledger-only evidence; signing remains blocked."
)

# llm-router 5a5181b7:packages/plugins/relay-keys/contracts.ts:6-11 (path,
# audience, binding, id), :40 (claims), :58-73 (verification); index.ts:18-25
# (verify, then service.consume, then execute); service.ts:34-50 (replay set,
# 12 readiness / 120 management nonces per scope), :69-88 (preflight branch).
ROUTER_PATH = "/register/relay-keys"
ROUTER_AUDIENCE = "llm-router-relay-keys"
ROUTER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}\Z")
NONCE_NAMESPACE = "relay-key-nonces"
READINESS_LIMIT = 12  # service.ts:45-47
MANAGEMENT_LIMIT = 120
# Relay signs its own preflight with notAfter = now + 10 s at most
# (e77bd09a:server/workers/inference/budget-authority.ts:130-137); the Router allows 60.
DELEGATION_SECONDS = 10
ROUTER_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 65536
USER_AGENT = "relay-worker-budget-file-b-check/1 (ailab operator)"
SUBJECT = "operator:file-b-check"
ACCESS = ("api", "subscription", "local")
PROOF_FIELDS = {"version", "action", "operationId", "requestDigest", "issuer", "tenant", "deploymentDigest",
                "recoveryEpoch", "policyDigests", "requiredUntil", "limit", "observedAt", "validUntil"}
# Router error codes this tool names; any other code is reported as unrecognized.
KNOWN_CODES = {"RATE_LIMITED", "RELAY_DELEGATION_DENIED", "GRANT_DENIED", "WORKER_BUDGET_UNQUALIFIED",
               "WORKER_BUDGET_UNAVAILABLE", "ASSERTION_REPLAY", "INVALID_REQUEST", "ROUTE_NOT_FOUND",
               "NOT_FOUND", "KEY_STORE_UNAVAILABLE", "PAYLOAD_TOO_LARGE"}


class Fail(Exception):
    pass


class RouterUnavailable(Fail):
    """Only the known absence of a zero-write operation may be acknowledged."""


class RouterFailure(Fail):
    """A Router outcome after (or instead of) the one request; never retried."""

    def __init__(self, status, message, *, nonce, http_status=None, code=None):
        super().__init__(message)
        self.status, self.nonce, self.http_status, self.code = status, nonce, http_status, code


class NotSent(Exception):
    """Connection or TLS setup failed: the request never left this process."""


class OutcomeUnknown(Exception):
    """The request may have reached the Router; its answer was lost."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Reserve exit 2 for ledger-only evidence; do not echo arbitrary argv.
        raise Fail("arguments: invalid command line; see --help")


def positive(value):
    return type(value) is int and 0 < value <= MAX_SAFE


def exact_keys(value, keys, where):
    if not isinstance(value, dict) or value.keys() != set(keys):
        raise Fail(f"{where}: unexpected or missing fields")


def parse_json(raw, where):
    def reject_constant(_):
        raise ValueError("non-finite number")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("size")
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        # Like relay-admin-call.py's allowlisted operations/redaction, never echo
        # arbitrary input, database errors, credentials, endpoints or policy text.
        raise Fail(f"{where}: invalid, duplicate-key or oversized JSON") from None


def read_json(path, where):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(path, "rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        return parse_json(raw, where)
    except OSError:
        raise Fail(f"{where}: cannot read input") from None


def canonical(value):
    # Match runtime.ts:22-30's recursive UTF-16 key ordering. Policy numbers are
    # safe integers only. Compact JSON preserves UTF-8 and lone surrogates
    # (JSON.stringify's well-formed escaping); never hash the digest itself.
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(canonical(key) + ":" + canonical(value[key]) for key in keys) + "}"
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return "".join(f"\\u{ord(c):04x}" if 0xD800 <= ord(c) <= 0xDFFF else c for c in raw)


def policy_digest(policy):
    return hashlib.sha256(canonical({k: v for k, v in policy.items() if k != "digest"}).encode()).hexdigest()


def validate_policy(policy, where):
    exact_keys(policy, POLICY_FIELDS, where)
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise Fail(f"{where}.version: expected 1")
    for field in sorted(HASH_FIELDS):
        if not isinstance(policy[field], str) or not DIGEST.fullmatch(policy[field]):
            raise Fail(f"{where}.{field}: expected lowercase SHA-256")
    for field in sorted(INTEGER_FIELDS):
        if not positive(policy[field]):
            raise Fail(f"{where}.{field}: expected positive safe integer")
    for field, limit in TEXT_LIMITS.items():
        if not isinstance(policy[field], str) or not 1 <= len(policy[field].encode("utf-16-le", "surrogatepass")) // 2 <= limit:
            raise Fail(f"{where}.{field}: invalid string length")
    for field in ("outputIncludesReasoning", "usageIncludesAllTokens"):
        if policy[field] is not True:
            raise Fail(f"{where}.{field}: must be true")
    if policy["provider"] not in ("local", "openai", "anthropic"):
        raise Fail(f"{where}.provider: unsupported provider")
    if policy["transport"] not in ("chat-completions", "anthropic-messages"):
        raise Fail(f"{where}.transport: unsupported transport")
    try:
        url = policy["baseUrl"]
        if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError()
        parsed = urllib.parse.urlsplit(url)
        if not parsed.scheme or not (parsed.netloc or parsed.path):
            raise ValueError()
        if parsed.scheme in ("http", "https", "ws", "wss", "ftp") and not parsed.hostname:
            raise ValueError()
        if parsed.netloc and parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise Fail(f"{where}.baseUrl: expected absolute URL") from None
    # Runtime qualification adds these checks to the schema (runtime.ts:115-124).
    if policy["maximumInputTokens"] + policy["maximumOutputTokens"] > MAX_SAFE:
        raise Fail(f"{where}: token bounds exceed safe integer sum")
    transport = "anthropic-messages" if policy["provider"] == "anthropic" else "chat-completions"
    if policy["transport"] != transport:
        raise Fail(f"{where}.transport: provider mismatch")
    if policy_digest(policy) != policy["digest"]:
        raise Fail(f"{where}.digest: canonical tokenPolicyDigest mismatch")


def rows_by_key(rows, where, *, validate):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise Fail(f"{where}.qualifications: expected array of at most 10000 rows")
    result, counts = {}, {}
    for index, row in enumerate(rows):
        label = f"{where}.qualifications[{index}]"
        exact_keys(row, {"deployment_digest", "digest", "policy"}, label)
        for field in ("deployment_digest", "digest"):
            if not isinstance(row[field], str) or not DIGEST.fullmatch(row[field]):
                raise Fail(f"{label}.{field}: expected lowercase SHA-256")
        key = row["deployment_digest"], row["digest"]
        if key in result:
            raise Fail(f"{label}: duplicate qualification row")
        if validate:
            validate_policy(row["policy"], f"{label}.policy")
            if row["digest"] != row["policy"]["digest"]:
                raise Fail(f"{label}.digest: differs from policy.digest")
        result[key] = row["policy"]
        counts[key[0]] = counts.get(key[0], 0) + 1
        if counts[key[0]] > 1000:  # Router RuntimeSnapshot, runtime.ts:20-21.
            raise Fail(f"{where}: more than 1000 policies for a deployment")
    return result


def check_ledger(file_b, snapshot, lifetime_seconds, now_ms):
    exact_keys(file_b, {"version", "qualifications"}, "File B")
    if type(file_b["version"]) is not int or file_b["version"] != 1:
        raise Fail("File B.version: expected 1")
    expected = rows_by_key(file_b["qualifications"], "File B", validate=True)
    if not expected:
        raise Fail("File B: no qualification rows")
    exact_keys(snapshot, {"version", "readOnly", "observedAt", "qualifications"}, "ledger")
    if type(snapshot["version"]) is not int or snapshot["version"] != 1 or snapshot["readOnly"] is not True:
        raise Fail("ledger: expected version 1 read-only snapshot")
    observed = snapshot["observedAt"]
    if not positive(observed) or abs(now_ms - observed) > FRESH_MS:
        raise Fail("ledger: stale readback or clock disagreement (maximum 15 seconds)")
    actual = rows_by_key(snapshot["qualifications"], "ledger", validate=False)
    missing, extra = expected.keys() - actual.keys(), actual.keys() - expected.keys()
    if missing or extra:
        raise Fail(f"ledger: qualification row set mismatch ({len(missing)} missing, {len(extra)} extra; deployment/digest keys)")
    if type(lifetime_seconds) is not int or not 1 <= lifetime_seconds <= 604800:
        # 004-preflight-errors.sql:17 bounds requiredUntil to seven days.
        raise Fail("required lifetime: expected 1..604800 seconds, including time until run start")
    required_until = max(now_ms, observed) + lifetime_seconds * 1000
    for index, (key, policy) in enumerate(expected.items()):
        stored = actual[key]
        label = f"ledger row for File B.qualifications[{index}]"
        exact_keys(stored, POLICY_FIELDS, label)
        for field in sorted(POLICY_FIELDS):
            if type(stored[field]) is not type(policy[field]) or stored[field] != policy[field]:
                raise Fail(f"{label}: {field} mismatch")
        expires = policy["qualificationExpiresAt"]
        if expires <= max(now_ms, observed):
            raise Fail(f"{label}: qualification expired")
        if expires < required_until:
            raise Fail(f"{label}: insufficient lifetime for requiredUntil={required_until}")
    return {"status": "pass", "rows": len(expected), "requiredUntil": required_until,
            "observedAt": observed, "fileBSha256": hashlib.sha256(canonical(file_b).encode()).hexdigest()}


def router_preflight():
    # 5a5181b7:packages/plugins/relay-keys/contracts.ts:58-73 authenticates
    # EdDSA JWTs bound to exact request bytes. index.ts:23-25 ALWAYS consumes
    # a durable nonce before service.ts:69-88's non-allocating evaluation.
    # worker-budget-authority/index.ts:39-71 exposes no HTTP diagnostic.
    # Default mode sends nothing; --router-preflight is the separate opt-in path.
    raise RouterUnavailable(ROUTER_GAP)


def check_router(acknowledge, preflight=None):
    try:
        (preflight or router_preflight)()
    except RouterUnavailable as error:
        return {"status": "unavailable", "acknowledged": acknowledge, "message": str(error)}
    except OSError:
        raise Fail("Router: unreachable; acknowledgement cannot bypass a failed request") from None
    # No success response contract for a zero-write endpoint exists at this pin.
    raise Fail("Router: no reviewed zero-write response contract; cannot accept a result")


def dsn_environment(dsn):
    """Keep connection secrets in child environment, never argv or SQL."""
    try:
        url = urllib.parse.urlsplit(dsn)
        if url.scheme not in ("postgres", "postgresql") or not url.hostname or not url.username or not url.path[1:] or url.fragment:
            raise ValueError()
        env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
        env.update(PGHOST=urllib.parse.unquote(url.hostname), PGPORT=str(url.port or 5432),
                   PGUSER=urllib.parse.unquote(url.username), PGDATABASE=urllib.parse.unquote(url.path[1:]),
                   PGPASSWORD=urllib.parse.unquote(url.password or ""), PGPASSFILE=os.devnull,
                   PGCONNECT_TIMEOUT="5", PGCLIENTENCODING="UTF8", PGAPPNAME="relay-worker-budget-file-b-check",
                   PSQL_HISTORY=os.devnull)
        allowed = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "hostaddr": "PGHOSTADDR"}
        seen = set()
        for key, value in urllib.parse.parse_qsl(url.query, strict_parsing=True):
            if key not in allowed or key in seen or not value:
                raise ValueError()
            seen.add(key)
            env[allowed[key]] = value
        if not env["PGHOST"].startswith("/") and env.get("PGSSLMODE") != "verify-full":
            raise ValueError()
        if any("\x00" in v for v in env.values()):
            raise ValueError()
        return env
    except (ValueError, TypeError):
        raise Fail("ledger DSN: expected PostgreSQL URL; TCP requires sslmode=verify-full; allowed options: sslmode, sslrootcert, hostaddr") from None


def read_ledger(dsn, sql=LEDGER_SQL):
    env = dsn_environment(dsn)
    try:
        result = subprocess.run(["psql", "-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                                input=sql, text=True, encoding="utf-8", capture_output=True, env=env, timeout=15)
    except subprocess.TimeoutExpired:
        raise Fail("ledger: psql timed out; no readback") from None
    except OSError:
        raise Fail("ledger: cannot start psql") from None
    except UnicodeError:
        raise Fail("ledger: invalid psql output encoding") from None
    if result.returncode:
        raise Fail(f"ledger: psql failed (exit {result.returncode}); connection, authentication or SELECT unavailable; diagnostics suppressed")
    return parse_json(result.stdout, "ledger")


# --- Ed25519 signing (RFC 8032 section 5.1; the section 6 reference algorithm) ---
# The stdlib has no Ed25519. One signature with an operator-held key on the
# operator's machine: Python integers are not constant-time, which matters only
# to an attacker timing this one process. Known-answer tests: RFC 8032 7.1.
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _point_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _point_mul(scalar, point):
    result = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _point_add(result, point)
        point = _point_add(point, point)
        scalar >>= 1
    return result


def _recover_x(y, sign):
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _point_compress(point):
    z_inverse = pow(point[2], _P - 2, _P)
    x, y = point[0] * z_inverse % _P, point[1] * z_inverse % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _secret_expand(seed):
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return scalar, digest[32:]


def ed25519_public(seed):
    return _point_compress(_point_mul(_secret_expand(seed)[0], _G))


def ed25519_sign(seed, message):
    scalar, prefix = _secret_expand(seed)
    public = _point_compress(_point_mul(scalar, _G))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    commitment = _point_compress(_point_mul(r, _G))
    k = int.from_bytes(hashlib.sha512(commitment + public + message).digest(), "little") % _L
    return commitment + ((r + k * scalar) % _L).to_bytes(32, "little")


# PKCS#8 v1 Ed25519 private key (RFC 8410 section 7), what `openssl genpkey
# -algorithm ed25519` wrote at Router connection step 1, and the SPKI prefix
# whose SHA-256 `openssl pkey -pubout -outform DER | sha256sum` prints.
PKCS8_ED25519 = bytes.fromhex("302e020100300506032b657004220420")
SPKI_ED25519 = bytes.fromhex("302a300506032b6570032100")


def read_reconciler_key(fd):
    """The reconciler's Ed25519 seed from an inherited pipe; never echoed, never on disk."""
    if type(fd) is not int or fd < 3:
        raise Fail("reconciler key: --reconciler-key-fd must name an inherited descriptor >= 3")
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            raise Fail("reconciler key: descriptor must be a pipe (kubectl ... | base64 -d), never a file")
        raw = bytearray()
        try:
            while len(raw) <= 8192:
                chunk = os.read(fd, 8193 - len(raw))
                if not chunk:
                    break
                raw += chunk
        finally:
            os.close(fd)
    except OSError:
        raise Fail("reconciler key: cannot read the inherited descriptor") from None
    try:
        if len(raw) > 8192:
            raise ValueError()
        lines = raw.decode("ascii").strip().split("\n")
        if len(lines) < 3 or lines[0].strip() != "-----BEGIN PRIVATE KEY-----" or lines[-1].strip() != "-----END PRIVATE KEY-----":
            raise ValueError()
        der = bytearray(base64.b64decode("".join(line.strip() for line in lines[1:-1]), validate=True))
        if len(der) != 48 or bytes(der[:16]) != PKCS8_ED25519:
            raise ValueError()
        seed = bytearray(der[16:])
        der[:] = bytes(len(der))
        return seed
    except (ValueError, UnicodeError):
        raise Fail("reconciler key: expected one unencrypted PKCS#8 Ed25519 PEM private key") from None
    finally:
        raw[:] = bytes(len(raw))


def read_connection(path, connection_id, tenant):
    """One reviewed Relay Router deployment record (Relay e77bd09a:server/router-credentials/transport.ts:24-38)."""
    records = read_json(path, "Router connection")
    fields = {"id", "name", "tenants", "origin", "issuer", "kid", "signingKeyFile", "reconcilerKid",
              "reconcilerKeyFile", "baoOrigin", "baoTokenFile", "baoMount", "baoPrefix"}
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise Fail("Router connection: expected an array of deployment records")
    for record in records:
        exact_keys(record, fields, "Router connection record")
    matches = [record for record in records if record["id"] == connection_id]
    if len(matches) != 1:
        raise Fail("Router connection: --router-connection-id must name exactly one record")
    record = matches[0]
    origin = re.compile(r"https://[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[1-9][0-9]{0,4})?\Z")
    tenants = record["tenants"]
    if (not isinstance(record["id"], str) or not UUID.fullmatch(record["id"])
            or not isinstance(tenants, list) or not tenants
            or any(not isinstance(t, str) or not UUID.fullmatch(t) for t in tenants)
            or not isinstance(record["origin"], str) or not origin.fullmatch(record["origin"]) or record["origin"].endswith(":443")
            or not isinstance(record["baoOrigin"], str) or not origin.fullmatch(record["baoOrigin"]) or record["baoOrigin"].endswith(":443")
            or not isinstance(record["issuer"], str) or not ROUTER_ID.fullmatch(record["issuer"])
            # The Router parses the header kid with its id schema (contracts.ts:11,64).
            or not isinstance(record["reconcilerKid"], str) or not ROUTER_ID.fullmatch(record["reconcilerKid"])
            or not isinstance(record["baoMount"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", record["baoMount"])
            or not isinstance(record["baoPrefix"], str)
            or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", record["baoPrefix"])):
        raise Fail("Router connection: record fields fail Relay's RouterDeployment schema or are not canonical")
    if tenant not in tenants:
        raise Fail("Router connection: --router-tenant is not one of the record's tenants")
    # Relay e77bd09a:server/router-credentials/service.ts:41-54: SHA-256 of the
    # sorted-key compact JSON of these six fields (origins already canonical).
    identity = {key: record[key] for key in ("id", "origin", "issuer", "baoOrigin", "baoMount", "baoPrefix")}
    return {"id": record["id"], "origin": record["origin"], "issuer": record["issuer"], "tenant": tenant,
            "kid": record["reconcilerKid"],
            "deploymentDigest": hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def router_scope(issuer, tenant):
    # relay-keys/service.ts:11: sha256(JSON.stringify([iss, tenant])); both are
    # ASCII ids (contracts.ts:11), so Python's compact JSON is byte-identical.
    return hashlib.sha256(json.dumps([issuer, tenant], separators=(",", ":")).encode()).hexdigest()


def router_slots(store, scope, now_ms, nonce_sha256=None):
    """Readiness slots of one scope from a fixed read-only store readback."""
    exact_keys(store, {"version", "readOnly", "observedAt", "namespace", "scopes"}, "Router store")
    if type(store["version"]) is not int or store["version"] != 1 or store["readOnly"] is not True or store["namespace"] != NONCE_NAMESPACE:
        raise Fail("Router store: expected version 1 read-only relay-key-nonces readback")
    observed = store["observedAt"]
    if not positive(observed) or abs(now_ms - observed) > FRESH_MS:
        raise Fail("Router store: stale readback or clock disagreement (maximum 15 seconds)")
    scopes = store["scopes"]
    if not isinstance(scopes, list) or len(scopes) > 10000:
        raise Fail("Router store: expected at most 10000 scopes")
    keys, entries = set(), []
    for item in scopes:
        exact_keys(item, {"key", "entries"}, "Router store scope")
        if not isinstance(item["key"], str) or not DIGEST.fullmatch(item["key"]) or item["key"] in keys:
            raise Fail("Router store: invalid or duplicate scope key")
        keys.add(item["key"])
        if not isinstance(item["entries"], list) or len(item["entries"]) > 10000:
            raise Fail("Router store: invalid replay set")
        for entry in item["entries"]:
            exact_keys(entry, {"exp", "readiness", "nonceSha256"}, "Router store entry")
            if (not positive(entry["exp"]) or type(entry["readiness"]) is not bool
                    or not isinstance(entry["nonceSha256"], str) or not DIGEST.fullmatch(entry["nonceSha256"])):
                raise Fail("Router store: invalid replay-set entry")
        if item["key"] == scope:
            entries = item["entries"]
    # The Router's own clock decides expiry, n.exp > floor(now / 1000) (service.ts:37-40).
    clock = observed // 1000
    live = [entry for entry in entries if entry["exp"] > clock]
    readiness = [entry for entry in live if entry["readiness"]]
    result = {"observedAt": observed, "limit": READINESS_LIMIT, "unexpiredReadiness": len(readiness),
              "remaining": max(0, READINESS_LIMIT - len(readiness)),
              "unexpiredManagement": len(live) - len(readiness), "managementLimit": MANAGEMENT_LIMIT,
              "retainedEntries": len(entries)}
    if readiness:
        result["nextExpiry"] = min(entry["exp"] for entry in readiness) * 1000
    if nonce_sha256 is not None:
        result["callNonceRecorded"] = any(entry["nonceSha256"] == nonce_sha256 for entry in entries)
    return result


def check_authority(rows):
    if not isinstance(rows, list) or len(rows) != 1:
        raise Fail("ledger authority: expected exactly one authority row")
    row = rows[0]
    exact_keys(row, {"instance_id", "recovery_epoch", "ready"}, "ledger authority")
    for field in ("instance_id", "recovery_epoch"):
        if not isinstance(row[field], str) or not UUID.fullmatch(row[field]):
            raise Fail(f"ledger authority.{field}: expected lowercase UUID")
    if row["ready"] is not True:
        # The Router would answer 503 after writing its nonce (meter.ts:121-125).
        raise Fail("ledger authority: ready is not true; the Router cannot qualify File B")
    return {"instanceId": row["instance_id"], "recoveryEpoch": row["recovery_epoch"], "ready": True}


def utf16_length(value):
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def router_parameters(routes, access, requested_model, limit):
    """Local copy of the Router's request schema (worker-budgets.ts:29-36) and the
    one ceiling-free check (service.ts:74), so an invalid request is refused
    before its nonce is spent."""
    if (not routes or len(routes) > 50 or len(set(routes)) != len(routes)
            or any(not isinstance(r, str) or not 1 <= utf16_length(r) <= 128 for r in routes)):
        raise Fail("--router-route: 1..50 distinct route names of 1..128 characters")
    if not access or len(set(access)) != len(access) or any(a not in ACCESS for a in access):
        raise Fail("--router-access: 1..3 distinct values of api, subscription, local")
    if not isinstance(requested_model, str) or not 1 <= utf16_length(requested_model) <= 200 or requested_model not in routes:
        raise Fail("--router-requested-model: must be one of the --router-route names")
    if type(limit) is not int or not 1 <= limit <= 1000000000:
        raise Fail("--router-limit: expected 1..1000000000 tokens")
    return {"routes": list(routes), "access": list(access), "requestedModel": requested_model, "limit": limit}


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def signed_request(connection, preflight, seed, now_ms):
    """The exact body bytes and the reconciler delegation for them (contracts.ts:40,58-73;
    the same claims Relay's SignedRouterBridge signs, transport.ts:160-198)."""
    operation_id = str(uuid.uuid4())
    raw = compact({"version": 1, "action": "worker-budget-preflight", "operationId": operation_id, "preflight": preflight})
    if len(raw) > 32768:
        raise Fail("Router request: body exceeds the Router's 32768-byte limit")
    nonce = b64url(os.urandom(24))
    iat = now_ms // 1000
    header = {"alg": "EdDSA", "typ": "JWT", "kid": connection["kid"]}
    claims = {"iss": connection["issuer"], "aud": ROUTER_AUDIENCE, "sub": SUBJECT, "tenant": connection["tenant"],
              "role": "reconciler", "action": "worker-budget-preflight", "operationId": operation_id,
              "iat": iat, "exp": iat + DELEGATION_SECONDS, "nonce": nonce,
              "binding": hashlib.sha256(f"POST\n{ROUTER_PATH}\n{hashlib.sha256(raw).hexdigest()}".encode()).hexdigest()}
    signing_input = f"{b64url(compact(header))}.{b64url(compact(claims))}"
    token = f"{signing_input}.{b64url(ed25519_sign(seed, signing_input.encode('ascii')))}"
    return {"raw": raw, "token": token, "operationId": operation_id, "exp": claims["exp"],
            "requestDigest": hashlib.sha256(raw).hexdigest(), "nonceSha256": hashlib.sha256(nonce.encode()).hexdigest()}


def router_post(origin, raw, token, *, context=None, timeout=ROUTER_TIMEOUT_SECONDS):
    """Exactly one HTTPS POST: TLS verified, no proxy, no redirect, no retry."""
    parsed = urllib.parse.urlsplit(origin)
    if context is None:
        context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout, context=context)
    try:
        try:
            connection.connect()
        except (OSError, http.client.HTTPException):
            raise NotSent() from None
        try:
            connection.request("POST", ROUTER_PATH, body=raw, headers={
                "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                "Accept": "application/json", "User-Agent": USER_AGENT})
            response = connection.getresponse()
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException):
            raise OutcomeUnknown() from None
    finally:
        connection.close()


def router_error(status, body):
    """Map a refusal to a distinct failure. Never echo the Router's message text."""
    code = None
    try:
        parsed = parse_json(body, "Router error") if len(body) <= MAX_RESPONSE_BYTES else None
        candidate = parsed.get("error", {}).get("code") if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) else None
        code = candidate if candidate in KNOWN_CODES else None
    except Fail:
        pass
    shown = code or "unrecognized code"
    after = "run the after pass: callNonceRecorded shows whether this nonce was written"
    if status == 429 and code == "RATE_LIMITED":
        return RouterFailure("rate-limited", "Router: readiness limit reached for this scope (429 RATE_LIMITED): "
                             "12 unexpired readiness nonces. No nonce was written (service.ts:46-47 refuses inside the "
                             "transaction). Not retried; rerun after the earliest nonce expires.",
                             nonce="not-written", http_status=status, code=code)
    if status == 409 and code == "ASSERTION_REPLAY":
        return RouterFailure("replay", "Router: this delegation's nonce is already in the replay set (409 ASSERTION_REPLAY), "
                             "so the same request reached the Router twice; its first delivery wrote the nonce. Not retried.",
                             nonce="written", http_status=status, code=code)
    if status == 403:
        # contracts.ts:42,58-73 refuse before consume(); service.ts:16,59,72-83 and meter.ts:126-129 after it.
        detail, nonce = {
            "RELAY_DELEGATION_DENIED": ("delegation refused (signature, kid, role, issuer/tenant trust, binding or clock; "
                                        "a wrong key is refused here). Refused before the nonce write unless the delegation "
                                        "expired between admission and execution", "unknown"),
            "GRANT_DENIED": ("request outside the Relay trust ceiling (limit, routes, access or requiredUntil). "
                             "Refused after the nonce write", "written"),
            "WORKER_BUDGET_UNQUALIFIED": ("the Router does not qualify every File B policy through requiredUntil, or the "
                                          "selected route has no qualified usable target. Refused after the nonce write",
                                          "written"),
        }.get(code, ("refused", "unknown"))
        return RouterFailure("denied", f"Router: {detail} (403 {shown}). Not retried"
                             + (f"; {after}." if nonce == "unknown" else "."),
                             nonce=nonce, http_status=status, code=code)
    if status == 503 and code == "WORKER_BUDGET_UNAVAILABLE":
        return RouterFailure("unavailable", "Router: worker budget authority unavailable, or its deployment/recovery identity "
                             "differs from this request (503 WORKER_BUDGET_UNAVAILABLE); refused after the nonce write. Not retried.",
                             nonce="written", http_status=status, code=code)
    return RouterFailure("http-error", f"Router: HTTP {status} ({shown}). Not retried; {after}.",
                         nonce="unknown", http_status=status, code=code)


def echoes_operation(body, operation_id):
    try:
        parsed = parse_json(body, "Router proof") if len(body) <= MAX_RESPONSE_BYTES else None
    except Fail:
        return False
    return isinstance(parsed, dict) and parsed.get("operationId") == operation_id


def validate_proof(body, sent, connection, preflight, now_ms):
    """The preflight branch's exact response (service.ts:85-88)."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise Fail("Router proof: oversized response")
    proof = parse_json(body, "Router proof")
    exact_keys(proof, PROOF_FIELDS, "Router proof")
    expected = {"version": 1, "action": "worker-budget-preflight", "operationId": sent["operationId"],
                "requestDigest": sent["requestDigest"], "issuer": connection["issuer"], "tenant": connection["tenant"],
                "deploymentDigest": preflight["deploymentDigest"], "recoveryEpoch": preflight["recoveryEpoch"],
                "policyDigests": preflight["policyDigests"], "requiredUntil": preflight["requiredUntil"],
                "limit": preflight["limit"]}
    for field in sorted(expected):
        if type(proof[field]) is not type(expected[field]) or proof[field] != expected[field]:
            raise Fail(f"Router proof: {field} mismatch")
    observed, valid = proof["observedAt"], proof["validUntil"]
    if not positive(observed) or not positive(valid):
        raise Fail("Router proof: observedAt/validUntil must be positive safe integers")
    # Relay's own acceptance (budget-authority.ts:155-161), with the ledger's
    # 15-second allowance for an operator clock instead of an in-cluster one.
    if abs(now_ms - observed) > FRESH_MS or valid <= now_ms or valid > observed + FRESH_MS or valid > sent["exp"] * 1000:
        raise Fail("Router proof: stale, expired or clock-inconsistent evidence")
    return {"deploymentDigest": proof["deploymentDigest"], "recoveryEpoch": proof["recoveryEpoch"],
            "policies": len(proof["policyDigests"]), "requiredUntil": proof["requiredUntil"], "limit": proof["limit"],
            "observedAt": observed, "validUntil": valid}


def notice(text):
    print(text, file=sys.stderr, flush=True)


def check_router_preflight(args, file_b, ledger, connection, parameters, store_before, now_ms):
    """Exactly one Router request; the caller has already passed the ledger half."""
    router = {"status": "not-called", "mode": "preflight", "called": False, "issuer": connection["issuer"],
              "tenant": connection["tenant"], "scope": router_scope(connection["issuer"], connection["tenant"]),
              "deploymentDigest": connection["deploymentDigest"], "nonceWritten": "no",
              "write": (f"one delegation nonce in the Router replay set ({NONCE_NAMESPACE}) for scope "
                        f"{connection['issuer']}/{connection['tenant']}")}
    try:
        deployments = {row["deployment_digest"] for row in file_b["qualifications"]}
        if deployments != {connection["deploymentDigest"]}:
            raise Fail("Router preflight: every File B row must belong to the connection's deployment digest")
        digests = sorted(row["digest"] for row in file_b["qualifications"])
        if len(digests) > 32:
            raise Fail("Router preflight: more than 32 File B policies (worker-budgets.ts:31)")
        preflight = {"deploymentDigest": connection["deploymentDigest"],
                     "recoveryEpoch": ledger["authority"]["recoveryEpoch"], "policyDigests": digests,
                     "requiredUntil": ledger["requiredUntil"], "limit": parameters["limit"],
                     "routes": parameters["routes"], "access": parameters["access"],
                     "requestedModel": parameters["requestedModel"]}
        before = router_slots(store_before, router["scope"], now_ms)
        router["slotsBefore"] = before
        if before["remaining"] == 0:
            raise Fail(f"Router preflight: no readiness slot for this scope (12 unexpired); not called; "
                       f"the earliest expires at {before['nextExpiry']}")
        seed = read_reconciler_key(args.reconciler_key_fd)
        try:
            router["reconcilerPublicKeySha256"] = hashlib.sha256(SPKI_ED25519 + ed25519_public(seed)).hexdigest()
            sent = signed_request(connection, preflight, seed, time.time_ns() // 1000000)
        finally:
            seed[:] = bytes(len(seed))
    except Fail as error:
        router["message"] = str(error)
        raise RouterFailed(router) from None
    router.update(operationId=sent["operationId"], requestDigest=sent["requestDigest"],
                  nonceSha256=sent["nonceSha256"], delegationExpiresAt=sent["exp"] * 1000)
    notice(f"Router preflight (opt-in; owner decision 2026-11-19): sending exactly ONE request to "
           f"{connection['origin']}{ROUTER_PATH}, never retried. The Router durably writes its delegation nonce to "
           f"its replay set ({NONCE_NAMESPACE}) for scope {connection['issuer']}/{connection['tenant']}. "
           f"Readiness slots before: {before['remaining']} of {READINESS_LIMIT} remaining (Router store observed at "
           f"{before['observedAt']}).")
    router["called"] = True
    expected_after = {"remaining": max(0, before["remaining"] - 1), "limit": READINESS_LIMIT,
                      "until": sent["exp"] * 1000}
    try:
        try:
            status, body = router_post(connection["origin"], sent["raw"], sent["token"])
        except NotSent:
            raise RouterFailure("not-sent", "Router: connection or TLS verification failed before the request was sent; "
                                "no nonce was written. Not retried.", nonce="not-written") from None
        except Exception:
            # OutcomeUnknown, or anything unexpected once sending may have begun.
            raise RouterFailure("outcome-unknown", "Router: the request may have been delivered but no answer arrived. "
                                "Not retried; run the after pass: callNonceRecorded shows whether this nonce was written.",
                                nonce="unknown") from None
        router["httpStatus"] = status
        if status != 200:
            raise router_error(status, body)
        try:
            router["proof"] = validate_proof(body, sent, connection, preflight, time.time_ns() // 1000000)
        except Exception as error:
            # The preflight branch answers only after consume(); a 200 echoing this operationId proves the write.
            written = echoes_operation(body, sent["operationId"])
            reason = str(error) if isinstance(error, Fail) else "Router proof: unreadable response"
            raise RouterFailure("invalid-proof", f"{reason}; nonce written: {'yes' if written else 'unknown'}. "
                                "Not retried" + ("." if written else "; run the after pass: callNonceRecorded shows "
                                                 "whether this nonce was written."),
                                nonce="written" if written else "unknown") from None
        router["nonceWritten"] = "yes"
    except RouterFailure as failure:
        router.update(status=failure.status, nonceWritten={"written": "yes", "not-written": "no"}.get(failure.nonce, "unknown"))
        if failure.code:
            router["code"] = failure.code
        if router["nonceWritten"] != "no":
            router["slotsAfterExpected"] = expected_after
        router["message"] = str(failure)
        notice(after_notice(router))
        raise RouterFailed(router) from None
    router["slotsAfterExpected"] = expected_after
    router["status"] = "pass"
    router["message"] = (f"Router qualified all {len(digests)} File B policies of deployment {connection['deploymentDigest']} "
                         f"through requiredUntil={preflight['requiredUntil']} on {', '.join(parameters['routes'])}; "
                         f"one nonce written.")
    notice(after_notice(router))
    return router


def after_notice(router):
    expected = router.get("slotsAfterExpected")
    slots = (f"expected readiness slots after: {expected['remaining']} of {expected['limit']} until {expected['until']}"
             if expected else f"readiness slots unchanged: {router['slotsBefore']['remaining']} of {READINESS_LIMIT}")
    return (f"Router preflight: request sent once; nonce written: {router['nonceWritten']} "
            f"(sha256 {router['nonceSha256']}); {slots}. Read them back with the after pass (--router-slots).")


class RouterFailed(Fail):
    def __init__(self, router):
        super().__init__(router["message"])
        self.router = router


def human_report(report):
    router = report["router"]
    print(f"File B: {report['status']}; ledger={report['ledger']['status']}; router={router['status']}; "
          f"signingReady={'true' if report['signingReady'] else 'false'}")
    if report["ledger"]["status"] == "pass":
        print(f"Exact readback: {report['ledger']['rows']} rows; requiredUntil={report['ledger']['requiredUntil']}")
    if "write" in router:
        print(f"This mode writes {router['write']}; nonce written: {router.get('nonceWritten', 'no')}.")
    if "slotsBefore" in router:
        print(f"Readiness slots before: {router['slotsBefore']['remaining']} of {READINESS_LIMIT}")
    if "slotsAfterExpected" in router:
        after = router["slotsAfterExpected"]
        print(f"Readiness slots after (expected): {after['remaining']} of {after['limit']} until {after['until']}; "
              f"nonce sha256 {router['nonceSha256']}; confirm with the after pass")
    if "proof" in router:
        proof = router["proof"]
        print(f"Router proof: deploymentDigest={proof['deploymentDigest']}; recoveryEpoch={proof['recoveryEpoch']}; "
              f"policies={proof['policies']}; limit={proof['limit']}; validUntil={proof['validUntil']}")
    print(report.get("error") or router.get("message", "Router not checked"))


def run_slots(args, report):
    if None in (args.router_store_json, args.router_connection, args.router_connection_id, args.router_tenant):
        raise Fail("--router-slots needs --router-store-json, --router-connection, --router-connection-id and --router-tenant")
    if args.router_nonce_sha256 is not None and not DIGEST.fullmatch(args.router_nonce_sha256):
        raise Fail("--router-nonce-sha256: expected lowercase SHA-256")
    connection = read_connection(args.router_connection, args.router_connection_id, args.router_tenant)
    store = read_json(args.router_store_json, "Router store")
    scope = router_scope(connection["issuer"], connection["tenant"])
    report["router"] = {"status": "slots", "scope": scope, "issuer": connection["issuer"], "tenant": connection["tenant"],
                        "slots": router_slots(store, scope, time.time_ns() // 1000000, args.router_nonce_sha256)}
    report["status"] = "router-slots"


def slots_report(report):
    if "slots" not in report["router"]:
        print(f"Router slots: fail; {report.get('error', 'not checked')}")
        return
    router, slots = report["router"], report["router"]["slots"]
    recorded = slots.get("callNonceRecorded")
    print(f"Router readiness slots for {router['issuer']}/{router['tenant']}: {slots['remaining']} of {slots['limit']} "
          f"remaining (observedAt={slots['observedAt']}; {slots['retainedEntries']} retained entries)"
          + ("" if recorded is None else f"; call nonce recorded: {'yes' if recorded else 'no'}"))


def main(argv=None):
    parser = ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file-b", help="reviewed JSON envelope (not executable SQL)")
    parser.add_argument("--required-lifetime-seconds", type=int)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ledger-dsn-env", metavar="NAME")
    source.add_argument("--ledger-dsn-file", metavar="PATH")
    source.add_argument("--ledger-json", metavar="PATH", help="fresh psql JSON readback; '-' reads stdin")
    printing = parser.add_mutually_exclusive_group()
    printing.add_argument("--print-ledger-sql", action="store_true", help="print fixed read-only SQL and exit; makes no connection")
    printing.add_argument("--print-router-ledger-sql", action="store_true",
                          help="print the --router-preflight ledger SQL (adds the authority row) and exit")
    printing.add_argument("--print-router-store-script", action="store_true",
                          help="print the fixed read-only Router store program (for `node -` in the Router container) and exit")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--skip-router-preflight-unavailable", action="store_true")
    mode.add_argument("--router-preflight", action="store_true",
                      help="opt in to ONE authenticated Router preflight; it durably writes one replay-set nonce")
    mode.add_argument("--router-slots", action="store_true",
                      help="report the scope's readiness slots from --router-store-json (zero-write) and exit")
    router = parser.add_argument_group("Router inputs (--router-preflight, --router-slots)")
    router.add_argument("--router-connection", metavar="PATH", help="reviewed Relay Router connections file")
    router.add_argument("--router-connection-id", metavar="UUID")
    router.add_argument("--router-tenant", metavar="UUID")
    router.add_argument("--router-store-json", metavar="PATH", help="fresh Router store readback; '-' reads stdin")
    router.add_argument("--router-route", action="append", metavar="NAME")
    router.add_argument("--router-access", action="append", metavar="KIND")
    router.add_argument("--router-requested-model", metavar="NAME")
    router.add_argument("--router-limit", type=int, metavar="TOKENS")
    router.add_argument("--reconciler-key-fd", type=int, metavar="FD",
                        help="inherited pipe carrying Relay's reconciler PEM; never a file, never argv")
    router.add_argument("--router-nonce-sha256", metavar="HEX", help="--router-slots: report whether this nonce is recorded")
    parser.add_argument("--json", action="store_true")
    argv = sys.argv[1:] if argv is None else argv
    json_output = "--json" in argv
    report = {"status": "fail", "signingReady": False, "ledger": {"status": "not-checked"},
              "router": {"status": "not-checked"}}
    router_mode = slots_mode = False
    try:
        args = parser.parse_args(argv)
        json_output = args.json
        if args.print_ledger_sql:
            sys.stdout.write(LEDGER_SQL)
            return 0
        if args.print_router_ledger_sql:
            sys.stdout.write(ROUTER_LEDGER_SQL)
            return 0
        if args.print_router_store_script:
            sys.stdout.write(ROUTER_STORE_SCRIPT)
            return 0
        router_inputs = (args.router_connection, args.router_connection_id, args.router_tenant, args.router_store_json,
                         args.router_route, args.router_access, args.router_requested_model, args.router_limit,
                         args.reconciler_key_fd, args.router_nonce_sha256)
        if not (args.router_preflight or args.router_slots) and any(value is not None for value in router_inputs):
            raise Fail("Router inputs require --router-preflight or --router-slots")
        slots_mode, router_mode = args.router_slots, args.router_preflight
        if slots_mode:
            if any(value is not None for value in (
                    args.file_b, args.required_lifetime_seconds, args.ledger_dsn_env, args.ledger_dsn_file,
                    args.ledger_json, args.router_route, args.router_access, args.router_requested_model,
                    args.router_limit, args.reconciler_key_fd)):
                raise Fail("--router-slots takes only --router-store-json, the Router connection inputs and --router-nonce-sha256")
            run_slots(args, report)
        else:
            if not args.file_b or args.required_lifetime_seconds is None or not any((args.ledger_dsn_env, args.ledger_dsn_file, args.ledger_json)):
                raise Fail("--file-b, --required-lifetime-seconds and one --ledger-* input are required")
            if router_mode:
                if None in (args.router_connection, args.router_connection_id, args.router_tenant, args.router_store_json,
                            args.router_route, args.router_access, args.router_requested_model, args.router_limit,
                            args.reconciler_key_fd) or args.router_nonce_sha256 is not None:
                    raise Fail("--router-preflight needs --router-connection, --router-connection-id, --router-tenant, "
                               "--router-store-json, --router-route, --router-access, --router-requested-model, "
                               "--router-limit and --reconciler-key-fd")
                if args.router_store_json == "-" and args.ledger_json == "-":
                    raise Fail("--ledger-json and --router-store-json cannot both read stdin")
                report["router"] = {"status": "not-called", "mode": "preflight", "called": False}
                parameters = router_parameters(args.router_route, args.router_access, args.router_requested_model,
                                               args.router_limit)
                connection = read_connection(args.router_connection, args.router_connection_id, args.router_tenant)
            file_b = read_json(args.file_b, "File B")
            if args.ledger_json:
                snapshot = read_json(args.ledger_json, "ledger")
            else:
                if args.ledger_dsn_env:
                    dsn = os.environ.get(args.ledger_dsn_env, "")
                else:
                    try:
                        with open(args.ledger_dsn_file) as stream:
                            dsn = stream.read(65537).strip()
                    except (OSError, UnicodeError):
                        raise Fail("ledger DSN: cannot read file") from None
                if not dsn or len(dsn) > 65536:
                    raise Fail("ledger DSN: empty or oversized")
                snapshot = read_ledger(dsn, ROUTER_LEDGER_SQL) if router_mode else read_ledger(dsn)
            if not router_mode:
                report["ledger"] = check_ledger(file_b, snapshot, args.required_lifetime_seconds, time.time_ns() // 1000000)
                report["router"] = check_router(args.skip_router_preflight_unavailable)
                report["status"] = "ledger-only" if report["router"]["acknowledged"] else "fail"
            else:
                if not isinstance(snapshot, dict) or "authority" not in snapshot:
                    raise Fail("ledger: --router-preflight needs the --print-router-ledger-sql readback")
                store_before = read_json(args.router_store_json, "Router store")
                now_ms = time.time_ns() // 1000000
                ledger = check_ledger(file_b, {k: v for k, v in snapshot.items() if k != "authority"},
                                      args.required_lifetime_seconds, now_ms)
                ledger["authority"] = check_authority(snapshot["authority"])
                report["ledger"] = ledger
                report["router"] = check_router_preflight(args, file_b, ledger, connection, parameters, store_before, now_ms)
                report["status"], report["signingReady"] = "pass", True
    except RouterFailed as failure:
        report["router"] = failure.router
        report["error"] = str(failure)
    except Fail as error:
        report["error"] = str(error)
    except Exception:
        report["error"] = "File B: unexpected verification error; diagnostics suppressed"
    if json_output:
        print(json.dumps(report, sort_keys=True))
    elif slots_mode:
        slots_report(report)
    elif router_mode:
        human_report(report)
    else:
        print(f"File B: {report['status']}; ledger={report['ledger']['status']}; signingReady=false")
        if report["ledger"]["status"] == "pass":
            print(f"Exact readback: {report['ledger']['rows']} rows; requiredUntil={report['ledger']['requiredUntil']}")
        print(report.get("error") or report["router"].get("message", "Router not checked"))
    if report["status"] == "router-slots":
        return 0
    if report["status"] == "pass" and report["signingReady"] is True:
        return 0
    return 2 if report["status"] == "ledger-only" else 1


if __name__ == "__main__":
    sys.exit(main())
