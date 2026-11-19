"""Offline File B checks. All database/Router interactions are fakes."""
import base64
import contextlib
import copy
import hashlib
import http.server
import importlib.util
import io
import json
import locale
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("file_b_check", ROOT / "scripts/relay-worker-budget-file-b-check.py")
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)
NOW = 1800000000000
DSN = "postgresql://reader:private-password@ledger.invalid/relay_budget?sslmode=verify-full"


# Golden vectors generated independently from llm-router 5a5181b7:
# git show 5a5181b7:packages/plugins/worker-budget-authority/runtime.ts
# copied line 1 (createHash import) and lines 22-30 verbatim into a scratch .ts
# outside this repo, then called canonical(policy) and tokenPolicyDigest(policy)
# with Node v22.23.5 using
# /workspace/c4/relay-pin-c0fe50f0-20261117/node_modules/.bin/tsx.
# Input was JSON containing these literal fixtures without a digest field.
# The nested probe deliberately extends the policy only to exercise recursive
# ordering (including UTF-16 vs code-point order), arrays and integer formatting;
# it is not a valid ReviewedTokenPolicy and must still fail strict validation.
ROUTER_POLICY = {
    "version": 1,
    "routerReleaseDigest": "1111111111111111111111111111111111111111111111111111111111111111",
    "adapterDigest": "2222222222222222222222222222222222222222222222222222222222222222",
    "accountId": "café-東京",
    "deploymentId": "route-🚀",
    "model": 'qwen-"quoted"\\path\n\t\b\f\r\x00\x1f\u2028\u2029\ud800',
    "provider": "local",
    "providerGeneration": 9007199254740991,
    "baseUrl": "https://fixture.invalid/v1",
    "transport": "chat-completions",
    "maximumInputTokens": 1000000000000,
    "maximumOutputTokens": 50,
    "outputIncludesReasoning": True,
    "usageIncludesAllTokens": True,
    "qualificationExpiresAt": 1800003600000,
    "evidenceDigest": "3333333333333333333333333333333333333333333333333333333333333333",
}
ROUTER_NESTED_POLICY = dict(ROUTER_POLICY, canonicalProbe={
    "z": [{"z": 0, "a": -42}, None, False],
    "\ue000": "BMP",
    "🚀": "astral",
    "a": {"10": "ten", "2": "two", "A": "\udfff"},
})
ROUTER_POLICY_CANONICAL = (
    '{"accountId":"café-東京",'
    '"adapterDigest":"2222222222222222222222222222222222222222222222222222222222222222",'
    '"baseUrl":"https://fixture.invalid/v1",'
    '"deploymentId":"route-🚀",'
    '"evidenceDigest":"3333333333333333333333333333333333333333333333333333333333333333",'
    '"maximumInputTokens":1000000000000,'
    '"maximumOutputTokens":50,'
    '"model":"qwen-\\"quoted\\"\\\\path\\n\\t\\b\\f\\r\\u0000\\u001f\u2028\u2029\\ud800",'
    '"outputIncludesReasoning":true,'
    '"provider":"local",'
    '"providerGeneration":9007199254740991,'
    '"qualificationExpiresAt":1800003600000,'
    '"routerReleaseDigest":"1111111111111111111111111111111111111111111111111111111111111111",'
    '"transport":"chat-completions",'
    '"usageIncludesAllTokens":true,'
    '"version":1}'
)
ROUTER_POLICY_DIGEST = 'a9d43e3e427d71faddbb722294c8f343bb7f35ef1b532d05361075f9cdfaa552'
ROUTER_NESTED_CANONICAL = (
    '{"accountId":"café-東京",'
    '"adapterDigest":"2222222222222222222222222222222222222222222222222222222222222222",'
    '"baseUrl":"https://fixture.invalid/v1",'
    '"canonicalProbe":{"a":{"10":"ten",'
    '"2":"two",'
    '"A":"\\udfff"},'
    '"z":[{"a":-42,'
    '"z":0},null,false],'
    '"🚀":"astral",'
    '"\ue000":"BMP"},'
    '"deploymentId":"route-🚀",'
    '"evidenceDigest":"3333333333333333333333333333333333333333333333333333333333333333",'
    '"maximumInputTokens":1000000000000,'
    '"maximumOutputTokens":50,'
    '"model":"qwen-\\"quoted\\"\\\\path\\n\\t\\b\\f\\r\\u0000\\u001f\u2028\u2029\\ud800",'
    '"outputIncludesReasoning":true,'
    '"provider":"local",'
    '"providerGeneration":9007199254740991,'
    '"qualificationExpiresAt":1800003600000,'
    '"routerReleaseDigest":"1111111111111111111111111111111111111111111111111111111111111111",'
    '"transport":"chat-completions",'
    '"usageIncludesAllTokens":true,'
    '"version":1}'
)
ROUTER_NESTED_DIGEST = 'e88b5bbe1c39a6a704e2f408279eb9555b0a9f3c2a2db8fec5510546007a2852'


def fixtures(**changes):
    # Synthetic test data, never a qualifying operator policy.
    policy = dict(version=1, routerReleaseDigest="1" * 64, adapterDigest="2" * 64,
                  accountId="fixture", deploymentId="fixture", model="fixture",
                  provider="local", providerGeneration=1, baseUrl="https://fixture.invalid/v1",
                  transport="chat-completions", maximumInputTokens=100, maximumOutputTokens=50,
                  outputIncludesReasoning=True, usageIncludesAllTokens=True,
                  qualificationExpiresAt=NOW + 3600000, evidenceDigest="3" * 64)
    policy.update(changes)
    policy["digest"] = check.policy_digest(policy)
    file_b = {"version": 1, "qualifications": [dict(deployment_digest="4" * 64,
                                                  digest=policy["digest"], policy=policy)]}
    snapshot = dict(version=1, readOnly=True, observedAt=NOW,
                    qualifications=copy.deepcopy(file_b["qualifications"]))
    return file_b, snapshot


