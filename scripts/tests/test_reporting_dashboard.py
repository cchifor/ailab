"""The AI Lab Fleet dashboard (scripts/gen-reporting-dashboard.py) and its generated ConfigMap.

The review bots skip reporting-dashboard.yaml (pr_reviewer_exclude_globs: it is one ~170 KB JSON line), so
nothing checked that the committed file is what the generator writes (reviewer-codex on #1891). Pinned here:

  * the committed ConfigMap is byte for byte what the generator writes now (run in a scratch copy of the
    tree: the generator writes its output next to itself when imported, and it reads the Local Drives
    generator beside it), and a run writes nothing else (loading the Local Drives generator does not write
    that dashboard's ConfigMap);
  * the Hypervisors row's two drive views (owner, 2026-11-20), which replaced #1891's four NVMe tiles, which
    had replaced two mixed-unit NVMe panels: all of those are gone; the two views sit side by side in the
    16x10 block the tiles filled, half each, and nothing repeats; the latency view is one ms line per host
    (reads and writes together), no fill, legend `nvme0n1 @ <host>`; the usage view is ONE panel drawing each
    host's pool used bytes and one dashed red capacity line (the max over the hosts), with the consumers as
    `<host> · <consumer>` rows of a Name-sorted legend table of last values, hidden from the graph; its guests
    are named by the Local Drives generator's own query, not a copy (which reads $host: a hidden, always-All
    `host` variable);
  * both views link to the Local Drives dashboard (uid ailab-local-drives) with the time range; the dashboard
    header links there too.

What the queries RETURN is checked with promtool in test_reporting_dashboard_promql.py.

    python3 -m unittest discover -s scripts/tests -p "test_reporting_dashboard.py" -v
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts/gen-reporting-dashboard.py"
LD_GEN = ROOT / "scripts/gen-local-drives-dashboard.py"
OUT_REL = Path("kubernetes/apps/infrastructure/monitoring/reporting-dashboard.yaml")
COMMITTED = ROOT / OUT_REL


def _dashboard(text):
    return json.loads(json.loads(text)["data"]["reporting.json"])


def _local_drives():
    spec = importlib.util.spec_from_file_location("gen_local_drives_dashboard", LD_GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DASH = _dashboard(COMMITTED.read_text(encoding="utf-8"))
LATENCY = "Disk Latency per Hypervisor (NVMe, average per I/O)"
USAGE = "Disk Usage by Consumer (local-lvm thin pool)"
GONE = ("NVMe Writes per Day", "NVMe Wear (", "NVMe Life Used", "NVMe Written · last 24 h",
        "NVMe Days to 100% (7-day rate)", "Thin Pool Used (local-lvm)")
LOCAL_DRIVES = "/d/ailab-local-drives/?${__url_time_range}"


def _panel(title):
    return next(p for p in DASH["panels"] if p["title"] == title)


def _props(panel, name):
    """The override properties for the series `name`, as {id: value}."""
    o = next(o for o in panel["fieldConfig"]["overrides"] if o["matcher"] == {"id": "byName", "options": name})
    return {p["id"]: p["value"] for p in o["properties"]}


def _run_generator(d):
    """Run the generator in the scratch tree `d` (it writes its output next to itself), the Local Drives generator
    beside it."""
    (Path(d) / "scripts").mkdir()
    (Path(d) / OUT_REL).parent.mkdir(parents=True)
    for g in (GEN, LD_GEN):
        shutil.copy(g, Path(d) / "scripts" / g.name)
    return subprocess.run([sys.executable, str(Path(d) / "scripts" / GEN.name)], capture_output=True, text=True,
                          timeout=120)


class GeneratedFile(unittest.TestCase):
    def test_committed_configmap_is_the_generator_output(self):
        with tempfile.TemporaryDirectory() as d:
            proc = _run_generator(d)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            fresh = (Path(d) / OUT_REL).read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(COMMITTED.read_bytes().replace(b"\r\n", b"\n"), fresh,
                         "re-run python scripts/gen-reporting-dashboard.py and commit the result")

    def test_it_writes_its_own_configmap_only(self):
        # It loads the Local Drives generator for that dashboard's queries. Loading must not write the Local Drives
        # ConfigMap too (reviewer-claude on #1896); that generator writes only under `if __name__ == "__main__"`.
        with tempfile.TemporaryDirectory() as d:
            proc = _run_generator(d)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            written = {p.relative_to(d).as_posix() for p in (Path(d) / "kubernetes").rglob("*") if p.is_file()}
        self.assertEqual(written, {OUT_REL.as_posix()})


class DriveViews(unittest.TestCase):
    def test_the_old_panels_and_tiles_are_gone(self):
        titles = [p["title"] for p in DASH["panels"]]
        self.assertFalse([t for t in titles if t.startswith(GONE)])

    def test_two_views_side_by_side_where_the_tiles_were(self):
        # #1891's four 8x5 tiles filled the 16x10 block right of the temperature panel; the two views take a half
        # each, at the temperature panel's height, and the next row starts right below (owner, 2026-11-20).
        temp = _panel("Temperature per Hypervisor (APU / NVMe)")["gridPos"]
        self.assertEqual((temp["x"], temp["w"], temp["h"]), (0, 8, 10))
        self.assertEqual(_panel(LATENCY)["gridPos"], {"x": 8, "y": temp["y"], "w": 8, "h": temp["h"]})
        self.assertEqual(_panel(USAGE)["gridPos"], {"x": 16, "y": temp["y"], "w": 8, "h": temp["h"]})
        below = min(p["gridPos"]["y"] for p in DASH["panels"] if p["gridPos"]["y"] > temp["y"])
        self.assertEqual(below, temp["y"] + temp["h"])
        self.assertEqual(next(p for p in DASH["panels"] if p["gridPos"]["y"] == below)["type"], "row")

    def test_one_usage_panel_and_nothing_repeats(self):
        self.assertEqual(len([p for p in DASH["panels"] if p["title"].startswith("Disk Usage")]), 1)
        self.assertEqual([p["title"] for p in DASH["panels"] if "repeat" in p], [])

    def test_no_two_panels_overlap(self):
        boxes = [(p["title"], p["gridPos"]) for p in DASH["panels"]]
        for i, (a, ga) in enumerate(boxes):
            for b, gb in boxes[i + 1:]:
                overlap = (ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"]
                           and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"])
                self.assertFalse(overlap, f"{a!r} overlaps {b!r}")

    def test_latency_is_one_ms_line_per_host_reads_and_writes_together(self):
        p = _panel(LATENCY)
        d = p["fieldConfig"]["defaults"]
        self.assertEqual(p["type"], "timeseries")
        self.assertEqual(d["unit"], "ms")
        self.assertEqual(d["custom"]["fillOpacity"], 0)
        self.assertEqual(d["custom"]["stacking"], {"mode": "none"})
        self.assertNotIn("max", d)                                   # auto range
        self.assertEqual(p["options"]["legend"], {"displayMode": "list", "placement": "bottom"})
        self.assertEqual(p["fieldConfig"]["overrides"], [])
        self.assertEqual(len(p["targets"]), 1)
        t = p["targets"][0]
        self.assertEqual(t["legendFormat"], "nvme0n1 @ {{nodename}}")
        for m in ("read_time_seconds", "write_time_seconds", "reads_completed", "writes_completed"):
            with self.subTest(m):
                self.assertIn(f'increase(node_disk_{m}_total{{job="proxmox-node",device="nvme0n1"}}[5m])', t["expr"])
        self.assertIn(") > 0)", t["expr"])                            # no 0/0 while the drive is idle
        self.assertTrue(t["expr"].startswith("max by (instance, nodename) ((1000 * ("))

    def test_usage_draws_three_host_lines_and_one_capacity_line(self):
        p = _panel(USAGE)
        d = p["fieldConfig"]["defaults"]
        self.assertEqual(p["type"], "timeseries")
        self.assertEqual(d["unit"], "bytes")                          # IEC
        self.assertEqual(d["custom"]["stacking"], {"mode": "none"})
        self.assertEqual(d["custom"]["fillOpacity"], 0)
        self.assertEqual([t["legendFormat"] for t in p["targets"]],
                         ["{{nodename}}", "capacity", "{{nodename}} · {{consumer}}"])
        used, cap, _ = (t["expr"] for t in p["targets"])
        self.assertTrue(used.startswith("max by (nodename) ("))
        self.assertIn("pve_thin_pool_data_percent{", used)
        self.assertTrue(cap.startswith("max(("))                      # one line, the max over the hosts
        self.assertIn('pve_thin_pool_size_bytes{job="proxmox-node",pool="pve/data"}', cap)
        capacity = _props(p, "capacity")
        self.assertEqual(capacity["color"], {"mode": "fixed", "fixedColor": "red"})
        self.assertEqual(capacity["custom.lineStyle"]["fill"], "dash")

    def test_consumers_are_legend_and_tooltip_rows_only(self):
        p = _panel(USAGE)
        hidden = [o for o in p["fieldConfig"]["overrides"] if o["matcher"] == {"id": "byFrameRefID", "options": "C"}]
        self.assertEqual(len(hidden), 1)
        self.assertEqual(hidden[0]["properties"],
                         [{"id": "custom.hideFrom", "value": {"viz": True, "legend": False, "tooltip": False}}])
        # nothing else is hidden: the host lines and capacity are drawn
        self.assertEqual([o for o in p["fieldConfig"]["overrides"]
                          if "custom.hideFrom" in [x["id"] for x in o["properties"]]], hidden)

    def test_legend_is_a_name_sorted_table_of_last_values(self):
        legend = _panel(USAGE)["options"]["legend"]
        self.assertEqual(legend["displayMode"], "table")
        self.assertEqual(legend["calcs"], ["last"])
        self.assertEqual((legend["sortBy"], legend["sortDesc"]), ("Name", False))
        self.assertEqual(legend["placement"], "bottom")               # below the graph, the owner's mockup

    def test_usage_names_guests_with_the_local_drives_query(self):
        ld = _local_drives()
        e = _panel(USAGE)["targets"][2]["expr"]
        self.assertIn(ld.thin_by_guest("pve_thin_lv_allocated_bytes"), e)
        self.assertIn(ld.GUEST_NAMES, e)
        self.assertTrue(e.startswith("sum by (nodename, consumer) ("))

    def test_the_host_variable_is_hidden_and_all(self):
        # Kept for the Local Drives queries the consumer rows reuse, which read $host; nothing repeats over it.
        v = next(v for v in DASH["templating"]["list"] if v["name"] == "host")
        self.assertEqual(v["hide"], 2)
        self.assertTrue(v["includeAll"] and v["multi"])
        self.assertEqual(v["current"]["value"], ["$__all"])
        self.assertEqual(v["definition"], 'label_values(node_uname_info{job="proxmox-node"}, nodename)')
        self.assertEqual([p["title"] for p in DASH["panels"] if "$host" in json.dumps(p)], [USAGE])

    def test_both_views_link_to_local_drives_with_the_time_range(self):
        for title in (LATENCY, USAGE):
            with self.subTest(title):
                self.assertEqual([lk["url"] for lk in _panel(title)["links"]], [LOCAL_DRIVES])

    def test_the_header_links_to_local_drives(self):
        self.assertIn("/d/ailab-local-drives/", [lk["url"] for lk in DASH.get("links", [])])


if __name__ == "__main__":
    unittest.main()
