"""Tests for scripts/qnap-iscsi-portal-watchdog.sh, the QNAP iSCSI portal watchdog.

The watchdog restarts the NAS's iSCSI target service. The restart drops every session, so most of
these tests are NEGATIVE: they pin the cases where it must NOT restart.
  * A target is stale only when an allowed portal is an IPv4 that no local interface holds
    (0.0.0.0, 127.0.0.1 and IPv6 values never count).
  * It acts on the second consecutive run that shows the same picture, never on the first, and a
    picture that changes between runs (a bridge rebuild in progress) resets the count.
  * Any logged-in session, or a session count it cannot parse, blocks the restart.
  * It skips while the service is (re)loading, and while another run holds the lock.
  * At most MAX_RESTARTS per boot, MIN_GAP_SECS apart.
  * `--check` never restarts and never writes state.

The script runs under /bin/sh (dash on the CI runners: stricter than the NAS's busybox ash) against
stub iscsiadmin/ip/ps/logger/log_tool/init commands driven by fixture files.
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "qnap-iscsi-portal-watchdog.sh"
SH = shutil.which("sh")

IQN = "iqn.2004-04.com.qnap:tbs-h574tx:iscsi.iscsi-trident-pvc-{n}.959afe"
IP_GOOD = """\
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 192.168.1.225/23 brd 192.168.1.255 scope global eth0
3: eth1    inet 10.55.1.254/24 brd 10.55.1.255 scope global eth1
9: tbtbr0    inet 10.55.0.254/24 brd 10.55.0.255 scope global tbtbr0
"""
IP_PLACEHOLDER = IP_GOOD.replace("10.55.0.254/24 brd 10.55.0.255", "169.254.6.247/16 brd 169.254.255.255")
PORTALS_GOOD = "Allow portal = 10.55.0.254\nAllow portal = 127.0.0.1\n"
PORTALS_STALE = "Allow portal = 169.254.6.247\nAllow portal = 127.0.0.1\n"

STUBS = {
    # get_target serves $FIX/targets, then swaps in $FIX/targets_next if present, so a test can change
    # the answer between two calls of ONE run. get_allow_portal fails like the NAS does (exit 2 and
    # "Target doesn't exist") without a portal file or with a noportal.<iqn> marker, and exits 1
    # silently with $FIX/portal_fail.
    "iscsiadmin": """\
        #!/bin/sh
        case "$1" in
          get_target)
            cat "$FIX/targets" 2>/dev/null
            [ -f "$FIX/targets_next" ] && mv -f "$FIX/targets_next" "$FIX/targets"
            exit 0 ;;
          get_allow_portal)
            [ -f "$FIX/portal_fail" ] && exit 1
            k=$(printf '%s' "$2" | tr ':' '_')
            f="$FIX/portal.$k"
            [ -f "$f" ] || f="$FIX/portals"
            if [ -f "$FIX/noportal.$k" ] || [ ! -f "$f" ]; then
              echo "Get allow portal count fail, res = 0x2040102, Target doesn't exist"; exit 2
            fi
            cat "$f" ;;
        esac
        """,
    "ip": """\
        #!/bin/sh
        [ -f "$FIX/ip_fail" ] && exit 1
        cat "$FIX/ip"
        """,
    "ps": """\
        #!/bin/sh
        echo "  PID USER       VSZ STAT COMMAND"
        echo "    1 admin     1234 S    init"
        cat "$FIX/ps_extra" 2>/dev/null
        exit 0
        """,
    "logger": """\
        #!/bin/sh
        echo "$*" >> "$FIX/syslog"
        """,
    "log_tool": """\
        #!/bin/sh
        echo "$*" >> "$FIX/qulog"
        """,
    # The init script: records each restart; if told to, the restart re-binds every target.
    "iscsitrgt.sh": """\
        #!/bin/sh
        echo "$1" >> "$FIX/restarts"
        [ -f "$FIX/restart_fixes" ] && cp "$FIX/portals_after" "$FIX/portals"
        exit 0
        """,
}


@unittest.skipUnless(SH and os.name == "posix", "needs a POSIX /bin/sh")
class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="iscsiwd-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.fix = self.root / "fix"
        self.state = self.root / "state"
        self.bin.mkdir()
        self.fix.mkdir()
        for name, body in STUBS.items():
            p = self.bin / name
            p.write_text(textwrap.dedent(body), encoding="utf-8")
            p.chmod(0o755)
        self.healthy()

    # --- fixture helpers ------------------------------------------------------------------------
    def put(self, name, text):
        (self.fix / name).write_text(text, encoding="utf-8")

    @staticmethod
    def targets_text(n=3, sessions=0):
        lines = []
        for i in range(1, n + 1):
            s = sessions if i == 1 else 0
            lines.append(f"target = {IQN.format(n=i)}, tid = {i}, sess_cnt = {s}, conn_cnt = {s}, target_enable = enable")
        return "\n".join(lines) + "\n"

    def targets(self, n=3, sessions=0, raw=None):
        self.put("targets", raw if raw is not None else self.targets_text(n, sessions))

    @staticmethod
    def key(i):
        return IQN.format(n=i).replace(":", "_")

    def healthy(self):
        self.put("ip", IP_GOOD)
        self.put("portals", PORTALS_GOOD)
        self.targets()

    def cold_boot(self, sessions=0):
        """tbtbr0 rebuilt with 10.55.0.254 after the targets were bound to its placeholder."""
        self.put("ip", IP_GOOD)
        self.put("portals", PORTALS_STALE)
        self.targets(sessions=sessions)

    def run_wd(self, *args, **env):
        e = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "FIX": str(self.fix),
            "ISCSI_WD_PATH": f"{self.bin}:/usr/bin:/bin",
            "ISCSI_INIT": str(self.bin / "iscsitrgt.sh"),
            "ISCSI_WD_STATE": str(self.state),
            "ISCSI_WD_LOG": str(self.root / "watchdog.log"),
            "LOG_TOOL": str(self.bin / "log_tool"),
            "MIN_GAP_SECS": "0",
        }
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run([SH, str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=30)

    def restarts(self):
        p = self.fix / "restarts"
        return p.read_text(encoding="utf-8").split() if p.exists() else []

    def read(self, name):
        p = self.fix / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def log(self):
        p = self.root / "watchdog.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # --- the shape of the script ----------------------------------------------------------------
    def test_script_parses_as_posix_sh(self):
        r = subprocess.run([SH, "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    # --- healthy --------------------------------------------------------------------------------
    def test_healthy_does_nothing_and_logs_nothing(self):
        for _ in range(3):
            r = self.run_wd()
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.restarts(), [])
        self.assertEqual(self.log(), "")
        self.assertEqual(self.read("qulog"), "")

    def test_any_loopback_and_ipv6_portals_are_never_stale(self):
        self.put("portals", "Allow portal = 0.0.0.0\nAllow portal = 127.0.0.1\nAllow portal = fe80::1\n")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])

    # --- the cold-boot failure ------------------------------------------------------------------
    def test_cold_boot_restarts_on_the_second_identical_run_then_reports_recovery(self):
        self.cold_boot()
        self.put("portals_after", PORTALS_GOOD)
        self.put("restart_fixes", "")

        self.run_wd()
        self.assertEqual(self.restarts(), [], "must not act on the first sighting")
        self.assertIn("re-checking", self.log())

        self.run_wd()
        self.assertEqual(self.restarts(), ["restart"])
        self.assertIn("169.254.6.247", self.read("qulog"))

        self.run_wd()  # bindings are good again
        self.assertEqual(self.restarts(), ["restart"])
        self.assertIn("recovered", self.log())
        self.assertIn("recovered", self.read("qulog"))

    def test_placeholder_still_present_is_not_stale(self):
        # Before the Thunderbolt init rebuilds tbtbr0, the placeholder IS local: nothing to fix yet.
        self.put("ip", IP_PLACEHOLDER)
        self.put("portals", PORTALS_STALE)
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])

    def test_changing_picture_resets_the_debounce(self):
        self.cold_boot()
        self.run_wd()
        # The bridge is mid-rebuild: the local addresses moved since the last run.
        self.put("ip", IP_GOOD.replace("9: tbtbr0    inet 10.55.0.254/24 brd 10.55.0.255 scope global tbtbr0\n", ""))
        self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.put("ip", IP_GOOD)
        self.run_wd()
        self.assertEqual(self.restarts(), [], "the first run of a new picture must not act")
        self.run_wd()
        self.assertEqual(self.restarts(), ["restart"])

    # --- the safety gates -----------------------------------------------------------------------
    def test_live_sessions_block_the_restart_and_raise_one_error(self):
        self.cold_boot(sessions=2)
        for _ in range(4):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.assertIn("NOT restarting", self.log())
        self.assertEqual(self.read("qulog").count("not restarting automatically"), 1)

    def test_unparseable_session_count_blocks_the_restart(self):
        self.cold_boot()
        self.targets(raw=f"target = {IQN.format(n=1)}, tid = 1, target_enable = enable\n"
                         f"target = {IQN.format(n=2)}, tid = 2, sess_cnt = 0, conn_cnt = 0, target_enable = enable\n")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.assertIn("sessions=unknown", self.log())

    def test_skips_while_the_service_is_loading(self):
        self.cold_boot()
        self.put("ps_extra", "30206 admin     36336 S   /usr/local/sbin/scst_util --load\n")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.put("ps_extra", "27163 admin      2736 S   /bin/sh /etc/init.d/iscsitrgt.sh restart\n")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])

    def test_no_targets_or_no_addresses_means_no_action(self):
        self.cold_boot()
        self.put("targets", "")
        for _ in range(3):
            self.run_wd()
        self.cold_boot()
        self.put("ip_fail", "")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.assertIn("no local IPv4 address could be listed", self.log())
        self.assertEqual(self.log().count("cannot judge"), 1, "logged once, not every minute")

    def test_live_lock_holder_blocks_and_dead_one_is_reclaimed(self):
        self.cold_boot()
        lock = self.state / "lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")  # this test process: alive
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        (lock / "pid").write_text("2147480000", encoding="utf-8")  # no such process
        self.run_wd()
        self.run_wd()
        self.assertEqual(self.restarts(), ["restart"])
        self.assertFalse(lock.exists(), "the lock must be released after the run")

    def test_lock_without_pid_is_reclaimed_after_three_runs(self):
        self.cold_boot()
        (self.state / "lock").mkdir(parents=True)
        self.run_wd()
        self.run_wd()
        self.assertFalse((self.state / "seen").exists(), "a held lock must stop the run before any judging")
        self.run_wd()  # third: reclaimed, first sighting
        self.run_wd()
        self.assertEqual(self.restarts(), ["restart"])

    def test_session_login_during_the_scan_blocks_the_restart(self):
        # The first get_target of the confirming run says 0; an initiator logs in to a reachable
        # target while the portals are scanned, so the re-read right before the restart says 1.
        self.cold_boot()
        self.run_wd()
        self.put("targets_next", self.targets_text(sessions=1))
        self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.assertIn("sessions=1", self.log())
        self.assertIn("not restarting automatically", self.read("qulog"))

    # --- a failed inspection is never "healthy", and always breaks the debounce -------------------
    def test_failed_portal_query_is_cannot_judge_not_healthy(self):
        for how in ("target-doesnt-exist", "silent-exit-1"):
            with self.subTest(how=how):
                self.setUp()
                if how == "target-doesnt-exist":
                    (self.fix / "portals").unlink()
                else:
                    self.put("portal_fail", "")
                r = self.run_wd("--check")
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn("cannot judge", r.stdout)
                # A restart happened earlier; a failed inspection must not report it as recovered.
                self.state.mkdir(parents=True, exist_ok=True)
                (self.state / "pending").write_text("", encoding="utf-8")
                for _ in range(3):
                    self.run_wd()
                self.assertTrue((self.state / "pending").exists())
                self.assertNotIn("recovered", self.log())
                self.assertEqual(self.log().count("cannot judge"), 1, "logged once, not every minute")
                self.assertEqual(self.restarts(), [])

    def test_one_failed_target_blocks_action_on_the_others(self):
        self.cold_boot()
        self.put(f"noportal.{self.key(2)}", "")
        for _ in range(3):
            self.run_wd()
        self.assertEqual(self.restarts(), [])
        self.assertIn("1 of 3 targets", self.log())

    def test_an_unjudgeable_run_breaks_the_debounce(self):
        interruptions = {
            "busy": lambda: self.put("ps_extra", "30206 admin 36336 S /usr/local/sbin/scst_util --load\n"),
            "no-addresses": lambda: self.put("ip_fail", ""),
            "no-targets": lambda: self.put("targets", ""),
            "portal-failure": lambda: self.put("portal_fail", ""),
        }
        for name, interrupt in interruptions.items():
            with self.subTest(interruption=name):
                self.setUp()
                self.cold_boot()
                self.run_wd()                       # stale picture A, first sighting
                interrupt()
                self.run_wd()                       # cannot judge
                for f in ("ps_extra", "ip_fail", "portal_fail"):
                    (self.fix / f).unlink(missing_ok=True)
                self.cold_boot()
                self.run_wd()                       # A again, but not CONSECUTIVELY
                self.assertEqual(self.restarts(), [], f"{name}: acted across an unjudged run")
                self.run_wd()
                self.assertEqual(self.restarts(), ["restart"])

    # --- the restart budget ---------------------------------------------------------------------
    def test_budget_caps_restarts_per_boot_and_reports_once(self):
        self.cold_boot()  # restarts never fix it
        for _ in range(12):
            self.run_wd(MAX_RESTARTS=2)
        self.assertEqual(self.restarts(), ["restart", "restart"])
        self.assertEqual(self.read("qulog").count("manual action needed"), 1)

    def test_min_gap_between_restarts(self):
        self.cold_boot()
        for _ in range(8):
            self.run_wd(MIN_GAP_SECS=3600)
        self.assertEqual(self.restarts(), ["restart"])

    def test_unknown_argument_is_refused_not_run(self):
        self.cold_boot()
        for _ in range(3):
            r = self.run_wd("--chekc")
            self.assertEqual(r.returncode, 2)
            self.assertIn("usage", r.stderr)
        self.assertEqual(self.restarts(), [])
        self.assertFalse(self.state.exists(), "a refused argument must not take the lock or write state")

    # --- --check ------------------------------------------------------------------------------
    def test_check_mode_reports_and_never_acts(self):
        r = self.run_wd("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok: 3 targets", r.stdout)

        self.cold_boot()
        for _ in range(3):
            r = self.run_wd("--check")
            self.assertEqual(r.returncode, 1)
            self.assertIn("STALE", r.stdout)
            self.assertIn("169.254.6.247", r.stdout)
        self.assertEqual(self.restarts(), [])
        self.assertFalse(self.state.exists(), "--check must not write state")

        self.put("ps_extra", "30206 admin     36336 S   /usr/local/sbin/scst_util --load\n")
        self.assertEqual(self.run_wd("--check").returncode, 2)


if __name__ == "__main__":
    unittest.main()
