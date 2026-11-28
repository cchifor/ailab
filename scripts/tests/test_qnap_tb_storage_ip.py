"""Tests for scripts/qnap-tb-storage-ip.sh, the QNAP storage reconciler (root cron, every minute).

It restarts NFS, which every control plane's disk is on (ADR 0043), so most of these tests pin the
cases where it must NOT restart:
  * the rw export of the pve-nfs share to the Thunderbolt subnet is present (the normal case);
  * the share is not exported yet (judge on a later run);
  * it was missing on one run only (exports still being added);
  * another share's entries never count.
It restarts once, on the second consecutive run that shows the Thunderbolt subnet's rw entry
missing while the 10GbE subnet's is present (2026-06-14), and then never again this boot.

The script runs under /bin/sh (dash on the CI runners) against stub ip/exportfs/logger/nfs commands.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "qnap-tb-storage-ip.sh"
SH = shutil.which("sh")

OPTS = "async,wdelay,hide,no_subtree_check,fsid=dcaddcf5,sec=sys,rw,insecure,no_root_squash,no_all_squash"


def export(path, *nets):
    return "".join(f"{path}\n\t\t{net}({OPTS})\n" for net in nets)


HEALTHY = (export("/share/ZFS18_DATA/pve-nfs", "10.55.0.0/24", "10.55.1.0/24", "192.168.0.0/24")
           + export("/share/NFSv=4/pve-nfs", "10.55.0.0/24", "10.55.1.0/24"))
TB_MISSING = (export("/share/ZFS18_DATA/pve-nfs", "10.55.1.0/24", "192.168.0.0/24")
              + export("/share/NFSv=4/pve-nfs", "10.55.1.0/24"))

STUBS = {
    "ip": """\
        #!/bin/sh
        case "$2" in
          show) [ -f "$FIX/has_ip" ] && echo "    inet 10.55.0.254/24 brd 10.55.0.255 scope global tbtbr0"; exit 0 ;;
          add) echo "$*" >> "$FIX/ip_add" ;;
        esac
        """,
    "exportfs": """\
        #!/bin/sh
        cat "$FIX/exports" 2>/dev/null
        """,
    "logger": """\
        #!/bin/sh
        echo "$*" >> "$FIX/syslog"
        """,
    "nfs": """\
        #!/bin/sh
        echo "$1" >> "$FIX/nfs"
        """,
}


@unittest.skipUnless(SH and os.name == "posix", "needs a POSIX /bin/sh")
class TbStorageIpTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="tbip-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.fix = self.root / "fix"
        self.state = self.root / "state"
        self.shares = self.root / "share"
        for d in (self.bin, self.fix, self.state, self.shares / "pve-nfs"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text(textwrap.dedent(body), encoding="utf-8")
            p.chmod(0o755)
        self.put("has_ip", "")
        self.put("exports", HEALTHY)

    def put(self, name, text):
        (self.fix / name).write_text(text, encoding="utf-8")

    def read(self, name):
        p = self.fix / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def run_it(self):
        env = {"PATH": "/usr/bin:/bin", "FIX": str(self.fix), "TB_PATH": f"{self.bin}:/usr/bin:/bin",
               "TB_STATE": str(self.state), "TB_SHARE_ROOT": str(self.shares),
               "TB_NFS_INIT": str(self.bin / "nfs")}
        r = subprocess.run([SH, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def flagged(self):
        return (self.state / ".ailab-nfs-reexported").exists()

    def test_script_parses_as_posix_sh(self):
        r = subprocess.run([SH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_healthy_exports_are_judged_once_and_never_restarted(self):
        for _ in range(3):
            self.run_it()
        self.assertEqual(self.read("nfs"), "")
        self.assertEqual(self.read("syslog"), "")
        self.assertTrue(self.flagged())

    def test_a_missing_tb_rule_restarts_nfs_on_the_second_run_only_once(self):
        self.put("exports", TB_MISSING)
        self.run_it()
        self.assertEqual(self.read("nfs"), "", "one sample may be mid-export: never act on it")
        self.assertFalse(self.flagged())
        self.run_it()
        self.assertEqual(self.read("nfs").split(), ["restart"])
        self.assertIn("missing (0 of 2 entries)", self.read("syslog"))
        self.assertTrue(self.flagged())
        for _ in range(3):
            self.run_it()
        self.assertEqual(self.read("nfs").split(), ["restart"], "once per boot")

    def test_a_rule_missing_on_one_run_only_is_not_restarted(self):
        self.put("exports", TB_MISSING)
        self.run_it()
        self.put("exports", HEALTHY)
        self.run_it()
        self.put("exports", TB_MISSING)
        self.run_it()
        self.assertEqual(self.read("nfs"), "")
        self.assertTrue(self.flagged(), "the healthy run judged the boot")

    def test_nothing_is_judged_before_the_share_is_exported(self):
        self.put("exports", "")
        for _ in range(3):
            self.run_it()
        self.assertFalse(self.flagged())
        self.assertEqual(self.read("nfs"), "")
        self.put("exports", HEALTHY)
        self.run_it()
        self.assertTrue(self.flagged())
        self.assertEqual(self.read("nfs"), "")

    def test_another_shares_entries_do_not_count(self):
        # A second share exported rw only to the 10GbE subnet must not make the TB subnet look short.
        self.put("exports", HEALTHY + export("/share/ZFS18_DATA/backups", "10.55.1.0/24", "10.55.1.0/24"))
        self.run_it()
        self.run_it()
        self.assertEqual(self.read("nfs"), "")
        self.assertTrue(self.flagged())

    def test_a_share_name_that_only_ends_alike_does_not_count(self):
        self.put("exports", HEALTHY + export("/share/ZFS18_DATA/old-pve-nfs", "10.55.1.0/24"))
        self.run_it()
        self.run_it()
        self.assertEqual(self.read("nfs"), "")

    def test_no_share_directory_means_no_judgement(self):
        shutil.rmtree(self.shares / "pve-nfs")
        self.put("exports", TB_MISSING)
        for _ in range(3):
            self.run_it()
        self.assertFalse(self.flagged())
        self.assertEqual(self.read("nfs"), "")

    def test_the_service_ip_is_added_only_when_missing(self):
        self.run_it()
        self.assertEqual(self.read("ip_add"), "")
        (self.fix / "has_ip").unlink()
        self.run_it()
        self.assertIn("10.55.0.254/24 dev tbtbr0", self.read("ip_add"))


if __name__ == "__main__":
    unittest.main()
