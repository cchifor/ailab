"""pve_base thin_textfile: the LVM thin pool fill as node_exporter textfile metrics.

Runs the role's real script (ansible/roles/pve_base/files/pve-thin-textfile.sh) with a fake `lvs` on PATH that
prints a canned listing in the format the script asks for, and renders the role's real units. Pinned here:

  * the pool's data and metadata use and sizes, and per thin volume its size, allocation share, allocated
    bytes (size x share) and whether its device is open (1 / 0), with the vmid taken from vm-/base- names
    (empty for a snapshot); lv_device_open's "unknown" (an inactive volume) reads 0, and a value other than
    "open", "unknown" or nothing is a row that does not parse;
  * linear volumes (root, swap) and other pools' volumes are not reported; an inactive volume (no data
    percent: a template's base-* volume) is skipped without counting an error;
  * a failing lvs, a pool missing from the listing, or a row that does not parse is counted in
    pve_thin_textfile_read_errors (and its series left out), the file is still written with its timestamp;
  * the file is replaced in one rename and is readable by node_exporter (0644);
  * the unit passes the role's pools and directory, the timer fires every 2 min, and the role default is on.

    python3 -m unittest discover -s scripts/tests -p "test_pve_thin_textfile.py" -v
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/pve_base"
SCRIPT = ROLE / "files/pve-thin-textfile.sh"
BASH = shutil.which("bash")

LISTING = """\
  data||thin-pool|1884114714624|61.23|2.29|16106127360|open
  root||linear|103079215104||||open
  swap||linear|8589934592||||open
  vm-4001-disk-0|data|thin|85899345920|45.10|||
  vm-5002-disk-0|data|thin|25769803776|30.00|||unknown
  vm-5002-disk-1|data|thin|171798691840|80.50|||open
  base-9000-disk-0|data|thin|3758096384||||
  snap_vm-5002-disk-0_pre|data|thin|25769803776|10.00|||
  vm-7000-disk-0|otherpool|thin|1073741824|50.00|||open
