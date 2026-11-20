#!/usr/bin/env python3
"""Generate reviewed File B (worker_budget.qualifications) from live Router
readings plus owner-decided values.

Writes two files into --output-dir:

* file-b.json: the expected complete table, exactly the format
  scripts/relay-worker-budget-file-b-check.py consumes (--file-b);
* ledger-step7b.sql: one psql transaction that inserts exactly that row, with
  File A preconditions, whole-row NULL-safe assertions, an in-database
  recomputation of the canonical digest and a grep-able compact readback.

Python stdlib only. Offline: it reads one JSON inputs file and makes no ledger,
Router or network request. Output is deterministic (byte-identical on rerun);
an existing output file is never overwritten with different content.

Inputs (a JSON object with exactly these fields; no defaults, no extras):

  deployment_digest      Relay Router deployment record digest; must equal
                         DEFAULT_DEPLOYMENT_DIGEST unless the same value is
                         also passed as --deployment-digest-override.
  routerReleaseDigest, adapterDigest, accountId, deploymentId, model, provider,
  providerGeneration, baseUrl, transport, maximumInputTokens,
  maximumOutputTokens    live readings from the FINAL Router process.
  qualificationExpiresAt ISO-8601 UTC instant E (YYYY-MM-DDTHH:MM:SS[.fff]Z),
                         stored as Unix milliseconds.
  evidenceDigest         SHA-256 of the independently retained evidence.

outputIncludesReasoning and usageIncludesAllTokens are always true; version is 1.
"""
from __future__ import annotations

import argparse
import calendar
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("relay_file_b_check", HERE / "relay-worker-budget-file-b-check.py")
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

Fail = check.Fail
MAX_SAFE = check.MAX_SAFE
DIGEST = check.DIGEST

# ailab #1753 comment 124071, File A (ledger-step7a.sql), committed before File B.
DEFAULT_DEPLOYMENT_DIGEST = "547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad"
FILE_A_CLIENT = {
    "login": "relay_budget_allocator",
    "issuer": "relay.chifor.me",
    "connection_id": "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8",
    "tenant_ids": ("00000000-0000-4000-8000-000000000001",),
    "max_tokens": 7441536,
    "max_requests": 8,
    "max_authority_ms": 120000,
}
FILE_A_READER_LOGIN = "relay_budget_router"

# llm-router 5a5181b7:packages/plugins/worker-budget-authority/runtime.ts:10-19
# (ReviewedTokenPolicy, strict) minus `version`, `digest` and the two literal-true
# flags, which the generator fixes. qualificationExpiresAt is given as ISO-8601.
READING_FIELDS = (
    "routerReleaseDigest", "adapterDigest", "accountId", "deploymentId", "model", "provider",
    "providerGeneration", "baseUrl", "transport", "maximumInputTokens", "maximumOutputTokens",
)
DECIDED_FIELDS = ("qualificationExpiresAt", "evidenceDigest")
INPUT_FIELDS = frozenset(("deployment_digest",) + READING_FIELDS + DECIDED_FIELDS)
PROVIDERS = ("local", "openai", "anthropic")
TRANSPORTS = ("chat-completions", "anthropic-messages")
ISO_UTC = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?Z\Z")
# Deliberately narrower than zod's z.string().url() (WHATWG URL.canParse): the
# value must survive zod unchanged (zod 4.6.5 trims and strips TAB/LF/CR) and
# must equal the Router account's baseUrl byte for byte (runtime.ts:120-121).
BASE_URL = re.compile(
    r"https?://"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r"|\[[0-9A-Fa-f:.]+\])"
    r"(?::[0-9]{1,5})?"
    r"(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?\Z"
)
OUTPUT_JSON = "file-b.json"
OUTPUT_SQL = "ledger-step7b.sql"


