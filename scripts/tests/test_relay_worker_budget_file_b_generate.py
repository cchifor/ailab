"""File B generator: Router-equal digest, strict refusals, determinism, checker
round trip, and an optional replay of ledger-step7b.sql on a throwaway PostgreSQL.

Optional cross-checks (skipped unless configured):

* RELAY_FILE_B_ROUTER_GIT: an llm-router git checkout containing 5a5181b7;
  RELAY_FILE_B_NODE_MODULES: a node_modules with zod 4.6.5 and tsx (the Router's
  lockfile pins); RELAY_FILE_B_NODE: a Node 22+ binary. Runs the Router's own
  ReviewedTokenPolicy and tokenPolicyDigest from runtime.ts at that commit.
* RELAY_FILE_B_RELAY_GIT: a Relay git checkout containing d1c77f87 (ledger
  schema 001-005), plus a usable `docker`. RELAY_FILE_B_POSTGRES_IMAGE overrides
  the image (default: the pinned relay-budget-authority image).
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("file_b_generate", ROOT / "scripts/relay-worker-budget-file-b-generate.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)
check = gen.check
SCRIPT = ROOT / "scripts/relay-worker-budget-file-b-generate.py"
ROUTER_COMMIT = "5a5181b7"
LEDGER_COMMIT = "d1c77f876606976362d70f42973033d31ac913c6"
NOW = 1800000000000

INPUTS = {
    "deployment_digest": gen.DEFAULT_DEPLOYMENT_DIGEST,
    "routerReleaseDigest": "1111111111111111111111111111111111111111111111111111111111111111",
    "adapterDigest": "2222222222222222222222222222222222222222222222222222222222222222",
    "accountId": "qwen-local",
    "deploymentId": "qwen-27b",
    "model": "qwen-27b",
    "provider": "local",
    "providerGeneration": 3,
    "baseUrl": "http://192.168.0.45:8080/v1",
    "transport": "chat-completions",
    "maximumInputTokens": 922000,
    "maximumOutputTokens": 8192,
    "qualificationExpiresAt": "2030-01-02T03:04:05.678Z",
    "evidenceDigest": "3333333333333333333333333333333333333333333333333333333333333333",
}
# Verified with the Router's own code: llm-router 5a5181b7
# packages/plugins/worker-budget-authority/runtime.ts ReviewedTokenPolicy.parse +
# tokenPolicyDigest, run by tsx 4.23.15 / zod 4.6.5 (the Router lockfile's pins) on
# Node v22.23.5; RouterCrossCheck below repeats that when configured.
GOLDEN_DIGEST = "9d1d374e0c4581a91587153573268c23a9a5bf09a790da5e5ea52ac621a562c7"
GOLDEN_EXPIRES = 1893553445678
# Normal-mode output for INPUTS (and the dollar-quote case) as produced before supersede mode
# existed: normal mode must stay byte-identical.
GOLDEN_FILE_B_SHA256 = "077c2ef55585ec1a0da2660588b7287452d24ef02e135cbc40cdb00f5dd44d20"
GOLDEN_SQL_SHA256 = "d2e2f714195017a38b53c2c81cba443ff3306e3c67c89be6f714408856a42842"
DOLLAR_MODEL = "m$post$x$post1$y$pre$"
GOLDEN_DOLLAR_SQL_SHA256 = "4031a191c8f1fb10f3c8a0e156979ea8ecd6aa493d2e7d353218f56eaf958a3a"
# A Router release changes routerReleaseDigest and providerGeneration: the reissued readings.
NEW_INPUTS = dict(INPUTS, routerReleaseDigest="4" * 64, providerGeneration=4,
                  qualificationExpiresAt="2030-01-03T00:00:00Z")


def generate(inputs=None, **kwargs):
    kwargs.setdefault("now_ms", NOW)
    return gen.generate(copy.deepcopy(INPUTS if inputs is None else inputs), **kwargs)


class Generate(unittest.TestCase):
    def test_normal_mode_is_byte_identical_to_before_supersede(self):
        _, file_b, sql = generate()
        self.assertEqual(hashlib.sha256(file_b).hexdigest(), GOLDEN_FILE_B_SHA256)
        self.assertEqual(hashlib.sha256(sql).hexdigest(), GOLDEN_SQL_SHA256)
        self.assertEqual(generate(supersede=None), (generate()))
        _, _, sql = generate(dict(INPUTS, model=DOLLAR_MODEL))
        self.assertEqual(hashlib.sha256(sql).hexdigest(), GOLDEN_DOLLAR_SQL_SHA256)

    def test_golden_digest_and_policy(self):
        policy, file_b, _ = generate()
        self.assertEqual(policy["digest"], GOLDEN_DIGEST)
        self.assertEqual(policy["qualificationExpiresAt"], GOLDEN_EXPIRES)
        self.assertEqual(list(policy), sorted(policy))
        self.assertEqual(policy.keys(), check.POLICY_FIELDS)
        self.assertIs(policy["outputIncludesReasoning"], True)
        self.assertIs(policy["usageIncludesAllTokens"], True)
        self.assertEqual(policy["version"], 1)
        self.assertEqual(check.policy_digest(policy), GOLDEN_DIGEST)
        expected_canonical = (
            '{"accountId":"qwen-local","adapterDigest":"' + "2" * 64 + '","baseUrl":"http://192.168.0.45:8080/v1",'
            '"deploymentId":"qwen-27b","evidenceDigest":"' + "3" * 64 + '","maximumInputTokens":922000,'
            '"maximumOutputTokens":8192,"model":"qwen-27b","outputIncludesReasoning":true,"provider":"local",'
            '"providerGeneration":3,"qualificationExpiresAt":1893553445678,"routerReleaseDigest":"' + "1" * 64 + '",'
            '"transport":"chat-completions","usageIncludesAllTokens":true,"version":1}')
        self.assertEqual(hashlib.sha256(expected_canonical.encode()).hexdigest(), GOLDEN_DIGEST)

    def test_file_b_is_exactly_the_checker_format(self):
        policy, file_b, _ = generate()
        document = json.loads(file_b)
        self.assertEqual(document, {"version": 1, "qualifications": [
            {"deployment_digest": gen.DEFAULT_DEPLOYMENT_DIGEST, "digest": GOLDEN_DIGEST, "policy": policy}]})
        self.assertTrue(file_b.endswith(b"}\n"))

    def test_checker_accepts_generated_file_b_end_to_end(self):
        policy, file_b, _ = generate()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "file-b.json"
            path.write_bytes(file_b)
            parsed = check.read_json(str(path), "File B")
        ledger = {"version": 1, "readOnly": True, "observedAt": NOW, "qualifications": [
            {"deployment_digest": gen.DEFAULT_DEPLOYMENT_DIGEST, "digest": GOLDEN_DIGEST, "policy": copy.deepcopy(policy)}]}
        result = check.check_ledger(parsed, ledger, 3600, NOW)
        self.assertEqual((result["status"], result["rows"]), ("pass", 1))
        # The checker still detects any drift from the generated content.
        ledger["qualifications"][0]["policy"]["providerGeneration"] = 4
        with self.assertRaisesRegex(check.Fail, "providerGeneration mismatch"):
            check.check_ledger(parsed, ledger, 3600, NOW)

    def test_deterministic_and_cli_write_once(self):
        first = generate()
        self.assertEqual(first, generate())
        with tempfile.TemporaryDirectory() as temp:
            inputs = Path(temp) / "inputs.json"
            inputs.write_text(json.dumps(INPUTS))
            outs = []
            for name in ("a", "b", "a"):  # a third run into "a" is an identical no-op
                out = Path(temp) / name
                out.mkdir(exist_ok=True)
                result = subprocess.run(["python3", str(SCRIPT), "--inputs", str(inputs), "--output-dir", str(out)],
                                        capture_output=True, text=True, check=True)
                outs.append(result.stdout)
                self.assertIn(f"digest {GOLDEN_DIGEST}\n", result.stdout)
                for file_name in (gen.OUTPUT_JSON, gen.OUTPUT_SQL):
                    digest = hashlib.sha256((out / file_name).read_bytes()).hexdigest()
                    self.assertIn(f"sha256 {digest}  {file_name}\n", result.stdout)
            self.assertEqual(len(set(outs)), 1)
            for file_name in (gen.OUTPUT_JSON, gen.OUTPUT_SQL):
                self.assertEqual((Path(temp) / "a" / file_name).read_bytes(), (Path(temp) / "b" / file_name).read_bytes())
            self.assertEqual((Path(temp) / "a" / gen.OUTPUT_JSON).read_bytes(), first[1])
            self.assertEqual((Path(temp) / "a" / gen.OUTPUT_SQL).read_bytes(), first[2])
            # A different reviewed input never overwrites retained output.
            inputs.write_text(json.dumps(dict(INPUTS, providerGeneration=4)))
            result = subprocess.run(["python3", str(SCRIPT), "--inputs", str(inputs), "--output-dir", str(Path(temp) / "a")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to overwrite", result.stderr)
            self.assertEqual((Path(temp) / "a" / gen.OUTPUT_JSON).read_bytes(), first[1])

    def test_sql_shape(self):
        policy, _, sql = generate()
        text = sql.decode()
        canonical = check.canonical(policy)
        for needle in ("\\set ON_ERROR_STOP on", "BEGIN;", "SET LOCAL ROLE relay_budget_owner;",
                       "LOCK TABLE worker_budget.authority IN SHARE ROW EXCLUSIVE MODE;",
                       "IS NOT DISTINCT FROM", "clock_timestamp()", "'" + canonical + "'::jsonb",
                       "'relay_budget_allocator'::name", "7441536::bigint, 8::bigint, 120000::integer",
                       "'relay_budget_router'::name", "jsonb_object_keys(q.policy)) = 17)"):
            self.assertIn(needle, text)
        self.assertTrue(text.endswith("COMMIT;\n"))
        self.assertEqual(text.count("INSERT INTO"), 1)
        self.assertLess(text.index("LOCK TABLE"), text.index("DO $pre$"))
        self.assertLess(text.index("DO $pre$"), text.index("INSERT INTO"))
        self.assertLess(text.index("INSERT INTO"), text.index("DO $post$"))

    def test_sql_quotes_apostrophes(self):
        policy, _, sql = generate(dict(INPUTS, model="o'brien-model"))
        self.assertIn("\"model\":\"o''brien-model\"", sql.decode())


    def test_dollar_quote_tags_avoid_policy_text(self):
        # PostgreSQL ends a dollar-quoted body at the first matching tag, even inside a
        # quoted literal: a policy string must never contain the tag closing its block.
        _, _, normal = generate()
        self.assertIn("DO $pre$", normal.decode())
        self.assertIn("DO $post$", normal.decode())
        model = "m$post$x$post1$y$pre$"
        _, _, sql = generate(dict(INPUTS, model=model))
        text = sql.decode()
        self.assertNotIn("\x01", text)
        for name, tag in (("pre", "pre"), ("post", "post2")):  # only the post block embeds the policy
            self.assertIn(f"DO ${tag}$\nBEGIN", text)
            body = text.split(f"DO ${tag}$", 1)[1].split(f"${tag}$;", 1)[0]
            self.assertNotIn(f"${tag}$", body)
        self.assertIn(model, text)


class Refusals(unittest.TestCase):
    def refuse(self, pattern, inputs=None, **kwargs):
        with self.assertRaisesRegex(check.Fail, pattern):
            generate(inputs, **kwargs)

    def test_unknown_and_missing_fields(self):
        self.refuse("unknown fields \\['digest'\\]", dict(INPUTS, digest="a" * 64))
        self.refuse("unknown fields \\['outputIncludesReasoning'\\]", dict(INPUTS, outputIncludesReasoning=True))
        for field in INPUTS:
            inputs = dict(INPUTS)
            del inputs[field]
            self.refuse(f"missing fields \\['{field}'\\]", inputs)
        self.refuse("expected JSON object", [INPUTS])

    def test_digests(self):
        for field in ("deployment_digest", "routerReleaseDigest", "adapterDigest", "evidenceDigest"):
            for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, 1, None):
                self.refuse(field, dict(INPUTS, **{field: bad}))

    def test_deployment_digest_override(self):
        other = "4" * 64
        self.refuse("deployment-digest-override", dict(INPUTS, deployment_digest=other))
        self.refuse("deployment-digest-override", dict(INPUTS, deployment_digest=other), override="5" * 64)
        self.refuse("--deployment-digest-override: expected", override="nope")
        policy, file_b, sql = generate(dict(INPUTS, deployment_digest=other), override=other)
        self.assertEqual(json.loads(file_b)["qualifications"][0]["deployment_digest"], other)
        self.assertEqual(policy["digest"], GOLDEN_DIGEST)  # not part of the policy
        self.assertNotIn(gen.DEFAULT_DEPLOYMENT_DIGEST, sql.decode())

    def test_integers(self):
        for field in ("providerGeneration", "maximumInputTokens", "maximumOutputTokens"):
            for bad in (0, -1, 1.0, 1.5, True, "1", None, check.MAX_SAFE + 1):
                self.refuse(field, dict(INPUTS, **{field: bad}))
        self.refuse("exceeds a safe integer", dict(INPUTS, maximumInputTokens=check.MAX_SAFE - 5, maximumOutputTokens=6))
        policy, _, _ = generate(dict(INPUTS, maximumInputTokens=check.MAX_SAFE - 6, maximumOutputTokens=6))
        self.assertEqual(policy["maximumInputTokens"], check.MAX_SAFE - 6)

    def test_text(self):
        for field, limit in check.TEXT_LIMITS.items():
            self.refuse(field, dict(INPUTS, **{field: ""}))
            self.refuse(field, dict(INPUTS, **{field: "x" * (limit + 1)}))
            self.refuse(field, dict(INPUTS, **{field: 5}))
            self.refuse(field, dict(INPUTS, **{field: "a\x00b"}))
            self.refuse(field, dict(INPUTS, **{field: "a\nb"}))
            self.refuse(field, dict(INPUTS, **{field: "a\ud800"}))
            generate(dict(INPUTS, **{field: "x" * limit}))
        # Limits are UTF-16 code units, like zod's string length.
        self.refuse("accountId", dict(INPUTS, accountId="\U0001F680" * 65))
        generate(dict(INPUTS, accountId="\U0001F680" * 64))

    def test_provider_and_transport(self):
        self.refuse("provider", dict(INPUTS, provider="vllm"))
        self.refuse("transport: expected", dict(INPUTS, transport="responses"))
        self.refuse("does not match provider", dict(INPUTS, transport="anthropic-messages"))
        self.refuse("does not match provider", dict(INPUTS, provider="anthropic"))
        self.refuse("does not match provider", dict(INPUTS, provider="openai", transport="anthropic-messages"))
        generate(dict(INPUTS, provider="anthropic", transport="anthropic-messages"))
        generate(dict(INPUTS, provider="openai"))

    def test_base_url(self):
        for bad in ("", " http://h/v1", "http://h/v1 ", "http://h/v1\t", "http://h /v1", "ftp://h/v1", "h/v1",
                    "http:h/v1", "http://", "http://h/v1?x=1", "http://h/v1#f", "http://-h/v1", 7):
            self.refuse("baseUrl", dict(INPUTS, baseUrl=bad))
        for good in ("https://api.openai.com/v1", "http://llm-router.svc:8080", "http://[::1]:8080/v1"):
            generate(dict(INPUTS, baseUrl=good))

    def test_expiry(self):
        for bad in ("2030-01-02T03:04:05+00:00", "2030-01-02 03:04:05Z", "2030-01-02T03:04:05.1234Z",
                    "2030-02-30T00:00:00Z", "2030-01-02T24:00:00Z", "2030-01-02T03:04:60Z", "1893553445678",
                    1893553445678, "2030-01-02", None):
            self.refuse("qualificationExpiresAt", dict(INPUTS, qualificationExpiresAt=bad))
        self.refuse("already elapsed", now_ms=GOLDEN_EXPIRES)
        policy, _, _ = generate(now_ms=GOLDEN_EXPIRES - 1)
        self.assertEqual(policy["qualificationExpiresAt"], GOLDEN_EXPIRES)
        for iso, ms in (("2030-01-02T03:04:05Z", 1893553445000), ("2030-01-02T03:04:05.6Z", 1893553445600),
                        ("2030-01-02T03:04:05.06Z", 1893553445060)):
            self.assertEqual(gen.iso_to_ms(iso), ms)
            self.assertEqual(gen.ms_to_iso(ms), iso[:19] + f".{ms % 1000:03d}Z")

    def test_cli_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            inputs = Path(temp) / "inputs.json"
            inputs.write_text(json.dumps(INPUTS)[:-1] + ', "model": "other"}')
            result = subprocess.run(["python3", str(SCRIPT), "--inputs", str(inputs), "--output-dir", temp],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("duplicate-key", result.stderr)
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["inputs.json"])


def old_file_b(inputs=None):
    return generate(inputs)[1]


def supersede(inputs=None, old=None, old_digest=GOLDEN_DIGEST, **kwargs):
    return generate(NEW_INPUTS if inputs is None else inputs,
                    supersede=(old_digest, old_file_b() if old is None else old), **kwargs)


def two_row_table():
    """An old table of two rows (another reviewed policy beside the golden one)."""
    other, _, _ = generate(dict(INPUTS, model="other-model"))
    golden, _, _ = generate()
    rows = sorted([golden, other], key=lambda p: p["digest"])
    return other, json.dumps({"version": 1, "qualifications": [
        {"deployment_digest": gen.DEFAULT_DEPLOYMENT_DIGEST, "digest": p["digest"], "policy": p} for p in rows]}).encode()


class Supersede(unittest.TestCase):
    def test_new_table_and_sql_shape(self):
        policy, file_b, sql = supersede()
        self.assertNotEqual(policy["digest"], GOLDEN_DIGEST)
        self.assertEqual(policy["routerReleaseDigest"], "4" * 64)
        self.assertEqual(json.loads(file_b), {"version": 1, "qualifications": [
            {"deployment_digest": gen.DEFAULT_DEPLOYMENT_DIGEST, "digest": policy["digest"], "policy": policy}]})
        # One new row: the same bytes normal mode writes for the new inputs.
        self.assertEqual(file_b, generate(NEW_INPUTS)[1])
        text = sql.decode()
        old_canonical = check.canonical(generate()[0])
        self.assertTrue(text.startswith("-- Relay worker budget ledger, runbook step 7, FILE B SUPERSEDE"))
        self.assertTrue(text.endswith("COMMIT;\n"))
        self.assertNotIn("\x01", text)
        for needle in ("SET LOCAL ROLE relay_budget_owner;", "IS NOT DISTINCT FROM", "clock_timestamp()",
                       "GET DIAGNOSTICS deleted = ROW_COUNT;", "'" + old_canonical + "'::jsonb",
                       "'" + check.canonical(policy) + "'::jsonb", "jsonb_object_keys(q.policy)) = 17)",
                       f"superseded digest      {GOLDEN_DIGEST}", "qualifications must be exactly the old reviewed File B",
                       "qualifications is not exactly the new reviewed File B", "referenced by worker_budget.outbox"):
            self.assertIn(needle, text)
        order = [text.index(n) for n in ("BEGIN;", "LOCK TABLE worker_budget.authority", "LOCK TABLE worker_budget.qualifications",
                                         "LOCK TABLE worker_budget.allocations, worker_budget.outbox", "DO $pre$",
                                         "DO $del$", "DELETE FROM worker_budget.qualifications", "INSERT INTO",
                                         "DO $post$", "\\pset format unaligned", "COMMIT;")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(text.count("INSERT INTO"), 1)
        self.assertEqual(text.count("DELETE FROM"), 1)
        self.assertNotIn("qualifications must be empty", text)

    def test_deterministic(self):
        first = supersede()
        self.assertEqual(first, supersede())
        self.assertEqual(first, supersede(old=json.dumps(json.loads(old_file_b())).encode()))  # formatting-independent

    def test_other_rows_are_kept(self):
        other, old = two_row_table()
        policy, file_b, sql = supersede(old=old)
        rows = json.loads(file_b)["qualifications"]
        self.assertEqual([r["digest"] for r in rows], sorted([other["digest"], policy["digest"]]))
        self.assertEqual({r["digest"]: r["policy"] for r in rows}[other["digest"]], other)
        self.assertIn("rows before/after      2/2", sql.decode())
        parsed = check.parse_json(file_b, "File B")
        ledger = {"version": 1, "readOnly": True, "observedAt": NOW, "qualifications": rows}
        self.assertEqual(check.check_ledger(parsed, ledger, 3600, NOW)["rows"], 2)

    def test_refusals(self):
        def refuse(pattern, **kwargs):
            with self.assertRaisesRegex(check.Fail, pattern):
                supersede(**kwargs)
        refuse("--supersede: expected lowercase", old_digest=GOLDEN_DIGEST.upper())
        refuse("--supersede: expected lowercase", old_digest=GOLDEN_DIGEST[:-1])
        refuse("contains 0 rows with the --supersede digest", old_digest="5" * 64)
        refuse("equals the superseded digest", inputs=INPUTS)
        refuse("already contains the new policy digest", old=two_row_table()[1],
               inputs=dict(INPUTS, model="other-model"), old_digest=GOLDEN_DIGEST)
        other = "6" * 64
        refuse("another deployment_digest", inputs=dict(NEW_INPUTS, deployment_digest=other), override=other)
        refuse("--supersede-file: invalid", old=b"{")
        refuse("duplicate-key", old=old_file_b()[:-2] + b', "version": 1}\n')
        refuse("--supersede-file: unexpected or missing fields", old=b'{"version": 1}')
        refuse("--supersede-file.version", old=json.dumps({"version": 2, "qualifications": []}).encode())
        tampered = json.loads(old_file_b())
        tampered["qualifications"][0]["policy"]["providerGeneration"] = 9  # digest no longer matches
        refuse("canonical tokenPolicyDigest mismatch", old=json.dumps(tampered).encode())
        # The new inputs are validated exactly as in normal mode.
        refuse("already elapsed", inputs=dict(NEW_INPUTS, qualificationExpiresAt="2001-01-01T00:00:00Z"))

    def test_dollar_quote_tags_avoid_policy_text(self):
        # The old policy is embedded in the pre and del blocks, the new one in post.
        old_inputs = dict(INPUTS, model="o$pre$x$del$y$post$")
        old_policy, old, _ = generate(old_inputs)
        _, _, sql = supersede(inputs=dict(NEW_INPUTS, model="n$post$z$post1$"), old=old, old_digest=old_policy["digest"])
        text = sql.decode()
        self.assertNotIn("\x01", text)
        for tag in ("pre1", "del1", "post2"):
            self.assertIn(f"DO ${tag}$\n", text)
            body = text.split(f"DO ${tag}$", 1)[1].split(f"${tag}$;", 1)[0]
            self.assertNotIn(f"${tag}$", body)


class SupersedeCli(unittest.TestCase):
    def run_cli(self, temp, *args):
        return subprocess.run(["python3", str(SCRIPT), "--inputs", str(Path(temp) / "new.json"), *args],
                              capture_output=True, text=True)

    def setUp(self):
        self.temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp)
        Path(self.temp, "new.json").write_text(json.dumps(NEW_INPUTS))
        self.old = Path(self.temp, "old")
        self.old.mkdir()
        (self.old / gen.OUTPUT_JSON).write_bytes(old_file_b())
        self.out = Path(self.temp, "out")
        self.out.mkdir()

    def test_argument_pairing(self):
        old = str(self.old / gen.OUTPUT_JSON)
        for args in (["--supersede", GOLDEN_DIGEST], ["--supersede-file", old],
                     ["--supersede", GOLDEN_DIGEST, "--supersede", GOLDEN_DIGEST, "--supersede-file", old],
                     ["--supersede", GOLDEN_DIGEST, "--supersede-file", old, "--supersede-file", old]):
            result = self.run_cli(self.temp, "--output-dir", str(self.out), *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertRegex(result.stderr, "must be given together|given more than once")
            self.assertEqual(list(self.out.iterdir()), [])

    def test_refusals_write_nothing(self):
        old = str(self.old / gen.OUTPUT_JSON)
        cases = [(["--supersede", "7" * 64, "--supersede-file", old], "contains 0 rows"),
                 (["--supersede", "NOPE", "--supersede-file", old], "expected lowercase"),
                 (["--supersede", GOLDEN_DIGEST, "--supersede-file", str(Path(self.temp, "missing.json"))], "cannot read")]
        for args, message in cases:
            result = self.run_cli(self.temp, "--output-dir", str(self.out), *args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn(message, result.stderr)
        Path(self.temp, "new.json").write_text(json.dumps(INPUTS))  # unchanged readings: new == old
        result = self.run_cli(self.temp, "--output-dir", str(self.out), "--supersede", GOLDEN_DIGEST, "--supersede-file", old)
        self.assertEqual(result.returncode, 1)
        self.assertIn("equals the superseded digest", result.stderr)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_writes_new_table_and_supersede_sql(self):
        old = str(self.old / gen.OUTPUT_JSON)
        policy, file_b, sql = gen.generate(copy.deepcopy(NEW_INPUTS), supersede=(GOLDEN_DIGEST, old_file_b()))
        outs = []
        for _ in range(2):  # a rerun is an identical no-op
            result = self.run_cli(self.temp, "--output-dir", str(self.out), "--supersede", GOLDEN_DIGEST, "--supersede-file", old)
            self.assertEqual(result.returncode, 0, result.stderr)
            outs.append(result.stdout)
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [gen.OUTPUT_JSON, gen.OUTPUT_SUPERSEDE_SQL])
        self.assertEqual((self.out / gen.OUTPUT_JSON).read_bytes(), file_b)
        self.assertEqual((self.out / gen.OUTPUT_SUPERSEDE_SQL).read_bytes(), sql)
        lines = outs[0].splitlines()
        self.assertEqual(lines[:3], [f"superseded-digest {GOLDEN_DIGEST}",
                                     f"superseded-file-b-sha256 {hashlib.sha256(old_file_b()).hexdigest()}",
                                     f"digest {policy['digest']}"])
        self.assertIn(f"sha256 {hashlib.sha256(file_b).hexdigest()}  {gen.OUTPUT_JSON}", lines)
        self.assertIn(f"sha256 {hashlib.sha256(sql).hexdigest()}  {gen.OUTPUT_SUPERSEDE_SQL}", lines)
        # Never into the old evidence directory: the old file-b.json is not overwritten.
        result = self.run_cli(self.temp, "--output-dir", str(self.old), "--supersede", GOLDEN_DIGEST, "--supersede-file", old)
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual((self.old / gen.OUTPUT_JSON).read_bytes(), old_file_b())


def git_show(repo, rev, path):
    return subprocess.run(["git", "-C", repo, "show", f"{rev}:{path}"], capture_output=True, check=True).stdout


ROUTER_PROBE = """import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const [root, file] = process.argv.slice(2);
const m = await import(pathToFileURL(`${root}/packages/plugins/worker-budget-authority/runtime.ts`).href);
const rows = JSON.parse(readFileSync(file, 'utf8')).qualifications.map((row) => {
  const parsed = m.ReviewedTokenPolicy.parse(row.policy);
  const { digest, ...rest } = parsed;
  return { digest: m.tokenPolicyDigest(rest), stored: digest,
    unchanged: m.canonical(parsed) === m.canonical(row.policy), canonical: m.canonical(rest) };
});
const refusals = JSON.parse(readFileSync(file + '.bad', 'utf8')).map((p) => m.ReviewedTokenPolicy.safeParse(p).success);
process.stdout.write(JSON.stringify({ rows, refusals }) + '\\n');
"""


@unittest.skipUnless(os.environ.get("RELAY_FILE_B_ROUTER_GIT") and os.environ.get("RELAY_FILE_B_NODE_MODULES")
                     and os.environ.get("RELAY_FILE_B_NODE"), "Router cross-check not configured")
class RouterCrossCheck(unittest.TestCase):
    def test_router_schema_and_digest_agree(self):
        modules = Path(os.environ["RELAY_FILE_B_NODE_MODULES"])
        zod = json.loads((modules / "zod/package.json").read_text())["version"]
        self.assertEqual(zod, "4.6.5")  # llm-router 5a5181b7 pnpm-lock.yaml
        cases = [INPUTS, dict(INPUTS, provider="anthropic", transport="anthropic-messages",
                              baseUrl="https://api.anthropic.com", model="claude-x", accountId="o'k é東")]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            root.mkdir()
            archive = subprocess.run(["git", "-C", os.environ["RELAY_FILE_B_ROUTER_GIT"], "archive", ROUTER_COMMIT,
                                      "packages/plugins/worker-budget-authority/runtime.ts", "packages/contracts",
                                      "packages/modules/inference-budget"], capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(root)], input=archive, check=True)
            (root / "node_modules").symlink_to(modules)
            (root / "package.json").write_text('{"type":"module"}')
            probe = Path(temp) / "probe.mts"
            probe.write_text(ROUTER_PROBE)
            for inputs in cases:
                policy, file_b, _ = generate(inputs)
                path = Path(temp) / "file-b.json"
                path.write_bytes(file_b)
                # Mutations the Router schema must reject too (generator parity).
                bad = [dict(policy, extra=1), dict(policy, outputIncludesReasoning=False),
                       dict(policy, transport="responses"), dict(policy, providerGeneration=0),
                       dict(policy, baseUrl="not a url")]
                Path(str(path) + ".bad").write_text(json.dumps(bad))
                result = subprocess.run([os.environ["RELAY_FILE_B_NODE"], str(modules / "tsx/dist/cli.mjs"),
                                         str(probe), str(root), str(path)],
                                        capture_output=True, text=True, check=True, cwd=temp)
                out = json.loads(result.stdout)
                row = out["rows"][0]
                self.assertEqual(row["digest"], policy["digest"])
                self.assertEqual(row["stored"], policy["digest"])
                self.assertTrue(row["unchanged"])  # zod normalizes nothing (e.g. baseUrl trim)
                self.assertEqual(row["canonical"], check.canonical({k: v for k, v in policy.items() if k != "digest"}))
                self.assertEqual(out["refusals"], [False] * len(bad))
                if inputs is INPUTS:
                    self.assertEqual(row["digest"], GOLDEN_DIGEST)


def docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def pinned_image():
    for line in (ROOT / "kubernetes/optional/relay-budget-authority/postgres.yaml").read_text().splitlines():
        if line.strip().startswith("image:"):
            return line.split()[1]
    raise AssertionError("no image")


@unittest.skipUnless(os.environ.get("RELAY_FILE_B_RELAY_GIT") and docker_ok(), "ledger replay not configured")
class LedgerReplay(unittest.TestCase):
    """Throwaway PostgreSQL 17 (tmpfs, fsync=off, no network) with Relay's ledger schema."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"file-b-replay-{uuid.uuid4().hex[:12]}"
        image = os.environ.get("RELAY_FILE_B_POSTGRES_IMAGE") or pinned_image()
        subprocess.run(["docker", "run", "--rm", "-d", "--name", cls.name, "--network", "none",
                        "--tmpfs", "/var/lib/postgresql/data:size=256m", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                        image, "-c", "fsync=off", "-c", "full_page_writes=off", "-c", "synchronous_commit=off"],
                       capture_output=True, check=True)
        try:
            for _ in range(120):
                ready = subprocess.run(["docker", "exec", cls.name, "pg_isready", "-h", "/var/run/postgresql", "-U", "postgres"],
                                       capture_output=True)
                # The image's init phase runs a temporary server; wait for the final one.
                if ready.returncode == 0 and cls.psql_raw("SELECT 1", db="postgres").returncode == 0:
                    probe = subprocess.run(["docker", "logs", cls.name], capture_output=True, text=True)
                    if "PostgreSQL init process complete" in probe.stdout + probe.stderr:
                        break
                time.sleep(0.5)
            else:
                raise AssertionError("postgres did not start")
            version = cls.psql_raw("SHOW server_version_num", db="postgres", flags=["-At"]).stdout.strip()
            assert version.startswith("17"), version
            cls.psql_raw("CREATE DATABASE relay_budget", db="postgres", check=True)
        except BaseException:
            subprocess.run(["docker", "rm", "-fv", cls.name], capture_output=True)
            raise

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-fv", cls.name], capture_output=True)

    @classmethod
    def psql_raw(cls, sql, db="relay_budget", flags=(), check=False, user="postgres"):
        return subprocess.run(["docker", "exec", "-i", "-e", "PSQL_HISTORY=/dev/null", cls.name, "psql", "-X",
                               "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql", "-U", user, "-d", db,
                               *flags, "-f", "-"], input=sql, capture_output=True, text=True, check=check)

    def setUp(self):
        # A fresh database per test: the ledger schema at the pin, initialize.sh's
        # login roles, one authority row, then File A's rows.
        self.psql_raw("DROP DATABASE IF EXISTS relay_budget WITH (FORCE)", db="postgres", check=True)
        self.psql_raw("DROP ROLE IF EXISTS relay_budget_allocator; DROP ROLE IF EXISTS relay_budget_router;"
                      "DROP ROLE IF EXISTS relay_budget_owner; DROP ROLE IF EXISTS relay_budget_client;"
                      "DROP ROLE IF EXISTS relay_budget_reader; CREATE DATABASE relay_budget;", db="postgres", check=True)
        repo = os.environ["RELAY_FILE_B_RELAY_GIT"]
        for schema in ("001-authority.sql", "002-preflight.sql", "003-scoped-requests.sql",
                       "004-preflight-errors.sql", "005-provenance-purpose.sql"):
            self.psql_raw(git_show(repo, LEDGER_COMMIT, f"deploy/worker-budget-ledger/{schema}").decode(), check=True)
        self.psql_raw("""
CREATE ROLE relay_budget_allocator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE relay_budget_router LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT relay_budget_client TO relay_budget_allocator;
GRANT relay_budget_reader TO relay_budget_router;
INSERT INTO worker_budget.authority (instance_id, recovery_epoch, witness_digest, ready)
VALUES (gen_random_uuid(), gen_random_uuid(), repeat('a', 64), true);
INSERT INTO worker_budget.readers (login, deployment_digest)
VALUES ('relay_budget_router', '547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad');
INSERT INTO worker_budget.clients
  (login, issuer, connection_id, deployment_digest, tenant_ids, max_tokens, max_requests, max_authority_ms)
VALUES ('relay_budget_allocator', 'relay.chifor.me', 'babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8',
        '547353ac649b454646cad921598d8e231c5e9db41c8e99b028351969e1a261ad',
        ARRAY['00000000-0000-4000-8000-000000000001']::uuid[], 7441536, 8, 120000);
""", check=True)

    def qualification_count(self):
        return int(self.psql_raw("SELECT count(*) FROM worker_budget.qualifications", flags=["-At"], check=True).stdout)

    def run_file_b(self, sql):
        return self.psql_raw(sql.decode())

    def test_commit_readback_checker_and_rerun_refusal(self):
        policy, file_b, sql = generate(now_ms=None)
        first = self.run_file_b(sql)
        self.assertEqual(first.returncode, 0, first.stderr)
        lines = first.stdout.splitlines()
        self.assertIn("COMMIT", lines)
        readback = [line for line in lines if line.startswith('{"')]
        self.assertEqual(readback, [check.canonical(policy)])
        self.assertEqual(hashlib.sha256(readback[0].encode()).hexdigest(),
                         hashlib.sha256(check.canonical(policy).encode()).hexdigest())
        self.assertIn(f"deployment_digest={gen.DEFAULT_DEPLOYMENT_DIGEST} digest={GOLDEN_DIGEST} recomputed={GOLDEN_DIGEST}",
                      lines)
        # The checker's own zero-write ledger SQL and comparison accept the committed table.
        snapshot = self.psql_raw(check.LEDGER_SQL, flags=["-qAt"], check=True).stdout
        observed = check.parse_json(snapshot.encode(), "ledger")
        result = check.check_ledger(json.loads(file_b), observed, 3600, int(time.time() * 1000))
        self.assertEqual(result["status"], "pass")
        # The Router's runtime_snapshot path returns the identical policy object.
        stored = self.psql_raw("SELECT policy FROM worker_budget.qualifications", flags=["-qAt"], check=True).stdout
        self.assertEqual(json.loads(stored), policy)
        # A second run refuses and writes nothing.
        second = self.run_file_b(sql)
        self.assertEqual(second.returncode, 3)
        self.assertIn("qualifications must be empty", second.stderr)
        self.assertNotIn("COMMIT", second.stdout.splitlines())
        self.assertEqual(self.qualification_count(), 1)

    def test_policy_text_with_dollar_quote_tags_commits_exactly(self):
        model = "m$post$x$post1$y$pre$"
        policy, _, sql = generate(dict(INPUTS, model=model), now_ms=None)
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("COMMIT", result.stdout.splitlines())
        stored = self.psql_raw("SELECT policy FROM worker_budget.qualifications", flags=["-qAt"], check=True).stdout
        self.assertEqual(json.loads(stored), policy)
        self.assertEqual(json.loads(stored)["model"], model)

    def assert_refused(self, setup_sql, message, sql=None):
        self.psql_raw(setup_sql, check=True)
        result = self.run_file_b(sql or generate(now_ms=None)[2])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("COMMIT", result.stdout.splitlines())
        self.assertEqual(self.qualification_count(), 0)

    def test_refuses_when_file_a_differs(self):
        self.assert_refused("UPDATE worker_budget.clients SET max_requests = NULL", "clients must be exactly File A")

    def test_refuses_extra_reader(self):
        self.assert_refused("INSERT INTO worker_budget.readers VALUES ('postgres', repeat('b', 64))",
                            "readers must be exactly File A")

    def test_refuses_without_authority(self):
        self.assert_refused("DELETE FROM worker_budget.authority", "exactly one authority row")

    def test_refuses_other_deployment(self):
        other = "4" * 64
        _, _, sql = generate(dict(INPUTS, deployment_digest=other), override=other, now_ms=None)
        self.assert_refused("SELECT 1", "clients must be exactly File A", sql=sql)

    def test_refuses_expired_on_ledger_clock(self):
        inputs = dict(INPUTS, qualificationExpiresAt="2001-01-01T00:00:00Z")
        _, _, sql = generate(inputs, now_ms=1)
        self.assert_refused("SELECT 1", "has elapsed on the ledger clock", sql=sql)

    # Supersede (reissue).

    def ledger_rows(self):
        out = self.psql_raw("SELECT deployment_digest, digest, policy FROM worker_budget.qualifications ORDER BY 1, 2",
                            flags=["-qAt", "-F", "\t"], check=True).stdout
        return [(d, g, json.loads(p)) for d, g, p in (line.split("\t") for line in out.splitlines())]

    def commit_old(self, inputs=None):
        policy, file_b, sql = generate(inputs, now_ms=None)
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 0, result.stderr)
        return policy, file_b

    def assert_supersede_refused(self, sql, message):
        before = self.ledger_rows()
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(message, result.stderr)
        self.assertNotIn("COMMIT", result.stdout.splitlines())
        self.assertEqual(self.ledger_rows(), before)

    def test_supersede_commits_new_table_checker_passes_and_rerun_refuses(self):
        old_policy, old = self.commit_old()
        policy, file_b, sql = supersede(old=old, now_ms=None)
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertIn("COMMIT", lines)
        self.assertEqual([line for line in lines if line.startswith('{"')], [check.canonical(policy)])
        self.assertIn(f"deployment_digest={gen.DEFAULT_DEPLOYMENT_DIGEST} digest={policy['digest']} "
                      f"recomputed={policy['digest']}", lines)
        self.assertIn(f"superseded={GOLDEN_DIGEST} present=0", lines)
        self.assertEqual(self.ledger_rows(), [(gen.DEFAULT_DEPLOYMENT_DIGEST, policy["digest"], policy)])
        # The checker's zero-write ledger SQL and comparison accept the new file, and refuse the old one.
        snapshot = check.parse_json(self.psql_raw(check.LEDGER_SQL, flags=["-qAt"], check=True).stdout.encode(), "ledger")
        result = check.check_ledger(json.loads(file_b), snapshot, 3600, int(time.time() * 1000))
        self.assertEqual((result["status"], result["rows"]), ("pass", 1))
        with self.assertRaisesRegex(check.Fail, "row set mismatch"):
            check.check_ledger(json.loads(old), snapshot, 3600, int(time.time() * 1000))
        # Rerun: the old row is gone, so the table no longer equals the old File B.
        self.assert_supersede_refused(sql, "qualifications must be exactly the old reviewed File B")
        # A second reissue chains from the new file.
        third, _, sql3 = supersede(inputs=dict(NEW_INPUTS, providerGeneration=5), old=file_b,
                                   old_digest=policy["digest"], now_ms=None)
        self.assertEqual(self.run_file_b(sql3).returncode, 0)
        self.assertEqual(self.ledger_rows(), [(gen.DEFAULT_DEPLOYMENT_DIGEST, third["digest"], third)])

    def test_supersede_keeps_other_reviewed_rows(self):
        other, old = two_row_table()
        self.commit_old()
        self.psql_raw("INSERT INTO worker_budget.qualifications VALUES ("
                      f"'{gen.DEFAULT_DEPLOYMENT_DIGEST}', '{other['digest']}', '{check.canonical(other)}'::jsonb)", check=True)
        policy, file_b, sql = supersede(old=old, now_ms=None)
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ledger_rows(), sorted([(gen.DEFAULT_DEPLOYMENT_DIGEST, policy["digest"], policy),
                                                     (gen.DEFAULT_DEPLOYMENT_DIGEST, other["digest"], other)]))
        snapshot = check.parse_json(self.psql_raw(check.LEDGER_SQL, flags=["-qAt"], check=True).stdout.encode(), "ledger")
        self.assertEqual(check.check_ledger(json.loads(file_b), snapshot, 3600, int(time.time() * 1000))["rows"], 2)

    def test_supersede_refuses_on_empty_table(self):
        self.assert_supersede_refused(supersede(now_ms=None)[2], "qualifications must be exactly the old reviewed File B")

    def test_supersede_refuses_extra_row(self):
        _, old = self.commit_old()
        other, _ = two_row_table()
        self.psql_raw("INSERT INTO worker_budget.qualifications VALUES ("
                      f"'{gen.DEFAULT_DEPLOYMENT_DIGEST}', '{other['digest']}', '{check.canonical(other)}'::jsonb)", check=True)
        self.assert_supersede_refused(supersede(old=old, now_ms=None)[2],
                                      "qualifications must be exactly the old reviewed File B")

    def test_supersede_refuses_missing_row_of_old_table(self):
        _, old = two_row_table()
        self.commit_old()  # only the golden row; the old file lists two
        self.assert_supersede_refused(supersede(old=old, now_ms=None)[2],
                                      "qualifications must be exactly the old reviewed File B")

    def test_supersede_refuses_changed_old_row(self):
        _, old = self.commit_old()
        for change in ("jsonb_set(policy, '{providerGeneration}', '9')",
                       "jsonb_set(policy, '{providerGeneration}', '3.0')",  # jsonb-equal, not canonical-equal
                       "policy - 'evidenceDigest'"):
            with self.subTest(change=change):
                self.psql_raw(f"UPDATE worker_budget.qualifications SET policy = {change}", check=True)
                self.assert_supersede_refused(supersede(old=old, now_ms=None)[2],
                                              "qualifications must be exactly the old reviewed File B")
                self.psql_raw("UPDATE worker_budget.qualifications SET policy = "
                              f"'{check.canonical(generate()[0])}'::jsonb", check=True)
        # Restored to the reviewed row, it supersedes.
        self.assertEqual(self.run_file_b(supersede(old=old, now_ms=None)[2]).returncode, 0)

    def test_supersede_refuses_when_an_allocation_references_the_old_digest(self):
        _, old = self.commit_old()
        instance, epoch, witness = self.psql_raw(
            "SELECT instance_id, recovery_epoch, witness_digest FROM worker_budget.authority",
            flags=["-qAt", "-F", " "], check=True).stdout.split()
        now = int(time.time() * 1000)
        request = {"tenantId": "00000000-0000-4000-8000-000000000001", "workerId": str(uuid.uuid4()),
                   "connectionId": "babbf5ca-38ab-42b2-b7c7-f7d50a7d99e8", "deploymentDigest": gen.DEFAULT_DEPLOYMENT_DIGEST,
                   "limit": 1000, "originalDeadline": now + 600000, "authorityExpiresAt": now + 60000,
                   "grantId": str(uuid.uuid4()), "grantRevision": 1, "grantDigest": "7" * 64,
                   "tokenPolicyDigests": [GOLDEN_DIGEST]}
        # The allocator's own runtime call (as its login), exactly as Relay prepares an allocation.
        prepared = self.psql_raw(f"SELECT worker_budget.prepare('{instance}', '{epoch}', '{witness}', "
                                 f"'{json.dumps(request)}'::jsonb)", flags=["-qAt"], user="relay_budget_allocator", check=True)
        self.assertEqual(json.loads(prepared.stdout)["authorization"]["policyDigests"], [GOLDEN_DIGEST])
        self.assert_supersede_refused(supersede(old=old, now_ms=None)[2],
                                      "old policy digest is referenced by worker_budget.outbox")

    def test_supersede_refuses_when_a_scope_run_mentions_the_old_digest(self):
        _, old = self.commit_old()
        self.psql_raw("INSERT INTO worker_budget.scope_runs (run_digest, run_document, client_login, tenant_id, worker_ids,"
                      " token_cap, request_cap, expires_at, recovery_epoch) SELECT repeat('8', 64),"
                      f" jsonb_build_object('tokenPolicyDigests', jsonb_build_array('{GOLDEN_DIGEST}')),"
                      " 'relay_budget_allocator', '00000000-0000-4000-8000-000000000001', ARRAY[gen_random_uuid()],"
                      " 1000, 10, 9000000000000, recovery_epoch FROM worker_budget.authority", check=True)
        self.assert_supersede_refused(supersede(old=old, now_ms=None)[2],
                                      "old policy digest is referenced by worker_budget.scope_runs")

    def test_supersede_refuses_when_file_a_differs_or_expired(self):
        _, old = self.commit_old()
        _, _, expired = supersede(inputs=dict(NEW_INPUTS, qualificationExpiresAt="2001-01-01T00:00:00Z"), old=old, now_ms=1)
        self.assert_supersede_refused(expired, "has elapsed on the ledger clock")
        self.psql_raw("UPDATE worker_budget.clients SET max_requests = NULL", check=True)
        self.assert_supersede_refused(supersede(old=old, now_ms=None)[2], "clients must be exactly File A")

    def test_supersede_policy_text_with_dollar_quote_tags_commits_exactly(self):
        old_policy, old = self.commit_old(dict(INPUTS, model="o$pre$x$del$y$post$"))
        policy, _, sql = supersede(inputs=dict(NEW_INPUTS, model="n$post$z$post1$$del$"), old=old,
                                   old_digest=old_policy["digest"], now_ms=None)
        result = self.run_file_b(sql)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.ledger_rows(), [(gen.DEFAULT_DEPLOYMENT_DIGEST, policy["digest"], policy)])


if __name__ == "__main__":
    unittest.main()
