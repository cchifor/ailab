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


def generate(inputs=None, **kwargs):
    kwargs.setdefault("now_ms", NOW)
    return gen.generate(copy.deepcopy(INPUTS if inputs is None else inputs), **kwargs)


class Generate(unittest.TestCase):
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
    def psql_raw(cls, sql, db="relay_budget", flags=(), check=False):
        return subprocess.run(["docker", "exec", "-i", "-e", "PSQL_HISTORY=/dev/null", cls.name, "psql", "-X",
                               "-v", "ON_ERROR_STOP=1", "-h", "/var/run/postgresql", "-U", "postgres", "-d", db,
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


if __name__ == "__main__":
    unittest.main()
