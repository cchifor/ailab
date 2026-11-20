"""The AI Lab Fleet dashboard (scripts/gen-reporting-dashboard.py) and its generated ConfigMap.

The review bots skip reporting-dashboard.yaml (pr_reviewer_exclude_globs: it is one ~170 KB JSON line), so
nothing checked that the committed file is what the generator writes (reviewer-codex on #1891). Pinned here:

  * the committed ConfigMap is byte for byte what the generator writes now (run in a scratch copy of the
    tree: the generator writes its output next to itself when imported);
  * the Hypervisors row's NVMe tiles that replaced the two NVMe panels (owner, 2026-11-20): the old panels
    are gone; each tile has one unit, one value per host named by host, and a link to the Local Drives
    dashboard (uid ailab-local-drives) that keeps the time range; the dashboard header links there too;
  * the bytes-written figures fall back to increase() when the drive's counter reset (a replaced drive),
    and the projection needs 1% used.

    python3 -m unittest discover -s scripts/tests -p "test_reporting_dashboard.py" -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts/gen-reporting-dashboard.py"
OUT_REL = Path("kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml")
COMMITTED = ROOT / OUT_REL


def _dashboard(text):
    return json.loads(json.loads(text)["data"]["reporting.json"])


DASH = _dashboard(COMMITTED.read_text(encoding="utf-8"))
TILES = ("NVMe Life Used", "NVMe Written · last 24 h", "NVMe Days to 100% (7-day rate)", "Thin Pool Used (local-lvm)")


def _panel(title):
    return next(p for p in DASH["panels"] if p["title"] == title)


class GeneratedFile(unittest.TestCase):
    def test_committed_configmap_is_the_generator_output(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "scripts").mkdir()
            (Path(d) / OUT_REL).parent.mkdir(parents=True)
            shutil.copy(GEN, Path(d) / "scripts" / GEN.name)
            proc = subprocess.run([sys.executable, str(Path(d) / "scripts" / GEN.name)], capture_output=True,
                                  text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            fresh = (Path(d) / OUT_REL).read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(COMMITTED.read_bytes().replace(b"\r\n", b"\n"), fresh,
                         "re-run python scripts/gen-reporting-dashboard.py and commit the result")


class NvmeTiles(unittest.TestCase):
    def test_the_two_mixed_unit_panels_are_gone(self):
        titles = [p["title"] for p in DASH["panels"]]
        self.assertFalse([t for t in titles if t.startswith(("NVMe Writes per Day", "NVMe Wear ("))])

    def test_four_tiles_fill_the_space_beside_the_temperature_panel(self):
        temp = _panel("Temperature per Hypervisor (APU / NVMe)")["gridPos"]
        boxes = sorted((_panel(t)["gridPos"]["y"], _panel(t)["gridPos"]["x"]) for t in TILES)
        y = temp["y"]
        self.assertEqual(boxes, [(y, 8), (y, 16), (y + 5, 8), (y + 5, 16)])
        for t in TILES:
            g = _panel(t)["gridPos"]
            self.assertEqual((g["w"], g["h"]), (8, 5), t)

    def test_each_tile_is_one_unit_one_value_per_host_and_links_to_local_drives(self):
        for t in TILES:
            p = _panel(t)
            with self.subTest(t):
                self.assertEqual(p["type"], "stat")
                self.assertTrue(p["fieldConfig"]["defaults"]["unit"])
                self.assertEqual(p["fieldConfig"]["overrides"], [])
                self.assertEqual(len(p["targets"]), 1)
                self.assertEqual(p["targets"][0]["legendFormat"], "{{nodename}}")
                self.assertTrue(p["targets"][0]["instant"])
                self.assertEqual([lk["url"] for lk in p["links"]], ["/d/ailab-local-drives/?${__url_time_range}"])

    def test_the_header_links_to_local_drives(self):
        self.assertIn("/d/ailab-local-drives/", [lk["url"] for lk in DASH.get("links", [])])

    def test_bytes_written_survive_a_drive_replacement(self):
        for t in ("NVMe Written · last 24 h", "NVMe Days to 100% (7-day rate)"):
            e = _panel(t)["targets"][0]["expr"]
            with self.subTest(t):
                self.assertIn("unless resets(nvme_data_units_written_total", e)
                self.assertIn("or increase(nvme_data_units_written_total", e)
        self.assertIn(">= 0.01", _panel("NVMe Days to 100% (7-day rate)")["targets"][0]["expr"])

    def test_the_pool_tile_counts_each_host_once(self):
        # a pve-exporter pod change is a new series; without max by (nodename) the pool reads twice its size
        self.assertIn("max by (nodename)", _panel("Thin Pool Used (local-lvm)")["targets"][0]["expr"])


if __name__ == "__main__":
    unittest.main()
