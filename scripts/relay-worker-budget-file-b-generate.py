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

Supersede (reissue) mode: --supersede <old digest> --supersede-file <old file-b.json>.
The old file is the previously reviewed complete table, parsed like the checker's
--file-b; it must contain exactly one row with the old digest, under the inputs'
deployment_digest. Writes file-b.json (the NEW complete table: the old table without
the old row, plus the new row) and ledger-step7b-supersede.sql: one transaction that,
under the same locks, requires the ledger table to equal the old reviewed table
exactly and the old digest to be unreferenced by retained runtime rows, deletes exactly
the old row, inserts the new one and asserts the new table exactly. Use a new
--output-dir: the old file-b.json is never overwritten.
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
OUTPUT_SUPERSEDE_SQL = "ledger-step7b-supersede.sql"


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


def dollar_quote_blocks(text, names=("pre", "post")):
    """Close each DO block with a dollar-quote tag absent from its body.

    PostgreSQL ends a dollar-quoted body at the first matching tag, even one inside an
    embedded single-quoted literal, so a policy string containing e.g. `$post$` would
    otherwise end the block early. The blocks are marked with control-character
    sentinels, which validated inputs cannot contain.
    """
    for name in names:
        start, end = "\x01" + name + "-open\x01", "\x01" + name + "-close\x01"
        if text.count(start) != 1 or text.count(end) != 1:
            raise Fail("internal: DO block sentinel")
        head, rest = text.split(start)
        body, tail = rest.split(end)
        tag, n = name, 0
        while "$" + tag + "$" in body:
            n += 1
            tag = f"{name}{n}"
        text = f"{head}${tag}${body}${tag}${tail}"
    if "\x01" in text:
        raise Fail("internal: DO block sentinel")
    return text


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

DO \x01pre-open\x01
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
\x01pre-close\x01;

-- qualifications: the one reviewed policy, compact canonical JSON (sorted keys).
INSERT INTO worker_budget.qualifications (deployment_digest, digest, policy)
VALUES {row};

-- Post-insert assertions: whole-row, NULL-safe (IS NOT DISTINCT FROM), exactly one row; the stored
-- policy re-serialized in-database must equal the reviewed canonical text byte for byte, and the
-- SHA-256 of its canonical form without `digest` must equal both digests (Router tokenPolicyDigest).
DO \x01post-open\x01
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
\x01post-close\x01;

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


# Supersede mode (reissue). Relay d1c77f876606976362d70f42973033d31ac913c6,
# deploy/worker-budget-ledger: qualifications (001-authority.sql:32-37) is created by
# relay_budget_owner (001:7-9) and nothing references it by foreign key; 001-005 define no
# trigger, rule or row-level security, so its owner may DELETE. The only references to a
# policy digest are by value: every authorization request in the append-only outbox carries
# its sorted `policyDigests` (001:102,152; 005:110,168), and inspect() refuses an allocation
# whose policy row is gone (001:168-171), as does a renewal naming other digests
# (001:126-131). Supersede therefore refuses while any retained runtime row mentions the
# old digest; those tables are locked first so none can start to before COMMIT.
REFERENCE_TABLES = ("allocations", "outbox", "scope_runs", "allocation_scopes")


def qualification_values(rows):
    """VALUES rows (deployment_digest, digest, policy, canonical) for (key, policy) pairs."""
    lines = []
    for (deployment, digest), policy in rows:
        canonical = check.canonical(policy)
        lines.append(f"({sql_text(deployment)}::text, {sql_text(digest)}::text, "
                     f"{sql_text(canonical)}::jsonb, {sql_text(canonical)}::text)")
    return ",\n                       ".join(lines)


