#!/usr/bin/env python3
"""Behavioural tests for gitea-runner-cache-verify.py (verify / publish / apply).

Runs on the live runner VMs as `runner` (runner-scripts workflow), so every path is a throwaway fixture:
fake npm and uv caches with planted corruption, a fake textfile dir, and a fake `runuser` that runs the
deletion as the current user. Nothing here reads or writes the host's real caches.

Usage: python3 ansible/roles/gitea_runner/tests/test-cache-verify.py   (exit 0 = pass)
"""
import base64
import getpass
import hashlib
import importlib.util
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / "files" / "gitea-runner-cache-verify.py"


def b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


class Fx:
    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.npm = self.home / ".npm/_cacache"
        self.uv = self.home / ".cache/uv"
        self.state = self.root / "state"
        self.textfile = self.root / "textfile"
        self.pytest = self.root / "tmp/pytest-of-runner"
        self.work = self.home / "act-runner/work"
        for d in (self.npm, self.uv, self.state, self.textfile, self.pytest, self.work, self.home / ".local/share/uv"):
            d.mkdir(parents=True, exist_ok=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # runuser -u <user> -- <cmd...>: run <cmd...> as the current user and record the call.
        (self.bin / "runuser").write_text('#!/usr/bin/env bash\necho "$*" >> "$FX/runuser.log"\nshift 3\nexec "$@"\n')
        (self.bin / "runuser").chmod(0o755)

    def npm_put(self, data, corrupt=False, algo="sha512"):
        h = hashlib.new(algo, data).hexdigest()
        p = self.npm / "content-v2" / algo / h[:2] / h[2:4] / h[4:]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data[:-1] + b"X" if corrupt else data)
        return h, p

    def uv_archive(self, aid, files, corrupt=(), pointer=True):
        a = self.uv / "archive-v0" / aid
        di = a / "pkg-1.0.dist-info"
        di.mkdir(parents=True)
        rows = []
        for rel, data in files.items():
            f = a / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            rows.append(f"{rel},sha256={b64(data)},{len(data)}")
        rows.append("pkg-1.0.dist-info/RECORD,,")
        rows.append("pkg/__pycache__/x.cpython-312.pyc,,")
        (di / "RECORD").write_text("\n".join(rows) + "\n")
        os.utime(a, None)
        time.sleep(0.01)
        for rel in corrupt:
            (a / rel).write_bytes(b"corrupted" + (a / rel).read_bytes())
        if pointer:
            w = self.uv / "wheels-v6/pypi/pkg"
            w.mkdir(parents=True, exist_ok=True)
            (w / "1.0-py3-none-any.http").write_bytes(b"\x92\xa5blob\xb0" + aid.encode() + b"\x00")
        return a

    def env(self, **over):
        # The root applier only trusts state files owned by the runner user: here, whoever runs the suite.
        e = {"FX": str(self.root), "GITEA_CACHE_VERIFY_ENV_FILE": "/nonexistent",
             "GITEA_RUNNER_USER": getpass.getuser(), "GITEA_RUNNER_HOME": str(self.home),
             "GITEA_CACHE_NPM_DIR": str(self.npm), "GITEA_CACHE_UV_DIR": str(self.uv),
             "GITEA_CACHE_STATE_DIR": str(self.state), "GITEA_CACHE_TEXTFILE_DIR": str(self.textfile),
             "GITEA_CACHE_BEACON": "1", "GITEA_CACHE_RUNUSER": str(self.bin / "runuser"),
             "GITEA_CACHE_LINK_ROOT_UVDATA": str(self.home / ".local/share/uv"),
             "GITEA_CACHE_LINK_ROOT_PYTEST": str(self.pytest), "GITEA_CACHE_LINK_ROOT_WORK": str(self.work)}
        e.update(over)
        return e

    def run(self, cmd, **over):
        with mock.patch.dict(os.environ, self.env(**over)):
            spec = importlib.util.spec_from_file_location(f"cv_{id(self)}_{cmd}_{time.time_ns()}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.main(["x", cmd])

    def queue(self):
        q = self.state / "purge.queue"
        return q.read_text().split("\n") if q.exists() else []

    def prom(self):
        p = self.textfile / "gitea_runner_cache_verify.prom"
        return p.read_text() if p.exists() else ""


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.fx = Fx(self._t.name)

    def tearDown(self):
        self._t.cleanup()


class VerifyTests(Base):
    def test_clean_caches_queue_nothing_and_publish_zeroes(self):
        self.fx.npm_put(b"good tarball")
        self.fx.npm_put(b"sha1 entry", algo="sha1")
        self.fx.uv_archive("A" * 20, {"pkg/_x.so": b"\x7fELF ok"})
        self.assertEqual(self.fx.run("verify"), 0)
        self.fx.run("publish")
        self.assertEqual([l for l in self.fx.queue() if l], [])
        prom = self.fx.prom()
        self.assertIn('gitea_runner_cache_checked{cache="npm"} 2', prom)
        self.assertIn('gitea_runner_cache_checked{cache="uv"} 1', prom)
        self.assertIn('gitea_runner_cache_corrupt_found{cache="npm"} 0', prom)
        self.assertIn('gitea_runner_cache_corrupt_pending{cache="uv"} 0', prom)

    def test_corrupt_npm_and_uv_are_found_queued_and_pending(self):
        h, _ = self.fx.npm_put(b"tarball bytes", corrupt=True)
        self.fx.uv_archive("B" * 20, {"pkg/_rust.abi3.so": b"\x7fELF good", "pkg/__init__.py": b"x=1\n"},
                           corrupt=("pkg/_rust.abi3.so",))
        self.fx.run("verify")
        self.fx.run("publish")
        q = [l for l in self.fx.queue() if l]
        self.assertIn(f"npm sha512:{h}", q)
        self.assertIn("uv " + "B" * 20, q)
        prom = self.fx.prom()
        self.assertIn('gitea_runner_cache_corrupt_found{cache="npm"} 1', prom)
        self.assertIn('gitea_runner_cache_corrupt_found{cache="uv"} 1', prom)
        self.assertIn('gitea_runner_cache_corrupt_pending{cache="npm"} 1', prom)
        self.assertIn('gitea_runner_cache_corrupt_pending{cache="uv"} 1', prom)

    def test_inplace_write_after_unpack_is_flagged(self):
        a = self.fx.uv_archive("C" * 20, {"pkg/m.so": b"ok bytes"})
        time.sleep(1.1)  # ctime granularity: the in-place write lands visibly after the archive dir's ctime
        with open(a / "pkg/m.so", "r+b") as fh:
            fh.write(b"XX")
        self.fx.run("verify")
        self.fx.run("publish")
        self.assertIn('gitea_runner_cache_corrupt_inplace_evidence{cache="uv"} 1', self.fx.prom())

    def test_symlink_to_a_fifo_is_skipped_not_hung(self):
        h = hashlib.sha512(b"real").hexdigest()
        p = self.fx.npm / "content-v2/sha512" / h[:2] / h[2:4] / h[4:]
        p.parent.mkdir(parents=True)
        fifo = self.fx.root / "fifo"
        os.mkfifo(fifo)
        p.symlink_to(fifo)
        a = self.fx.uv_archive("D" * 20, {"pkg/a.py": b"a"})
        os.unlink(a / "pkg/a.py")
        os.mkfifo(a / "pkg/a.py")   # a FIFO in place of a RECORD file: O_NONBLOCK open + fstat rejects it
        t0 = time.time()
        self.fx.run("verify")
        self.assertLess(time.time() - t0, 10)
        # the npm symlink is skipped (never followed); the FIFO standing in for a RECORD-listed file makes
        # that archive unusable, so it is queued - and neither one hangs the open
        self.assertEqual([l for l in self.fx.queue() if l], ["uv " + "D" * 20])

    def test_record_paths_that_escape_the_archive_mark_it_corrupt_and_are_never_opened(self):
        a = self.fx.uv_archive("E" * 20, {"pkg/a.py": b"a"})
        secret = self.fx.root / "secret"
        secret.write_bytes(b"not part of the archive")
        rec = a / "pkg-1.0.dist-info/RECORD"
        rec.write_text(rec.read_text() + f"../../../secret,sha256={b64(b'other')},1\n/etc/passwd,sha256=AAAA,1\n")
        opened = []
        with mock.patch.dict(os.environ, self.fx.env()):
            spec = importlib.util.spec_from_file_location(f"cv_escape_{time.time_ns()}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            real = mod.digest

            def spy(path, algo):
                opened.append(path)
                return real(path, algo)
            mod.digest = spy
            mod.main(["x", "verify"])
        self.assertEqual([l for l in self.fx.queue() if l], ["uv " + "E" * 20])
        self.assertFalse([p for p in opened if "secret" in p or "passwd" in p], opened)

    def test_hex_digests_are_accepted_when_they_match_and_corrupt_when_they_do_not(self):
        # uv 0.8.2's build backend writes hex sha256 into RECORD (ag_ui_protocol 0.1.14, 2026-10-12)
        for aid, good in (("H" * 20, True), ("I" * 20, False)):
            a = self.fx.uv / "archive-v0" / aid
            (a / "pkg").mkdir(parents=True)
            (a / "pkg-1.0.dist-info").mkdir()
            (a / "pkg/__init__.py").write_bytes(b"")
            (a / "pkg/m.py").write_bytes(b"x = 1\n" if good else b"x = 2\n")
            rows = [f"pkg/__init__.py,sha256={hashlib.sha256(b'').hexdigest()},0",
                    f"pkg/m.py,sha256={hashlib.sha256(b'x = 1' + bytes([10])).hexdigest()},6",
                    "pkg-1.0.dist-info/RECORD,,"]
            (a / "pkg-1.0.dist-info/RECORD").write_text("\n".join(rows) + "\n")
        self.fx.run("verify")
        q = [l for l in self.fx.queue() if l]
        self.assertNotIn("uv " + "H" * 20, q, "a matching hex digest is a valid RECORD")
        self.assertIn("uv " + "I" * 20, q, "a hex digest that does not match is still corruption")

    def test_archives_without_a_usable_record_are_corrupt_but_a_uv_environment_is_not(self):
        nodi = self.fx.uv / "archive-v0" / ("N" * 20)          # no .dist-info at all
        (nodi / "pkg").mkdir(parents=True)
        (nodi / "pkg/a.py").write_text("x")
        norec = self.fx.uv_archive("O" * 20, {"pkg/a.py": b"a"}, pointer=False)
        os.unlink(norec / "pkg-1.0.dist-info/RECORD")          # a dist-info without its RECORD
        unhashed = self.fx.uv_archive("P" * 20, {"pkg/a.py": b"a"}, pointer=False)
        rec = unhashed / "pkg-1.0.dist-info/RECORD"
        rec.write_text(rec.read_text() + "pkg/b.so,,\n")      # a package file listed without a hash
        strong = self.fx.uv_archive("Q" * 20, {"pkg/a.py": b"a"}, pointer=False)
        (strong / "pkg/c.py").write_bytes(b"c")
        d = base64.urlsafe_b64encode(hashlib.sha512(b"c").digest()).rstrip(b"=").decode()
        rec = strong / "pkg-1.0.dist-info/RECORD"
        rec.write_text(rec.read_text() + f"pkg/c.py,sha512={d},1\n")   # sha512 is allowed by the spec
        env = self.fx.uv / "archive-v0" / ("S" * 20)             # a cached uv environment: no RECORD of its own
        (env / "lib").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
        self.fx.run("verify")
        q = [l for l in self.fx.queue() if l]
        for aid in ("N", "O", "P"):
            self.assertIn("uv " + aid * 20, q)
        self.assertNotIn("uv " + "Q" * 20, q)
        self.assertNotIn("uv " + "S" * 20, q)

    def test_hardlinked_venv_is_recorded_and_a_mere_hardlink_is_not_inplace(self):
        a = self.fx.uv_archive("F" * 20, {"pkg/m.so": b"good"}, corrupt=("pkg/m.so",))
        time.sleep(1.1)   # the link below bumps the inode's ctime well past the archive dir's: mtime must not move
        venv = self.fx.pytest / "pytest-7/test_x0/.venv"
        (venv / "lib/python3.12/site-packages/pkg").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        os.link(a / "pkg/m.so", venv / "lib/python3.12/site-packages/pkg/m.so")
        self.fx.run("verify")
        self.fx.run("publish")
        self.assertIn("venv pytest pytest-7/test_x0/.venv", self.fx.queue())
        self.assertIn('gitea_runner_cache_corrupt_inplace_evidence{cache="uv"} 0', self.fx.prom())

    def test_a_cached_uv_environment_sharing_a_corrupt_inode_is_queued(self):
        a = self.fx.uv_archive("J" * 20, {"pkg/m.so": b"good"}, corrupt=("pkg/m.so",))
        env = self.fx.uv / "archive-v0" / ("K" * 20)   # a uvx / `uv run --with` environment
        (env / "lib/python3.12/site-packages/pkg").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text("home = /usr/bin\n")
        os.link(a / "pkg/m.so", env / "lib/python3.12/site-packages/pkg/m.so")
        self.fx.run("verify")
        q = [l for l in self.fx.queue() if l]
        self.assertIn("uv " + "J" * 20, q)
        self.assertIn("uv " + "K" * 20, q, "the cached env would keep serving the corrupt inode")
        self.fx.run("apply")
        self.assertFalse(env.exists())

    def test_missing_recorded_file_and_unreadable_record_are_corrupt(self):
        a = self.fx.uv_archive("M" * 20, {"pkg/a.py": b"a", "pkg/b.so": b"b"})
        os.unlink(a / "pkg/b.so")
        r = self.fx.uv_archive("R" * 20, {"pkg/a.py": b"a"}, pointer=False)
        (r / "pkg-1.0.dist-info/RECORD").write_bytes(b"\xff\xfe not utf-8")
        self.fx.run("verify")
        q = self.fx.queue()
        self.assertIn("uv " + "M" * 20, q)
        self.assertIn("uv " + "R" * 20, q)

    def test_a_deferred_purge_does_not_grow_the_queue(self):
        self.fx.npm_put(b"tarball", corrupt=True)
        self.fx.run("verify")
        self.fx.run("verify")
        self.fx.run("publish")
        self.assertEqual(len([l for l in self.fx.queue() if l]), 1)
        self.assertIn('gitea_runner_cache_corrupt_pending{cache="npm"} 1', self.fx.prom())

    def test_an_archive_vanishing_mid_walk_does_not_abort_the_pass(self):
        self.fx.uv_archive("V" * 20, {"pkg/a.py": b"a"})
        h, _ = self.fx.npm_put(b"tarball", corrupt=True)
        with mock.patch.dict(os.environ, self.fx.env()):
            spec = importlib.util.spec_from_file_location(f"cv_vanish_{time.time_ns()}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            real = mod.digest

            def flaky(path, algo):
                if "VVVV" in path:
                    raise FileNotFoundError(path)
                return real(path, algo)
            mod.digest = flaky
            self.assertEqual(mod.main(["x", "verify"]), 0)
        self.assertIn(f"npm sha512:{h}", self.fx.queue(), "the rest of the pass still ran")

    def test_an_unreadable_directory_is_skipped_not_fatal(self):
        h, _ = self.fx.npm_put(b"tarball", corrupt=True)
        odd = self.fx.npm / "content-v2/sha512/zz"
        odd.mkdir(parents=True)
        odd.chmod(0)   # a job dropped a 000-mode dir into the cache
        try:
            self.assertEqual(self.fx.run("verify"), 0)
        finally:
            odd.chmod(0o755)
        self.assertIn(f"npm sha512:{h}", self.fx.queue(), "the rest of the pass still ran")

    def test_an_archive_deleted_during_the_pass_is_not_reported_corrupt(self):
        import shutil
        a = self.fx.uv_archive("Y" * 20, {"pkg/a.py": b"a", "pkg/b.py": b"b"})
        with mock.patch.dict(os.environ, self.fx.env()):
            spec = importlib.util.spec_from_file_location(f"cv_prune_{time.time_ns()}", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            calls = []

            def pruned(path, algo):   # a job's `uv cache prune` empties the archive while it is hashed:
                calls.append(path)    # the first file is already gone, the dir goes a moment later
                if len(calls) > 1:
                    shutil.rmtree(a, ignore_errors=True)
                return None, None
            mod.digest = pruned
            mod.main(["x", "verify"])
        self.assertEqual([l for l in self.fx.queue() if l], [])


class ApplyTests(Base):
    def test_apply_purges_npm_uv_pointer_and_linked_venv_as_runner(self):
        h, npm_file = self.fx.npm_put(b"tarball", corrupt=True)
        a = self.fx.uv_archive("G" * 20, {"pkg/m.so": b"good"}, corrupt=("pkg/m.so",))
        venv = self.fx.pytest / "pytest-3/test_y0/.venv"
        (venv / "lib").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("x\n")
        os.link(a / "pkg/m.so", venv / "lib/m.so")
        pointer = self.fx.uv / "wheels-v6/pypi/pkg/1.0-py3-none-any.http"
        self.fx.run("verify")
        self.assertEqual(self.fx.run("apply"), 0)
        self.assertFalse(npm_file.exists())
        self.assertFalse(a.exists())
        self.assertFalse(pointer.exists())
        self.assertFalse(venv.exists())
        self.assertTrue((self.fx.pytest / "pytest-3").exists(), "only the venv root goes, not its parents")
        self.assertEqual([l for l in self.fx.queue() if l], [])
        log = (self.fx.root / "runuser.log").read_text().splitlines()
        me = getpass.getuser()
        # root starts ONE runner-side purge; the walking and every rm happen inside it, as runner
        self.assertEqual(len(log), 1, log)
        self.assertTrue(log[0].startswith(f"-u {me} -- ") and log[0].endswith(" purge"), log)
        self.assertIn('gitea_runner_cache_corrupt_pending{cache="uv"} 0', self.fx.prom())

    def test_symlink_pointers_to_a_purged_archive_or_env_are_removed(self):
        aid, eid = "T" * 20, "U" * 20
        a = self.fx.uv_archive(aid, {"pkg/m.so": b"good"}, corrupt=("pkg/m.so",), pointer=False)
        env = self.fx.uv / "archive-v0" / eid
        (env / "lib").mkdir(parents=True)
        (env / "pyvenv.cfg").write_text("x\n")
        os.link(a / "pkg/m.so", env / "lib/m.so")
        built = self.fx.uv / "sdists-v9/pypi/pkg/1.0/abc"
        built.mkdir(parents=True)
        (built / "pkg-1.0-py3-none-any").symlink_to(Path("../../../../../archive-v0") / aid)
        envs = self.fx.uv / "environments-v2"
        envs.mkdir()
        (envs / "tool-1234").symlink_to(env)
        other = self.fx.uv / "sdists-v9/pypi/other"
        other.mkdir(parents=True)
        (other / "keep").symlink_to(self.fx.uv / "archive-v0" / ("W" * 20))
        self.fx.run("verify")
        self.fx.run("apply")
        self.assertFalse(a.exists() or env.exists())
        self.assertFalse(os.path.lexists(built / "pkg-1.0-py3-none-any"))
        self.assertFalse(os.path.lexists(envs / "tool-1234"))
        self.assertTrue(os.path.lexists(other / "keep"), "a pointer to another archive stays")
        self.assertEqual([l for l in self.fx.queue() if l], [])

    def test_a_failing_purge_keeps_the_queue_and_leaves_its_mtime_alone(self):
        aid = "X" * 20
        self.fx.uv_archive(aid, {"pkg/a.py": b"a"}, pointer=False)
        q = self.fx.state / "purge.queue"
        q.write_text(f"uv {aid}\n")
        os.utime(q, (1_000_000_000, 1_000_000_000))
        base = self.fx.uv / "archive-v0"
        base.chmod(0o555)   # the runner cannot unlink inside it: the rm fails
        try:
            self.fx.run("apply")
        finally:
            base.chmod(0o755)
        self.assertEqual(q.read_text(), f"uv {aid}\n")
        self.assertEqual(q.stat().st_mtime, 1_000_000_000, "an unchanged queue must not be rewritten")

    def test_forged_queue_lines_are_rejected_and_nothing_outside_the_roots_is_touched(self):
        victim = self.fx.root / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        self.fx.state.joinpath("purge.queue").write_text("\n".join([
            "npm sha512:../../../victim",
            "uv ../../victim",
            f"venv pytest ../../{victim.name}",
            f"venv work {victim}",
            "venv pytest .",
            "venv nosuchroot a/b",
            "rm -rf /",
            "uv " + "H" * 20 + " extra",
        ]) + "\n")
        self.fx.run("apply")
        self.assertTrue((victim / "keep").exists())
        self.assertFalse((self.fx.root / "runuser.log").exists(), "no deletion may run for a forged line")
        self.assertEqual([l for l in self.fx.queue() if l], [], "rejected lines are dropped, not retried")

    def test_a_symlinked_queue_is_refused_and_its_target_untouched(self):
        victim = self.fx.root / "victim.txt"
        victim.write_text("precious\n")
        (self.fx.state / "purge.queue").symlink_to(victim)
        self.fx.run("apply")
        self.assertEqual(victim.read_text(), "precious\n")
        self.assertFalse((self.fx.root / "runuser.log").exists())

    def test_a_queue_not_owned_by_the_runner_user_is_refused(self):
        q = self.fx.state / "purge.queue"
        q.write_text("npm sha512:" + "a" * 128 + "\n")
        self.fx.run("apply", GITEA_RUNNER_USER="root" if getpass.getuser() != "root" else "nobody")
        self.assertEqual(q.read_text(), "npm sha512:" + "a" * 128 + "\n")
        self.assertFalse((self.fx.root / "runuser.log").exists())

    def test_venv_entry_must_really_be_a_venv(self):
        d = self.fx.work / "abc/hostexecutor/src"
        d.mkdir(parents=True)
        (d / "code.py").write_text("x")
        self.fx.state.joinpath("purge.queue").write_text("venv work abc/hostexecutor/src\n")
        self.fx.run("apply")
        self.assertTrue((d / "code.py").exists())

    def test_a_symlinked_uv_bucket_is_not_walked(self):
        aid = "L" * 20
        outside = self.fx.root / "outside"
        outside.mkdir()
        (outside / "names-the-id").write_bytes(b"pointer " + aid.encode())
        (self.fx.uv / "wheels-v9").symlink_to(outside)   # a bucket whose files WOULD be read
        self.fx.state.joinpath("purge.queue").write_text(f"uv {aid}\n")
        self.fx.run("apply")
        self.assertTrue((outside / "names-the-id").exists())

    def test_publish_without_a_result_writes_no_beacon(self):
        self.fx.run("publish")
        self.assertEqual(self.fx.prom(), "")

    def test_a_non_finite_result_writes_no_beacon(self):
        res = self.fx.state / "result.json"
        base = '"checked": {"npm": 1, "uv": 1}, "found": {"npm": 0, "uv": 0}, "inplace": {"npm": 0, "uv": 0}'
        for bad in ('"finished": 1e999, "duration": 1.0', '"finished": 1, "duration": NaN',
                    '"finished": 1, "duration": -5'):
            res.write_text("{" + bad + ", " + base + "}")
            self.fx.run("publish")
            self.assertEqual(self.fx.prom(), "", bad)


if __name__ == "__main__":
    unittest.main(verbosity=1)
