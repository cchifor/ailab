#!/usr/bin/env python3
"""Preflight of scripts/verify-sandbox-boundary.sh, the ADR 0019 boundary canary harness (#1505,
plans/2026-10-31-playground-leftovers-plan.md).

The live matrix needs the cluster. What this tests offline is the part that decides WHAT the canary
runs against, through the script's own seams (AF_CANARY_KUBECTL, AF_CANARY_PREFLIGHT_ONLY) and a fake
kubectl: the workspace refusal, the sandbox-image discovery, and the two broker targets. Each failure
mode here must stop the run, or keep it from going GREEN. Stdlib-only, like every module in
scripts/tests.

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
# Bound at import (discovery), before any test runs: test_reviewbot.py replaces the global
# subprocess.run with a fake and does not restore it, which would hand later tests its canned output.
_RUN = subprocess.run
SCRIPT = "scripts/verify-sandbox-boundary.sh"
DIGEST = "registry.chifor.me/agentforge/sandbox@sha256:" + "ab" * 32
OTHER = "registry.chifor.me/agentforge/sandbox@sha256:" + "cd" * 32

# Records every call in $FAKE_LOG, then answers the four queries the preflight makes. FAKE_DEPLOY picks the
# `get deploy -A` answer, FAKE_PODS the broker pods.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  *" version "*|*" version") exit 0 ;;
  *"get pvc"*) exit 0 ;;
  *"get deploy -A"*)
    case "$FAKE_DEPLOY" in
      error) echo "boom" >&2; exit 1 ;;
      none) printf 'kube-system/coredns\t\nagentforge/agentforge-platform\t\n' ;;
      one) printf 'kube-system/coredns\t\naf-tenant-tenant-zero-platform-dev/af-orch-platform-dev-delivery\t%s\n' "$FAKE_IMAGE" ;;
      two-same) printf 'af-tenant-a/af-orch-a\t%s\naf-tenant-b/af-orch-b\t%s\n' "$FAKE_IMAGE" "$FAKE_IMAGE" ;;
      two-diff) printf 'af-tenant-a/af-orch-a\t%s\naf-tenant-b/af-orch-b\t%s\n' "$FAKE_IMAGE" "$FAKE_OTHER" ;;
      tag) printf 'af-tenant-a/af-orch-a\tregistry.chifor.me/agentforge/sandbox:latest\n' ;;
    esac
    exit 0 ;;
  *"get pods -l app.kubernetes.io/name=broker-openai-codex"*)
    case "$FAKE_PODS" in
      none|no-positive) exit 0 ;;
      *) printf 'broker-openai-codex-x|10.0.0.1||True\n' ;;
    esac
    exit 0 ;;
  *"get pods -l app.kubernetes.io/name=broker-anthropic-max1"*)
    case "$FAKE_PODS" in
      none|no-deny) exit 0 ;;
      unready) printf 'broker-anthropic-max1-x|10.0.0.2||False\n' ;;
      terminating) printf 'broker-anthropic-max1-x|10.0.0.2|2026-10-31T00:00:00Z|True\n' ;;
      same-ip) printf 'broker-anthropic-max1-x|10.0.0.1||True\n' ;;
      *) printf 'broker-anthropic-max1-x|10.0.0.2||True\n' ;;
    esac
    exit 0 ;;
esac
echo "fake kubectl: unexpected call: $args" >&2
exit 3
"""


