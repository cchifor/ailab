#!/usr/bin/env python3
"""Unit tests for kubernetes/apps/infrastructure/agentforge-broker/servicemonitor.yaml (C-P0-07).

Nothing here talks to a cluster (BRIEFING.md: never run kubectl against ailab). The subject is
purely textual/structural agreement between the new ServiceMonitor and the broker manifest set it
scrapes:

  1. the ServiceMonitor's `selector.matchLabels` actually match the labels every pinned Service in
     `broker-*.yaml` carries (else Prometheus would discover zero targets);
  2. a `metrics` port exists on each seat's routable Service (else the ServiceMonitor's `port:
     metrics` endpoint resolves to nothing);
  3. the ServiceMonitor's own filename is NOT matched by the `broker-*.yaml` glob
     `scripts/gen-broker-inventory.py:278` (`load_seats`) uses to derive the seat inventory — a
     ServiceMonitor caught by that glob would make `parse_seat` raise (it expects exactly one
     Deployment per matched file) rather than silently miscount, but the file is named
     `servicemonitor.yaml` specifically so the question never comes up in production;
  4. `python3 scripts/gen-broker-inventory.py --check` (run against the real, on-disk repo, exactly
     as .gitea/workflows/broker-inventory.yaml invokes it) stays green with the new file present.

Runs against the REAL repo tree (read-only) rather than a sandbox copy: unlike
test_gen_broker_inventory.py this test writes nothing and mutates no module globals, so there is
nothing to isolate. Reuses gen-broker-inventory.py's own private YAML-block helpers (loaded by path,
matching that script's "stdlib-only, no PyYAML" convention — the CI runner installs no dependency
for scripts/tests) rather than re-implementing a second parser that could disagree with the one that
actually derives the inventory.

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import subprocess
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
BROKER_DIR = REPO / "kubernetes/apps/infrastructure/agentforge-broker"
SERVICEMONITOR = BROKER_DIR / "servicemonitor.yaml"

_MOD_PATH = REPO / "scripts" / "gen-broker-inventory.py"
_spec = importlib.util.spec_from_file_location("gen_broker_inventory", _MOD_PATH)
gbi = importlib.util.module_from_spec(_spec)
sys.modules["gen_broker_inventory"] = gbi
_spec.loader.exec_module(gbi)  # must NOT perform any I/O at import time


def _broker_files() -> list[pathlib.Path]:
    """The seat manifests the ServiceMonitor is meant to scrape (mirrors load_seats' own glob)."""
    return sorted(p for p in BROKER_DIR.glob("broker-*.yaml") if p != gbi.INVENTORY)


def _services(path: pathlib.Path) -> dict[str, str]:
    """name -> full Service doc text, for every `kind: Service` document in one broker-*.yaml."""
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for doc in gbi._docs(text):
        if gbi._kind(doc) != "Service":
            continue
        name = gbi._name(doc)
        assert name, f"{path}: a Service document has no metadata.name"
        out[name] = doc
    return out


def _service_labels(doc: str) -> dict[str, str]:
    labels_block = gbi._sub_block(gbi._top_block(doc, "metadata"), "labels", 2)
    out: dict[str, str] = {}
    for line in labels_block.splitlines():
        m = re.match(r"^[ ]{4}([A-Za-z0-9_./-]+):[ \t]*(\S.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _service_port_names(doc: str) -> set[str]:
    ports_block = gbi._sub_block(gbi._top_block(doc, "spec"), "ports", 2)
    return set(re.findall(r"(?m)^[ ]*-[ ]*name:[ ]*([A-Za-z0-9-]+)", ports_block))


def _servicemonitor_selector_labels() -> dict[str, str]:
    assert SERVICEMONITOR.exists(), f"{SERVICEMONITOR} does not exist"
    text = SERVICEMONITOR.read_text(encoding="utf-8")
    spec_block = gbi._top_block(text, "spec")
    selector_block = gbi._sub_block(spec_block, "selector", 2)
    match_labels_block = gbi._sub_block(selector_block, "matchLabels", 4)
    out: dict[str, str] = {}
    for line in match_labels_block.splitlines():
        m = re.match(r"^[ ]{6}([A-Za-z0-9_./-]+):[ \t]*(\S.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


class ServiceMonitorFileExists(unittest.TestCase):
    def test_file_is_present(self):
        self.assertTrue(
            SERVICEMONITOR.is_file(),
            f"{SERVICEMONITOR} must exist (C-P0-07: ServiceMonitor for the per-seat brokers)",
        )


class SelectorMatchesEveryPinnedService(unittest.TestCase):
    """selector.matchLabels must actually select every Service the broker manifests declare."""

    def test_selector_labels_present_on_every_service_in_every_seat(self):
        selector = _servicemonitor_selector_labels()
        self.assertTrue(selector, "ServiceMonitor selector.matchLabels must not be empty")

        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            services = _services(path)
            self.assertTrue(services, f"{path}: no Service documents found")
            for svc_name, doc in services.items():
                labels = _service_labels(doc)
                for key, value in selector.items():
                    self.assertEqual(
                        labels.get(key),
                        value,
                        f"{path.name}: Service {svc_name!r} label {key!r} is "
                        f"{labels.get(key)!r}, expected {value!r} (the ServiceMonitor selector "
                        "would not match this Service otherwise)",
                    )


class MetricsPortExistsOnEverySeat(unittest.TestCase):
    def test_metrics_port_on_the_routable_service(self):
        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            services = _services(path)
            # the routable (non-headless) Service is the one carrying the deployment's own name
            routable = {n: d for n, d in services.items() if not n.endswith("-headless")}
            self.assertTrue(routable, f"{path.name}: no non-headless Service found")
            for svc_name, doc in routable.items():
                ports = _service_port_names(doc)
                self.assertIn(
                    "metrics",
                    ports,
                    f"{path.name}: Service {svc_name!r} has no port named 'metrics' "
                    f"(found {sorted(ports)}) — the ServiceMonitor's `port: metrics` endpoint "
                    "would resolve to nothing",
                )


class NotMatchedByTheInventoryGlob(unittest.TestCase):
    """scripts/gen-broker-inventory.py:278 globs `broker-*.yaml`; the ServiceMonitor must dodge it."""

    def test_filename_does_not_match_broker_star_glob(self):
        self.assertTrue(SERVICEMONITOR.is_file())
        matched = {p.name for p in BROKER_DIR.glob("broker-*.yaml")}
        self.assertNotIn(
            SERVICEMONITOR.name,
            matched,
            f"{SERVICEMONITOR.name} must NOT match broker-*.yaml — "
            "scripts/gen-broker-inventory.py's load_seats() would treat it as a seat manifest "
            "and parse_seat() would raise (it requires exactly one Deployment per matched file)",
        )

    def test_load_seats_ignores_it_and_still_finds_every_real_seat(self):
        seats = gbi.load_seats()
        sources = {seat.source for seat in seats}
        self.assertNotIn(
            SERVICEMONITOR.relative_to(REPO).as_posix(),
            sources,
            "load_seats() must not have derived a seat from servicemonitor.yaml",
        )
        # every broker-*.yaml on disk (other than the generated inventory) is still a real seat
        expected_sources = {p.relative_to(REPO).as_posix() for p in _broker_files()}
        self.assertEqual(sources, expected_sources)


class GenBrokerInventoryCheckStaysGreen(unittest.TestCase):
    """The exact invocation .gitea/workflows/broker-inventory.yaml runs on every push/PR."""

    def test_check_subcommand_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, str(_MOD_PATH), "--check"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            "gen-broker-inventory.py --check must stay green with servicemonitor.yaml present "
            f"(rc={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
