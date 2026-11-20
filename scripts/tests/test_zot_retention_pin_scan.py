"""Tests for scripts/zot-retention-pin-scan.py: the in-use safety gate for a zot retention change (no registry needed).

The retention model is checked against zot v2.1.18's semantics (pkg/retention/retention.go, rules.go): first-match
policy and keepTags entry, OR between the rules of one entry, tags without statistics kept, unstable sort at the
count boundary reported as ambiguous. End-to-end runs replay a registry snapshot (--snapshot-in)."""
import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

_PATH = pathlib.Path(__file__).resolve().parents[1] / "zot-retention-pin-scan.py"
_spec = importlib.util.spec_from_file_location("zot_retention_pin_scan", _PATH)
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

H = 3600 * 10**9
NOW = 1_000_000 * H
D1, D2, D3, D4 = ("sha256:" + c * 64 for c in "1234")
SHA = {"patterns": ["^sha-.*"], "mostRecentlyPushedCount": 2, "pushedWithin": "48h"}
POLICY = {"repositories": ["strive/**"], "deleteUntagged": True,
          "keepTags": [{"patterns": ["^latest$"]}, SHA]}


def tag(digest, age_h, pull_age_h=None, children=(), stats=True):
    return {"digest": digest, "push": NOW - age_h * H, "pull": 0 if pull_age_h is None else NOW - pull_age_h * H,
            "stats": stats, "children": list(children), "referrers": {}, "layers": {}}


class RefsTest(unittest.TestCase):
    def test_reference_shapes(self):
        text = ("image: registry.chifor.me/strive/web:sha-abc\n"
                f"image: registry.chifor.me/strive/ci-build/x@{D1}\n"
                f"image: registry.chifor.me/strive/web:ailab@{D2}\n"
                f"digest: {D3}\n"
                f"image: registry.chifor.me/library/python@{D4}\n"
                "image: registry.chifor.me/strive/{{ .Values.name }}\n")
        refs = {(r["repo"], r["tag"], r["digest"]) for r in scan.refs_from_text(text, "f")}
        # D4 sits in a library/python ref: not strive, and not a bare pin either
        self.assertEqual(refs, {("strive/web", "sha-abc", None), ("strive/ci-build/x", None, D1),
                                ("strive/web", "ailab", D2), (None, None, D3)})

    def test_comments_are_not_use(self):
        yaml_text = f"web:\n  image:\n    digest: {D1}  # was {D2}\n# rollback: registry.chifor.me/strive/web@{D3}\n"
        got = {r["digest"] for r in scan.refs_from_text(scan.without_comments(yaml_text, "ailab.yaml"), "f")}
        self.assertEqual(got, {D1})
        docker = f"# FROM registry.chifor.me/strive/web@{D1}\nFROM registry.chifor.me/strive/web@{D2} AS web\n"
        got = {r["digest"] for r in scan.refs_from_text(scan.without_comments(docker, "Dockerfile"), "f")}
        self.assertEqual(got, {D2})
        py = f'X = "registry.chifor.me/strive/app-runner@{D1}"  # old: {D2}\n'
        got = {r["digest"] for r in scan.refs_from_text(scan.without_comments(py, "domain.py"), "f")}
        self.assertEqual(got, {D1})
        templated = "{{- if .Values.x }}\n" + f"image: registry.chifor.me/strive/web@{D1} # {D2}\n"
        got = {r["digest"] for r in scan.refs_from_text(scan.without_comments(templated, "t.yaml"), "f")}
        self.assertEqual(got, {D1})

    def test_kube_spec_and_status_but_not_metadata(self):
        pod = {"kind": "Pod", "metadata": {"name": "p", "namespace": "n", "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": f"registry.chifor.me/strive/old@{D4}"}},
               "spec": {"initContainers": [{"image": f"registry.chifor.me/strive/init@{D1}"}],
                        "ephemeralContainers": [{"image": "registry.chifor.me/strive/debug:sha-1"}],
                        "containers": [{"image": "registry.chifor.me/strive/web:ailab",
                                        "env": [{"name": "RUNTIMES", "value": json.dumps(
                                            [{"image": f"registry.chifor.me/strive/workflow@{D2}"}])}]}]},
               "status": {"containerStatuses": [{"image": "x", "imageID": f"registry.chifor.me/strive/web@{D3}"}]}}
        refs = {(r["repo"], r["tag"], r["digest"]) for r in scan.refs_from_kube({"items": [pod]}, "pods.json")}
        self.assertEqual(refs, {("strive/init", None, D1), ("strive/debug", "sha-1", None),
                                ("strive/web", "ailab", None), ("strive/workflow", None, D2),
                                ("strive/web", None, D3)})


