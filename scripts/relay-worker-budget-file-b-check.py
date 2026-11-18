#!/usr/bin/env python3
"""Compare reviewed File B with a fresh, read-only ledger readback.

Python stdlib; psql is needed only for direct connections. No Router request is
safe under the zero-write contract at the pinned revision. Exit 0 with the
explicit acknowledgement means ledger-only, never permission to sign a run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

MAX_SAFE = 9007199254740991
MAX_BYTES = 16 * 1024 * 1024
FRESH_MS = 15000
DIGEST = re.compile(r"[a-f0-9]{64}\Z")

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

# llm-router 5a5181b7:packages/plugins/worker-budget-authority/runtime.ts:9-30.
HASH_FIELDS = {"digest", "routerReleaseDigest", "adapterDigest", "evidenceDigest"}
INTEGER_FIELDS = {"providerGeneration", "maximumInputTokens", "maximumOutputTokens", "qualificationExpiresAt"}
TEXT_LIMITS = {"accountId": 128, "deploymentId": 128, "model": 256}
POLICY_FIELDS = HASH_FIELDS | INTEGER_FIELDS | TEXT_LIMITS.keys() | {
    "version", "provider", "baseUrl", "transport", "outputIncludesReasoning", "usageIncludesAllTokens",
}
ROUTER_GAP = (
    "Router preflight unavailable under the zero-write contract at 5a5181b7: "
    "POST /register/relay-keys action worker-budget-preflight is authenticated and non-allocating, "
    "but durably consumes a nonce (relay-keys/index.ts:23-25; service.ts:34-50). "
    "A separately authenticated qualification operation without persistent writes is required. "
    "--skip-router-preflight-unavailable acknowledges ledger-only evidence; signing remains blocked."
)


class Fail(Exception):
    pass


class RouterUnavailable(Fail):
    """Only the known absence of a zero-write operation may be acknowledged."""


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
    # ReviewedTokenPolicy has ASCII keys and safe integer numbers only. Sorted
    # compact JSON matches runtime.ts:22-30, including UTF-8 and lone surrogates
    # (JSON.stringify's well-formed escaping); never hash the digest itself.
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
    # No HTTP allowlist, token input or User-Agent is needed: no call is made.
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


def read_ledger(dsn):
    env = dsn_environment(dsn)
    try:
        result = subprocess.run(["psql", "-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1", "-f", "-"],
                                input=LEDGER_SQL, text=True, capture_output=True, env=env, timeout=15)
    except subprocess.TimeoutExpired:
        raise Fail("ledger: psql timed out; no readback") from None
    except OSError:
        raise Fail("ledger: cannot start psql") from None
    except UnicodeError:
        raise Fail("ledger: invalid psql output encoding") from None
    if result.returncode:
        raise Fail(f"ledger: psql failed (exit {result.returncode}); connection, authentication or SELECT unavailable; diagnostics suppressed")
    return parse_json(result.stdout, "ledger")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-b", help="reviewed JSON envelope (not executable SQL)")
    parser.add_argument("--required-lifetime-seconds", type=int)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ledger-dsn-env", metavar="NAME")
    source.add_argument("--ledger-dsn-file", metavar="PATH")
    source.add_argument("--ledger-json", metavar="PATH", help="fresh psql JSON readback; '-' reads stdin")
    parser.add_argument("--print-ledger-sql", action="store_true", help="print fixed read-only SQL and exit; makes no connection")
    parser.add_argument("--skip-router-preflight-unavailable", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.print_ledger_sql:
        sys.stdout.write(LEDGER_SQL)
        return 0
    report = {"status": "fail", "signingReady": False, "ledger": {"status": "not-checked"},
              "router": {"status": "not-checked"}}
    try:
        if not args.file_b or args.required_lifetime_seconds is None or not any((args.ledger_dsn_env, args.ledger_dsn_file, args.ledger_json)):
            raise Fail("--file-b, --required-lifetime-seconds and one --ledger-* input are required")
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
            snapshot = read_ledger(dsn)
        report["ledger"] = check_ledger(file_b, snapshot, args.required_lifetime_seconds, time.time_ns() // 1000000)
        report["router"] = check_router(args.skip_router_preflight_unavailable)
        report["status"] = "ledger-only" if report["router"]["acknowledged"] else "fail"
    except Fail as error:
        report["error"] = str(error)
    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print(f"File B: {report['status']}; ledger={report['ledger']['status']}; signingReady=false")
        if report["ledger"]["status"] == "pass":
            print(f"Exact readback: {report['ledger']['rows']} rows; requiredUntil={report['ledger']['requiredUntil']}")
        print(report.get("error") or report["router"].get("message", "Router not checked"))
    return 0 if report["status"] == "ledger-only" else 1


if __name__ == "__main__":
    sys.exit(main())
