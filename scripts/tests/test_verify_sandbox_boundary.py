#!/usr/bin/env python3
"""scripts/verify-sandbox-boundary.sh, the ADR 0019 boundary canary harness (#1505,
plans/2026-10-31-playground-leftovers-plan.md), driven offline through its own seams:
- AF_CANARY_KUBECTL: a fake kubectl;
- AF_CANARY_PREFLIGHT_ONLY: stop after resolving the workspace, image and brokers;
- AF_CANARY_FAKE_RUN: read the two canary logs from files instead of running Jobs.

Under test: WHAT the canary runs against (workspace refusal, sandbox-image discovery, the two broker
targets), and that the matrix never ends GREEN without its evidence.
- A fake run exits 3 when every assertion passes: it is never a proof.
- A failed assertion exits 1.

Stdlib-only, like every module in scripts/tests.

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

# Records every call in $FAKE_LOG and answers what the harness asks:
# - FAKE_DEPLOY: the `get deploy -A` answer;
# - FAKE_PODS: the broker pods;
# - FAKE_DENY_AFTER: the denied pod on its re-read;
# - FAKE_RBAC: the binding list.
# Pod lines are name|ip|deletionTimestamp|uid|restarts|ready/transition.
FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
args="$*"
CODEX='broker-openai-codex-a|10.0.0.1||uid-a|0,|True/t0'
CODEX2='broker-openai-codex-b|10.0.0.3||uid-b|0,|True/t0'
MAX1='broker-anthropic-max1-x|10.0.0.2||uid-x|0,|True/t0'
case "$args" in
  *" version "*|*" version") exit 0 ;;
  *"get pvc"*) exit 0 ;;
  *"get deploy -A"*)
    case "$FAKE_DEPLOY" in
      error) echo "boom" >&2; exit 1 ;;
      none) printf 'kube-system/coredns\t\nagentforge/agentforge-platform\t\n' ;;
      one) printf 'kube-system/coredns\t\naf-tenant-tenant-zero-platform-dev/af-orch-platform-dev-delivery\tdeclared:%s\n' "$FAKE_IMAGE" ;;
      two-same) printf 'af-tenant-a/af-orch-a\tdeclared:%s\naf-tenant-b/af-orch-b\tdeclared:%s\n' "$FAKE_IMAGE" "$FAKE_IMAGE" ;;
      two-diff) printf 'af-tenant-a/af-orch-a\tdeclared:%s\naf-tenant-b/af-orch-b\tdeclared:%s\n' "$FAKE_IMAGE" "$FAKE_OTHER" ;;
      tag) printf 'af-tenant-a/af-orch-a\tdeclared:registry.chifor.me/agentforge/sandbox:latest\n' ;;
      valuefrom) printf 'af-tenant-a/af-orch-a\tdeclared:%s\naf-tenant-b/af-orch-b\tdeclared:\n' "$FAKE_IMAGE" ;;
    esac
    exit 0 ;;
  *"get pods -l app.kubernetes.io/name=broker-openai-codex"*)
    case "$FAKE_PODS" in
      none|no-positive) ;;
      *) printf '%s\n%s\n' "$CODEX" "$CODEX2" ;;
    esac
    exit 0 ;;
  *"get pods -l app.kubernetes.io/name=broker-anthropic-max1"*)
    case "$FAKE_PODS" in
      none|no-deny) ;;
      unready) printf 'broker-anthropic-max1-x|10.0.0.2||uid-x|0,|False/t0\n' ;;
      terminating) printf 'broker-anthropic-max1-x|10.0.0.2|2026-10-31T00:00:00Z|uid-x|0,|True/t0\n' ;;
      same-ip) printf 'broker-anthropic-max1-x|10.0.0.1||uid-x|0,|True/t0\n' ;;
      *) printf '%s\n' "$MAX1" ;;
    esac
    exit 0 ;;
  *"get pod broker-anthropic-max1-x"*)
    case "${FAKE_DENY_AFTER:-same}" in
      same) printf '%s\n' "$MAX1" ;;
      restarted) printf 'broker-anthropic-max1-x|10.0.0.2||uid-x|1,|True/t1\n' ;;
      flapped) printf 'broker-anthropic-max1-x|10.0.0.2||uid-x|0,|True/t1\n' ;;
      gone) echo 'Error from server (NotFound)' >&2; exit 1 ;;
    esac
    exit 0 ;;
  *"get pod broker-openai-codex-b"*) printf '%s\n' "$CODEX2"; exit 0 ;;
  *"get runtimeclass kata"*|*"get ciliumnetworkpolicy"*|*"get networkpolicy default-deny-all"*) exit 0 ;;
  *"get ns agentforge-sandbox"*) printf 'restricted'; exit 0 ;;
  *"get sa agentforge-sandbox"*) printf 'false'; exit 0 ;;
  *"get rolebindings,clusterrolebindings"*)
    case "${FAKE_RBAC:-clean}" in
      clean) printf 'ServiceAccount/agentforge/agentforge-reaper\nUser//admin\n' ;;
      bound) printf 'ServiceAccount/agentforge-sandbox/agentforge-sandbox\n' ;;
      error) echo 'Error from server (Forbidden)' >&2; exit 1 ;;
    esac
    exit 0 ;;
  *"apply --dry-run=server"*)
    f=""; prev=""
    for a in "$@"; do [ "$prev" = "-f" ] && f="$a"; prev="$a"; done
    if [ "$f" = "-" ]; then body="$(cat)"; else body="$(cat "$f")"; fi
    if printf '%s' "$body" | grep -qE 'privileged: true|automountServiceAccountToken: true|hostNetwork: true|runtimeClassName: runc|hostPath:'; then
      echo 'admission webhook denied the request'; exit 1
    fi
    echo 'job.batch/af-boundary-dryrun created (server dry run)'; exit 0 ;;
  *"get node node-a"*) printf '6.12.48-talos'; exit 0 ;;
  *"delete job"*) exit 0 ;;
esac
echo "fake kubectl: unexpected call: $args" >&2
exit 3
"""

