"""ready-watchdog (kubernetes/apps/infrastructure/testpool/ready-watchdog.yaml) — the in-guest
process that decides when an env member's ready-port is open.

The script is loaded from the ConfigMap's data key, so the test exercises exactly what the pod
runs. Checks are replaced by scripted fakes; the loop is driven one tick() at a time; the port
is a real listener on a free localhost port so "open/closed" is observed the way the kubelet
observes it (a TCP connect).

Run: python3 -m unittest scripts.tests.test_ready_watchdog  (CI: .gitea/workflows/manifests.yaml)
"""
import importlib.util
import os
import pathlib
import socket
import tempfile
import threading
import time
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "kubernetes/apps/infrastructure/testpool/ready-watchdog.yaml"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _connect(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _load(port):
    src = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["data"]["ready-watchdog.py"]
    os.environ.update(READY_PORT=str(port), READY_CHECK_PERIOD="0", READY_CHECK_TIMEOUT="0.2",
                      READY_FAILS_TO_CLOSE="2", READY_SUCCESSES_TO_REOPEN="3",
                      READY_WORK_DIR=tempfile.gettempdir(), DOCKER_SOCK="/nonexistent")
    path = pathlib.Path(tempfile.gettempdir()) / "ready-watchdog-under-test.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("ready_watchdog", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ScriptedCheck:
    """A check whose outcome per call is scripted: "ok", "fail" (raises) or "hang" (blocks until
    released — the D-state shape)."""

    def __init__(self):
        self.script = []
        self.release = threading.Event()

    def __call__(self):
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "fail":
            raise RuntimeError("scripted failure")
        if outcome == "hang":
            self.release.wait()


class ReadyWatchdog(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        self.rw = _load(self.port)
        self.fs, self.dk = ScriptedCheck(), ScriptedCheck()
        self.checks = [self.rw.Check("virtiofs", self.fs), self.rw.Check("dockerd", self.dk)]
        self.p = self.rw.Port(self.port)
        self.wd = self.rw.Watchdog(self.checks, self.p)
        self.log = []
        self.rw.log = self.log.append

    def tearDown(self):
        self.fs.release.set()
        self.dk.release.set()
        self.p.close()

    def step(self, n=1):
        for _ in range(n):
            self.wd.tick()

    def test_opens_after_first_pass_and_stays_open_while_healthy(self):
        self.assertFalse(_connect(self.port))
        self.step()
        self.assertTrue(_connect(self.port), "port must open after the first passing pass")
        self.step(5)
        self.assertTrue(_connect(self.port))
        self.assertEqual(1, sum("open on" in m for m in self.log))

    def test_one_failure_is_tolerated_two_close_the_port(self):
        self.step()
        self.dk.script = ["fail"]
        self.step()
        self.assertTrue(_connect(self.port), "a single failed check must not close the port")
        self.dk.script = ["fail", "fail"]
        self.step(2)
        self.assertFalse(_connect(self.port), "two consecutive failures close the port")

    def test_hung_check_closes_immediately_and_spawns_no_second_thread(self):
        self.step()
        self.fs.script = ["hang"]
        self.step()
        self.assertFalse(_connect(self.port), "a hung check closes the port at once")
        before = threading.active_count()
        self.step(3)  # still hung: reported again, no new threads
        self.assertEqual(before, threading.active_count(), "at most one outstanding thread per check")
        self.assertGreaterEqual(sum("virtiofs check hung" in m for m in self.log), 4)

    def test_recovery_reopens_after_consecutive_passes(self):
        # THE REGRESSION (codex, ailab#800): a stall that recovers must not leave the port closed
        # forever — that would turn a brief hiccup into a warm-pool deletion for any member older
        # than the readiness grace period.
        self.step()
        self.fs.script = ["hang"]
        self.step()
        self.assertFalse(_connect(self.port))
        self.fs.release.set()          # the stall clears; the hung thread returns
        time.sleep(0.05)
        self.step(2)                   # 2 passes: not yet (needs 3)
        self.assertFalse(_connect(self.port), "reopen needs SUCCESSES_TO_REOPEN consecutive passes")
        self.step()
        self.assertTrue(_connect(self.port), "port reopens after 3 consecutive passes")
        self.assertTrue(any("reopened on" in m for m in self.log))
        self.assertTrue(any("checks recovered" in m for m in self.log))

    def test_failure_during_recovery_resets_the_count(self):
        self.step()
        self.dk.script = ["fail", "fail"]
        self.step(2)
        self.assertFalse(_connect(self.port))
        self.dk.script = ["ok", "ok", "fail", "ok", "ok"]
        self.step(5)
        self.assertFalse(_connect(self.port), "2 passes, a failure, 2 passes: still not 3 in a row")
        self.step()
        self.assertTrue(_connect(self.port))


if __name__ == "__main__":
    unittest.main()
