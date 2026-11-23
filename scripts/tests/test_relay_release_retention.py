#!/usr/bin/env python3
"""Relay release-asset retention: the planner's rule and verifier, and the wrapper's paging, dry run, sha gate,
id-only deletion and verification, against a fake forge.

    python3 -m unittest scripts.tests.test_relay_release_retention
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import relay_release_retention as rr  # noqa: E402

SHA = "a" * 40
BUNDLES = ["relay-codex-linux-x64-%s.tar.gz.part-0001" % SHA, "relay-codex-linux-x64-%s.tar.gz.part-0002" % SHA,
           "relay-pi-linux-x64-%s.tar.gz" % SHA, "relay-holder-%s.tar.gz" % SHA]
# near misses the planner must never select: receipts, checksums, other relay-* assets, a hostile name
SMALL = ["SHA256SUMS", "manifest.json", "control-plane-manifest.json", "relay-connector-linux-amd64",
         "relay-agent-tools-%s.tar.gz" % SHA, "install.sh", "relay-holder-receipt.json",
         "relay-pi-linux-x64-%s.tar.gz.sha256" % SHA, "relay-holder-%s.tar.gz\n9\t901" % SHA]


def release(rid, tag, day, names, **extra):
    r = {"id": rid, "tag_name": tag, "created_at": "2026-11-%02dT10:00:00Z" % day, "draft": False, "prerelease": False,
         "assets": [{"id": rid * 100 + n, "name": name, "size": 1000 * (n + 1)} for n, name in enumerate(names)]}
    r.update(extra)
    return r


def fixture():
    rels = [release(i, "v0.10.%d" % i, i, BUNDLES + SMALL) for i in range(1, 11)]   # v0.10.1 .. v0.10.10
    rels.append(release(75, "v0.2.0", 1, ["router-0.1.0-x.tar.gz", "relay-pi-linux-x64-%s.tar.gz" % SHA]))
    return rels


class Planner(unittest.TestCase):
    def test_the_rule(self):
        keep, items = rr.plan(fixture(), pinned={"v0.10.2"}, extra_keep=["v0.10.4"])
        self.assertEqual(set(keep), {"v0.10.10", "v0.10.9", "v0.10.8", "v0.10.7", "v0.10.6",   # newest 5
                                     "v0.10.2", "v0.10.4", "v0.2.0"})
        self.assertEqual({i["tag"] for i in items}, {"v0.10.1", "v0.10.3", "v0.10.5"})
        self.assertEqual(sorted({i["name"] for i in items}), sorted(BUNDLES))   # exactly the bundles, no near miss
        self.assertEqual(len(items), 3 * len(BUNDLES))

    def test_fewer_than_five_newest_is_refused(self):
        for n in (0, 1, 4):
            with self.assertRaises(ValueError):
                rr.plan(fixture(), set(), n)

    def test_drafts_and_prereleases_are_never_touched(self):
        rels = fixture()
        rels[0]["draft"] = True
        rels[2]["prerelease"] = True
        tags = {i["tag"] for i in rr.plan(rels, set())[1]}
        self.assertNotIn("v0.10.1", tags)
        self.assertNotIn("v0.10.3", tags)

    def test_the_sha_ignores_input_order_and_changes_with_the_list(self):
        a = rr.plan(fixture(), set())[1]
        b = rr.plan(list(reversed(fixture())), set())[1]
        self.assertEqual(rr.plan_sha256(a), rr.plan_sha256(b))
        self.assertNotEqual(rr.plan_sha256(a), rr.plan_sha256(rr.plan(fixture(), {"v0.10.1"})[1]))

    def test_versions_as_pins_write_them(self):
        text = "tag: v0.10.8\nversion: 0.10.11\nrc: v0.11.0-rc.1\nip: 192.168.0.36\nnode: 22.23.4.1\n"
        found = {"v" + m for m in rr.VERSION.findall(text)}
        self.assertEqual(found, {"v0.10.8", "v0.10.11", "v0.11.0-rc.1"})

    PATHS = ("kubernetes/apps/apps/relay", "ansible/roles/relay_connector/defaults", "docs/relay-workers")

    def tree(self, t):
        root = Path(t)
        for d in self.PATHS:
            (root / d).mkdir(parents=True)
        (root / "kubernetes/apps/apps/relay/relay.yaml").write_text("# hub v0.10.16, rollback v0.10.9\nip: 192.168.0.36\n")
        # a bare pin, as relay_connector_release writes it: no `v`, no helper comment (reviewer-codex, #1957)
        (root / "ansible/roles/relay_connector/defaults/main.yml").write_text("relay_connector_release:\n  version: 0.10.11\n")
        # the researcher binding's agent release (Codex review of #1957)
        (root / "docs/relay-workers/researcher-binding-inputs.json").write_text('{"agentRelease": {"tag": "v0.10.8"}}')
        return root

    def test_pins_are_read_from_every_source(self):
        with tempfile.TemporaryDirectory() as t:
            pins = rr.pinned_versions(self.tree(t), self.PATHS)
            self.assertEqual(pins, {"v0.10.16", "v0.10.9", "v0.10.11", "v0.10.8"})
            planned = {i["tag"] for i in rr.plan(fixture(), pins)[1]}
            self.assertFalse(planned & pins)

    def test_the_default_pin_sources_include_the_researcher_binding(self):
        self.assertIn("docs/relay-workers", rr.PIN_PATHS)
        self.assertTrue(all((ROOT / p).exists() for p in rr.PIN_PATHS), rr.PIN_PATHS)

    def test_an_unreadable_pin_file_stops_the_plan(self):
        with tempfile.TemporaryDirectory() as t:
            root = self.tree(t)
            bad = root / "ansible/roles/relay_connector/defaults/main.yml"
            real = Path.read_text

            def read_text(p, *a, **k):
                if p == bad:
                    raise PermissionError(13, "Permission denied", str(p))
                return real(p, *a, **k)
            with unittest.mock.patch.object(Path, "read_text", read_text):
                with self.assertRaises(rr.PinReadError):
                    rr.pinned_versions(root, self.PATHS)

    def test_an_unlistable_nested_directory_stops_the_plan(self):
        # Path.rglob() would skip it silently (reviewer-codex, #1957 round 3); os.walk's onerror must raise
        with tempfile.TemporaryDirectory() as t:
            root = self.tree(t)
            nested = root / "docs/relay-workers/sub"
            nested.mkdir()
            (nested / "pins.json").write_text('{"tag": "v0.10.3"}')
            real = os.scandir

            def scandir(p="."):
                if Path(p) == nested:
                    raise PermissionError(13, "Permission denied", str(p))
                return real(p)
            with unittest.mock.patch.object(os, "scandir", scandir):
                with self.assertRaises(rr.PinReadError):
                    rr.pinned_versions(root, self.PATHS)
            rel = Path(t) / "r.json"
            rel.write_text(json.dumps(fixture()))
            real_pins = rr.pinned_versions
            with unittest.mock.patch.object(os, "scandir", scandir), \
                    unittest.mock.patch.object(rr, "pinned_versions", lambda: real_pins(root, self.PATHS)), \
                    unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(rr.main(["plan", "--releases", str(rel)]), 3)
            self.assertNotIn("PLAN-SHA256", out.getvalue())

    def test_an_unknown_keep_tag_stops_the_plan(self):
        with self.assertRaises(ValueError):
            rr.plan(fixture(), set(), extra_keep=["v0.10.99"])

    def test_drafts_take_no_newest_slot(self):
        rels = fixture()
        rels.append(release(11, "v0.10.11", 20, BUNDLES, draft=True))   # the newest by date, but a draft
        keep = rr.plan(rels, set())[0]
        self.assertEqual({t for t, why in keep.items() if why.startswith("newest")},
                         {"v0.10.10", "v0.10.9", "v0.10.8", "v0.10.7", "v0.10.6"})

    def test_a_missing_pin_path_stops_the_plan(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(rr.PinReadError):
                rr.pinned_versions(self.tree(t), self.PATHS + ("ansible/roles/renamed_role",))

    def test_main_prints_no_plan_when_pins_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as t:
            rel = Path(t) / "r.json"
            rel.write_text(json.dumps(fixture()))
            with unittest.mock.patch.object(rr, "pinned_versions", side_effect=rr.PinReadError("cannot read x")):
                with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    self.assertEqual(rr.main(["plan", "--releases", str(rel)]), 3)
            self.assertNotIn("PLAN-SHA256", out.getvalue())

    def test_this_checkout_pins_the_running_hub(self):
        self.assertTrue(any(p.startswith("v0.10.") for p in rr.pinned_versions()))


class Verifier(unittest.TestCase):
    def setUp(self):
        self.before = fixture()
        self.items = rr.plan(self.before, set())[1]
        self.rel = json.loads(json.dumps(next(r for r in self.before if r["tag_name"] == "v0.10.1")))
        planned = {i["asset_id"] for i in self.items}
        self.rel["assets"] = [a for a in self.rel["assets"] if a["id"] not in planned]

    def test_exactly_the_planned_removal_passes(self):
        self.assertEqual(rr.verify(self.before, self.items, self.rel), [])

    def test_a_collateral_deletion_fails(self):
        self.rel["assets"] = [a for a in self.rel["assets"] if a["name"] != "manifest.json"]
        self.assertTrue(any("NOT in the plan are gone" in p for p in rr.verify(self.before, self.items, self.rel)))

    def test_a_planned_asset_still_present_fails(self):
        self.rel["assets"].append({"id": self.items[0]["asset_id"], "name": "x", "size": 1})
        self.assertTrue(any("still present" in p for p in rr.verify(self.before, self.items, self.rel)))

    def test_a_malformed_reread_fails(self):
        self.assertTrue(rr.verify(self.before, self.items, {"id": 1}))
        self.assertTrue(rr.verify(self.before, self.items, None))


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Wrapper(unittest.TestCase):
    """The wrapper against a fake forge that serves FOUR releases per page (a cap below the requested 50)."""

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
import json, os, re, sys
state_path = %(state)r
method, path = sys.argv[1], sys.argv[2]
rels = json.load(open(state_path))
if method == "GET" and "/releases?" in path:
    page = int(re.search(r"page=(\\d+)", path).group(1))
    print("HTTP 200"); print(json.dumps(rels[(page - 1) * 4: page * 4])); sys.exit(0)
m = re.match(r"/repos/cchifor/relay/releases/(\\d+)(?:/assets/(\\d+))?$", path)
rid = int(m.group(1)); rel = next(r for r in rels if r["id"] == rid)
if method == "GET":
    print("HTTP 200"); print(json.dumps(rel)); sys.exit(0)
if method == "DELETE":
    aid = int(m.group(2))
    rel["assets"] = [a for a in rel["assets"] if a["id"] != aid]
    if os.environ.get("FAKE_COLLATERAL") and rel["assets"]:
        rel["assets"] = [a for a in rel["assets"] if a["name"] != "manifest.json"]
    json.dump(rels, open(state_path, "w")); print("HTTP 204"); sys.exit(0)
print("HTTP 400"); sys.exit(1)
PY
""" % {"log": self.log.as_posix(), "py": Path(sys.executable).as_posix(), "state": state.as_posix()})
        fake.chmod(0o755)
        self.env = dict(os.environ, RELAY_RETENTION_API=fake.as_posix())
        self.env.pop("MSYS_NO_PATHCONV", None)
        self.state = state

    def run_wrapper(self, *args, **env):
        return subprocess.run(["bash", (ROOT / "scripts/relay-release-retention.sh").as_posix(), *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=180)

    def calls(self, method):
        return [l for l in self.log.read_text().splitlines() if l.startswith(method)] if self.log.exists() else []

    def sha_of(self, dry):
        return [l.split()[1] for l in dry.stdout.splitlines() if l.startswith("PLAN-SHA256 ")][0]

    def test_paging_dry_run_then_execute_exactly_the_plan(self):
        dry = self.run_wrapper()
        self.assertEqual(dry.returncode, 0, dry.stderr + dry.stdout)
        self.assertIn("releases read: 11", dry.stdout)            # 3 pages of 4 and the empty 4th
        self.assertEqual(len([c for c in self.calls("GET") if "page=" in c]), 4)
        self.assertIn("DRY RUN: nothing deleted", dry.stdout)
        self.assertEqual(self.calls("DELETE"), [])
        sha = self.sha_of(dry)

        wrong = self.run_wrapper("--execute", "0" * 64)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("the plan changed", wrong.stderr)
        self.assertEqual(self.calls("DELETE"), [])

        ok = self.run_wrapper("--execute", sha)
        self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)
        self.assertIn("DONE: plan %s executed and verified" % sha, ok.stdout)
        planned = rr.plan(fixture(), rr.pinned_versions())[1]
        want = sorted("DELETE /repos/cchifor/relay/releases/%d/assets/%d" % (i["release_id"], i["asset_id"]) for i in planned)
        self.assertEqual(sorted(self.calls("DELETE")), want)       # exactly the plan: the hostile name injected nothing

    def test_a_collateral_deletion_fails_verification(self):
        sha = self.sha_of(self.run_wrapper())
        r = self.run_wrapper("--execute", sha, FAKE_COLLATERAL="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("NOT in the plan are gone", r.stdout)

    def test_a_short_sha_and_a_low_keep_newest_are_refused(self):
        self.assertEqual(self.run_wrapper("--execute", "abc").returncode, 2)
        low = self.run_wrapper("--keep-newest", "1")
        self.assertNotEqual(low.returncode, 0)
        self.assertNotIn("PLAN-SHA256", low.stdout)
        self.assertEqual(self.calls("DELETE"), [])


if __name__ == "__main__":
    unittest.main()