class LedgerChecks(unittest.TestCase):
    def run_check(self, file_b, snapshot, seconds=60):
        return check.check_ledger(file_b, snapshot, seconds, NOW)

    def test_pass(self):
        result = self.run_check(*fixtures())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["requiredUntil"], NOW + 60000)

    def test_all_policy_mismatch_classes(self):
        # Every release/evidence digest, route identity, provider generation,
        # bound, coverage flag, version and lifetime must match exactly.
        for field in check.POLICY_FIELDS:
            with self.subTest(field=field):
                file_b, snapshot = fixtures()
                stored = snapshot["qualifications"][0]["policy"]
                value = stored[field]
                stored[field] = (not value if type(value) is bool else value + 1 if type(value) is int else value + "x")
                with self.assertRaisesRegex(check.Fail, field + " mismatch"):
                    self.run_check(file_b, snapshot)

    def test_key_digest_and_deployment_mismatch(self):
        for field in ("digest", "deployment_digest"):
            with self.subTest(field=field):
                file_b, snapshot = fixtures()
                snapshot["qualifications"][0][field] = "9" * 64
                with self.assertRaisesRegex(check.Fail, "1 missing, 1 extra"):
                    self.run_check(file_b, snapshot)

    def test_missing_row(self):
        file_b, snapshot = fixtures()
        snapshot["qualifications"] = []
        with self.assertRaisesRegex(check.Fail, "1 missing, 0 extra"):
            self.run_check(file_b, snapshot)

    def test_extra_row_in_other_deployment(self):
        file_b, snapshot = fixtures()
        row = copy.deepcopy(snapshot["qualifications"][0])
        row["deployment_digest"] = "9" * 64
        snapshot["qualifications"].append(row)
        with self.assertRaisesRegex(check.Fail, "0 missing, 1 extra"):
            self.run_check(file_b, snapshot)

    def test_duplicate_rows(self):
        for source in (0, 1):
            with self.subTest(source=source):
                values = fixtures()
                values[source]["qualifications"] *= 2
                with self.assertRaisesRegex(check.Fail, "duplicate qualification"):
                    self.run_check(*values)

    def test_expired(self):
        with self.assertRaisesRegex(check.Fail, "qualification expired"):
            self.run_check(*fixtures(qualificationExpiresAt=NOW))

    def test_lifetime_too_short(self):
        with self.assertRaisesRegex(check.Fail, "insufficient lifetime"):
            self.run_check(*fixtures(qualificationExpiresAt=NOW + 59999))

    def test_lifetime_boundary(self):
        self.assertEqual(self.run_check(*fixtures(qualificationExpiresAt=NOW + 60000))["status"], "pass")

    def test_lifetime_uses_later_clock(self):
        file_b, snapshot = fixtures(qualificationExpiresAt=NOW + 60000)
        snapshot["observedAt"] += 1
        with self.assertRaisesRegex(check.Fail, "insufficient lifetime"):
            self.run_check(file_b, snapshot)

    def test_invalid_lifetime(self):
        for seconds in (0, -1, 604801, True, 1.5):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(check.Fail, "required lifetime"):
                self.run_check(*fixtures(), seconds=seconds)

    def test_stale_or_future_readback(self):
        for delta in (-15001, 15001):
            file_b, snapshot = fixtures()
            snapshot["observedAt"] += delta
            with self.assertRaisesRegex(check.Fail, "stale readback or clock disagreement"):
                self.run_check(file_b, snapshot)

    def test_read_only_receipt_required(self):
        file_b, snapshot = fixtures()
        snapshot["readOnly"] = False
        with self.assertRaisesRegex(check.Fail, "read-only snapshot"):
            self.run_check(file_b, snapshot)

    def test_schema_and_empty_manifest(self):
        for bad in ({}, {"version": 1, "qualifications": []},
                    {"version": True, "qualifications": []}, {"version": 1, "qualifications": "bad"}):
            with self.subTest(bad=bad), self.assertRaises(check.Fail):
                self.run_check(bad, fixtures()[1])

    def test_row_digest_must_equal_policy_digest(self):
        file_b, snapshot = fixtures()
        file_b["qualifications"][0]["digest"] = "0" * 64
        with self.assertRaisesRegex(check.Fail, "differs from policy.digest"):
            self.run_check(file_b, snapshot)

    def test_stored_missing_or_extra_policy_field(self):
        for extra in (True, False):
            file_b, snapshot = fixtures()
            policy = snapshot["qualifications"][0]["policy"]
            if extra:
                policy["unknown"] = True
            else:
                del policy["baseUrl"]
            with self.assertRaisesRegex(check.Fail, "unexpected or missing fields"):
                self.run_check(file_b, snapshot)

    def test_json_order_does_not_matter(self):
        file_b, snapshot = fixtures()
        snapshot["qualifications"][0]["policy"] = dict(reversed(list(snapshot["qualifications"][0]["policy"].items())))
        self.assertEqual(self.run_check(file_b, snapshot)["status"], "pass")

    def test_multiple_rows_and_deployments_ignore_row_order(self):
        file_b, snapshot = fixtures()
        other = fixtures(model="second-fixture")[0]["qualifications"][0]
        other["deployment_digest"] = "8" * 64
        file_b["qualifications"].append(other)
        snapshot["qualifications"].insert(0, copy.deepcopy(other))
        self.assertEqual(self.run_check(file_b, snapshot)["rows"], 2)


