#!/usr/bin/env python3
"""Offline tests for scripts/s2s/flux-resume.sh: resuming the frozen strive release.

A stateful fake Flux stands in for the cluster:
- The GitRepository `flux-system/platform` fetches platform main some polls after the reconcile
  request.
- The Kustomization `platform-app` applies the fetched revision some polls after it is resumed,
  and that re-apply clears the HelmRelease's hand-set suspend (as observed live on 2026-10-06 at
  17:22:39Z).
- The HelmRelease upgrades its chart to the applied revision.

A fake `git` answers `ls-remote` for platform main. Faults model what the gates must catch:
- a source stuck on the old commit;
- a Kustomization that never applies;
- a HelmRelease left suspended;
- an upgrade that never lands;
- a harness that survives a revert;
- main moving on while the resume waits.

    python3 -m unittest scripts.s2s.test_flux_resume

FAIL-SAFE, as in test_phase4_probes: every run first asserts that `kubectl` and `git` resolve to
this test's stubs. KUBECONFIG points at an empty file and every proxy at a closed port. This file
is also the stub (`--fake-kubectl`, `--fake-git`).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "flux-resume.sh"
OLD = "1" * 40
TARGET = "a" * 40
LATER = "b" * 40
WRITES = {"annotate", "patch", "scale", "rollout", "delete", "apply", "edit", "label", "replace"}
#: The Kustomizations that apply platform-app (R19: a freeze suspends them top-down).
PARENTS = ("platform", "flux-system")


def find_bash():
    override = os.environ.get("PHASE4_TEST_BASH")
    if override:
        return override
    if os.name == "nt":
        # usr/bin/bash.exe keeps the caller's PATH order; bin/bash.exe (a launcher) puts ~/bin,
        # which may hold the real kubectl, ahead of the stubs.
        candidate = r"C:\Program Files\Git\usr\bin\bash.exe"
        if os.path.exists(candidate):
            return candidate
    return shutil.which("bash")


BASH = find_bash()


def rev(sha):
    return "main@sha1:" + sha


# ── The fakes ──────────────────────────────────────────────────────────────


def _load(state_dir):
    with open(os.path.join(state_dir, "flux.json"), encoding="utf-8") as f:
        return json.load(f)


def _save(state_dir, st):
    with open(os.path.join(state_dir, "flux.json"), "w", encoding="utf-8") as f:
        json.dump(st, f)


def _log(state_dir, entry):
    with open(os.path.join(state_dir, "calls.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _out(text):
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


def fake_git(argv):
    state = os.environ["FLUX_FAKE_DIR"]
    st = _load(state)
    _log(state, ["git"] + argv)
    if argv[:1] != ["ls-remote"] or argv[-1] != "refs/heads/main":
        sys.stderr.write("fake git: unexpected %r\n" % (argv,))
        return 99
    st["ls_remote_calls"] = st.get("ls_remote_calls", 0) + 1
    if st.get("main_moves_after") is not None and st["ls_remote_calls"] > st["main_moves_after"]:
        st["main"] = st["main_next"]
    _save(state, st)
    return _out("%s\trefs/heads/main\n" % st["main"])


def fake_kubectl(argv):
    state = os.environ["FLUX_FAKE_DIR"]
    st = _load(state)
    _log(state, ["kubectl"] + argv)
    args, i = [], 0
    while i < len(argv):
        if argv[i] in ("--context", "-n"):
            i += 2
            continue
        args.append(argv[i])
        i += 1
    kind = args[1] if len(args) > 1 else ""
    try:
        if args[0] == "annotate" and kind == "gitrepository":
            st["annotated"] = True
            return _out("gitrepository.source.toolkit.fluxcd.io/platform annotated\n")
        if args[0] == "get" and kind == "gitrepository":
            if st.get("annotated") and st.get("source_fetches", True):
                st["source_polls"] = st.get("source_polls", 0) + 1
                if st["source_polls"] >= st.get("source_after", 1):
                    st["source"] = rev(st["main"])
            return _out(st["source"])
        name = args[2] if len(args) > 2 else ""
        if args[0] == "patch" and kind == "kustomization" and name in PARENTS:
            assert '"suspend":false' in args[-1]
            st.setdefault("parents", {})[name] = False
            return _out("kustomization.kustomize.toolkit.fluxcd.io/%s patched\n" % name)
        if args[0] == "annotate" and kind == "kustomization" and name in PARENTS:
            value = [a for a in args if a.startswith("reconcile.fluxcd.io/requestedAt=")][0].split("=", 1)[1]
            st.setdefault("parent_requested", {})[name] = value
            return _out("kustomization.kustomize.toolkit.fluxcd.io/%s annotated\n" % name)
        if args[0] == "get" and kind == "kustomization" and name in PARENTS:
            if name in st.get("parent_get_fails", []):
                sys.stderr.write("error: the server is currently unable to handle the request\n")
                return 1
            suspended = st.get("parents", {}).get(name, False)
            handled = st.setdefault("parent_handled", {}).get(name, "stale")
            if not suspended and name not in st.get("parent_never_reconciles", []):
                handled = st.get("parent_requested", {}).get(name, handled)
                st["parent_handled"][name] = handled
            return _out("%s|%s|True" % ("true" if suspended else "false", handled))
        if args[0] == "patch" and kind == "kustomization":
            assert '"suspend":false' in args[-1]
            st["ks_suspended"] = False
            return _out("kustomization.kustomize.toolkit.fluxcd.io/platform-app patched\n")
        if args[0] == "get" and kind == "kustomization":
            if not st["ks_suspended"] and st.get("ks_applies", True):
                st["ks_polls"] = st.get("ks_polls", 0) + 1
                if st["ks_polls"] >= st.get("ks_after", 1):
                    st["ks_applied"] = st["source"]
                    if st.get("ks_clears_hr_suspend", True):
                        st["hr_suspended"] = False
            return _out("%s|%s|True" % ("true" if st["ks_suspended"] else "false", st["ks_applied"]))
        if args[0] == "patch" and kind == "helmrelease":
            assert '"suspend":false' in args[-1]
            st["hr_suspended"] = False
            return _out("helmrelease.helm.toolkit.fluxcd.io/strive patched\n")
        if args[0] == "get" and kind == "helmrelease":
            if not st["hr_suspended"] and st.get("hr_upgrades", True):
                st["hr_polls"] = st.get("hr_polls", 0) + 1
                if st["hr_polls"] >= st.get("hr_after", 1):
                    applied = st["ks_applied"].split("sha1:")[-1]
                    st["chart"] = "0.2.0+%s.1" % applied[:12]
                    if st.get("revert") and not st.get("harness_survives"):
                        st["harness"] = False
            return _out("%s|%s|True" % ("true" if st["hr_suspended"] else "", st["chart"]))
        if args[0] == "get" and kind == "deployment":
            if any(a.startswith("jsonpath=") and "availableReplicas" in a for a in args):
                n = st.get("harness_replicas", 1) if st["harness"] else 0
                return _out("%d|%s" % (n, n if n else ""))
            return _out("deployment.apps/harness\n" if st["harness"] else "")
        if args[0] == "scale":
            st["replicas"] = int(args[-1].split("=")[1])
            return _out("deployment.apps/harness scaled\n")
        if args[0] == "rollout":
            return _out('deployment "harness" successfully rolled out\n')
        sys.stderr.write("fake kubectl: unexpected %r\n" % (argv,))
        return 99
    finally:
        _save(state, st)


# ── The tests ──────────────────────────────────────────────────────────────


@unittest.skipIf(BASH is None, "bash is required (set PHASE4_TEST_BASH)")
class FluxResume(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="flux-resume-")
        self.stubs = os.path.join(self.dir, "bin")
        os.mkdir(self.stubs)
        py = sys.executable.replace("\\", "/")
        me = str(pathlib.Path(__file__).resolve()).replace("\\", "/")
        for name, role in (("kubectl", "--fake-kubectl"), ("git", "--fake-git")):
            path = os.path.join(self.stubs, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write('#!/usr/bin/env bash\nexec "%s" "%s" %s "$@"\n' % (py, me, role))
            os.chmod(path, 0o755)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def state(self, **overrides):
        st = {
            "main": TARGET,
            "source": rev(OLD),
            "source_after": 2,
            "ks_suspended": True,
            "ks_applied": rev(OLD),
            "ks_after": 2,
            "hr_suspended": True,
            "hr_after": 2,
            "chart": "0.2.0+%s.1" % OLD[:12],
            "harness": True,
            "revert": False,
        }
        st.update(overrides)
        _save(self.dir, st)

    def env(self):
        env = dict(os.environ)
        env.update(
            PATH=self.stubs + os.pathsep + env.get("PATH", ""),
            FLUX_FAKE_DIR=self.dir,
            PHASE4_RESUME_POLL_SECONDS="0.1",
            PHASE4_RESUME_TIMEOUT_SECONDS="8",
        )
        kubeconfig = os.path.join(self.dir, "empty-kubeconfig")
        open(kubeconfig, "w").close()
        env["KUBECONFIG"] = kubeconfig
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[var] = "http://127.0.0.1:9"
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
        found = subprocess.run([BASH, "-c", "command -v kubectl; command -v git"], env=env, capture_output=True, text=True)
        paths = found.stdout.split()
        self.assertEqual(len(paths), 2, found.stdout + found.stderr)
        for path in paths:
            self.assertIn("/%s/bin/" % os.path.basename(self.dir), path, "not the stub: %s (refusing to run)" % path)
        return env

    def run_script(self, *args):
        done = subprocess.run([BASH, str(SCRIPT)] + list(args), env=self.env(), capture_output=True, timeout=300)
        return done.returncode, done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")

    def calls(self):
        path = os.path.join(self.dir, "calls.log")
        return [json.loads(line) for line in open(path, encoding="utf-8")] if os.path.exists(path) else []

    def verbs(self):
        out = []
        for call in self.calls():
            if call[0] != "kubectl":
                continue
            args = [a for i, a in enumerate(call[1:]) if a not in ("--context", "-n") and (i == 0 or call[1:][i - 1] not in ("--context", "-n"))]
            kind = args[1] if len(args) > 1 else ""
            if kind == "kustomization" and len(args) > 2 and args[2] in PARENTS:
                kind = "kustomization/" + args[2]
            out.append((args[0], kind))
        return out

    def index(self, verb, kind):
        verbs = self.verbs()
        self.assertIn((verb, kind), verbs, verbs)
        return verbs.index((verb, kind))

    def final(self):
        return _load(self.dir)

    def test_after_drill_lands_the_commit_in_order(self):
        self.state()
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 0, text)
        self.assertIn("RESUMED", text)
        # The source is reconciled and verified BEFORE the Kustomization is resumed, and the
        # Kustomization is verified before the HelmRelease is read and before the scale.
        annotate, ks = self.index("annotate", "gitrepository"), self.index("patch", "kustomization")
        last_source_get = max(i for i, v in enumerate(self.verbs()) if v == ("get", "gitrepository"))
        self.assertLess(annotate, last_source_get)
        self.assertLess(last_source_get, ks)
        first_hr_get = min(i for i, v in enumerate(self.verbs()) if v == ("get", "helmrelease"))
        last_ks_get = max(i for i, v in enumerate(self.verbs()) if v == ("get", "kustomization"))
        self.assertLess(last_ks_get, first_hr_get)
        self.assertLess(first_hr_get, self.index("scale", "deployment/harness"))
        st = self.final()
        self.assertEqual(st["ks_applied"], rev(TARGET))
        self.assertIn(TARGET[:12], st["chart"])
        self.assertEqual(st.get("replicas"), 1)
        # The Kustomization's re-apply cleared the HelmRelease suspend: no hand patch was needed.
        self.assertNotIn(("patch", "helmrelease"), self.verbs())
        self.assertIn("phase4-probes.sh", text)

    def test_after_revert_requires_the_harness_gone(self):
        self.state(revert=True)
        rc, text = self.run_script("--after-revert")
        self.assertEqual(rc, 0, text)
        self.assertNotIn(("scale", "deployment/harness"), self.verbs())
        self.assertIn("deployment/harness is gone", text)

    def test_a_harness_that_survives_the_revert_stops(self):
        self.state(revert=True, harness_survives=True)
        rc, text = self.run_script("--after-revert")
        self.assertEqual(rc, 1, text)
        self.assertIn("STOP", text)
        self.assertIn("deployment/harness still exists", text)

    def test_a_stale_source_never_resumes_anything(self):
        # The reviewer's case: the source still serves the pre-merge artifact. Resuming the
        # Kustomization would re-apply the OLD HelmRelease and clear its suspend.
        self.state(source_fetches=False)
        rc, text = self.run_script("--after-revert")
        self.assertEqual(rc, 1, text)
        self.assertIn("STOP", text)
        self.assertIn("GitRepository flux-system/platform", text)
        self.assertNotIn(("patch", "kustomization"), self.verbs())
        self.assertNotIn(("patch", "helmrelease"), self.verbs())
        self.assertTrue(self.final()["ks_suspended"])

    def test_an_explicit_sha_is_the_target(self):
        # Main is at TARGET, but the operator pins LATER (not yet fetched): the gate must wait for it.
        self.state()
        rc, text = self.run_script("--after-drill", "--sha", LATER)
        self.assertEqual(rc, 1, text)
        self.assertNotIn(("patch", "kustomization"), self.verbs())

    def test_main_moving_on_is_accepted_as_a_descendant(self):
        # Platform main advances while the resume waits; the source fetches the newer main, which
        # (main being protected, append-only) descends from the target. That is what lands.
        self.state(main_moves_after=1, main_next=LATER, source_after=3)
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 0, text)
        st = self.final()
        self.assertEqual(st["ks_applied"], rev(LATER))
        self.assertIn(LATER[:12], st["chart"])

    def test_a_kustomization_that_never_applies_stops_before_the_helmrelease(self):
        self.state(ks_applies=False)
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertIn("Kustomization flux-system/platform-app", text)
        self.assertNotIn(("get", "helmrelease"), self.verbs())
        self.assertNotIn(("scale", "deployment/harness"), self.verbs())

    def test_a_helmrelease_still_suspended_is_resumed_only_after_the_apply(self):
        self.state(ks_clears_hr_suspend=False)
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 0, text)
        last_ks_get = max(i for i, v in enumerate(self.verbs()) if v == ("get", "kustomization"))
        self.assertLess(last_ks_get, self.index("patch", "helmrelease"))
        self.assertIn("still suspended", text)

    def test_an_upgrade_that_never_lands_stops_before_the_scale(self):
        self.state(hr_upgrades=False)
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertIn("HelmRelease strive-ailab/strive", text)
        self.assertNotIn(("scale", "deployment/harness"), self.verbs())

    def test_unreadable_main_changes_nothing(self):
        self.state(main="not-a-sha")
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertFalse(WRITES & {v for v, _ in self.verbs()})

    def test_dry_run_and_usage_make_no_call(self):
        self.state()
        rc, text = self.run_script("--after-revert", "--dry-run")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.calls(), [])
        for needle in ("reconcile.fluxcd.io/requestedAt", ".status.artifact.revision", ".status.lastAppliedRevision", ".status.history[0].chartVersion", '"suspend":false'):
            self.assertIn(needle, text)
        for bad in ((), ("--after-drill", "--after-revert"), ("--after-drill", "--sha", "abc"), ("--bogus",)):
            rc, _ = self.run_script(*bad)
            self.assertEqual(rc, 2, bad)
        self.assertEqual(self.calls(), [])

    def test_suspended_parents_are_resumed_last_platform_then_flux_system(self):
        # R19: a freeze suspends flux-system, platform, platform-app and the HelmRelease top-down,
        # because a parent re-applies its children and drops a kubectl-patch suspend. They are
        # resumed only after the release landed, the inner parent first.
        self.state(parents={"platform": True, "flux-system": True})
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 0, text)
        last_hr_get = max(i for i, v in enumerate(self.verbs()) if v == ("get", "helmrelease"))
        platform = self.index("patch", "kustomization/platform")
        flux_system = self.index("patch", "kustomization/flux-system")
        self.assertLess(last_hr_get, platform)
        self.assertLess(platform, flux_system)
        self.assertEqual(self.final()["parents"], {"platform": False, "flux-system": False})
        self.assertIn("RESUMED", text)

    def test_a_gate_stop_leaves_the_parents_suspended(self):
        self.state(parents={"platform": True, "flux-system": True}, source_fetches=False)
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertNotIn(("patch", "kustomization/platform"), self.verbs())
        self.assertNotIn(("patch", "kustomization/flux-system"), self.verbs())
        self.assertEqual(self.final()["parents"], {"platform": True, "flux-system": True})

    def test_parents_that_are_not_suspended_are_warned_about_and_left_alone(self):
        self.state(parents={"platform": False, "flux-system": False})
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 0, text)
        self.assertIn("WARNING", text)
        self.assertIn("top-down", text)
        self.assertNotIn(("patch", "kustomization/platform"), self.verbs())
        self.assertNotIn(("patch", "kustomization/flux-system"), self.verbs())

    def test_after_config_keeps_the_harness(self):
        # R22: drill 2 keeps the assistant up; a config-only rollback/re-forward must leave the
        # harness deployed (no scale, no removal).
        self.state(parents={"platform": True, "flux-system": True})
        rc, text = self.run_script("--after-config")
        self.assertEqual(rc, 0, text)
        self.assertNotIn(("scale", "deployment/harness"), self.verbs())
        self.assertIn("deployment/harness is up", text)
        self.assertEqual(self.final()["parents"], {"platform": False, "flux-system": False})

    def test_after_config_stops_when_the_harness_is_gone(self):
        self.state(harness=False)
        rc, text = self.run_script("--after-config")
        self.assertEqual(rc, 1, text)
        self.assertIn("STOP", text)
        self.assertIn("deployment/harness is missing", text)

    def test_dry_run_names_the_parent_step_and_the_config_mode(self):
        self.state()
        rc, text = self.run_script("--after-config", "--dry-run")
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.calls(), [])
        self.assertIn("kustomization platform", text)
        self.assertIn("kustomization flux-system", text)
        self.assertIn("deployment/harness", text)
        rc, _ = self.run_script("--after-config", "--after-drill")
        self.assertEqual(rc, 2)

    def test_a_parent_whose_stale_ready_is_not_a_fresh_reconcile_stops(self):
        # Review of #1159 (reviewer-codex): Ready=True right after the unsuspend can be the condition
        # from before the freeze. The gate requires lastHandledReconcileAt == the requestedAt it set.
        self.state(parents={"platform": True, "flux-system": True}, parent_never_reconciles=["platform"])
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertIn("STOP", text)
        self.assertIn("Kustomization flux-system/platform", text)
        self.assertNotIn(("patch", "kustomization/flux-system"), self.verbs())
        self.assertTrue(self.final()["parents"]["flux-system"])

    def test_an_unreadable_parent_at_step_0_changes_nothing(self):
        # Review of #1159 (reviewer-claude): a failed read is not "not suspended".
        self.state(parents={"platform": True, "flux-system": True}, parent_get_fails=["platform"])
        rc, text = self.run_script("--after-drill")
        self.assertEqual(rc, 1, text)
        self.assertIn("cannot read Kustomization flux-system/platform", text)
        self.assertFalse(WRITES & {v for v, _ in self.verbs()})

    def test_after_config_refuses_a_harness_scaled_to_zero(self):
        # Review of #1159 (reviewer-claude): `rollout status` succeeds at 0 replicas; the mode proves the
        # assistant stayed up, so at least one available replica is required.
        self.state(harness_replicas=0)
        rc, text = self.run_script("--after-config")
        self.assertEqual(rc, 1, text)
        self.assertIn("STOP", text)
        self.assertIn("no available replica", text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-kubectl":
        sys.exit(fake_kubectl(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-git":
        sys.exit(fake_git(sys.argv[2:]))
    unittest.main()