# Answers the manifest HEAD the harness makes: FAKE_REGISTRY=present (200), missing (404), down (000), cut
# (200 then a failed transfer, curl rc 28).
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
case "${FAKE_REGISTRY:-present}" in present) printf 200 ;; missing) printf 404 ;; down) printf 000; exit 7 ;; cut) printf 200; exit 28 ;; esac
"""

AGENT_OK = """ok (blocked): world-ip
ok (blocked): metadata
ok (blocked): alt-dns
ok (blocked): other-pool-broker
ok (blocked): openbao
ok (blocked): denied-broker-ip
BROKER REACHABLE (10.0.0.1:8700)
GUEST_KERNEL: 6.12.42
PROC1 CLEAN
BOUNDARY OK: agent confined
"""
TEST_OK = """ok (blocked): world-ip
ok (blocked): metadata
ok (blocked): alt-dns
ok (blocked): broker-svc
ok (blocked): openbao
ok (blocked): broker-ip
GUEST_KERNEL: 6.12.42
BOUNDARY OK: test tier zero egress (broker proven unreachable)
"""


# POSIX only: on Windows `bash` is usually WSL's, which cannot execute the fake by its Windows path.
@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "needs POSIX bash")
class Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.fake = self.dir / "kubectl"
        self.fake.write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
        self.fake.chmod(0o755)
        self.curl = self.dir / "curl"
        self.curl.write_text(FAKE_CURL, encoding="utf-8", newline="\n")
        self.curl.chmod(0o755)
        self.log = self.dir / "calls.log"
        self.log.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_script(self, deploy="one", pods="ok", preflight=True, **extra) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AF_CANARY_")}
        env.update(AF_CANARY_KUBECTL=str(self.fake), AF_CANARY_CURL=str(self.curl), FAKE_LOG=str(self.log), FAKE_DEPLOY=deploy,
                   FAKE_PODS=pods, FAKE_IMAGE=DIGEST, FAKE_OTHER=OTHER, **extra)
        if preflight:
            env["AF_CANARY_PREFLIGHT_ONLY"] = "1"
        return _RUN(["bash", SCRIPT], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)

    def run_full(self, agent=AGENT_OK, test=TEST_OK, meta="node-a kata", **extra) -> subprocess.CompletedProcess:
        run = self.dir / "run"
        run.mkdir(exist_ok=True)
        (run / "agent.log").write_text(agent, encoding="utf-8")
        (run / "test.log").write_text(test, encoding="utf-8")
        (run / "agent.meta").write_text(meta, encoding="utf-8")
        return self.run_script(preflight=False, AF_CANARY_FAKE_RUN=str(run), **extra)

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8")


class Preflight(Harness):
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
        self.assertEqual(self.run_script(deploy="two-same").returncode, 0)

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

    def test_a_declared_image_without_a_literal_digest_is_fatal(self) -> None:
        for deploy in ("tag", "valuefrom"):
            with self.subTest(deploy=deploy):
                proc = self.run_script(deploy=deploy)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("without a literal digest-pinned value", proc.stderr)
        proc = self.run_script(AF_CANARY_IMAGE="registry.chifor.me/agentforge/sandbox:latest")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not digest-pinned", proc.stderr)

    def test_an_image_the_registry_no_longer_has_is_fatal(self) -> None:
        for reg in ("missing", "down", "cut"):
            with self.subTest(registry=reg):
                proc = self.run_script(FAKE_REGISTRY=reg)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("is not in the registry", proc.stderr)
        self.assertIn(f"/v2/agentforge/sandbox/manifests/sha256:{'ab' * 32}", self.calls())

    def test_no_live_orchestrator_falls_back_to_the_committed_digest_and_says_so(self) -> None:
        proc = self.run_script(deploy="none")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("source=committed fallback", proc.stdout)
        committed = (REPO / "kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml").read_text(encoding="utf-8")
        self.assertIn(proc.stdout.split("image=", 1)[1].split()[0], committed)

    def test_a_missing_or_unhealthy_broker_target_fails(self) -> None:
        for pods in ("no-positive", "no-deny", "none", "unready", "terminating"):
            with self.subTest(pods=pods):
                proc = self.run_script(pods=pods)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("FAIL", proc.stdout)

    def test_the_deny_target_is_never_the_positive_pod(self) -> None:
        proc = self.run_script(pods="same-ip")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("deny=none", proc.stdout)
        # the admitting broker as the deny target: its OTHER replica is picked (the mutation check)
        proc = self.run_script(AF_CANARY_DENY_BROKER="broker-openai-codex")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("broker=10.0.0.1 deny=10.0.0.3", proc.stdout)


class Matrix(Harness):
    def test_complete_evidence_passes_every_assertion_but_a_fake_run_is_not_green(self) -> None:
        proc = self.run_full()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn(" 0 failed ==", proc.stdout)
        self.assertNotIn("GREEN", proc.stdout)
        self.assertIn("NOT a boundary proof", proc.stderr)
        self.assertIn("UNREACHABLE by pod IP", proc.stdout)

    def assert_fails(self, needle: str, **kw) -> None:
        proc = self.run_full(**kw)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, rf"FAIL .*{needle}")

    def test_deny_evidence_must_be_the_exact_line(self) -> None:
        line = "ok (blocked): denied-broker-ip\n"
        for variant in ("", "ok (blocked): denied-broker-ipv6\n", "xok (blocked): denied-broker-ip\n",
                        "ok (blocked): denied-broker-ip (late)\n"):
            with self.subTest(variant=variant):
                self.assert_fails("no denied-broker-ip evidence", agent=AGENT_OK.replace(line, variant))

    def test_a_reached_deny_target_is_a_breach(self) -> None:
        agent = AGENT_OK.replace("ok (blocked): denied-broker-ip\n", "BREACH: reached denied-broker-ip (http://10.0.0.2:8700/)\n")
        self.assert_fails("egress BREACH", agent=agent)

    def test_a_deny_target_that_changed_during_the_probe_is_inconclusive(self) -> None:
        for after in ("restarted", "flapped", "gone"):
            with self.subTest(after=after):
                self.assert_fails("during the probe", FAKE_DENY_AFTER=after)

    def test_an_unreadable_proc1_is_not_clean(self) -> None:
        agent = AGENT_OK.replace("PROC1 CLEAN\n", "INCONCLUSIVE: /proc/1/environ unreadable (grep rc=2)\n").replace(
            "BOUNDARY OK: agent confined\n", "")
        self.assert_fails("could not confirm /proc/1 isolation", agent=agent)

    def test_an_rbac_lookup_error_is_not_an_empty_binding_list(self) -> None:
        self.assert_fails("could not list Role/ClusterRoleBindings", FAKE_RBAC="error")
        self.assert_fails("bound by 1 RoleBinding", FAKE_RBAC="bound")

    def test_empty_logs_fail_closed(self) -> None:
        self.assert_fails("AGENT: canary produced NO log", agent="")
        self.assert_fails("TEST: canary produced NO log", test="")


class Shape(unittest.TestCase):
    """What only a cluster can exercise, pinned as text."""

    text = (REPO / SCRIPT).read_text(encoding="utf-8")

    def test_no_override_remains(self) -> None:
        self.assertNotIn("AF_CANARY_DANGEROUS_OVERRIDE", self.text)

    def test_the_probe_distinguishes_an_unreadable_proc1(self) -> None:
        manifest = (REPO / "kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml").read_text(encoding="utf-8")
        for src in (self.text, manifest):
            self.assertIn('elif [ "$g" -eq 1 ]', src)
            self.assertIn("INCONCLUSIVE: /proc/1/environ unreadable", src)

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
