"""Offline File B checks. All database/Router interactions are fakes."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

        with patch("locale.getencoding", return_value="iso8859-1"), patch.object(check.subprocess, "run", side_effect=fake_psql):
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


if __name__ == "__main__":
    unittest.main()