class PolicyChecks(unittest.TestCase):
    def test_router_golden_vectors(self):
        for policy, expected, digest in (
            (ROUTER_POLICY, ROUTER_POLICY_CANONICAL, ROUTER_POLICY_DIGEST),
            (ROUTER_NESTED_POLICY, ROUTER_NESTED_CANONICAL, ROUTER_NESTED_DIGEST),
        ):
            with self.subTest(digest=digest):
                self.assertEqual(check.canonical(policy).encode("utf-8"), expected.encode("utf-8"))
                self.assertEqual(check.policy_digest(policy), digest)
                self.assertEqual(check.policy_digest(dict(policy, digest=digest)), digest)
        check.validate_policy(dict(ROUTER_POLICY, digest=ROUTER_POLICY_DIGEST), "golden policy")
        with self.assertRaisesRegex(check.Fail, "unexpected or missing fields"):
            check.validate_policy(dict(ROUTER_NESTED_POLICY, digest=ROUTER_NESTED_DIGEST), "nested probe")

    def test_invalid_policy_values(self):
        cases = [("version", True), ("routerReleaseDigest", "A" * 64), ("adapterDigest", "bad"),
                 ("evidenceDigest", "bad"), ("accountId", ""), ("deploymentId", "x" * 129),
                 ("model", "x" * 257), ("provider", "unknown"), ("providerGeneration", 0),
                 ("maximumInputTokens", True), ("maximumOutputTokens", 1.5),
                 ("maximumOutputTokens", check.MAX_SAFE), ("baseUrl", "relative/path"),
                 ("baseUrl", "https://[bad"), ("baseUrl", "https://"),
                 ("baseUrl", "https://fixture.invalid:99999"), ("baseUrl", "https://fixture.invalid/\x00"),
                 ("qualificationExpiresAt", check.MAX_SAFE + 1),
                 ("outputIncludesReasoning", False), ("usageIncludesAllTokens", 1),
                 ("transport", "anthropic-messages")]
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(check.Fail):
                file_b, _ = fixtures(**{field: value})
                check.validate_policy(file_b["qualifications"][0]["policy"], "policy")

    def test_canonical_digest_recomputed(self):
        policy = fixtures()[0]["qualifications"][0]["policy"]
        policy["maximumInputTokens"] += 1
        with self.assertRaisesRegex(check.Fail, "canonical tokenPolicyDigest mismatch"):
            check.validate_policy(policy, "policy")

    def test_canonical_unicode_and_escaping(self):
        raw = {"z": "\U0001f680\ud800\n\"\\", "a": 42}
        expected = '{"a":42,"z":"\U0001f680\\ud800\\n\\\"\\\\"}'
        self.assertEqual(check.canonical(raw), expected)
        self.assertEqual(check.policy_digest(raw), hashlib.sha256(expected.encode()).hexdigest())

    def test_strict_json(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaisesRegex(check.Fail, "invalid"):
                check.parse_json(raw, "test")


class TransportChecks(unittest.TestCase):
    def test_psql_is_fixed_and_credentials_stay_out_of_argv(self):
        snapshot = fixtures()[1]
        with patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(snapshot), "")) as run:
            self.assertEqual(check.read_ledger(DSN), snapshot)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["psql", "-X", "-qAt", "-w", "-v", "ON_ERROR_STOP=1", "-f", "-"])
        self.assertNotIn("private-password", str(args))
        self.assertNotIn("private-password", kwargs["input"])
        self.assertEqual(kwargs["env"]["PGPASSWORD"], "private-password")
        self.assertEqual(kwargs["env"]["PGSSLMODE"], "verify-full")
        self.assertEqual(kwargs["env"]["PGPASSFILE"], os.devnull)
        self.assertEqual(kwargs["env"]["PGCLIENTENCODING"], "UTF8")
        self.assertEqual(kwargs["encoding"], "utf-8")

    def test_psql_utf8_readback_ignores_locale(self):
        snapshot = fixtures(accountId="café-東京", model="qwen-🚀")[1]
        payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        run_child = subprocess.run

        def fake_psql(argv, **kwargs):
            return run_child([sys.executable, "-c", "import sys; sys.stdout.buffer.write(" + repr(payload) + ")"], **kwargs)

        # A Latin-1 locale must not affect the explicit UTF-8 decode. Patch both locale hooks
        # (getencoding exists only on Python 3.11+, so create it where missing).
        with patch.object(locale, "getencoding", create=True, return_value="iso8859-1"), \
                patch.object(locale, "getpreferredencoding", return_value="iso8859-1"), \
                patch.object(check.subprocess, "run", side_effect=fake_psql):
            self.assertEqual(check.read_ledger(DSN), snapshot)

    def test_sql_statements_are_read_only_and_have_no_mutating_calls(self):
        # Parse the small fixed SQL subset: quoted strings cannot hide separators.
        clean = re.sub(r"'(?:[^']|'')*'", "''", check.LEDGER_SQL)
        statements = [s.strip() for s in clean.split(";") if s.strip()]
        self.assertEqual(len(statements), 6)
        self.assertEqual(statements[0], "BEGIN READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertRegex(statements[1], r"^SET LOCAL statement_timeout = ''$")
        self.assertRegex(statements[2], r"^SET LOCAL lock_timeout = ''$")
        self.assertEqual(statements[3], "SET LOCAL search_path = pg_catalog")
        query = statements[4]
        self.assertTrue(query.startswith("SELECT "))
        self.assertNotRegex(query.upper(), r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|CALL|COPY|INTO|LOCK|FOR|WITH)\b")
        self.assertEqual(set(re.findall(r"\b([a-z_][a-z_0-9]*)\s*\(", query.lower())),
                         {"jsonb_build_object", "current_setting", "floor", "extract", "statement_timestamp", "coalesce", "jsonb_agg"})
        self.assertEqual(re.findall(r"worker_budget\.\w+", query), ["worker_budget.qualifications"])
        self.assertNotIn("WHERE", query.upper())

    def test_database_failure_and_timeout_are_redacted(self):
        fake = subprocess.CompletedProcess([], 2, "private-password", "private-password")
        with patch.object(check.subprocess, "run", return_value=fake), self.assertRaisesRegex(check.Fail, "psql failed") as error:
            check.read_ledger(DSN)
        self.assertNotIn("private-password", str(error.exception))
        with patch.object(check.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-password", 15)), self.assertRaisesRegex(check.Fail, "timed out"):
            check.read_ledger(DSN)

    def test_dsn_validation_redacts_and_requires_tls(self):
        for dsn in ("private-password", "postgresql://u:private-password@h/d", DSN + "&options=private-password"):
            with self.subTest(dsn=dsn), self.assertRaises(check.Fail) as error:
                check.dsn_environment(dsn)
            self.assertNotIn("private-password", str(error.exception))

    def test_socket_dsn(self):
        env = check.dsn_environment("postgresql://postgres@%2Fvar%2Frun%2Fpostgresql/relay_budget")
        self.assertEqual(env["PGHOST"], "/var/run/postgresql")

    def test_router_refusal_not_skippable(self):
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge), self.assertRaisesRegex(check.Fail, "refused"):
                check.check_router(acknowledge, preflight=lambda: (_ for _ in ()).throw(check.Fail("Router: refused")))

    def test_router_unreachable_not_skippable(self):
        for acknowledge in (False, True):
            with self.subTest(acknowledge=acknowledge), self.assertRaisesRegex(check.Fail, "unreachable") as error:
                check.check_router(acknowledge, preflight=lambda: (_ for _ in ()).throw(OSError("private-password")))
            self.assertNotIn("private-password", str(error.exception))

    def test_router_unreviewed_success_is_refused(self):
        with self.assertRaisesRegex(check.Fail, "no reviewed zero-write response"):
            check.check_router(True, preflight=lambda: {"status": "pass"})


class CommandChecks(unittest.TestCase):
    def invoke(self, acknowledge=False, *, snapshot=None, human=False):
        file_b, default_snapshot = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file-b.json"
            path.write_text(json.dumps(file_b))
            argv = ["--file-b", str(path), "--required-lifetime-seconds", "60", "--ledger-dsn-env", "FILE_B_TEST_DSN"]
            if not human:
                argv.append("--json")
            if acknowledge:
                argv.append("--skip-router-preflight-unavailable")
            out = io.StringIO()
            with patch.dict(os.environ, FILE_B_TEST_DSN=DSN), patch.object(check, "read_ledger", return_value=snapshot or default_snapshot), patch.object(check.time, "time_ns", return_value=NOW * 1000000), contextlib.redirect_stdout(out):
                rc = check.main(argv)
            return rc, out.getvalue() if human else json.loads(out.getvalue())

    def test_router_unavailable_without_acknowledgement(self):
        rc, report = self.invoke()
        self.assertEqual(rc, 1)
        self.assertEqual(report["ledger"]["status"], "pass")
        self.assertEqual(report["router"]["status"], "unavailable")
        self.assertFalse(report["router"]["acknowledged"])
        self.assertFalse(report["signingReady"])

    def test_ledger_only_acknowledgement_exits_two_not_signable(self):
        rc, report = self.invoke(acknowledge=True)
        self.assertEqual(rc, 2)
        self.assertEqual(report["status"], "ledger-only")
        self.assertTrue(report["router"]["acknowledged"])
        self.assertFalse(report["signingReady"])

    def test_acknowledgement_does_not_hide_ledger_failure(self):
        snapshot = fixtures()[1]
        snapshot["qualifications"] = []
        rc, report = self.invoke(acknowledge=True, snapshot=snapshot)
        self.assertEqual(rc, 1)
        self.assertIn("missing", report["error"])

    def test_router_refusal_exits_nonzero(self):
        with patch.object(check, "router_preflight", side_effect=check.Fail("Router: refused")):
            rc, report = self.invoke(acknowledge=True)
        self.assertEqual(rc, 1)
        self.assertEqual(report["error"], "Router: refused")

    def test_unexpected_errors_are_redacted_and_fail_with_or_without_acknowledgement(self):
        for stage in ("check_ledger", "check_router"):
            for kind in (KeyError, TypeError, RuntimeError):
                for acknowledge in (False, True):
                    for human in (False, True):
                        with self.subTest(stage=stage, kind=kind, acknowledge=acknowledge, human=human):
                            err = io.StringIO()
                            with patch.object(check, stage, side_effect=kind("private-password")), contextlib.redirect_stderr(err):
                                rc, report = self.invoke(acknowledge=acknowledge, human=human)
                            self.assertEqual(rc, 1)
                            self.assertEqual(err.getvalue(), "")
                            self.assertNotIn("private-password", str(report))
                            self.assertNotIn("Traceback", str(report))
                            message = "File B: unexpected verification error; diagnostics suppressed"
                            if human:
                                self.assertIn(message, report)
                                self.assertIn("File B: fail", report)
                            else:
                                self.assertEqual(report["error"], message)
                                self.assertEqual(report["status"], "fail")
                                self.assertFalse(report["signingReady"])

    def test_invalid_arguments_exit_one_and_are_redacted(self):
        for args in (["--unknown=private-password"], ["--required-lifetime-seconds", "private-password"],
                     ["--file-b"], ["--ledger-json", "-", "--ledger-dsn-env", "private-password"]):
            with self.subTest(args=args):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = check.main(["--json"] + args)
                self.assertEqual(rc, 1)
                self.assertEqual(json.loads(out.getvalue())["error"], "arguments: invalid command line; see --help")
                self.assertNotIn("private-password", out.getvalue())
                self.assertEqual(err.getvalue(), "")

    def test_print_sql_never_connects(self):
        out = io.StringIO()
        with patch.object(check.subprocess, "run", side_effect=AssertionError("unexpected connection")), contextlib.redirect_stdout(out):
            self.assertEqual(check.main(["--print-ledger-sql"]), 0)
        self.assertEqual(out.getvalue(), check.LEDGER_SQL)

    def test_stdin_readback_and_human_output(self):
        file_b, snapshot = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file-b.json"
            path.write_text(json.dumps(file_b))
            out = io.StringIO()
            with io.TextIOWrapper(io.BytesIO(json.dumps(snapshot).encode())) as stdin, patch.object(check.sys, "stdin", stdin), patch.object(check.time, "time_ns", return_value=NOW * 1000000), patch.object(check.subprocess, "run", side_effect=AssertionError("unexpected connection")), contextlib.redirect_stdout(out):
                rc = check.main(["--file-b", str(path), "--required-lifetime-seconds", "60", "--ledger-json", "-"])
            self.assertEqual(rc, 1)
            self.assertIn("ledger=pass; signingReady=false", out.getvalue())
            self.assertIn("durably consumes a nonce", out.getvalue())

    def test_dsn_file_input_and_json_file_readback(self):
        file_b, snapshot = fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            path, secret, readback = (Path(tmp) / name for name in ("file-b.json", "dsn", "readback.json"))
            path.write_text(json.dumps(file_b))
            secret.write_text(DSN)
            readback.write_text(json.dumps(snapshot))
            for source in (["--ledger-dsn-file", str(secret)], ["--ledger-json", str(readback)]):
                with self.subTest(source=source):
                    out = io.StringIO()
                    with patch.object(check, "read_ledger", return_value=snapshot) as read, patch.object(check.time, "time_ns", return_value=NOW * 1000000), contextlib.redirect_stdout(out):
                        rc = check.main(["--file-b", str(path), "--required-lifetime-seconds", "60", "--skip-router-preflight-unavailable", "--json"] + source)
                    self.assertEqual(rc, 2)
                    self.assertFalse(json.loads(out.getvalue())["signingReady"])
                    self.assertNotIn("private-password", out.getvalue())
                    if source[0] == "--ledger-dsn-file":
                        read.assert_called_once_with(DSN)
                    else:
                        read.assert_not_called()



# --- --router-preflight (opt-in, one nonce write) -------------------------------------------

CONNECTIONS = ROOT / "kubernetes/components/relay-router-connection/connections.json"
CONNECTION_ID = "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8"
TENANT = "00000000-0000-4000-8000-000000000001"
# relay-router-connection.md "Deployment digest", computed there with Relay's own functions.
DEPLOYMENT = "547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad"
ISSUER, KID = "relay.chifor.me", "relay-reconciler-2026-11"
INSTANCE, EPOCH = "11111111-2222-4333-8444-555555555555", "66666666-7777-4888-9999-aaaaaaaaaaaa"
ROUTE = "relay-researcher-luna"
# RFC 8032 section 7.1 known answers (seed, public key, message, signature); TEST 1 is also the
# throwaway reconciler key of these tests, never a real key.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]
TEST_SEED = bytes.fromhex(RFC8032[0][0])
TEST_PUBLIC = bytes.fromhex(RFC8032[0][1])


def pem(der, label="PRIVATE KEY"):
    body = base64.b64encode(der).decode()
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n".encode()


TEST_PEM = pem(check.PKCS8_ED25519 + TEST_SEED)


def b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def ed25519_verify(public, message, signature):
    """Verification [S]B == R + [k]A over RFC 8032's decoding; signing is pinned by the KATs."""
    def decode(raw):
        y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
        x = check._recover_x(y, raw[31] >> 7)
        return None if x is None else (x, y, 1, x * y % check._P)

    def same(a, b):
        return (a[0] * b[2] - b[0] * a[2]) % check._P == 0 and (a[1] * b[2] - b[1] * a[2]) % check._P == 0

    point, commitment = decode(public), decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if point is None or commitment is None or s >= check._L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % check._L
    return same(check._point_mul(s, check._G), check._point_add(commitment, check._point_mul(k, point)))


def router_fixtures(**changes):
    file_b, snapshot = fixtures(**changes)
    for rows in (file_b["qualifications"], snapshot["qualifications"]):
        rows[0]["deployment_digest"] = DEPLOYMENT
    snapshot["authority"] = [{"instance_id": INSTANCE, "recovery_epoch": EPOCH, "ready": True}]
    return file_b, snapshot


def store_readback(entries=(), observed=NOW, scope=None, **changes):
    scope = scope or check.router_scope(ISSUER, TENANT)
    value = {"version": 1, "readOnly": True, "observedAt": observed, "namespace": "relay-key-nonces",
             "scopes": [{"key": scope, "entries": list(entries)}] if entries else []}
    value.update(changes)
    return value


class FakeRouter:
    """POST /register/relay-keys at llm-router 5a5181b7: verify (contracts.ts:58-73), consume
    (service.ts:34-50, the durable replay set), then the preflight branch (service.ts:69-88)."""

    def __init__(self, respond=None, entries=()):
        self.calls, self.respond = [], respond
        self.replay = list(entries)  # the scope's stored {nonce, exp, readiness?} entries

    def __call__(self, origin, raw, token):
        self.calls.append((origin, raw, token))
        test = unittest.TestCase()
        test.assertEqual(origin, "https://router.chifor.me")
        header_part, claims_part, signature_part = token.split(".")
        for part in (header_part, claims_part, signature_part):
            test.assertRegex(part, r"^[A-Za-z0-9_-]+$")
        header = json.loads(b64url_decode(header_part))
        claims = json.loads(b64url_decode(claims_part))
        test.assertEqual(header, {"alg": "EdDSA", "typ": "JWT", "kid": KID})
        test.assertTrue(ed25519_verify(TEST_PUBLIC, f"{header_part}.{claims_part}".encode(), b64url_decode(signature_part)))
        test.assertEqual(set(claims), {"iss", "aud", "sub", "tenant", "role", "action", "operationId", "iat", "exp", "nonce", "binding"})
        body = json.loads(raw)
        test.assertEqual(set(body), {"version", "action", "operationId", "preflight"})
        test.assertEqual((claims["iss"], claims["aud"], claims["tenant"], claims["role"], claims["action"]),
                         (ISSUER, "llm-router-relay-keys", TENANT, "reconciler", "worker-budget-preflight"))
        test.assertRegex(claims["sub"], r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
        test.assertEqual((body["version"], body["action"], body["operationId"]), (1, "worker-budget-preflight", claims["operationId"]))
        test.assertEqual(claims["iat"], NOW // 1000)
        test.assertEqual(claims["exp"] - claims["iat"], 10)
        test.assertRegex(claims["nonce"], r"^[A-Za-z0-9_-]{22,128}$")
        test.assertEqual(claims["binding"], hashlib.sha256(
            ("POST\n/register/relay-keys\n" + hashlib.sha256(raw).hexdigest()).encode()).hexdigest())
        preflight = body["preflight"]
        test.assertEqual(set(preflight), {"deploymentDigest", "recoveryEpoch", "policyDigests", "requiredUntil",
                                          "limit", "routes", "access", "requestedModel"})
        # consume(): refusals inside the transaction write nothing.
        clock = NOW // 1000
        live = [n for n in self.replay if n["exp"] > clock]
        if any(n["nonce"] == claims["nonce"] for n in live):
            return 409, json.dumps({"error": {"code": "ASSERTION_REPLAY", "message": "x", "type": "router_error"}}).encode()
        if sum(1 for n in live if n.get("readiness") is True) >= 12:
            return 429, json.dumps({"error": {"code": "RATE_LIMITED", "message": "x", "type": "router_error"}}).encode()
        self.replay = live + [{"nonce": claims["nonce"], "exp": claims["exp"], "readiness": True}]
        if self.respond:
            return self.respond(raw, claims)
        return 200, json.dumps(proof_for(raw, claims)).encode()

    def store(self, observed=NOW):
        return store_readback([{"exp": n["exp"], "readiness": n.get("readiness") is True,
                                "nonceSha256": hashlib.sha256(n["nonce"].encode()).hexdigest()} for n in self.replay],
                              observed=observed)


def proof_for(raw, claims, **changes):
    preflight = json.loads(raw)["preflight"]
    proof = {"version": 1, "action": "worker-budget-preflight", "operationId": claims["operationId"],
             "requestDigest": hashlib.sha256(raw).hexdigest(), "issuer": claims["iss"], "tenant": claims["tenant"],
             "deploymentDigest": preflight["deploymentDigest"], "recoveryEpoch": preflight["recoveryEpoch"],
             "policyDigests": list(preflight["policyDigests"]), "requiredUntil": preflight["requiredUntil"],
             "limit": preflight["limit"], "observedAt": NOW, "validUntil": min(NOW + 10000, claims["exp"] * 1000)}
    proof.update(changes)
    return proof


def error_response(status, code):
    return lambda raw, claims: (status, json.dumps({"error": {"code": code, "message": "private-password router text",
                                                               "type": "router_error"}}).encode())


class RouterPreflightChecks(unittest.TestCase):
    def preflight(self, router=None, *, file_b=None, snapshot=None, store=None, key=TEST_PEM, human=False,
                  argv_changes=None, key_source="pipe"):
        router = router if router is not None else FakeRouter()
        default_file_b, default_snapshot = router_fixtures()
        file_b, snapshot = file_b or default_file_b, snapshot or default_snapshot
        store = store if store is not None else store_readback()
        with tempfile.TemporaryDirectory() as tmp:
            paths = {name: Path(tmp) / f"{name}.json" for name in ("file-b", "ledger", "store")}
            paths["file-b"].write_text(json.dumps(file_b))
            paths["ledger"].write_text(json.dumps(snapshot))
            paths["store"].write_text(json.dumps(store))
            if key_source == "pipe":
                read_fd, write_fd = os.pipe()
                os.write(write_fd, key)
                os.close(write_fd)
            else:
                (Path(tmp) / "key.pem").write_bytes(key)
                read_fd = os.open(Path(tmp) / "key.pem", os.O_RDONLY)
            identity = os.fstat(read_fd)
            options = {"--file-b": str(paths["file-b"]), "--required-lifetime-seconds": "60",
                       "--ledger-json": str(paths["ledger"]), "--router-connection": str(CONNECTIONS),
                       "--router-connection-id": CONNECTION_ID, "--router-tenant": TENANT,
                       "--router-store-json": str(paths["store"]), "--router-route": ROUTE, "--router-access": "api",
                       "--router-requested-model": ROUTE, "--router-limit": "1000000", "--reconciler-key-fd": str(read_fd)}
            options.update(argv_changes or {})
            argv = ["--router-preflight"] + [item for key_, value in options.items() if value is not None for item in (key_, value)]
            if not human:
                argv.append("--json")
            out, err = io.StringIO(), io.StringIO()
            try:
                with patch.object(check, "router_post", router), patch.object(check.time, "time_ns", return_value=NOW * 1000000), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = check.main(argv)
            finally:
                try:
                    current = os.fstat(read_fd)
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        os.close(read_fd)  # the tool reads (and closes) the key only right before signing
                except OSError:
                    pass
        report = out.getvalue() if human else json.loads(out.getvalue())
        for secret in (TEST_PEM.decode().split("\n")[1], TEST_SEED.hex(), "private-password"):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        for _, _, token in getattr(router, "calls", ()):
            self.assertNotIn(token, out.getvalue() + err.getvalue())
            nonce = json.loads(b64url_decode(token.split(".")[1]))["nonce"]
            self.assertNotIn(nonce, out.getvalue() + err.getvalue())
        return rc, report, err.getvalue(), router

    def test_pass_is_signing_ready_with_one_request_and_slot_reports(self):
        rc, report, err, router = self.preflight()
        self.assertEqual(rc, 0)
        self.assertEqual((report["status"], report["signingReady"]), ("pass", True))
        self.assertEqual(len(router.calls), 1)
        r = report["router"]
        self.assertEqual((r["status"], r["called"], r["nonceWritten"], r["httpStatus"]), ("pass", True, "yes", 200))
        self.assertEqual((r["issuer"], r["tenant"], r["deploymentDigest"]), (ISSUER, TENANT, DEPLOYMENT))
        self.assertEqual(r["write"], f"one delegation nonce in the Router replay set (relay-key-nonces) for scope {ISSUER}/{TENANT}")
        self.assertEqual(r["slotsBefore"]["remaining"], 12)
        self.assertEqual(r["slotsAfterExpected"], {"remaining": 11, "limit": 12, "until": (NOW // 1000 + 10) * 1000})
        self.assertEqual(r["reconcilerPublicKeySha256"], hashlib.sha256(check.SPKI_ED25519 + TEST_PUBLIC).hexdigest())
        self.assertEqual(r["proof"]["recoveryEpoch"], EPOCH)
        self.assertEqual(report["ledger"]["authority"], {"instanceId": INSTANCE, "recoveryEpoch": EPOCH, "ready": True})
        # The notice precedes the call and the expected slots follow it.
        before, after = err.index("exactly ONE request"), err.index("nonce written: yes")
        self.assertLess(before, after)
        self.assertIn(f"replay set (relay-key-nonces) for scope {ISSUER}/{TENANT}", err)
        self.assertIn("Readiness slots before: 12 of 12 remaining", err)
        self.assertIn("expected readiness slots after: 11 of 12", err)
        # The request carried File B's digests for this deployment and the ledger's deadline.
        _, raw, token = router.calls[0]
        preflight = json.loads(raw)["preflight"]
        file_b = router_fixtures()[0]
        self.assertEqual(preflight, {"deploymentDigest": DEPLOYMENT, "recoveryEpoch": EPOCH,
                                     "policyDigests": [file_b["qualifications"][0]["digest"]],
                                     "requiredUntil": report["ledger"]["requiredUntil"], "limit": 1000000,
                                     "routes": [ROUTE], "access": ["api"], "requestedModel": ROUTE})
        self.assertEqual(r["nonceSha256"], hashlib.sha256(json.loads(b64url_decode(token.split(".")[1]))["nonce"].encode()).hexdigest())
        self.assertEqual(r["requestDigest"], hashlib.sha256(raw).hexdigest())

    def test_after_pass_reads_back_the_written_nonce(self):
        rc, report, _, router = self.preflight()
        self.assertEqual(rc, 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "after.json"
            path.write_text(json.dumps(router.store()))
            out = io.StringIO()
            with patch.object(check, "router_post", side_effect=AssertionError("no request")), \
                    patch.object(check.time, "time_ns", return_value=NOW * 1000000), contextlib.redirect_stdout(out):
                rc = check.main(["--router-slots", "--router-connection", str(CONNECTIONS), "--router-connection-id",
                                 CONNECTION_ID, "--router-tenant", TENANT, "--router-store-json", str(path),
                                 "--router-nonce-sha256", report["router"]["nonceSha256"], "--json"])
        slots = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual((slots["status"], slots["signingReady"]), ("router-slots", False))
        self.assertEqual(slots["router"]["slots"]["remaining"], 11)
        self.assertTrue(slots["router"]["slots"]["callNonceRecorded"])

    def test_human_output(self):
        rc, text, _, _ = self.preflight(human=True)
        self.assertEqual(rc, 0)
        self.assertIn("File B: pass; ledger=pass; router=pass; signingReady=true", text)
        self.assertIn("Readiness slots before: 12 of 12", text)
        self.assertIn("Readiness slots after (expected): 11 of 12", text)
        self.assertIn("one nonce written", text)

    def test_rate_limit_is_local_when_slots_are_exhausted(self):
        entries = [{"exp": NOW // 1000 + 5, "readiness": True, "nonceSha256": f"{i:064x}"} for i in range(12)]
        rc, report, err, router = self.preflight(store=store_readback(entries))
        self.assertEqual(rc, 1)
        self.assertEqual(router.calls, [])
        self.assertEqual(report["router"]["status"], "not-called")
        self.assertEqual(report["router"]["slotsBefore"]["remaining"], 0)
        self.assertIn("no readiness slot", report["error"])
        self.assertEqual(err, "")

    def test_slot_counting_ignores_expired_management_and_other_scopes(self):
        clock = NOW // 1000
        entries = [{"exp": clock + 5, "readiness": True, "nonceSha256": "1" * 64},
                   {"exp": clock, "readiness": True, "nonceSha256": "2" * 64},          # expired: exp <= now
                   {"exp": clock + 50, "readiness": False, "nonceSha256": "3" * 64}]
        store = store_readback(entries)
        store["scopes"].append({"key": "f" * 64, "entries": [{"exp": clock + 9, "readiness": True, "nonceSha256": "4" * 64}]})
        slots = check.router_slots(store, check.router_scope(ISSUER, TENANT), NOW, "2" * 64)
        self.assertEqual((slots["remaining"], slots["unexpiredReadiness"], slots["unexpiredManagement"], slots["retainedEntries"]),
                         (11, 1, 1, 3))
        self.assertTrue(slots["callNonceRecorded"])
        self.assertFalse(check.router_slots(store, check.router_scope(ISSUER, TENANT), NOW, "4" * 64)["callNonceRecorded"])
        self.assertEqual(check.router_slots(store_readback(), check.router_scope(ISSUER, TENANT), NOW)["remaining"], 12)

    def test_invalid_store_readbacks_refuse_before_the_call(self):
        cases = [store_readback(observed=NOW - 15001), store_readback(readOnly=False), store_readback(namespace="other"),
                 store_readback([{"exp": None, "readiness": True, "nonceSha256": "1" * 64}]),
                 store_readback([{"exp": 1, "readiness": True, "nonceSha256": None}]),
                 store_readback([{"exp": 1, "readiness": 1, "nonceSha256": "1" * 64}]),
                 store_readback(scopes=[{"key": "a" * 64, "entries": []}] * 2), dict(store_readback(), extra=1)]
        for store in cases:
            with self.subTest(store=store):
                rc, report, _, router = self.preflight(store=store)
                self.assertEqual((rc, router.calls), (1, []))
                self.assertIn("Router store", report["error"])

    def test_response_validation_failures_are_not_signable_and_not_retried(self):
        file_digest = router_fixtures()[0]["qualifications"][0]["digest"]
        mutations = {"version": 2, "action": "worker-budget-inspect", "operationId": str(uuid.uuid4()),
                     "requestDigest": "0" * 64, "issuer": "other.issuer", "tenant": "00000000-0000-4000-8000-000000000002",
                     "deploymentDigest": "0" * 64, "recoveryEpoch": "00000000-0000-4000-8000-000000000003",
                     "policyDigests": [file_digest, "0" * 64], "requiredUntil": 1, "limit": 999999,
                     "observedAt": NOW - 15001, "validUntil": NOW}
        for field, value in mutations.items():
            with self.subTest(field=field):
                router = FakeRouter(lambda raw, claims, f=field, v=value: (200, json.dumps(proof_for(raw, claims, **{f: v})).encode()))
                rc, report, _, _ = self.preflight(router)
                self.assertEqual((rc, report["signingReady"], len(router.calls)), (1, False, 1))
                # A 200 that does not echo this operationId does not prove the Router answered this request.
                self.assertEqual((report["router"]["status"], report["router"]["nonceWritten"]),
                                 ("invalid-proof", "unknown" if field == "operationId" else "yes"))
                self.assertIn("Router proof", report["error"])
        for name, respond in {
            "extra field": lambda raw, claims: (200, json.dumps(proof_for(raw, claims, extra=1)).encode()),
            "missing field": lambda raw, claims: (200, json.dumps({k: v for k, v in proof_for(raw, claims).items() if k != "limit"}).encode()),
            "boolean limit": lambda raw, claims: (200, json.dumps(proof_for(raw, claims, limit=True)).encode()),
            "valid past delegation": lambda raw, claims: (200, json.dumps(proof_for(raw, claims, validUntil=claims["exp"] * 1000 + 1)).encode()),
            "not json": lambda raw, claims: (200, b"<html>private-password</html>"),
        }.items():
            with self.subTest(case=name):
                router = FakeRouter(respond)
                rc, report, _, _ = self.preflight(router)
                self.assertEqual((rc, report["router"]["status"], len(router.calls)), (1, "invalid-proof", 1))
                self.assertEqual(report["router"]["nonceWritten"], "unknown" if name == "not json" else "yes")

    def test_refusals_map_to_distinct_failures_without_retry(self):
        cases = [(429, "RATE_LIMITED", "rate-limited", "no"), (409, "ASSERTION_REPLAY", "replay", "yes"),
                 (403, "RELAY_DELEGATION_DENIED", "denied", "unknown"), (403, "GRANT_DENIED", "denied", "yes"),
                 (403, "WORKER_BUDGET_UNQUALIFIED", "denied", "yes"), (403, "SOMETHING_ELSE", "denied", "unknown"),
                 (503, "WORKER_BUDGET_UNAVAILABLE", "unavailable", "yes"), (500, "SOMETHING_ELSE", "http-error", "unknown"),
                 (429, "OTHER", "http-error", "unknown")]
        messages = set()
        for status, code, kind, written in cases:
            with self.subTest(status=status, code=code):
                router = FakeRouter(error_response(status, code))
                rc, report, err, _ = self.preflight(router)
                self.assertEqual((rc, report["signingReady"], len(router.calls)), (1, False, 1))
                r = report["router"]
                self.assertEqual((r["status"], r["httpStatus"], r["nonceWritten"]), (kind, status, written))
                self.assertEqual(r.get("code"), code if code in check.KNOWN_CODES else None)
                self.assertIn("Not retried", report["error"])
                self.assertNotIn("router text", report["error"])
                self.assertIn("exactly ONE request", err)
                messages.add(report["error"])
                if written == "no":
                    self.assertNotIn("slotsAfterExpected", r)
                else:
                    self.assertEqual(r["slotsAfterExpected"]["remaining"], 11)
        self.assertEqual(len(messages), len(cases))

    def test_real_router_rules_rate_limit_and_replay(self):
        clock = NOW // 1000
        full = FakeRouter(entries=[{"nonce": f"n{i:023d}", "exp": clock + 5, "readiness": True} for i in range(12)])
        rc, report, _, _ = self.preflight(full, store=store_readback())  # a stale before-count; the Router decides
        self.assertEqual((rc, report["router"]["status"], len(full.calls), len(full.replay)), (1, "rate-limited", 1, 12))

        class Duplicate(FakeRouter):
            def __call__(self, origin, raw, token):  # a proxy that delivered the same bytes twice
                super().__call__(origin, raw, token)
                return super().__call__(origin, raw, token)
        twice = Duplicate()
        rc, report, _, _ = self.preflight(twice)
        self.assertEqual((rc, report["router"]["status"], report["router"]["code"]), (1, "replay", "ASSERTION_REPLAY"))

    def test_transport_outcomes(self):
        for error, kind, written in ((check.NotSent(), "not-sent", "no"), (check.OutcomeUnknown(), "outcome-unknown", "unknown"),
                                     (ValueError("private-password"), "outcome-unknown", "unknown")):
            with self.subTest(kind=kind):
                calls = []

                def failing(origin, raw, token, e=error):
                    calls.append(token)
                    raise e
                rc, report, _, _ = self.preflight(failing)
                self.assertEqual((rc, len(calls), report["router"]["status"], report["router"]["nonceWritten"]),
                                 (1, 1, kind, written))
                self.assertTrue(report["router"]["called"])

    def test_local_refusals_never_call_the_router(self):
        other = copy.deepcopy(router_fixtures())
        other[0]["qualifications"][0]["deployment_digest"] = "4" * 64
        other[1]["qualifications"][0]["deployment_digest"] = "4" * 64
        stale = router_fixtures()[1]
        stale["observedAt"] -= 15001
        not_ready = router_fixtures()[1]
        not_ready["authority"][0]["ready"] = False
        two = router_fixtures()[1]
        two["authority"] *= 2
        no_authority = fixtures()[1]
        cases = {
            "File B of another deployment": dict(file_b=other[0], snapshot=other[1]),
            "stale ledger": dict(snapshot=stale), "authority not ready": dict(snapshot=not_ready),
            "two authority rows": dict(snapshot=two), "default ledger SQL readback": dict(snapshot=no_authority),
            "model outside routes": dict(argv_changes={"--router-requested-model": "gpt-6-luna"}),
            "bad access": dict(argv_changes={"--router-access": "admin"}),
            "limit": dict(argv_changes={"--router-limit": "0"}),
            "unknown connection": dict(argv_changes={"--router-connection-id": str(uuid.uuid4())}),
            "tenant outside record": dict(argv_changes={"--router-tenant": "00000000-0000-4000-8000-000000000002"}),
            "missing key fd": dict(argv_changes={"--reconciler-key-fd": None}),
            "stdin key fd": dict(argv_changes={"--reconciler-key-fd": "0"}),
            "key from a file": dict(key_source="file"),
            "malformed key": dict(key=b"-----BEGIN PRIVATE KEY-----\nprivate-password\n-----END PRIVATE KEY-----\n"),
            "public key": dict(key=pem(check.SPKI_ED25519 + TEST_PUBLIC, "PUBLIC KEY")),
            "non-Ed25519 PKCS#8": dict(key=pem(bytes.fromhex("302e020100300506032b656e04220420") + TEST_SEED)),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                router = FakeRouter()
                rc, report, err, _ = self.preflight(router, **kwargs)
                self.assertEqual((rc, router.calls, report["signingReady"]), (1, [], False))
                self.assertEqual(err, "")
                self.assertNotEqual(report["router"].get("called"), True)

    def test_mode_flags_are_exclusive_and_router_inputs_need_a_mode(self):
        for argv in (["--router-preflight", "--skip-router-preflight-unavailable"], ["--router-preflight", "--router-slots"],
                     ["--file-b", "x", "--required-lifetime-seconds", "60", "--ledger-json", "-", "--router-limit", "5"],
                     ["--print-ledger-sql", "--print-router-store-script"]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with patch.object(check, "router_post", side_effect=AssertionError("no request")), contextlib.redirect_stdout(out):
                    self.assertEqual(check.main(argv + ["--json"]), 1)
                self.assertIn("error", json.loads(out.getvalue()))

    def test_default_modes_never_send_a_request(self):
        for extra, code in (([], 1), (["--skip-router-preflight-unavailable"], 2)):
            with self.subTest(extra=extra):
                file_b, snapshot = fixtures()
                with tempfile.TemporaryDirectory() as tmp:
                    paths = Path(tmp) / "file-b.json", Path(tmp) / "ledger.json"
                    paths[0].write_text(json.dumps(file_b))
                    paths[1].write_text(json.dumps(snapshot))
                    out, err = io.StringIO(), io.StringIO()
                    with patch.object(check, "router_post", side_effect=AssertionError("no request")), \
                            patch.object(check.http.client, "HTTPSConnection", side_effect=AssertionError("no socket")), \
                            patch.object(check.time, "time_ns", return_value=NOW * 1000000), \
                            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = check.main(["--file-b", str(paths[0]), "--required-lifetime-seconds", "60",
                                         "--ledger-json", str(paths[1]), "--json"] + extra)
                report = json.loads(out.getvalue())
                self.assertEqual(rc, code)
                self.assertEqual((report["router"]["status"], report["signingReady"]), ("unavailable", False))
                self.assertIn("--router-preflight performs the check", report["router"]["message"])
                self.assertEqual(err.getvalue(), "")

    def test_slots_mode_rejects_verification_inputs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = check.main(["--router-slots", "--router-connection", str(CONNECTIONS), "--router-connection-id", CONNECTION_ID,
                             "--router-tenant", TENANT, "--router-store-json", "-", "--file-b", "x", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "fail")


class RouterPreflightPieces(unittest.TestCase):
    def test_ed25519_rfc8032_known_answers(self):
        for seed, public, message, signature in RFC8032:
            with self.subTest(public=public):
                self.assertEqual(check.ed25519_public(bytes.fromhex(seed)).hex(), public)
                self.assertEqual(check.ed25519_sign(bytearray.fromhex(seed), bytes.fromhex(message)).hex(), signature)
                self.assertTrue(ed25519_verify(bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)))
                self.assertFalse(ed25519_verify(bytes.fromhex(public), b"other", bytes.fromhex(signature)))

    @unittest.skipUnless(shutil.which("openssl"), "openssl CLI not available")
    def test_delegation_signature_verifies_with_openssl(self):
        connection = check.read_connection(str(CONNECTIONS), CONNECTION_ID, TENANT)
        preflight = {"deploymentDigest": DEPLOYMENT, "recoveryEpoch": EPOCH, "policyDigests": ["5" * 64],
                     "requiredUntil": NOW + 60000, "limit": 5, "routes": [ROUTE], "access": ["api"], "requestedModel": ROUTE}
        sent = check.signed_request(connection, preflight, bytearray(TEST_SEED), NOW)
        header, claims, signature = sent["token"].split(".")
        with tempfile.TemporaryDirectory() as tmp:
            files = {name: Path(tmp) / name for name in ("public.pem", "message", "signature")}
            files["public.pem"].write_bytes(pem(check.SPKI_ED25519 + TEST_PUBLIC, "PUBLIC KEY"))
            files["message"].write_bytes(f"{header}.{claims}".encode())
            files["signature"].write_bytes(b64url_decode(signature))
            result = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(files["public.pem"]),
                                     "-rawin", "-in", str(files["message"]), "-sigfile", str(files["signature"])],
                                    capture_output=True, text=True)
        if "rawin" in result.stderr and result.returncode:
            self.skipTest("openssl without pkeyutl -rawin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reconciler_key_from_pipe(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, TEST_PEM)
        os.close(write_fd)
        seed = check.read_reconciler_key(read_fd)
        self.assertEqual(bytes(seed), TEST_SEED)
        with self.assertRaises(OSError):
            os.fstat(read_fd)  # closed after reading

    def test_reconciler_key_errors_never_echo_material(self):
        for raw in (TEST_PEM.replace(b"PRIVATE KEY", b"ENCRYPTED PRIVATE KEY"), TEST_PEM[:-30], b"x" * 9000,
                    TEST_PEM.replace(b"\n-----END", b"AAAA\n-----END")):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, raw)
            os.close(write_fd)
            with self.subTest(size=len(raw)), self.assertRaises(check.Fail) as error:
                check.read_reconciler_key(read_fd)
            self.assertNotIn(TEST_PEM.decode().split("\n")[1][:20], str(error.exception))

    def test_connection_record_is_the_reviewed_one(self):
        connection = check.read_connection(str(CONNECTIONS), CONNECTION_ID, TENANT)
        self.assertEqual(connection, {"id": CONNECTION_ID, "origin": "https://router.chifor.me", "issuer": ISSUER,
                                      "tenant": TENANT, "kid": KID, "deploymentDigest": DEPLOYMENT})
        record = json.loads(CONNECTIONS.read_text())
        for field, value in (("origin", "https://router.chifor.me/"), ("origin", "http://router.chifor.me"),
                             ("origin", "https://router.chifor.me:443"), ("issuer", "bad issuer"), ("extra", 1),
                             ("reconcilerKid", "")):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                changed = copy.deepcopy(record)
                changed[0][field] = value
                path = Path(tmp) / "c.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(check.Fail):
                    check.read_connection(str(path), CONNECTION_ID, TENANT)

    def test_scope_matches_router_json_stringify(self):
        self.assertEqual(check.router_scope(ISSUER, TENANT),
                         hashlib.sha256(b'["relay.chifor.me","00000000-0000-4000-8000-000000000001"]').hexdigest())

    def test_router_ledger_sql_is_read_only_and_adds_only_the_authority_row(self):
        self.assertTrue(check.ROUTER_LEDGER_SQL.startswith(check.LEDGER_SQL.split("SELECT jsonb_build_object(")[0]))
        clean = re.sub(r"'(?:[^']|'')*'", "''", check.ROUTER_LEDGER_SQL)
        statements = [s.strip() for s in clean.split(";") if s.strip()]
        self.assertEqual([statements[0], statements[3], statements[-1]], ["BEGIN READ ONLY", "SET LOCAL search_path = pg_catalog", "ROLLBACK"])
        self.assertEqual(len(statements), 6)
        query = statements[4]
        self.assertNotRegex(query.upper(), r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|CALL|COPY|INTO|LOCK|FOR|WITH)\b")
        self.assertEqual(set(re.findall(r"\b([a-z_][a-z_0-9]*)\s*\(", query.lower())),
                         {"jsonb_build_object", "current_setting", "floor", "extract", "statement_timestamp",
                          "coalesce", "jsonb_agg"})
        self.assertEqual(sorted(re.findall(r"worker_budget\.\w+", query)), ["worker_budget.authority", "worker_budget.qualifications"])
        self.assertNotIn("witness", query)
        self.assertNotIn("WHERE", query.upper())

    def test_store_script_is_fixed_and_read_only(self):
        script = check.ROUTER_STORE_SCRIPT
        self.assertIn("new DatabaseSync('/data/router.sqlite', { readOnly: true })", script)
        self.assertIn("PRAGMA query_only = 1", script)
        # Every statement the program runs, in order: two pragmas, one read transaction, one SELECT.
        self.assertEqual(re.findall(r"db\.(?:exec|prepare)\((['\"])(.*?)\1\)", script),
                         [("'", "PRAGMA query_only = 1"), ("'", "BEGIN"), ("'", "PRAGMA query_only"),
                          ('"', "SELECT key, value FROM kv WHERE ns = 'relay-key-nonces' ORDER BY key"), ("'", "ROLLBACK")])
        self.assertEqual(len(re.findall(r"db\.\w+\(", script)), 6)  # the five above and db.close()
        self.assertNotIn("${", script)
        self.assertNotRegex(script, r"nonce:\s*n\.nonce")  # only sha(n.nonce) leaves the container

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_store_script_reads_a_live_wal_database_without_writing(self):
        version = subprocess.run(["node", "-e", "try{require('node:sqlite');process.stdout.write(process.versions.node)}catch{}"],
                                 capture_output=True, text=True).stdout
        if not version or tuple(int(p) for p in version.split(".")[:2]) < (22, 12):
            self.skipTest("node without node:sqlite readOnly")
        import sqlite3
        clock = NOW // 1000
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "router.sqlite"
            writer = sqlite3.connect(path)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE kv (ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(ns,key))")
                scope = check.router_scope(ISSUER, TENANT)
                writer.execute("INSERT INTO kv VALUES (?,?,?,?)", ("relay-key-nonces", scope, json.dumps(
                    [{"nonce": "private-nonce-aaaaaaaaaaaaa", "exp": clock + 5, "readiness": True},
                     {"nonce": "private-nonce-bbbbbbbbbbbbb", "exp": clock + 50}]), 1))
                writer.execute("INSERT INTO kv VALUES (?,?,?,?)", ("api-keys", "state", '{"secret":"private-password"}', 1))
                writer.commit()  # stays in the WAL: the writer keeps the database open, as the Router does
                files = {p.name: p.read_bytes() for p in Path(tmp).iterdir() if not p.name.endswith("-shm")}
                script = check.ROUTER_STORE_SCRIPT.replace("/data/router.sqlite", str(path))
                result = subprocess.run(["node", "--no-warnings", "-"], input=script, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(files, {p.name: p.read_bytes() for p in Path(tmp).iterdir() if not p.name.endswith("-shm")})
            finally:
                writer.close()
        self.assertNotIn("private", result.stdout)
        store = json.loads(result.stdout)
        slots = check.router_slots(store, scope, store["observedAt"],
                                   hashlib.sha256(b"private-nonce-aaaaaaaaaaaaa").hexdigest())
        # Expiry follows the readback's own clock (the Router's): both test entries are still unexpired.
        self.assertEqual((slots["unexpiredReadiness"], slots["unexpiredManagement"], slots["retainedEntries"],
                          slots["callNonceRecorded"]), (1, 1, 2, True))

    def test_print_utilities_never_connect(self):
        for flag, text in (("--print-router-ledger-sql", check.ROUTER_LEDGER_SQL),
                           ("--print-router-store-script", check.ROUTER_STORE_SCRIPT)):
            out = io.StringIO()
            with patch.object(check.subprocess, "run", side_effect=AssertionError("connection")), \
                    patch.object(check, "router_post", side_effect=AssertionError("request")), contextlib.redirect_stdout(out):
                self.assertEqual(check.main([flag]), 0)
            self.assertEqual(out.getvalue(), text)

    def test_router_mode_dsn_uses_the_authority_sql(self):
        file_b, snapshot = router_fixtures()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file-b.json"
            path.write_text(json.dumps(file_b))
            out = io.StringIO()
            with patch.dict(os.environ, FILE_B_TEST_DSN=DSN), patch.object(check, "read_ledger", return_value=snapshot) as read, \
                    patch.object(check, "router_slots", side_effect=check.Fail("Router store: stop here")), \
                    patch.object(check.time, "time_ns", return_value=NOW * 1000000), contextlib.redirect_stdout(out):
                store = Path(tmp) / "store.json"
                store.write_text(json.dumps(store_readback()))
                rc = check.main(["--router-preflight", "--file-b", str(path), "--required-lifetime-seconds", "60",
                                 "--ledger-dsn-env", "FILE_B_TEST_DSN", "--router-connection", str(CONNECTIONS),
                                 "--router-connection-id", CONNECTION_ID, "--router-tenant", TENANT,
                                 "--router-store-json", str(store), "--router-route", ROUTE, "--router-access", "api",
                                 "--router-requested-model", ROUTE, "--router-limit", "5", "--reconciler-key-fd", "9", "--json"])
        self.assertEqual(rc, 1)
        read.assert_called_once_with(DSN, check.ROUTER_LEDGER_SQL)
        self.assertNotIn("private-password", out.getvalue())


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI not available")
class RouterTransportChecks(unittest.TestCase):
    """router_post against a local HTTPS server: one request, verified TLS, no redirect."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cert, key = Path(cls.tmp.name) / "cert.pem", Path(cls.tmp.name) / "key.pem"
        result = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                                 "-nodes", "-keyout", str(key), "-out", str(cls.cert), "-days", "1", "-subj", "/CN=localhost",
                                 "-addext", "subjectAltName=DNS:localhost"], capture_output=True)
        if result.returncode:
            cls.tmp.cleanup()
            raise unittest.SkipTest("openssl cannot create a test certificate")
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def serve(self, status, body=b"{}", headers=(), close=False):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.command, self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
                if close:
                    self.close_connection = True
                    return
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.socket = self.server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"https://localhost:{server.server_address[1]}", seen

    def client(self):
        return ssl.create_default_context(cafile=str(self.cert))

    def test_one_post_with_fixed_headers(self):
        origin, seen = self.serve(200, b'{"ok":true}')
        status, body = check.router_post(origin, b'{"a":1}', "header.claims.signature", context=self.client())
        self.assertEqual((status, body), (200, b'{"ok":true}'))
        self.assertEqual(len(seen), 1)
        method, path, headers, raw = seen[0]
        self.assertEqual((method, path, raw), ("POST", "/register/relay-keys", b'{"a":1}'))
        self.assertEqual(headers["Authorization"], "Bearer header.claims.signature")
        self.assertEqual(headers["User-Agent"], "relay-worker-budget-file-b-check/1 (ailab operator)")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_redirect_is_returned_not_followed(self):
        origin, seen = self.serve(302, headers=[("Location", "https://localhost/elsewhere")])
        status, _ = check.router_post(origin, b"{}", "t.t.t", context=self.client())
        self.assertEqual((status, len(seen)), (302, 1))

    def test_unverified_certificate_is_not_sent(self):
        origin, seen = self.serve(200)
        with self.assertRaises(check.NotSent):
            check.router_post(origin, b"{}", "t.t.t")  # system trust: the test certificate is unknown
        self.assertEqual(seen, [])

    def test_lost_answer_is_outcome_unknown(self):
        origin, seen = self.serve(200, close=True)
        with self.assertRaises(check.OutcomeUnknown):
            check.router_post(origin, b"{}", "t.t.t", context=self.client())
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
