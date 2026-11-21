"""The "Local Drives" dashboard (scripts/gen-local-drives-dashboard.py) and its generated ConfigMap.

Pinned here, without Grafana or Prometheus:

  * the committed ConfigMap is exactly what the generator renders now (no hand edit, no stale output),
    and Flux deploys it (listed in the monitoring kustomization, sidecar label, namespace);
  * the layout: unique panel ids, every panel inside the 24-column grid and below its row, no two
    panels overlapping;
  * the host selector works everywhere: every query filters on $host, the variable is multi-select
    with All, and $topn is defined where it is used;
  * one unit per time-series panel: no override changes a panel's unit (the owner's complaint about
    the old "NVMe Writes per Day + Busy %" panel was TB/day and % on one axis);
  * every query is well-formed text: balanced brackets and quotes, and no Python formatting left over;
  * the tables rename and order only columns their queries produce (Value #<refId> of a real target).

The PromQL itself is checked against live Prometheus and, for the series that are not live yet (#1886's
pve_thin_*, the trim beacons), against synthetic series with promtool (the PR records both runs).

    python3 -m unittest discover -s scripts/tests -p "test_local_drives_dashboard.py" -v
"""
import importlib.util
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN = ROOT / "scripts/gen-local-drives-dashboard.py"
OUT = ROOT / "kubernetes/apps/infrastructure/monitoring/local-drives-dashboard.yaml"
KUSTOMIZATION = ROOT / "kubernetes/apps/infrastructure/monitoring/kustomization.yaml"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_local_drives_dashboard", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN_MOD = _load_generator()
DASH = GEN_MOD.build()
PANELS = [p for p in DASH["panels"] if p["type"] != "row"]
TARGETS = [(p, t) for p in PANELS for t in p.get("targets", [])]