"""


class TextfileScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.out = self.tmp / "textfile"
        self.out.mkdir()
        self.listing = self.tmp / "listing"
        (self.bin / "lvs").write_text(
            '#!/bin/bash\necho "$*" >> "$LVS_CALLS"\n'
            '[ -f "$LVS_FAIL" ] && { echo "  Volume group \\"pve\\" not found" >&2; exit 5; }\ncat "$LVS_LISTING"\n',
            encoding="utf-8", newline="\n")
        os.chmod(self.bin / "lvs", 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, listing=LISTING, fail=False, pools="pve/data"):
        self.listing.write_text(listing, encoding="utf-8", newline="\n")
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", PVE_THIN_POOLS=pools,
                   PVE_THIN_TEXTFILE_DIR=str(self.out), PVE_THIN_NOW="1795160000",
                   LVS_LISTING=str(self.listing), LVS_CALLS=str(self.tmp / "calls"),
                   LVS_FAIL=str(self.tmp / ("fail" if fail else "nofail")))
        if fail:
            (self.tmp / "fail").write_text("", encoding="utf-8")
        r = subprocess.run([BASH, str(SCRIPT)], env=env, capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stderr)
        self.stderr = r.stderr
        text = (self.out / "pve_thin.prom").read_text(encoding="utf-8")
        samples = dict(re.findall(r"^([a-z_]+(?:\{[^}]*\})?) (\S+)$", text, re.M))
        return text, samples

    def test_pool_and_volumes(self):
        text, s = self.run_script()
        pool = '{pool="pve/data"}'
        self.assertEqual("61.23", s["pve_thin_pool_data_percent" + pool])
        self.assertEqual("2.29", s["pve_thin_pool_metadata_percent" + pool])
        self.assertEqual("1884114714624", s["pve_thin_pool_size_bytes" + pool])
        self.assertEqual("16106127360", s["pve_thin_pool_metadata_size_bytes" + pool])
        v = '{pool="pve/data",lv="vm-5002-disk-1",vmid="5002"}'
        self.assertEqual("171798691840", s["pve_thin_lv_size_bytes" + v])
        self.assertEqual("80.50", s["pve_thin_lv_data_percent" + v])
        self.assertEqual(str(round(171798691840 * 0.805)), s["pve_thin_lv_allocated_bytes" + v])
        self.assertEqual(str(round(85899345920 * 45.10 / 100)),
                         s['pve_thin_lv_allocated_bytes{pool="pve/data",lv="vm-4001-disk-0",vmid="4001"}'])
        self.assertIn('pve_thin_lv_data_percent{pool="pve/data",lv="snap_vm-5002-disk-0_pre",vmid=""}', s)
        self.assertEqual("1", s["pve_thin_lv_open" + v], "an attached disk is open")
        self.assertEqual("0", s['pve_thin_lv_open{pool="pve/data",lv="vm-4001-disk-0",vmid="4001"}'],
                         "a volume nothing holds (a source kept after a move) is not")
        self.assertEqual("0", s['pve_thin_lv_open{pool="pve/data",lv="vm-5002-disk-0",vmid="5002"}'],
                         "lvs says unknown for an inactive volume")
        self.assertEqual("0", s["pve_thin_textfile_read_errors"])
        self.assertEqual("", self.stderr, "a clean read says nothing on stderr")
        self.assertEqual("1795160000", s["pve_thin_textfile_timestamp_seconds"])
        for absent in ("root", "swap", "base-9000-disk-0", "vm-7000-disk-0"):
            self.assertNotIn(f'lv="{absent}"', text)
        self.assertEqual("--noheadings --units b --nosuffix --separator | -o "
                         "lv_name,pool_lv,segtype,lv_size,data_percent,metadata_percent,lv_metadata_size,"
                         "lv_device_open pve",
                         (self.tmp / "calls").read_text(encoding="utf-8").strip())

    def test_every_sample_has_help_and_type(self):
        text, _ = self.run_script()
        families = set(re.findall(r"^([a-z_]+)(?:\{| )", text, re.M))
        for f in families:
            self.assertIn(f"# HELP {f} ", text)
            self.assertIn(f"# TYPE {f} gauge", text)

    def test_a_failing_lvs_is_counted_and_the_file_still_written(self):
        text, s = self.run_script(fail=True)
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        self.assertIn('lvs pve failed:   Volume group "pve" not found', self.stderr, "lvs's own message reaches the journal")
        self.assertEqual("1795160000", s["pve_thin_textfile_timestamp_seconds"])
        self.assertNotIn("pve_thin_pool_data_percent", text)

    def test_a_missing_pool_or_a_bad_row_is_counted(self):
        _, s = self.run_script(listing=LISTING.replace("  data||thin-pool", "  other||thin-pool"))
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        _, s = self.run_script(listing=LISTING.replace("|61.23|", "|n/a|"))
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        self.assertNotIn('pve_thin_pool_data_percent{pool="pve/data"}', s)
        _, s = self.run_script(listing=LISTING.replace("|45.10|", "|4x.10|"))
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        self.assertIn("cannot parse a volume's row:   vm-4001-disk-0|data|thin|85899345920|4x.10|||", self.stderr)
        _, s = self.run_script(listing=LISTING.replace("|80.50|||open", "|80.50|||yes"))
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        self.assertNotIn('pve_thin_lv_open{pool="pve/data",lv="vm-5002-disk-1",vmid="5002"}', s)
        _, s = self.run_script(pools="not-a-pool")
        self.assertEqual("1", s["pve_thin_textfile_read_errors"])
        self.assertIn("'not-a-pool' is not a VG/LV name", self.stderr)

    def test_one_rename_and_readable(self):
        self.run_script()
        self.assertEqual(["pve_thin.prom"], sorted(p.name for p in self.out.iterdir()))
        if os.name == "posix":
            self.assertEqual(0o644, (self.out / "pve_thin.prom").stat().st_mode & 0o777)


class RoleWiring(unittest.TestCase):
    def setUp(self):
        self.defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
        self.env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)

    def render(self, name, **v):
        return self.env.from_string((ROLE / "templates" / name).read_text(encoding="utf-8")).render(**v)

    def test_on_by_default_for_the_pve_data_pool(self):
        self.assertIs(True, self.defaults["pve_thin_textfile_enabled"])
        self.assertEqual(["{{ pve_thin_pool }}"], self.defaults["pve_thin_textfile_pools"])
        self.assertEqual("pve/data", self.defaults["pve_thin_pool"])

    def test_the_role_writes_and_removes_the_enabled_marker(self):
        tasks = (ROLE / "tasks/thin_textfile.yml").read_text(encoding="utf-8")
        self.assertEqual(2, tasks.count("pve_thin_textfile_enabled.prom"), "written when on, removed when off")
        self.assertIn("pve_thin_textfile_enabled 1", tasks)

    def test_the_units(self):
        svc = self.render("pve-thin-textfile.service.j2", pve_thin_textfile_pools=["pve/data"],
                          pve_thin_textfile_dir="/var/lib/prometheus/node-exporter")
        self.assertIn('Environment="PVE_THIN_POOLS=pve/data"', svc)
        self.assertIn("Environment=PVE_THIN_TEXTFILE_DIR=/var/lib/prometheus/node-exporter", svc)
        self.assertIn("ExecStart=/usr/local/sbin/pve-thin-textfile", svc)
        tmr = self.render("pve-thin-textfile.timer.j2")
        self.assertIn("OnUnitActiveSec=2min", tmr)
        self.assertIn("WantedBy=timers.target", tmr)


if __name__ == "__main__":
    unittest.main()