# POSIX only: on Windows `bash` is usually WSL's, which cannot execute the fake by its Windows path.
@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs POSIX bash")
class Preflight(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.fake = d / "kubectl"
        self.fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        self.fake.chmod(0o755)
        self.log = d / "calls.log"
        self.log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, deploy="one", pods="ok", **extra) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AF_CANARY_")}
        env.update(AF_CANARY_KUBECTL=str(self.fake), AF_CANARY_PREFLIGHT_ONLY="1", FAKE_LOG=str(self.log),
                   FAKE_DEPLOY=deploy, FAKE_PODS=pods, FAKE_IMAGE=DIGEST, FAKE_OTHER=OTHER, **extra)
        return _RUN(["bash", SCRIPT], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def test_syntax(self) -> None:
        proc = _RUN(["bash", "-n", SCRIPT], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_live_orchestrator_image_is_used_and_named(self) -> None:
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"image={DIGEST}", proc.stdout)
        self.assertIn("source=live: af-tenant-tenant-zero-platform-dev/af-orch-platform-dev-delivery", proc.stdout)
        self.assertIn("workspace=tenant-zero/boundary-canary pool=canary", proc.stdout)
        self.assertIn("broker=10.0.0.1 deny=10.0.0.2 failures=0", proc.stdout)

    def test_agreeing_orchestrators_pass(self) -> None:
        proc = self.run_script(deploy="two-same")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_any_other_workspace_is_refused_before_any_kubectl_call(self) -> None:
        for ws in ("platform-dev", "playground", "ailab"):
            with self.subTest(ws=ws):
                proc = self.run_script(AF_CANARY_WORKSPACE=ws, AF_CANARY_DANGEROUS_OVERRIDE="yes")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("REFUSING workspace", proc.stderr)
                self.assertEqual(self.calls(), "", "the refusal must come before any kubectl call")

    def test_a_discovery_error_is_fatal_not_a_fallback(self) -> None:
        proc = self.run_script(deploy="error")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("image discovery failed", proc.stderr)

    def test_disagreeing_orchestrators_are_fatal(self) -> None:
        proc = self.run_script(deploy="two-diff")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("disagree on AF_SANDBOX_IMAGE", proc.stderr)

    def test_a_non_digest_image_is_fatal(self) -> None:
        proc = self.run_script(deploy="tag")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not digest-pinned", proc.stderr)
        proc = self.run_script(AF_CANARY_IMAGE="registry.chifor.me/agentforge/sandbox:latest")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_no_live_orchestrator_falls_back_to_the_committed_digest_and_says_so(self) -> None:
        proc = self.run_script(deploy="none")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("source=committed fallback", proc.stdout)
        committed = (REPO / "kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml").read_text(encoding="utf-8")
        image = proc.stdout.split("image=", 1)[1].split()[0]
        self.assertIn(image, committed)

    def test_a_missing_or_unhealthy_broker_target_fails(self) -> None:
        for pods in ("no-positive", "no-deny", "none", "unready", "terminating"):
            with self.subTest(pods=pods):
                proc = self.run_script(pods=pods)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("FAIL", proc.stdout)
                self.assertNotIn("failures=0", proc.stdout)

    def test_the_same_ip_for_both_broker_targets_fails(self) -> None:
        proc = self.run_script(pods="same-ip")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("same IP", proc.stdout)
        self.assertIn("deny=none", proc.stdout)


class Shape(unittest.TestCase):
    """The parts of the live matrix that are only text until a cluster runs them."""

    text = (REPO / SCRIPT).read_text(encoding="utf-8")

    def test_no_override_remains(self) -> None:
        self.assertNotIn("AF_CANARY_DANGEROUS_OVERRIDE", self.text)

    def test_the_denied_broker_probe_has_its_own_assertion(self) -> None:
        self.assertIn('deny denied-broker-ip "http://${AF_CANARY_DENY_BROKER_IP}:8700/"', self.text)
        self.assertIn('has "$AGENT_LOG" "ok (blocked): denied-broker-ip"', self.text)
        self.assertIn('INCONCLUSIVE: no AF_CANARY_DENY_BROKER_IP', self.text)

    def test_the_canary_identity_matches_its_broker_rule_and_pvc(self) -> None:
        cnp = (REPO / "kubernetes/apps/infrastructure/agentforge-broker/broker-openai-codex.yaml").read_text(encoding="utf-8")
        self.assertIn("agentforge.io/pool: canary\n            agentforge.io/org: tenant-zero\n"
                      "            agentforge.io/workspace: boundary-canary", cnp)
        pvc = (REPO / "kubernetes/apps/infrastructure/agentforge-sandbox/boundary-canary-workspace-pvc.yaml").read_text(encoding="utf-8")
        self.assertIn("name: af-sbx-ws-tenant-zero-boundary-canary", pvc)
        for broker in ("broker-anthropic-max1", "broker-anthropic-max2", "broker-anthropic-claude-max-3",
                       "broker-anthropic-claude-max-4"):
            with self.subTest(broker=broker):
                other = (REPO / f"kubernetes/apps/infrastructure/agentforge-broker/{broker}.yaml").read_text(encoding="utf-8")
                self.assertNotIn("workspace: boundary-canary", other)
                self.assertNotIn("workspace: playground", other)


if __name__ == "__main__":
    unittest.main()