def table_equals_sql(rows):
    """True iff qualifications holds exactly these rows: whole-row NULL-safe and canonical text."""
    return (f"((SELECT count(*) FROM worker_budget.qualifications) = {len(rows)}\n"
            f"      AND (SELECT count(*) FROM worker_budget.qualifications q\n"
            f"             JOIN (VALUES {qualification_values(rows)})\n"
            f"                  AS r(deployment_digest, digest, policy, canonical)\n"
            f"               ON (q.deployment_digest, q.digest, q.policy) IS NOT DISTINCT FROM (r.deployment_digest, r.digest, r.policy)\n"
            f"              AND {canonical_sql('q.policy', False)} IS NOT DISTINCT FROM r.canonical) = {len(rows)})")


def supersede_sql(deployment, policy, old_rows, old_key):
    """One transaction: old table asserted exactly, old row deleted, new row inserted, new table asserted."""
    canonical = check.canonical(policy)
    digest, expires = policy["digest"], policy["qualificationExpiresAt"]
    old_policy = dict(old_rows)[old_key]
    new_rows = sorted([item for item in old_rows if item[0] != old_key] + [((deployment, digest), policy)],
                      key=lambda item: item[0])
    old_digest = old_key[1]
    c = FILE_A_CLIENT
    client_row = (
        f"({sql_text(c['login'])}::name, {sql_text(c['issuer'])}::text, {sql_text(c['connection_id'])}::uuid,\n"
        f"                 {sql_text(deployment)}::text,\n"
        f"                 ARRAY[{', '.join(sql_text(t) for t in c['tenant_ids'])}]::uuid[], "
        f"{c['max_tokens']}::bigint, {c['max_requests']}::bigint, {c['max_authority_ms']}::integer)")
    reader_row = f"({sql_text(FILE_A_READER_LOGIN)}::name, {sql_text(deployment)}::text)"
    row = f"({sql_text(deployment)}::text, {sql_text(digest)}::text, {sql_text(canonical)}::jsonb)"
    old_row = f"({sql_text(old_key[0])}::text, {sql_text(old_digest)}::text, {sql_text(check.canonical(old_policy))}::jsonb)"
    new_key = f"q.deployment_digest = {sql_text(deployment)} AND q.digest = {sql_text(digest)}"
    stored = "q.policy"
    references = "\n".join(
        f"  IF EXISTS (SELECT 1 FROM worker_budget.{table} t WHERE strpos(t::text, {sql_text(old_digest)}) > 0) THEN\n"
        f"    RAISE EXCEPTION 'step 7B supersede precondition failed: the old policy digest is referenced by "
        f"worker_budget.{table}; its rows are retained runtime history and deleting the policy would make "
        f"inspect()/renewal of that allocation refuse (qualification unavailable)';\n"
        f"  END IF;" for table in REFERENCE_TABLES)
    return f"""-- Relay worker budget ledger, runbook step 7, FILE B SUPERSEDE: reissue worker_budget.qualifications.
-- Generated by scripts/relay-worker-budget-file-b-generate.py --supersede from reviewed inputs; do not edit.
-- deployment_digest      {deployment}
-- superseded digest      {old_digest}
-- policy digest          {digest}
-- qualificationExpiresAt {expires} ({ms_to_iso(expires)})
-- rows before/after      {len(old_rows)}/{len(new_rows)}
-- Run as OS/DB user postgres over the local socket; relay_budget_owner is NOLOGIN, hence SET LOCAL ROLE.
-- relay_budget_owner owns qualifications (Relay d1c77f87 001-authority.sql:7-9,32-37); no FK,
-- trigger, rule or RLS stops its DELETE.
-- One transaction: any failed precondition or assertion aborts it and writes nothing.
-- Success = psql exit 0 AND a literal "COMMIT" line in the captured output.
-- A second run refuses (qualifications must equal the OLD reviewed File B).
\\set ON_ERROR_STOP on
\\pset pager off
\\encoding UTF8
BEGIN;
SET LOCAL standard_conforming_strings = on;
SET LOCAL ROLE relay_budget_owner;
-- Held until COMMIT, taken BEFORE the preconditions: blocks a concurrent ready flip or
-- witness rotation and serializes a concurrent File B run.
LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;
-- Supersede only, ACCESS EXCLUSIVE and in this order: qualifications first waits for any
-- transaction that already read it (an in-flight prepare() that saw the old digest) to commit,
-- so its allocation is visible to the reference check below; every later reader waits for this
-- COMMIT and sees the new table. The reference tables second, so such a prepare() can still take
-- its outbox/allocation locks and finish (no deadlock). Runtime reads pause only for the reissue.
LOCK TABLE worker_budget.qualifications IN ACCESS EXCLUSIVE MODE;
LOCK TABLE {', '.join('worker_budget.' + t for t in REFERENCE_TABLES)} IN ACCESS EXCLUSIVE MODE;

DO \x01pre-open\x01
BEGIN
  IF (SELECT count(*) FROM worker_budget.authority) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: need exactly one authority row';
  END IF;
  IF (SELECT count(*) FROM worker_budget.clients) <> 1
     OR (SELECT count(*) FROM worker_budget.clients
          WHERE (login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms)
                IS NOT DISTINCT FROM
                {client_row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: clients must be exactly File A''s row';
  END IF;
  IF (SELECT count(*) FROM worker_budget.readers) <> 1
     OR (SELECT count(*) FROM worker_budget.readers
          WHERE (login, deployment_digest) IS NOT DISTINCT FROM
                {reader_row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: readers must be exactly File A''s row';
  END IF;
  -- The whole table must be exactly the old reviewed File B (every row, whole-row, NULL-safe).
  IF NOT {table_equals_sql(old_rows)} THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: qualifications must be exactly the old reviewed File B (already superseded, or changed?)';
  END IF;
{references}
  IF {sql_text(digest)} = {sql_text(old_digest)} THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: the new digest equals the superseded digest';
  END IF;
  IF {expires}::bigint <= floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint THEN
    RAISE EXCEPTION 'step 7B supersede precondition failed: qualificationExpiresAt has elapsed on the ledger clock';
  END IF;
END
\x01pre-close\x01;

-- Exactly the old reviewed row, matched whole-row.
DO \x01del-open\x01
DECLARE deleted bigint;
BEGIN
  DELETE FROM worker_budget.qualifications
   WHERE (deployment_digest, digest, policy) IS NOT DISTINCT FROM
         {old_row};
  GET DIAGNOSTICS deleted = ROW_COUNT;
  IF deleted <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: expected to delete exactly one old row';
  END IF;
END
\x01del-close\x01;

-- qualifications: the new reviewed policy, compact canonical JSON (sorted keys).
INSERT INTO worker_budget.qualifications (deployment_digest, digest, policy)
VALUES {row};

-- Post-insert assertions: the new row whole-row, NULL-safe, exactly once; its stored policy
-- re-serialized in-database equals the reviewed canonical text and its SHA-256 without `digest`
-- equals both digests (Router tokenPolicyDigest); the old row is gone; the whole table is
-- exactly the new reviewed File B.
DO \x01post-open\x01
BEGIN
  IF (SELECT count(*) FROM worker_budget.qualifications q
       WHERE (q.deployment_digest, q.digest, q.policy) IS NOT DISTINCT FROM
             {row}) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: qualifications row differs from the reviewed File B';
  END IF;
  IF (SELECT count(*) FROM worker_budget.qualifications q
       WHERE {new_key}
         AND {canonical_sql(stored, False)} IS NOT DISTINCT FROM {sql_text(canonical)}::text
         AND encode(sha256(convert_to({canonical_sql(stored, True)}, 'UTF8')), 'hex') IS NOT DISTINCT FROM q.digest
         AND q.policy->>'digest' IS NOT DISTINCT FROM q.digest
         AND (SELECT count(*) FROM jsonb_object_keys(q.policy)) = {len(check.POLICY_FIELDS)}) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: stored policy is not the reviewed canonical policy';
  END IF;
  IF EXISTS (SELECT 1 FROM worker_budget.qualifications q WHERE q.digest = {sql_text(old_digest)}) THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: the superseded row is still present';
  END IF;
  IF NOT {table_equals_sql(new_rows)} THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: qualifications is not exactly the new reviewed File B';
  END IF;
  IF (SELECT count(*) FROM worker_budget.authority) <> 1 THEN
    RAISE EXCEPTION 'step 7B supersede assertion failed: authority row count changed';
  END IF;
END
\x01post-close\x01;

-- Readbacks, inside the transaction, before COMMIT. The first line beginning {{" is the NEW
-- stored policy as compact canonical JSON; grep '^{{"' must equal file-b.json's new policy.
\\pset format unaligned
\\pset tuples_only on
SELECT {canonical_sql(stored, False)} FROM worker_budget.qualifications q WHERE {new_key};
SELECT 'deployment_digest=' || q.deployment_digest || ' digest=' || q.digest
       || ' recomputed=' || encode(sha256(convert_to({canonical_sql(stored, True)}, 'UTF8')), 'hex')
  FROM worker_budget.qualifications q ORDER BY q.deployment_digest, q.digest;
SELECT 'superseded=' || {sql_text(old_digest)} || ' present=' || count(*) FROM worker_budget.qualifications q
 WHERE q.digest = {sql_text(old_digest)};
\\pset tuples_only off
\\pset format aligned
SELECT ready, (SELECT count(*) FROM worker_budget.qualifications) AS qualifications FROM worker_budget.authority;
SELECT clock_timestamp() AT TIME ZONE 'UTC' AS ledger_clock_utc;   -- compare with the operator's `date -u` (NTP readback)
COMMIT;
"""