def require_text(inputs, field, limit=None):
    value = inputs[field]
    if not isinstance(value, str):
        raise Fail(f"inputs.{field}: expected string")
    units = len(value.encode("utf-16-le", "surrogatepass")) // 2
    if units < 1 or (limit is not None and units > limit):
        raise Fail(f"inputs.{field}: invalid string length")
    # Stricter than the Router schema: the ledger's jsonb cannot store NUL or
    # unpaired surrogates, and no reviewed identifier needs control characters.
    if any(ord(c) < 0x20 or ord(c) == 0x7F or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise Fail(f"inputs.{field}: control or unpaired surrogate character")
    return value


def require_digest(inputs, field):
    value = inputs[field]
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise Fail(f"inputs.{field}: expected lowercase 64-hex SHA-256")
    return value


def require_positive(inputs, field):
    value = inputs[field]
    if not check.positive(value):  # type(value) is int: rejects bool and float
        raise Fail(f"inputs.{field}: expected positive safe integer")
    return value


def iso_to_ms(value):
    if not isinstance(value, str):
        raise Fail("inputs.qualificationExpiresAt: expected ISO-8601 UTC string")
    match = ISO_UTC.fullmatch(value)
    if not match:
        raise Fail("inputs.qualificationExpiresAt: expected YYYY-MM-DDTHH:MM:SS[.fff]Z (UTC)")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        instant = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        raise Fail("inputs.qualificationExpiresAt: not a valid calendar instant") from None
    millis = int((match.group(7) or "").ljust(3, "0"))
    result = calendar.timegm(instant.timetuple()) * 1000 + millis
    if not 0 < result <= MAX_SAFE:
        raise Fail("inputs.qualificationExpiresAt: outside positive safe integer milliseconds")
    return result


def ms_to_iso(ms):
    seconds, millis = divmod(ms, 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{millis:03d}Z"


def build_policy(inputs, *, override=None, now_ms=None):
    """Validate the inputs and return (deployment_digest, policy with digest)."""
    if not isinstance(inputs, dict):
        raise Fail("inputs: expected JSON object")
    if inputs.keys() != INPUT_FIELDS:
        unknown, missing = sorted(inputs.keys() - INPUT_FIELDS), sorted(INPUT_FIELDS - inputs.keys())
        raise Fail(f"inputs: unknown fields {unknown}, missing fields {missing}")
    deployment = require_digest(inputs, "deployment_digest")
    if override is not None and not DIGEST.fullmatch(override):
        raise Fail("--deployment-digest-override: expected lowercase 64-hex SHA-256")
    if deployment != DEFAULT_DEPLOYMENT_DIGEST and deployment != override:
        raise Fail("inputs.deployment_digest: differs from File A's reader/client digest; "
                   "pass the same value as --deployment-digest-override to confirm")
    policy = {"version": 1}
    for field in ("routerReleaseDigest", "adapterDigest", "evidenceDigest"):
        policy[field] = require_digest(inputs, field)
    for field, limit in check.TEXT_LIMITS.items():
        policy[field] = require_text(inputs, field, limit)
    for field in ("providerGeneration", "maximumInputTokens", "maximumOutputTokens"):
        policy[field] = require_positive(inputs, field)
    if policy["maximumInputTokens"] + policy["maximumOutputTokens"] > MAX_SAFE:
        raise Fail("inputs: maximumInputTokens + maximumOutputTokens exceeds a safe integer")
    provider, transport = inputs["provider"], inputs["transport"]
    if provider not in PROVIDERS:
        raise Fail("inputs.provider: expected local, openai or anthropic")
    if transport not in TRANSPORTS:
        raise Fail("inputs.transport: expected chat-completions or anthropic-messages")
    # runtime.ts:123-124 and inference-budget/index.ts:44 drop any other pairing.
    if transport != ("anthropic-messages" if provider == "anthropic" else "chat-completions"):
        raise Fail("inputs.transport: does not match provider (anthropic uses anthropic-messages, others chat-completions)")
    policy["provider"], policy["transport"] = provider, transport
    base_url = require_text(inputs, "baseUrl")
    if not BASE_URL.fullmatch(base_url):
        raise Fail("inputs.baseUrl: expected an exact http(s) URL without whitespace, query or fragment")
    policy["baseUrl"] = base_url
    policy["qualificationExpiresAt"] = iso_to_ms(inputs["qualificationExpiresAt"])
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if policy["qualificationExpiresAt"] <= now_ms:
        raise Fail("inputs.qualificationExpiresAt: already elapsed")
    policy["outputIncludesReasoning"] = True
    policy["usageIncludesAllTokens"] = True
    policy["digest"] = check.policy_digest(policy)
    policy = {key: policy[key] for key in sorted(policy)}
    # The checker's own schema validation, independently of the code above.
    check.validate_policy(policy, "generated policy")
    return deployment, policy


def file_b_bytes(deployment, policy):
    document = {"version": 1, "qualifications": [
        {"deployment_digest": deployment, "digest": policy["digest"], "policy": policy}]}
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def sql_text(value):
    """A standard_conforming_strings literal; inputs contain no control characters."""
    if not isinstance(value, str) or "\x00" in value:
        raise Fail("internal: unsupported SQL literal")
    return "'" + value.replace("'", "''") + "'"


def canonical_sql(policy_expr, exclude_digest):
    # The Router's canonical() over a flat object (runtime.ts:22-26): keys in
    # UTF-16 order (= "C" byte order for these ASCII keys), compact members,
    # PostgreSQL's JSON scalar rendering (same escaping as JSON.stringify).
    where = " WHERE key <> 'digest'" if exclude_digest else ""
    return (f"(SELECT '{{' || string_agg(to_jsonb(key)::text || ':' || value::text, ',' ORDER BY key COLLATE \"C\")"
            f" || '}}' FROM jsonb_each({policy_expr}){where})")


def ledger_sql(deployment, policy):
    canonical = check.canonical(policy)
    digest, expires = policy["digest"], policy["qualificationExpiresAt"]
    c = FILE_A_CLIENT
    client_row = (
        f"({sql_text(c['login'])}::name, {sql_text(c['issuer'])}::text, {sql_text(c['connection_id'])}::uuid,\n"
        f"                 {sql_text(deployment)}::text,\n"
        f"                 ARRAY[{', '.join(sql_text(t) for t in c['tenant_ids'])}]::uuid[], "
        f"{c['max_tokens']}::bigint, {c['max_requests']}::bigint, {c['max_authority_ms']}::integer)")
    reader_row = f"({sql_text(FILE_A_READER_LOGIN)}::name, {sql_text(deployment)}::text)"
    row = f"({sql_text(deployment)}::text, {sql_text(digest)}::text, {sql_text(canonical)}::jsonb)"
    stored = "q.policy"
    return f"""-- Relay worker budget ledger, runbook step 7, FILE B: worker_budget.qualifications.
-- Generated by scripts/relay-worker-budget-file-b-generate.py from reviewed inputs; do not edit.
-- deployment_digest      {deployment}
-- policy digest          {digest}
-- qualificationExpiresAt {expires} ({ms_to_iso(expires)})
-- Run as OS/DB user postgres over the local socket; relay_budget_owner is NOLOGIN, hence SET LOCAL ROLE.
-- One transaction: any failed precondition or assertion aborts it and writes nothing.
-- Success = psql exit 0 AND a literal "COMMIT" line in the captured output.
-- A second run refuses (qualifications must be empty).
\\set ON_ERROR_STOP on
\\pset pager off
\\encoding UTF8
BEGIN;
SET LOCAL standard_conforming_strings = on;
SET LOCAL ROLE relay_budget_owner;
-- Held until COMMIT, taken BEFORE the preconditions: blocks a concurrent ready flip or
-- witness rotation and serializes a concurrent File B run; runtime reads are not blocked.
LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE worker_budget.qualifications IN SHARE ROW EXCLUSIVE MODE;

DO $pre$
BEGIN
  IF (SELECT count(*) FROM worker_budget.authority) <> 1 THEN
    RAISE EXCEPTION 'step 7B precondition failed: need exactly one authority row';
  END IF;
  IF (SELECT count(*) FROM worker_budget.clients) <> 1
     OR (SELECT count(*) FROM worker_budget.clients
          WHERE (login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms)
                IS NOT DISTINCT FROM
                {client_row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B precondition failed: clients must be exactly File A''s row';
  END IF;
  IF (SELECT count(*) FROM worker_budget.readers) <> 1
     OR (SELECT count(*) FROM worker_budget.readers
          WHERE (login, deployment_digest) IS NOT DISTINCT FROM
                {reader_row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B precondition failed: readers must be exactly File A''s row';
  END IF;
  IF EXISTS (SELECT 1 FROM worker_budget.qualifications) THEN
    RAISE EXCEPTION 'step 7B precondition failed: qualifications must be empty (already applied?)';
  END IF;
  IF {expires}::bigint <= floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint THEN
    RAISE EXCEPTION 'step 7B precondition failed: qualificationExpiresAt has elapsed on the ledger clock';
  END IF;
END
$pre$;

-- qualifications: the one reviewed policy, compact canonical JSON (sorted keys).
INSERT INTO worker_budget.qualifications (deployment_digest, digest, policy)
VALUES {row};

-- Post-insert assertions: whole-row, NULL-safe (IS NOT DISTINCT FROM), exactly one row; the stored
-- policy re-serialized in-database must equal the reviewed canonical text byte for byte, and the
-- SHA-256 of its canonical form without `digest` must equal both digests (Router tokenPolicyDigest).
DO $post$
BEGIN
  IF (SELECT count(*) FROM worker_budget.qualifications) <> 1
     OR (SELECT count(*) FROM worker_budget.qualifications
          WHERE (deployment_digest, digest, policy) IS NOT DISTINCT FROM
                {row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B assertion failed: qualifications row differs from the reviewed File B';
  END IF;
  IF (SELECT count(*) FROM worker_budget.qualifications q
       WHERE {canonical_sql(stored, False)} IS NOT DISTINCT FROM {sql_text(canonical)}::text
         AND encode(sha256(convert_to({canonical_sql(stored, True)}, 'UTF8')), 'hex') IS NOT DISTINCT FROM q.digest
         AND q.policy->>'digest' IS NOT DISTINCT FROM q.digest
         AND (SELECT count(*) FROM jsonb_object_keys(q.policy)) = {len(check.POLICY_FIELDS)}) <> 1 THEN
    RAISE EXCEPTION 'step 7B assertion failed: stored policy is not the reviewed canonical policy';
  END IF;
  IF (SELECT count(*) FROM worker_budget.authority) <> 1 THEN
    RAISE EXCEPTION 'step 7B assertion failed: authority row count changed';
  END IF;
END
$post$;

-- Readbacks, inside the transaction, before COMMIT. The first line beginning {{" is the stored
-- policy as compact canonical JSON; grep '^{{"' step7b.out must equal file-b.json's policy.
\\pset format unaligned
\\pset tuples_only on
SELECT {canonical_sql(stored, False)} FROM worker_budget.qualifications q;
SELECT 'deployment_digest=' || q.deployment_digest || ' digest=' || q.digest
       || ' recomputed=' || encode(sha256(convert_to({canonical_sql(stored, True)}, 'UTF8')), 'hex')
  FROM worker_budget.qualifications q;
\\pset tuples_only off
\\pset format aligned
SELECT ready, (SELECT count(*) FROM worker_budget.qualifications) AS qualifications FROM worker_budget.authority;
SELECT clock_timestamp() AT TIME ZONE 'UTC' AS ledger_clock_utc;   -- compare with the operator's `date -u` (NTP readback)
COMMIT;
"""


def write_once(path, data):
    """Create path with data; an identical existing file is accepted, a different one refused."""
    try:
        with open(path, "rb") as stream:
            existing = stream.read()
    except FileNotFoundError:
        existing = None
    except OSError:
        raise Fail(f"{path.name}: cannot read existing output") from None
    if existing is not None:
        if existing != data:
            raise Fail(f"{path.name}: exists with different content; refusing to overwrite")
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except OSError:
        raise Fail(f"{path.name}: cannot write output") from None


def generate(inputs, *, override=None, now_ms=None):
    """Return (policy, file-b.json bytes, ledger-step7b.sql bytes); writes nothing."""
    deployment, policy = build_policy(inputs, override=override, now_ms=now_ms)
    file_b = file_b_bytes(deployment, policy)
    # End-to-end: the checker's own parser and complete-table validation accept it.
    parsed = check.parse_json(file_b, "generated File B")
    check.exact_keys(parsed, {"version", "qualifications"}, "generated File B")
    rows = check.rows_by_key(parsed["qualifications"], "generated File B", validate=True)
    if parsed["version"] != 1 or rows != {(deployment, policy["digest"]): policy}:
        raise Fail("internal: generated File B does not round-trip through the checker")
    return policy, file_b, ledger_sql(deployment, policy).encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", required=True, help="JSON inputs file (see module docstring)")
    parser.add_argument("--output-dir", required=True, help="existing directory for file-b.json and ledger-step7b.sql")
    parser.add_argument("--deployment-digest-override", metavar="SHA256",
                        help="confirm a deployment_digest other than File A's " + DEFAULT_DEPLOYMENT_DIGEST)
    args = parser.parse_args(argv)
    try:
        inputs = check.read_json(args.inputs, "inputs")
        policy, file_b, sql = generate(inputs, override=args.deployment_digest_override)
        out = Path(args.output_dir)
        if not out.is_dir():
            raise Fail("--output-dir: not an existing directory")
        write_once(out / OUTPUT_JSON, file_b)
        write_once(out / OUTPUT_SQL, sql)
    except Fail as error:
        print(f"relay-worker-budget-file-b-generate: {error}", file=sys.stderr)
        return 1
    expires = policy["qualificationExpiresAt"]
    print(f"digest {policy['digest']}")
    print(f"qualificationExpiresAt {expires} {ms_to_iso(expires)}")
    print(f"readback-line-sha256 {hashlib.sha256(check.canonical(policy).encode()).hexdigest()}")
    print(f"sha256 {hashlib.sha256(file_b).hexdigest()}  {OUTPUT_JSON}")
    print(f"sha256 {hashlib.sha256(sql).hexdigest()}  {OUTPUT_SQL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