class PolicyModelTest(unittest.TestCase):
    def test_globs(self):
        rx = scan.glob_regex("strive/**")
        for repo, want in (("strive", True), ("strive/web", True), ("strive/a/b", True), ("strivex/a", False)):
            self.assertEqual(bool(rx.match(repo)), want, repo)
        self.assertTrue(scan.glob_regex("**").match("library/python"))
        self.assertTrue(scan.glob_regex("strive/web").match("strive/web"))
        self.assertFalse(scan.glob_regex("strive/web").match("strive/web/x"))
        for bad in ("strive/{a,b}", "a/**/b", "strive/[ab]"):
            with self.assertRaises(scan.ScanError):
                scan.glob_regex(bad)

    def test_first_matching_policy_wins(self):
        pols = [{"repositories": ["strive/ci-build/**"], "keepTags": []}, POLICY, {"repositories": ["**"]}]
        self.assertIs(scan.repo_policy(pols, "strive/ci-build/x"), pols[0])
        self.assertIs(scan.repo_policy(pols, "strive/web"), POLICY)
        self.assertIs(scan.repo_policy(pols, "library/python"), pols[2])
        self.assertIsNone(scan.repo_policy(pols[:2], "library/python"))

    def test_count_or_window(self):
        tags = {"sha-new": tag(D1, 1), "sha-2nd": tag(D2, 100), "sha-window": tag(D3, 30), "sha-old": tag(D4, 200)}
        got = {t: d for t, (d, _) in scan.simulate_repo(POLICY, tags, NOW).items()}
        # count 2 keeps the two newest (new, window); the 48h window adds nothing more; 2nd and old are out of both
        self.assertEqual(got, {"sha-new": "keep", "sha-window": "keep", "sha-2nd": "delete", "sha-old": "delete"})
        wide = {**POLICY, "keepTags": [{**SHA, "pushedWithin": "150h"}]}
        got = {t: d for t, (d, _) in scan.simulate_repo(wide, tags, NOW).items()}
        self.assertEqual(got["sha-2nd"], "keep")  # outside the count, inside the window: OR keeps it
        self.assertEqual(got["sha-old"], "delete")

    def test_entries_and_statistics(self):
        tags = {"latest": tag(D1, 5000), "other": tag(D2, 1), "sha-x": tag(D3, 5000, stats=False)}
        got = scan.simulate_repo(POLICY, tags, NOW)
        self.assertEqual(got["latest"][0], "keep")         # patterns-only entry
        self.assertEqual(got["other"][0], "delete")        # matches no entry
        self.assertEqual(got["sha-x"], ("keep", "tag statistics not found"))
        self.assertTrue(all(d == "keep" for d, _ in scan.simulate_repo(None, tags, NOW).values()))

    def test_first_matching_entry_claims_the_tag(self):
        pol = {"repositories": ["**"], "keepTags": [{"patterns": ["^sha-"], "mostRecentlyPushedCount": 1},
                                                    {"patterns": ["^sha-old"]}]}
        got = scan.simulate_repo(pol, {"sha-new": tag(D1, 1), "sha-old": tag(D2, 9)}, NOW)
        self.assertEqual(got["sha-old"][0], "delete")  # the second entry never sees it

    def test_tie_at_the_count_boundary_is_ambiguous(self):
        tags = {"sha-a": tag(D1, 1), "sha-b": tag(D2, 5), "sha-c": tag(D3, 5)}
        pol = {**POLICY, "keepTags": [{"patterns": ["^sha-"], "mostRecentlyPushedCount": 2}]}
        got = {t: d for t, (d, _) in scan.simulate_repo(pol, tags, NOW).items()}
        self.assertEqual(got, {"sha-a": "keep", "sha-b": "ambiguous", "sha-c": "ambiguous"})

    def test_pulled_within_counts_a_pull_or_a_push(self):
        pol = {**POLICY, "keepTags": [{"patterns": ["^ailab-"], "mostRecentlyPushedCount": 1, "pulledWithin": "24h"}]}
        tags = {"ailab-new": tag(D1, 1), "ailab-pinned": tag(D2, 900, pull_age_h=3), "ailab-stale": tag(D3, 900, 400)}
        got = {t: d for t, (d, _) in scan.simulate_repo(pol, tags, NOW).items()}
        self.assertEqual(got, {"ailab-new": "keep", "ailab-pinned": "keep", "ailab-stale": "delete"})

    def test_durations_and_times(self):
        self.assertEqual(scan.parse_duration_ns("336h"), 336 * H)
        self.assertEqual(scan.parse_duration_ns("1h30m"), 90 * 60 * 10**9)
        with self.assertRaises(scan.ScanError):
            scan.parse_duration_ns("14d")
        self.assertEqual(scan.parse_time_ns("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(scan.parse_time_ns("0001-01-01T00:00:00Z"), 0)
        a = scan.parse_time_ns("2026-11-17T15:26:04.995273539Z")
        self.assertEqual(a - scan.parse_time_ns("2026-11-17T15:26:04.995273538Z"), 1)  # ns precision for ties


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tags = {"sha-kept": tag(D1, 1, children=[D3]), "sha-gone": tag(D2, 500, children=[D3, D4])}
        decisions = scan.simulate_repo({**POLICY, "keepTags": [{"patterns": ["^sha-"], "mostRecentlyPushedCount": 1}]},
                                       self.tags, NOW)
        self.repos = {"strive/web": {"tags": self.tags, "decisions": decisions, "policy": POLICY}}

    def run_refs(self, *refs):
        return scan.evaluate(self.repos, [dict(r, source="t") for r in refs])

    def test_tag_plus_digest_follows_the_digest(self):
        # a runtime pulls repo:tag@digest by the digest: a deleted tag does not matter while the digest stays
        fails, warns, _ = self.run_refs({"repo": "strive/web", "tag": "sha-gone", "digest": D1})
        self.assertEqual((fails, warns), ([], []))
        fails, _, _ = self.run_refs({"repo": "strive/web", "tag": "sha-kept", "digest": D2})
        self.assertEqual(len(fails), 1)

    def test_untagged_in_use_digest_fails_where_untagged_is_collected(self):
        untagged = "sha256:" + "7" * 64
        for exists in (True, None):  # confirmed, or not determined: fail closed
            fails, _, _ = self.run_refs({"repo": "strive/web", "tag": None, "digest": untagged, "exists": exists})
            self.assertEqual(len(fails), 1, exists)
            self.assertIn("deleteUntagged", fails[0]["why"])
        self.repos["strive/web"]["policy"] = {**POLICY, "deleteUntagged": False}
        fails, warns, _ = self.run_refs({"repo": "strive/web", "tag": None, "digest": untagged, "exists": True})
        self.assertEqual((fails, warns), ([], []))
        self.assertTrue(scan.deletes_untagged(None))  # no policy: zot's default deletes untagged manifests
        self.assertTrue(scan.deletes_untagged({"repositories": ["x"]}))

    def test_digest_held_by_a_kept_tag_or_a_kept_index_survives(self):
        fails, warns, _ = self.run_refs({"repo": "strive/web", "tag": None, "digest": D1},
                                        {"repo": "strive/web", "tag": None, "digest": D3})  # D3: child of a kept index
        self.assertEqual((fails, warns), ([], []))

    def test_doomed_digest_tag_and_child_fail(self):
        fails, _, _ = self.run_refs({"repo": "strive/web", "tag": None, "digest": D2},
                                    {"repo": "strive/web", "tag": "sha-gone", "digest": None},
                                    {"repo": "strive/web", "tag": None, "digest": D4})
        self.assertEqual(len(fails), 3)

    def test_bare_digest_is_matched_across_repos(self):
        fails, _, notes = self.run_refs({"repo": None, "tag": None, "digest": D2},
                                        {"repo": None, "tag": None, "digest": "sha256:" + "9" * 64})
        self.assertEqual(len(fails), 1)
        self.assertEqual(len(notes), 1)

    def test_unknown_references_warn(self):
        _, warns, _ = self.run_refs({"repo": "strive/web", "tag": "sha-nope", "digest": None},
                                    {"repo": "strive/other", "tag": "latest", "digest": None},
                                    {"repo": "strive/web", "tag": None, "digest": "sha256:" + "8" * 64, "exists": False})
        self.assertEqual(len(warns), 3)

    def test_totals_count_children_once_and_spare_kept_children(self):
        self.tags["sha-gone"]["referrers"] = {"": [], D3: ["sha256:" + "a" * 64], D4: ["sha256:" + "b" * 64]}
        per, _ = scan.totals(self.repos)
        # index D2 + child D4 + D4's referrer; D3 stays (the kept index lists it) and so does its referrer
        self.assertEqual(per["strive/web"]["manifests"], 3)
        self.assertEqual((per["strive/web"]["images"], per["strive/web"]["tags"]["delete"]), (1, 1))


class MainTest(unittest.TestCase):
    """End to end over a snapshot, with a config file, kubectl JSON and a scanned file."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.dir.name)
        t = "2026-11-20T10:00:00Z"
        old, new = "2026-10-01T00:00:00Z", "2026-11-19T00:00:00Z"
        self.snapshot = d / "snap.json"
        self.snapshot.write_text(json.dumps({"taken_at": t, "exists": {}, "repos": {"strive/web": {
            "sha-new": {**tag(D1, 0), "push": scan.parse_time_ns(new), "pull": 0},
            "sha-old": {**tag(D2, 0), "push": scan.parse_time_ns(old), "pull": 0},
            "latest": {**tag(D1, 0), "push": scan.parse_time_ns(new), "pull": 0}}}}))
        self.config = d / "config.json"
        self.config.write_text(json.dumps({"storage": {"retention": {"policies": [
            {"repositories": ["strive/**"], "keepTags": [{"patterns": ["^latest$"]},
                                                         {"patterns": ["^sha-"], "mostRecentlyPushedCount": 1}]}]}}}))
        self.empty = d / "empty"
        self.empty.mkdir()
        self.d = d

    def tearDown(self):
        self.dir.cleanup()

    def run_main(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = scan.main(["--config", str(self.config), "--snapshot-in", str(self.snapshot),
                            "--scan", str(self.empty), *extra])
        return rc, out.getvalue()

    def test_pass_and_fail(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0, out)
        pods = self.d / "pods.json"
        pods.write_text(json.dumps({"items": [{"kind": "Pod", "metadata": {"name": "w"}, "spec": {
            "containers": [{"image": f"registry.chifor.me/strive/web@{D2}"}]}}]}))
        rc, out = self.run_main("--kube-json", str(pods), "--json", str(self.d / "out.json"))
        self.assertEqual(rc, 1, out)
        self.assertIn("RESULT: FAIL", out)
        self.assertEqual(len(json.loads((self.d / "out.json").read_text())["failures"]), 1)

    def test_existing_untagged_pinned_digest_fails(self):
        untagged = "sha256:" + "7" * 64
        snap = json.loads(self.snapshot.read_text())
        snap["exists"] = {f"strive/web@{untagged}": True}
        self.snapshot.write_text(json.dumps(snap))
        (self.empty / "pins.yaml").write_text(f"image: registry.chifor.me/strive/web@{untagged}\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 1, out)
        self.assertIn("exists untagged", out)

    def test_unreadable_source_fails_closed(self):
        (self.empty / "pins.yaml").write_text(f"image: registry.chifor.me/strive/web@{D2}\n")
        real = pathlib.Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "pins.yaml":
                raise PermissionError(13, "Permission denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "read_text", read_text):
            rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read reference source", out)

    def test_unexpected_error_is_not_a_fail_verdict(self):
        self.snapshot.write_text("{not json")
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)  # exit 1 is reserved for "an in-use image would be deleted"

    def test_fail_closed(self):
        self.config.write_text(json.dumps({"storage": {"retention": {"policies": [
            {"repositories": ["strive/{a,b}"], "keepTags": [{"patterns": ["x"]}]}]}}}))
        rc, out = self.run_main()
        self.assertEqual(rc, 2, out)
        self.assertIn("FAIL-CLOSED", out)
        rc, out = self.run_main("--platform", str(self.empty))  # not a platform checkout
        self.assertEqual(rc, 2, out)


class RoleRenderTest(unittest.TestCase):
    """The scan reads the policy the role renders (needs jinja2 + PyYAML, like the role's own tests)."""

    def test_renders_the_strive_policy(self):
        retention = scan.render_role_retention(scan.DEFAULT_ROLE, {})
        strive = scan.repo_policy(retention["policies"], "strive/airlock")
        self.assertTrue(strive["deleteUntagged"])
        self.assertIs(retention["dryRun"], False)
        override = scan.render_role_retention(scan.DEFAULT_ROLE, scan.parse_set(["registry_zot_strive_keep_recent=20"]))
        counts = {e.get("mostRecentlyPushedCount") for p in override["policies"] for e in p.get("keepTags") or []
                  if p["repositories"] != ["agentforge/**"]}
        self.assertIn(20, counts)


if __name__ == "__main__":
    unittest.main()
