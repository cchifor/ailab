"""Run the owner automation's offline Node contract tests; never skip missing Node."""
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a.test.cjs"
AUTOMATION_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-automation.test.cjs"
SOCKET_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-socket.test.cjs"
RENEWAL_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-renewal.test.cjs"
COLLECTOR_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-collector.test.cjs"
INTEGRATION_FIXTURE = ROOT / "docs/runbooks/inference-canary-v0113/owner-phase-a-automation-integration.test.cjs"
PROXY_IMAGE = "node@sha256:8fa96264f302f58bbb13aa7339c541547b56975766ce956c875da149452a3e40"


class PiOwnerPhaseA(unittest.TestCase):
    def test_offline_response_and_safety_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip owner acceptance safety tests")
        result = subprocess.run(
            [node, "--test", str(FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_noninteractive_host_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip automation host tests")
        result = subprocess.run(
            [node, "--test", str(AUTOMATION_FIXTURE), str(SOCKET_FIXTURE), str(RENEWAL_FIXTURE), str(COLLECTOR_FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_complete_noninteractive_flow(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required: do not skip the complete automation flow")
        result = subprocess.run(
            [node, "--test", str(INTEGRATION_FIXTURE)], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", timeout=390,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_offline_complete_session_proxy_flow(self):
        docker = shutil.which("docker")
        self.assertIsNotNone(docker, "Docker is required for the isolated actual-proxy flow")
        name = "pi-owner-proxy-" + uuid.uuid4().hex
        try:
            # The unmodified proxy fixes its socket under /run. Isolate that
            # path in this container; never create it on the runner host.
            result = subprocess.run(
                [docker, "run", "--rm", "--init", "--name", name,
                 "--network", "none", "--read-only", "--user", "1000:1000",
                 "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                 "--tmpfs", "/tmp:rw,mode=1777",
                 "--tmpfs", "/run/relay-session:rw,uid=1000,gid=1000,mode=0700",
                 "--mount", f"type=bind,source={ROOT},target=/repo,readonly",
                 "--workdir", "/repo", PROXY_IMAGE,
                 "timeout", "390s", "node", "--test",
                 "docs/runbooks/inference-canary-v0113/owner-phase-a-proxy-integration.test.cjs"],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=600,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            # Killing the client can leave its test container behind. This UUID
            # belongs only to this invocation; --rm normally removed it already.
            cleanup = subprocess.run(
                [docker, "rm", "-f", name], capture_output=True, text=True,
                encoding="utf-8", timeout=30,
            )
            self.assertTrue(cleanup.returncode == 0 or "No such container" in cleanup.stderr,
                            "Could not confirm removal of the isolated proxy test container")


if __name__ == "__main__":
    unittest.main()