def _balanced(expr):
    """Brackets balance outside string literals, and every string literal is closed."""
    pairs, stack, quote, i = {")": "(", "]": "[", "}": "{"}, [], None, 0
    while i < len(expr):
        c = expr[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c in "([{":
            stack.append(c)
        elif c in ")]}":
            if not stack or stack.pop() != pairs[c]:
                return False
        i += 1
    return not stack and quote is None


class GeneratedFile(unittest.TestCase):
    def test_committed_configmap_is_the_generator_output(self):
        self.assertEqual(OUT.read_text(encoding="utf-8"), GEN_MOD.render(),
                         "re-run python scripts/gen-local-drives-dashboard.py and commit the result")

    def test_configmap_is_loaded_by_the_grafana_sidecar(self):
        cm = json.loads(OUT.read_text(encoding="utf-8"))
        self.assertEqual(cm["kind"], "ConfigMap")
        self.assertEqual(cm["metadata"]["namespace"], "monitoring")
        self.assertEqual(cm["metadata"]["labels"], {"grafana_dashboard": "1"})
        (key, body), = cm["data"].items()
        self.assertTrue(key.endswith(".json"))
        self.assertEqual(json.loads(body)["uid"], "ailab-local-drives")

    def test_flux_deploys_it(self):
        entries = [line.split("#")[0].strip() for line in KUSTOMIZATION.read_text(encoding="utf-8").splitlines()]
        self.assertIn("- local-drives-dashboard.yaml", entries)

    def test_build_is_deterministic(self):
        self.assertEqual(GEN_MOD.render(), GEN_MOD.render())


class Layout(unittest.TestCase):
    def test_panel_ids_are_unique(self):
        ids = [p["id"] for p in DASH["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_panel_fits_the_grid(self):
        for p in DASH["panels"]:
            g = p["gridPos"]
            with self.subTest(p["title"]):
                self.assertGreaterEqual(g["x"], 0)
                self.assertLessEqual(g["x"] + g["w"], 24)
                self.assertGreater(g["h"], 0)

    def test_no_two_panels_overlap(self):
        boxes = [(p["title"], p["gridPos"]) for p in DASH["panels"]]
        for i, (a, ga) in enumerate(boxes):
            for b, gb in boxes[i + 1:]:
                overlap = (ga["x"] < gb["x"] + gb["w"] and gb["x"] < ga["x"] + ga["w"]
                           and ga["y"] < gb["y"] + gb["h"] and gb["y"] < ga["y"] + ga["h"])
                self.assertFalse(overlap, f"{a!r} overlaps {b!r}")

    def test_a_repeated_panel_has_its_line_to_itself(self):
        # Grafana lays the clones of a horizontally repeated panel side by side across the full width.
        for p in DASH["panels"]:
            if p.get("repeat"):
                g = p["gridPos"]
                with self.subTest(p["title"]):
                    self.assertEqual(p["repeat"], "host")
                    self.assertEqual(g["x"], 0)
                    others = [q for q in DASH["panels"] if q is not p and q["gridPos"]["y"] < g["y"] + g["h"]
                              and g["y"] < q["gridPos"]["y"] + q["gridPos"]["h"]]
                    self.assertEqual(others, [])

    def test_panels_follow_their_row(self):
        last_row_y = -1
        for p in DASH["panels"]:
            if p["type"] == "row":
                self.assertGreater(p["gridPos"]["y"], last_row_y)
                last_row_y = p["gridPos"]["y"]
            else:
                self.assertGreater(p["gridPos"]["y"], last_row_y, p["title"])


class Queries(unittest.TestCase):
    def test_every_query_filters_on_the_host_selector(self):
        for p, t in TARGETS:
            with self.subTest(f"{p['title']} {t['refId']}"):
                self.assertIn("$host", t["expr"])

    def test_every_query_is_well_formed(self):
        for p, t in TARGETS:
            e = t["expr"]
            with self.subTest(f"{p['title']} {t['refId']}"):
                self.assertTrue(_balanced(e), e[:200])
                self.assertNotRegex(e, r"\{\{\{|\}\}\}|\bNone\b")      # f-string debris

    def test_a_time_series_reads_the_whole_range_only_when_pinned_to_its_end(self):
        # rate(x[$__range]) at every step of a graph would look back a whole range from each point; the only
        # use is the top-N pick, evaluated once at the range's end.
        marker = " and on(nodename, name) last_over_time((topk($topn, "
        for p, t in TARGETS:
            if p["type"] != "timeseries" or "$__range" not in t["expr"]:
                continue
            with self.subTest(f"{p['title']} {t['refId']}"):
                drawn, _, pick = t["expr"].partition(marker)
                self.assertTrue(pick, "a $__range window outside the top-N pick")
                self.assertNotIn("$__range", drawn)
                self.assertTrue(pick.endswith("[1m:1m] @ end())"))

    def test_top_n_panels_draw_what_they_pick(self):
        # The pick ranks the same metric the panel draws, so the legend is the top N of THAT metric.
        marker = " and on(nodename, name) last_over_time((topk($topn, "
        metrics = lambda e: set(re.findall(r"\b((?:node_disk|pve_thin_lv)_\w+?)\{", e)) - {  # noqa: E731
            "node_disk_device_mapper_info", "node_disk_info"}
        for p, t in TARGETS:
            if marker in t["expr"]:
                drawn, _, pick = t["expr"].partition(marker)
                with self.subTest(p["title"]):
                    self.assertTrue(metrics(drawn))
                    self.assertEqual(metrics(drawn), metrics(pick))

    def test_per_guest_counters_carry_their_volume_before_any_range_function(self):
        # A dm-N number changes owner when a guest stops or the host reboots. rate()/increase() on the dm counter
        # joined to its owner AFTERWARDS would hand a window's (or the range's) old-owner I/O, and the reset, to
        # the new owner (reviewer-codex on #1890): the volume name is attached to each raw sample first.
        for p, t in TARGETS:
            with self.subTest(f"{p['title']} {t['refId']}"):
                self.assertNotRegex(t["expr"], r'(rate|increase)\(node_disk_\w+\{[^}]*dm-')
                for m in re.finditer(r'node_disk_\w+\{[^}]*device=~"dm-\[0-9\]\+"\}', t["expr"]):
                    after = t["expr"][m.end():]
                    self.assertTrue(after.startswith(" * on(instance, device) group_left(lv_name) "
                                                     "node_disk_device_mapper_info"), after[:80])

    def test_variables_used_are_defined(self):
        defined = {v["name"] for v in DASH["templating"]["list"]}
        builtin = {"__rate_interval", "__range", "__range_s", "__interval"}
        for p, t in TARGETS:
            for v in re.findall(r"\$(__\w+|[a-z]\w*)", t["expr"]):
                with self.subTest(f"{p['title']} {t['refId']} ${v}"):
                    self.assertIn(v, defined | builtin)
        for p in PANELS:
            for v in re.findall(r"\$([a-z]\w*)", p["title"]):
                self.assertIn(v, defined, p["title"])

    def test_host_variable_is_multi_with_all(self):
        host = next(v for v in DASH["templating"]["list"] if v["name"] == "host")
        self.assertTrue(host["multi"])
        self.assertTrue(host["includeAll"])
        self.assertEqual(host["current"]["value"], ["$__all"])
        self.assertIn('job="proxmox-node"', host["definition"])

    def test_the_drive_is_pinned(self):
        # One drive per host; a second NVMe or a re-enumeration must not mix into the drive panels.
        for p, t in TARGETS:
            for name, sel in re.findall(r"(node_disk_\w+)\{([^}]*)\}", t["expr"]):
                if 'job="proxmox-node"' in sel and name != "node_disk_device_mapper_info" and "dm-" not in sel:
                    with self.subTest(f"{p['title']} {t['refId']} {name}"):
                        self.assertIn('device="nvme0n1"', sel)

    def test_per_guest_queries_never_count_the_pool_layers(self):
        # data_tdata / data-tpool are the SUM of the guest volumes: counting them as consumers doubles the total.
        for p, t in TARGETS:
            for sel in re.findall(r'node_disk_device_mapper_info\{[^}]*\}', t["expr"]):
                with self.subTest(f"{p['title']} {sel}"):
                    self.assertRegex(sel, r'lv_name=~"(\(vm\|base\)-\[0-9\]\+-disk-\[0-9\]\+|root\|swap\|data_tmeta)"')

    def test_guest_names_come_from_the_ailab_exporter_only(self):
        # cloudlab's exporter (job cloud-pve) reuses vmids.
        for p, t in TARGETS:
            for sel in re.findall(r"pve_guest_info\{[^}]*\}", t["expr"]):
                self.assertIn('job="pve-exporter"', sel)


class Units(unittest.TestCase):
    def test_one_unit_per_time_series_panel(self):
        for p in PANELS:
            if p["type"] != "timeseries":
                continue
            with self.subTest(p["title"]):
                self.assertTrue(p["fieldConfig"]["defaults"].get("unit"))
                for o in p["fieldConfig"]["overrides"]:
                    self.assertNotIn("unit", [prop["id"] for prop in o["properties"]])

    def test_iops_and_bytes_are_never_on_one_panel(self):
        for p in PANELS:
            if p["type"] == "timeseries":
                units = {p["fieldConfig"]["defaults"]["unit"]}
                exprs = " ".join(t["expr"] for t in p["targets"])
                with self.subTest(p["title"]):
                    if "completed_total" in exprs and "write_time" not in exprs and "read_time" not in exprs:
                        self.assertEqual(units, {"iops"})
                    if "_bytes_total" in exprs:
                        self.assertNotIn("iops", units)


class Tables(unittest.TestCase):
    def test_columns_named_exist(self):
        for p in PANELS:
            if p["type"] != "table":
                continue
            refs = {t["refId"] for t in p["targets"]}
            org = next(x for x in p["transformations"] if x["id"] == "organize")["options"]
            names = set(org["renameByName"]) | set(org["indexByName"]) | set(org["excludeByName"])
            for n in names:
                m = re.fullmatch(r"Value #(\w+)", n)
                if m:
                    with self.subTest(f"{p['title']} {n}"):
                        self.assertIn(m.group(1), refs)

    def test_tables_merge_instant_queries(self):
        for p in PANELS:
            if p["type"] == "table":
                with self.subTest(p["title"]):
                    self.assertEqual(p["transformations"][0]["id"], "merge")
                    for t in p["targets"]:
                        self.assertTrue(t.get("instant"))
                        self.assertEqual(t.get("format"), "table")


if __name__ == "__main__":
    unittest.main()
