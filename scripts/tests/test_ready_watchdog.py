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


class _DummyConn:
    def close(self):
        pass


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

    # ------------------------------------------------ the listener itself (reviewer-claude, #800)
    class _FakeListener:
        """accept() plays a script: an exception instance is raised, "conn" yields a dummy
        connection, "timeout" raises socket.timeout; an exhausted script keeps timing out."""

        def __init__(self, script):
            self.script, self.calls, self.closed = list(script), 0, False

        def accept(self):
            self.calls += 1
            item = self.script.pop(0) if self.script else "timeout"
            if item == "timeout":
                raise socket.timeout()
            if isinstance(item, BaseException):
                raise item
            return _DummyConn(), ("127.0.0.1", 0)

        def close(self):
            self.closed = True

    def _serve_with(self, script, wait=0.4):
        ls = self._FakeListener(script)
        self.p.ls = ls
        t = threading.Thread(target=self.p._serve, args=(ls,), daemon=True)
        self.p.thread = t
        t.start()
        time.sleep(wait)
        return ls, t

    def test_transient_accept_errors_keep_the_listener_alive(self):
        import errno
        ls, t = self._serve_with([OSError(errno.ECONNABORTED, "aborted"), "conn",
                                  OSError(errno.EMFILE, "too many files"), "conn"])
        self.assertTrue(t.is_alive(), "ECONNABORTED/EMFILE are retried, the serve loop survives")
        self.assertTrue(self.p.is_open())
        self.assertGreaterEqual(ls.calls, 5)
        self.assertFalse(any("listener died" in m for m in self.log))
        self.p.close()

    def test_dead_listener_marks_itself_closed_and_the_next_tick_reopens(self):
        # THE FINDING: a non-transient accept() error used to kill the serve thread and close the
        # socket while is_open() stayed True — a silent, permanent NotReady.
        import errno
        self.step(3)  # healthy, port open, oks=3
        real = self.p.ls
        ls, t = self._serve_with([OSError(errno.EBADF, "bad fd")])
        real.close()
        t.join(1)
        self.assertFalse(t.is_alive())
        self.assertTrue(ls.closed)
        self.assertFalse(self.p.is_open(), "a dead listener must not be reported open")
        self.assertTrue(any("listener died" in m for m in self.log))
        self.step()  # checks pass, oks already >= 3: reopens at once
        self.assertTrue(self.p.is_open())
        self.assertTrue(_connect(self.port), "the port is really accepting again")
        self.assertTrue(any("reopened on" in m for m in self.log))

    def test_status_line_is_read_across_short_reads(self):
        class Sock:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def recv(self, n):
                return self.chunks.pop(0)[:n] if self.chunks else b""

        self.assertEqual(b"HTTP/1.0 200 OK\r\n",
                         self.rw.read_status_line(Sock([b"HTTP/1.", b"0 200 O", b"K\r\n", b"Content"])))
        self.assertEqual(b"HTTP/1.0 200 OK\r\n", self.rw.read_status_line(Sock([b"HTTP/1.0 200 OK\r\nX: y\r\n"])))
        self.assertEqual(b"", self.rw.read_status_line(Sock([])), "EOF before any byte is an empty line")
        self.assertEqual(64, len(self.rw.read_status_line(Sock([b"x" * 200]))), "capped without a newline")

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


class EnvReaperPlacement(unittest.TestCase):
    """env-reaper.yaml is built through the testpool kustomization but MUST land in kube-system
    (its header says why: testpool's NetworkPolicy blocks the API path and tep-worker's pods/exec
    would make a hostPID pod a root shell). A `namespace:` transformer on the testpool
    kustomization, or a targetNamespace on its Flux Kustomization, would silently move it —
    reviewer-claude on ailab#800."""

    TESTPOOL = ROOT / "kubernetes/apps/infrastructure/testpool"
    FLUX_KS = ROOT / "kubernetes/apps/clusters/ai/testpool.yaml"

    def test_reaper_workload_declares_kube_system_and_rbac_declares_testpool(self):
        docs = [d for d in yaml.safe_load_all((self.TESTPOOL / "env-reaper.yaml").read_text(encoding="utf-8")) if d]
        by_kind = {d["kind"]: d["metadata"]["namespace"] for d in docs}
        self.assertEqual({"ServiceAccount": "kube-system", "ConfigMap": "kube-system", "DaemonSet": "kube-system",
                          "Role": "testpool", "RoleBinding": "testpool"}, by_kind)
        rb = next(d for d in docs if d["kind"] == "RoleBinding")
        self.assertEqual([{"kind": "ServiceAccount", "name": "env-reaper", "namespace": "kube-system"}], rb["subjects"])

    def test_nothing_in_the_build_chain_rewrites_namespaces(self):
        kust = yaml.safe_load((self.TESTPOOL / "kustomization.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("namespace", kust, "a kustomize namespace transformer would move the reaper into testpool")
        self.assertIn("env-reaper.yaml", kust["resources"])
        flux = yaml.safe_load(self.FLUX_KS.read_text(encoding="utf-8"))
        self.assertNotIn("targetNamespace", flux["spec"], "a Flux targetNamespace would move the reaper into testpool")


if __name__ == "__main__":
    unittest.main()
