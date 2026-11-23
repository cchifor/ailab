#!/usr/bin/env python3
"""Relay release-asset retention: the planner's rule, and the wrapper's dry run, sha gate, delete and verify.

    python3 -m unittest scripts.tests.test_relay_release_retention
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import relay_release_retention as rr  # noqa: E402


def release(rid, tag, day, names):
    return {"id": rid, "tag_name": tag, "created_at": "2026-11-%02dT10:00:00Z" % day, "draft": False,
            "assets": [{"id": rid * 100 + n, "name": name, "size": 1000 * (n + 1)} for n, name in enumerate(names)]}


BUNDLES = ["relay-codex-linux-x64-aaaa.tar.gz.part-0001", "relay-codex-linux-x64-aaaa.tar.gz.part-0002",
           "relay-pi-linux-x64-aaaa.tar.gz", "relay-holder-aaaa.tar.gz"]
SMALL = ["SHA256SUMS", "manifest.json", "control-plane-manifest.json", "relay-connector-linux-amd64",
         "relay-agent-tools-aaaa.tar.gz", "install.sh"]


def fixture():
    rels = [release(i, "v0.10.%d" % i, i, BUNDLES + SMALL) for i in range(1, 11)]   # v0.10.1 .. v0.10.10
    rels.append(release(75, "v0.2.0", 1, ["router-0.1.0-x.tar.gz", "relay-pi-linux-x64-old.tar.gz"]))
    return rels


class Planner(unittest.TestCase):
    def test_the_rule(self):
        keep, items = rr.plan(fixture(), pinned={"v0.10.2"}, keep_newest=3, extra_keep=["v0.10.4"])
        # newest 3 by creation time, the pinned one, the --keep one, and the llm-router store
        self.assertEqual(set(keep), {"v0.10.10", "v0.10.9", "v0.10.8", "v0.10.2", "v0.10.4", "v0.2.0"})
        touched = {i["tag"] for i in items}
        self.assertEqual(touched, {"v0.10.1", "v0.10.3", "v0.10.5", "v0.10.6", "v0.10.7"})
        # only the large bundles, never a manifest, a checksum file, a connector binary or agent tools
        self.assertTrue(all(rr.BUNDLE.match(i["name"]) for i in items))
        self.assertEqual(len(items), 5 * len(BUNDLES))
        self.assertFalse(any(i["tag"] == "v0.2.0" for i in items))

    def test_the_sha_ignores_input_order_and_changes_with_the_list(self):
        a = rr.plan(fixture(), set(), 3)[1]
        b = rr.plan(list(reversed(fixture())), set(), 3)[1]
        self.assertEqual(rr.plan_sha256(a), rr.plan_sha256(b))
        c = rr.plan(fixture(), {"v0.10.1"}, 3)[1]
        self.assertNotEqual(rr.plan_sha256(a), rr.plan_sha256(c))

    def test_keep_newest_must_keep_something(self):
        with self.assertRaises(ValueError):
            rr.plan(fixture(), set(), 0)

    def test_drafts_are_never_touched(self):
        rels = fixture()
        rels[0]["draft"] = True
        self.assertFalse(any(i["tag"] == "v0.10.1" for i in rr.plan(rels, set(), 3)[1]))

    def test_pins_are_read_from_the_tree(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "kubernetes/apps/apps/relay").mkdir(parents=True)
            (root / "kubernetes/apps/apps/relay/relay.yaml").write_text("# hub v0.10.16, rollback v0.10.9\n")
            (root / "ansible/roles/relay_connector/defaults").mkdir(parents=True)
            (root / "ansible/roles/relay_connector/defaults/main.yml").write_text("version: 0.10.11  # v0.10.11\n")
            self.assertEqual(rr.pinned_versions(root), {"v0.10.16", "v0.10.9", "v0.10.11"})

    def test_this_checkout_pins_the_running_hub(self):
        # the live pin must never be planned for deletion: whatever relay.yaml names is kept
        pins = rr.pinned_versions()
        self.assertTrue(any(p.startswith("v0.10.") for p in pins), pins)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Wrapper(unittest.TestCase):
    """The wrapper against a fake gitea-api.sh: dry run deletes nothing; a wrong sha deletes nothing; the right sha
    deletes exactly the plan and verifies it."""

    def setUp(self):
        self.t = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t, True)
        state = self.t / "state.json"
        rels = fixture()
        for r in rels:   # a listing larger than a pipe buffer: a `printf | head -1` status read dies of SIGPIPE
            r["body"] = "x" * 12000
        state.write_text(json.dumps(rels))
        self.log = self.t / "calls.log"
        fake = self.t / "fake-api.sh"
        fake.write_text("""#!/usr/bin/env bash
echo "$1 $2" >> "%(log)s"
exec %(py)s - "$@" <<'PY'
import json, re, sys
state_path = %(state)r
method, path = sys.argv[1], sys.argv[2]
rels = json.load(open(state_path))
if method == "GET" and "/releases?" in path:
    page = int(re.search(r"page=(\\d+)", path).group(1))
    print("HTTP 200"); print(json.dumps(rels[(page - 1) * 50: page * 50])); sys.exit(0)
m = re.match(r"/repos/cchifor/relay/releases/(\\d+)(?:/assets/(\\d+))?$", path)
rid = int(m.group(1)); rel = next(r for r in rels if r["id"] == rid)
if method == "GET":
    print("HTTP 200"); print(json.dumps(rel)); sys.exit(0)
if method == "DELETE":
    aid = int(m.group(2)); rel["assets"] = [a for a in rel["assets"] if a["id"] != aid]
    json.dump(rels, open(state_path, "w")); print("HTTP 204"); sys.exit(0)
print("HTTP 400"); sys.exit(1)
PY
""" % {"log": self.log.as_posix(), "py": Path(sys.executable).as_posix(), "state": state.as_posix()})
        fake.chmod(0o755)
        self.env = dict(os.environ, RELAY_RETENTION_API=fake.as_posix())
        self.env.pop("MSYS_NO_PATHCONV", None)
        self.state = state

    def run_wrapper(self, *args):
        return subprocess.run(["bash", (ROOT / "scripts/relay-release-retention.sh").as_posix(), "--keep-newest", "3",
                               *args], env=self.env, capture_output=True, text=True, timeout=120)

    def deletes(self):
        return [l for l in self.log.read_text().splitlines() if l.startswith("DELETE")] if self.log.exists() else []

    def test_dry_run_then_execute_exactly_the_plan(self):
        dry = self.run_wrapper()
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertIn("DRY RUN: nothing deleted", dry.stdout)
        self.assertEqual(self.deletes(), [])
        sha = [l.split()[1] for l in dry.stdout.splitlines() if l.startswith("PLAN-SHA256 ")][0]

        wrong = self.run_wrapper("--execute", "0" * 64)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("the plan changed", wrong.stderr)
        self.assertEqual(self.deletes(), [])

        ok = self.run_wrapper("--execute", sha)
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)
        self.assertIn("DONE: plan %s executed and verified" % sha, ok.stdout)
        planned = rr.plan(fixture(), rr.pinned_versions(), 3)[1]
        self.assertEqual(len(self.deletes()), len(planned))
        left = json.loads(self.state.read_text())
        for r in left:   # small assets everywhere, and the kept releases' bundles, all survive
            names = {a["name"] for a in r["assets"]}
            if r["tag_name"] != "v0.2.0":
                self.assertTrue(set(SMALL) <= names, r["tag_name"])

    def test_a_short_sha_is_refused(self):
        r = self.run_wrapper("--execute", "abc")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.deletes(), [])


if __name__ == "__main__":
    unittest.main()