def table_bytes(rows):
    """file-b.json for (key, policy) pairs, rows ordered like the checker's ledger readback."""
    document = {"version": 1, "qualifications": [
        {"deployment_digest": deployment, "digest": digest, "policy": policy}
        for (deployment, digest), policy in sorted(rows, key=lambda item: item[0])]}
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def parse_old_file_b(raw, old_digest):
    """The previously reviewed complete table, parsed like the checker's --file-b; returns (rows, old key)."""
    if not isinstance(old_digest, str) or not DIGEST.fullmatch(old_digest):
        raise Fail("--supersede: expected lowercase 64-hex SHA-256")
    document = check.parse_json(raw, "--supersede-file")
    check.exact_keys(document, {"version", "qualifications"}, "--supersede-file")
    if type(document["version"]) is not int or document["version"] != 1:
        raise Fail("--supersede-file.version: expected 1")
    rows = check.rows_by_key(document["qualifications"], "--supersede-file", validate=True)
    keys = [key for key in rows if key[1] == old_digest]
    if len(keys) != 1:
        raise Fail(f"--supersede-file: contains {len(keys)} rows with the --supersede digest (expected exactly 1)")
    return sorted(rows.items()), keys[0]


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


def generate(inputs, *, override=None, now_ms=None, supersede=None):
    """Return (policy, file-b.json bytes, SQL bytes); writes nothing.

    supersede: None (normal mode, ledger-step7b.sql) or (old digest, old file-b.json bytes),
    which selects supersede mode (ledger-step7b-supersede.sql) and makes file-b.json the new
    complete table: the old reviewed table without the old row, plus the new row.
    """
    deployment, policy = build_policy(inputs, override=override, now_ms=now_ms)
    if supersede is None:
        expected = {(deployment, policy["digest"]): policy}
        file_b = file_b_bytes(deployment, policy)
    else:
        old_digest, old_raw = supersede
        old_rows, old_key = parse_old_file_b(old_raw, old_digest)
        if policy["digest"] == old_digest:
            raise Fail("--supersede: the new policy digest equals the superseded digest (unchanged readings?)")
        if old_key[0] != deployment:
            raise Fail("--supersede-file: the superseded row belongs to another deployment_digest than the inputs")
        if (deployment, policy["digest"]) in dict(old_rows):
            raise Fail("--supersede-file: already contains the new policy digest")
        expected = dict(item for item in old_rows if item[0] != old_key)
        expected[(deployment, policy["digest"])] = policy
        file_b = table_bytes(expected.items())
    # End-to-end: the checker's own parser and complete-table validation accept it.
    parsed = check.parse_json(file_b, "generated File B")
    check.exact_keys(parsed, {"version", "qualifications"}, "generated File B")
    rows = check.rows_by_key(parsed["qualifications"], "generated File B", validate=True)
    if parsed["version"] != 1 or rows != expected:
        raise Fail("internal: generated File B does not round-trip through the checker")
    if supersede is None:
        return policy, file_b, dollar_quote_blocks(ledger_sql(deployment, policy)).encode("utf-8")
    sql = supersede_sql(deployment, policy, old_rows, old_key)
    return policy, file_b, dollar_quote_blocks(sql, ("pre", "del", "post")).encode("utf-8")


class Once(argparse.Action):
    """An option that may be given at most once (no silent last-one-wins)."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f"{option_string} given more than once")
        setattr(namespace, self.dest, values)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--inputs", required=True, help="JSON inputs file (see module docstring)")
    parser.add_argument("--output-dir", required=True, help="existing directory for file-b.json and ledger-step7b.sql")
    parser.add_argument("--deployment-digest-override", metavar="SHA256",
                        help="confirm a deployment_digest other than File A's " + DEFAULT_DEPLOYMENT_DIGEST)
    parser.add_argument("--supersede", metavar="SHA256", action=Once,
                        help="reissue: replace the committed row with this policy digest (needs --supersede-file)")
    parser.add_argument("--supersede-file", metavar="FILE_B_JSON", action=Once,
                        help="the previously reviewed complete table (old file-b.json) the ledger must equal")
    args = parser.parse_args(argv)
    if (args.supersede is None) != (args.supersede_file is None):
        parser.error("--supersede and --supersede-file must be given together")
    sql_name, old_sha256 = OUTPUT_SQL, None
    try:
        supersede = None
        if args.supersede is not None:
            try:
                with open(args.supersede_file, "rb") as stream:
                    old_raw = stream.read(check.MAX_BYTES + 1)
            except OSError:
                raise Fail("--supersede-file: cannot read input") from None
            supersede, sql_name = (args.supersede, old_raw), OUTPUT_SUPERSEDE_SQL
            old_sha256 = hashlib.sha256(old_raw).hexdigest()
        inputs = check.read_json(args.inputs, "inputs")
        policy, file_b, sql = generate(inputs, override=args.deployment_digest_override, supersede=supersede)
        out = Path(args.output_dir)
        if not out.is_dir():
            raise Fail("--output-dir: not an existing directory")
        write_once(out / OUTPUT_JSON, file_b)
        write_once(out / sql_name, sql)
    except Fail as error:
        print(f"relay-worker-budget-file-b-generate: {error}", file=sys.stderr)
        return 1
    expires = policy["qualificationExpiresAt"]
    if old_sha256 is not None:
        print(f"superseded-digest {args.supersede}")
        print(f"superseded-file-b-sha256 {old_sha256}")
    print(f"digest {policy['digest']}")
    print(f"qualificationExpiresAt {expires} {ms_to_iso(expires)}")
    print(f"readback-line-sha256 {hashlib.sha256(check.canonical(policy).encode()).hexdigest()}")
    print(f"sha256 {hashlib.sha256(file_b).hexdigest()}  {OUTPUT_JSON}")
    print(f"sha256 {hashlib.sha256(sql).hexdigest()}  {sql_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
