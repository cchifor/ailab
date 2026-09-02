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
     as .gitea/workflows/broker-inventory.yaml invokes it) stays green with the new file present;
  5. the two broker alerts in monitoring/agentforge-rules.yaml keep the shape the spec and the
     review-bot rounds decided (expr/for/severity/`{{ $labels.seat }}`), and the `seat` identity
     they share is the SAME string on both sides: the ServiceMonitor derives it from the routable
     Service's `app.kubernetes.io/name`, the kube-state-metrics rule derives it from the
     Deployment's name via label_replace — so every seat's Deployment name, routable Service name
     and `app.kubernetes.io/name` label must be one and the same value.

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
RULES_FILE = REPO / "kubernetes/apps/infrastructure/monitoring/agentforge-rules.yaml"

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


def _clean_value(raw: str) -> str:
    """Strip a trailing YAML comment then surrounding quotes — matches gbi._field's own
    tokenisation, so this test tracks the same parsing the inventory generator relies on."""
    return re.sub(r"\s+#.*$", "", raw).strip().strip('"').strip("'")


def _labels_from_block(block: str, indent: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(rf"^[ ]{{{indent}}}([A-Za-z0-9_./-]+):[ \t]*(\S.*)$", line)
        if m:
            out[m.group(1)] = _clean_value(m.group(2))
    return out


def _service_labels(doc: str) -> dict[str, str]:
    labels_block = gbi._sub_block(gbi._top_block(doc, "metadata"), "labels", 2)
    return _labels_from_block(labels_block, 4)


def _service_namespace(doc: str) -> str | None:
    return gbi._field(gbi._top_block(doc, "metadata"), "namespace", 2)


def _service_port_names(doc: str) -> set[str]:
    ports_block = gbi._sub_block(gbi._top_block(doc, "spec"), "ports", 2)
    return set(re.findall(r"(?m)^[ ]*-[ ]*name:[ ]*([A-Za-z0-9-]+)", ports_block))


def _servicemonitor_text() -> str:
    assert SERVICEMONITOR.exists(), f"{SERVICEMONITOR} does not exist"
    return SERVICEMONITOR.read_text(encoding="utf-8")


def _servicemonitor_spec_block() -> str:
    return gbi._top_block(_servicemonitor_text(), "spec")


def _servicemonitor_selector_labels() -> dict[str, str]:
    selector_block = gbi._sub_block(_servicemonitor_spec_block(), "selector", 2)
    match_labels_block = gbi._sub_block(selector_block, "matchLabels", 4)
    return _labels_from_block(match_labels_block, 6)


def _servicemonitor_metadata() -> tuple[str | None, dict[str, str]]:
    """(metadata.namespace, metadata.labels) of the ServiceMonitor itself."""
    metadata_block = gbi._top_block(_servicemonitor_text(), "metadata")
    namespace = gbi._field(metadata_block, "namespace", 2)
    labels_block = gbi._sub_block(metadata_block, "labels", 2)
    return namespace, _labels_from_block(labels_block, 4)


def _servicemonitor_namespace_selector_match_names() -> set[str]:
    ns_selector_block = gbi._sub_block(_servicemonitor_spec_block(), "namespaceSelector", 2)
    m = re.search(r"matchNames:[ \t]*\[([^\]]*)\]", ns_selector_block)
    assert m, f"namespaceSelector.matchNames not found (inline-list form) in:\n{ns_selector_block}"
    return {_clean_value(item) for item in m.group(1).split(",") if item.strip()}


def _servicemonitor_endpoint_ports() -> set[str]:
    """The `port:` value of every entry under spec.endpoints — what Prometheus actually resolves
    against each matched Service's named ports, as opposed to a test-hardcoded literal."""
    endpoints_block = gbi._sub_block(_servicemonitor_spec_block(), "endpoints", 2)
    assert endpoints_block, "spec.endpoints not found in servicemonitor.yaml"
    return {
        _clean_value(v)
        for v in re.findall(r"(?m)^[ \t]*-?[ \t]*port:[ \t]*(\S.*)$", endpoints_block)
    }


def _alert_block(name: str) -> str:
    """Text of one `- alert: <name>` entry in agentforge-rules.yaml: its own line plus every
    following line indented STRICTLY deeper than it (expr/for/labels/annotations). Stops at the
    first line back at (or above) the alert line's own indent — a sibling `- alert:`/`- record:`
    item OR that sibling's OWN preceding `#` comment block, both of which sit at the identical
    indent in this file and must NOT be attributed to this alert (a naive "stop at the next
    literal `- alert:` line" regex pulls the next alert's comment block, including any incidental
    mention of this alert's own field values, into this one — verified load-bearing: mutating
    ForgeBrokerTargetDown's `for: 5m` to `for: 5s` must fail this alert's own test, not pass
    because the next alert's preceding comment happens to say "for: 5m" too). Raises StopIteration
    (a failing test) when no such alert exists."""
    lines = RULES_FILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip() == f"- alert: {name}")
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped and (len(lines[i]) - len(stripped)) <= indent:
            end = i
            break
    return "\n".join(lines[start:end])


def _deployments(path: pathlib.Path) -> dict[str, str]:
    """name -> full Deployment doc text, for every `kind: Deployment` document in one broker-*.yaml."""
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for doc in gbi._docs(text):
        if gbi._kind(doc) != "Deployment":
            continue
        name = gbi._name(doc)
        assert name, f"{path}: a Deployment document has no metadata.name"
        out[name] = doc
    return out


def _deployment_namespace(doc: str) -> str | None:
    return gbi._field(gbi._top_block(doc, "metadata"), "namespace", 2)


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
    """The ServiceMonitor's own `spec.endpoints[].port` values (NOT a hardcoded literal) must each
    resolve to a named port on every routable Service — renaming the port on either side must fail
    this test rather than leaving Prometheus with zero targets."""

    def test_endpoint_ports_are_parsed_and_non_empty(self):
        ports = _servicemonitor_endpoint_ports()
        self.assertTrue(ports, "servicemonitor.yaml: spec.endpoints[].port must not be empty")
        self.assertIn(
            "metrics",
            ports,
            f"servicemonitor.yaml: expected a 'metrics' endpoint port, found {sorted(ports)}",
        )

    def test_every_endpoint_port_exists_on_the_routable_service_of_every_seat(self):
        endpoint_ports = _servicemonitor_endpoint_ports()
        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            services = _services(path)
            # the routable (non-headless) Service is the one carrying the deployment's own name
            routable = {n: d for n, d in services.items() if not n.endswith("-headless")}
            self.assertTrue(routable, f"{path.name}: no non-headless Service found")
            for svc_name, doc in routable.items():
                ports = _service_port_names(doc)
                for endpoint_port in endpoint_ports:
                    self.assertIn(
                        endpoint_port,
                        ports,
                        f"{path.name}: Service {svc_name!r} has no port named {endpoint_port!r} "
                        f"(found {sorted(ports)}) — the ServiceMonitor's `port: {endpoint_port}` "
                        "endpoint would resolve to nothing",
                    )


class RoutableServiceCarriesTheSeatNameLabel(unittest.TestCase):
    """`ForgeBrokerTargetDown`'s `{{ $labels.seat }}` and the documented `sum by (seat) (...)`
    triage query depend on the ServiceMonitor's `relabelings` finding `app.kubernetes.io/name` on
    the target's Service (review-bot round, ailab#467, reviewer-claude, important/medium) — the
    selector only matches on `component`/`part-of`, so nothing else guarantees
    `app.kubernetes.io/name` is present. If a seat's routable Service ever lacked it, targets
    would still be scraped but `seat` would silently resolve to empty, breaking the alert's
    identity and every `by (seat)` query the descriptions lean on. The label's VALUE is asserted
    too, not just its presence: it must equal the routable Service's own name, which is what
    `ForgeBrokerSeatReplicasUnavailable` (kube-state-metrics, keyed by Deployment name) resolves
    `seat` to — see BrokerDeploymentIdentityMatchesTheSeatLabel for the Deployment side."""

    def test_every_routable_service_carries_app_kubernetes_io_name(self):
        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            services = _services(path)
            routable = {n: d for n, d in services.items() if not n.endswith("-headless")}
            self.assertTrue(routable, f"{path.name}: no non-headless Service found")
            for svc_name, doc in routable.items():
                labels = _service_labels(doc)
                self.assertTrue(
                    labels.get("app.kubernetes.io/name"),
                    f"{path.name}: Service {svc_name!r} has no (or an empty) "
                    "app.kubernetes.io/name label -- the ServiceMonitor's relabelings derive "
                    "the `seat` label from exactly this, on every af_broker_*/up series and in "
                    "ForgeBrokerTargetDown's {{ $labels.seat }} text",
                )
                self.assertEqual(
                    labels["app.kubernetes.io/name"],
                    svc_name,
                    f"{path.name}: Service {svc_name!r} carries app.kubernetes.io/name="
                    f"{labels['app.kubernetes.io/name']!r} -- the `seat` label the ServiceMonitor "
                    "derives from it must be the routable Service's own name, the value the "
                    "kube-state-metrics rule ForgeBrokerSeatReplicasUnavailable reaches via the "
                    "Deployment name, so both alert families name the same seat the same way",
                )


class BrokerDeploymentIdentityMatchesTheSeatLabel(unittest.TestCase):
    """`ForgeBrokerSeatReplicasUnavailable` (review-bot rounds, ailab#467, reviewer-codex,
    important/high x4) reads kube-state-metrics, whose only identity for a broker seat is the
    `(namespace, deployment)` pair — it never sees the ServiceMonitor's `seat` relabeling. The
    rule therefore label_replace()s `deployment` into `seat`, and that is only the SAME seat the
    ServiceMonitor's `seat` names if, for every seat, Deployment name == routable Service name ==
    the Service's `app.kubernetes.io/name` (RoutableServiceCarriesTheSeatNameLabel covers the last
    equality). The rule's `namespace="agentforge-broker"` matcher must likewise be the namespace
    every broker Deployment and Service actually declares, and every Deployment in that directory
    must be a seat — the rule is namespace-scoped rather than name-enumerated precisely so a fifth
    seat is covered without a rules edit, which is only safe while the namespace holds nothing
    but seats."""

    RULE = "ForgeBrokerSeatReplicasUnavailable"

    def _rule_namespaces(self) -> set[str]:
        block = _alert_block(self.RULE)
        return set(re.findall(r'namespace="([^"]+)"', block))

    def test_rule_pins_exactly_one_namespace_and_it_is_the_broker_namespace(self):
        namespaces = self._rule_namespaces()
        self.assertEqual(
            namespaces,
            {"agentforge-broker"},
            f"{self.RULE}: every kube_deployment_* selector must pin namespace="
            f'"agentforge-broker" and nothing else (found {sorted(namespaces)})',
        )

    def test_every_broker_deployment_is_a_seat_in_the_rules_namespace(self):
        (rule_namespace,) = self._rule_namespaces()
        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            deployments = _deployments(path)
            self.assertEqual(
                len(deployments),
                1,
                f"{path.name}: expected exactly one Deployment (one seat), found "
                f"{sorted(deployments)}",
            )
            (dep_name, dep_doc), = deployments.items()
            self.assertEqual(
                _deployment_namespace(dep_doc),
                rule_namespace,
                f"{path.name}: Deployment {dep_name!r} is in namespace "
                f"{_deployment_namespace(dep_doc)!r}, but {self.RULE} only reads "
                f"kube_deployment_* in {rule_namespace!r} -- this seat would be invisible to it",
            )
            services = _services(path)
            routable = {n: d for n, d in services.items() if not n.endswith("-headless")}
            self.assertEqual(
                set(routable),
                {dep_name},
                f"{path.name}: routable Service(s) {sorted(routable)} must be named exactly like "
                f"the Deployment {dep_name!r} -- {self.RULE} resolves `seat` from the Deployment "
                "name while the ServiceMonitor resolves it from the Service's "
                "app.kubernetes.io/name, and the two must agree for the alerts to name one seat",
            )
            for svc_name, doc in routable.items():
                self.assertEqual(
                    _service_namespace(doc),
                    rule_namespace,
                    f"{path.name}: Service {svc_name!r} namespace must equal the rule's "
                    f"{rule_namespace!r}",
                )


class HeadlessServiceCarriesNoMetricsPort(unittest.TestCase):
    """The ServiceMonitor's selector deliberately also matches each seat's `-headless` twin (no
    narrower selector is maintained to exclude it) — that is only harmless because the headless
    Service exposes no `metrics` port, so Prometheus generates no target for it. Nothing enforced
    that invariant before this test (review-bot round, ailab#467, reviewer-claude, nit/low): if a
    future edit gave the headless Service a `metrics` port (common when a headless/routable pair
    share a port list), Prometheus would scrape TWO targets per pod, doubling every `af_broker_*`
    and `up` series for that seat."""

    def test_headless_services_have_no_metrics_port(self):
        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        for path in broker_files:
            services = _services(path)
            headless = {n: d for n, d in services.items() if n.endswith("-headless")}
            self.assertTrue(headless, f"{path.name}: no *-headless Service found")
            for svc_name, doc in headless.items():
                ports = _service_port_names(doc)
                self.assertNotIn(
                    "metrics",
                    ports,
                    f"{path.name}: headless Service {svc_name!r} now has a 'metrics' port "
                    f"(found {sorted(ports)}) — the ServiceMonitor's selector also matches this "
                    "Service, so Prometheus would generate a second, duplicate target per pod "
                    "(doubling every af_broker_*/up series for this seat) unless the selector is "
                    "narrowed to exclude it",
                )


class NamespaceSelectorCoversEveryServiceNamespace(unittest.TestCase):
    """spec.namespaceSelector.matchNames must include the namespace every selected Service
    actually lives in — otherwise selector.matchLabels agreeing is not enough: Prometheus never
    even looks in that namespace and discovers zero targets regardless."""

    def test_match_names_cover_every_seat_services_namespace(self):
        match_names = _servicemonitor_namespace_selector_match_names()
        self.assertTrue(match_names, "servicemonitor.yaml: namespaceSelector.matchNames is empty")

        broker_files = _broker_files()
        self.assertTrue(broker_files, f"no broker-*.yaml under {BROKER_DIR}")

        service_namespaces: set[str] = set()
        for path in broker_files:
            for svc_name, doc in _services(path).items():
                ns = _service_namespace(doc)
                self.assertTrue(ns, f"{path.name}: Service {svc_name!r} has no metadata.namespace")
                service_namespaces.add(ns)

        for ns in service_namespaces:
            self.assertIn(
                ns,
                match_names,
                f"servicemonitor.yaml: namespaceSelector.matchNames {sorted(match_names)} does "
                f"not include {ns!r}, which every broker Service actually lives in — Prometheus "
                "would never look there.",
            )


class ServiceMonitorMetadataAgreesWithOperatorDefaults(unittest.TestCase):
    """The kube-prometheus-stack operator's default serviceMonitorSelector/ruleSelector key off
    `release: <helm release name>`, and only ServiceMonitors IN the `monitoring` namespace are
    picked up unless serviceMonitorNamespaceSelector is widened. Dropping either silently drops
    every target while every other test in this module stays green."""

    def test_namespace_is_monitoring(self):
        namespace, _ = _servicemonitor_metadata()
        self.assertEqual(
            namespace,
            "monitoring",
            "servicemonitor.yaml: metadata.namespace must be 'monitoring' for the operator's "
            "default serviceMonitorNamespaceSelector to ever look at this object",
        )

    def test_release_label_matches_kube_prometheus_stack(self):
        _, labels = _servicemonitor_metadata()
        self.assertEqual(
            labels.get("release"),
            "kube-prometheus-stack",
            f"servicemonitor.yaml: metadata.labels.release is {labels.get('release')!r}, "
            "expected 'kube-prometheus-stack' — the operator's serviceMonitorSelector keys on "
            "this label; without it Prometheus never adopts this ServiceMonitor at all",
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


class BrokerAlertRulesAreShapedAndOrdered(unittest.TestCase):
    """Regression coverage for both C-P0-07 broker alerts in agentforge-rules.yaml (codex
    cross-review round 1, ailab#467, medium): `promtool check rules` validates PromQL/YAML
    syntax (run in this PR's verification tier) but nothing in this repo asserted that either
    alert's expr/for/severity/seat-label shape actually matches what the spec and the review-bot
    rounds decided, so a silent hand-edit (e.g. `for: 5m` -> `for: 5s`, or `severity: critical` ->
    `warning`) would pass promtool while quietly breaking the alert's documented behaviour.
    Nothing else in this file (or anywhere else in scripts/tests) has ever had a Python test for
    a PrometheusRule alert — reused this module's own stdlib-only, no-PyYAML text-block
    convention (`_alert_block`) rather than introducing a new parsing approach for one file."""

    def test_forgebrokertargetdown_is_shaped_as_the_spec_decided(self):
        block = _alert_block("ForgeBrokerTargetDown")
        self.assertIn('expr: up{job="agentforge-broker"} == 0', block)
        self.assertIn("for: 5m", block)
        self.assertIn("severity: critical", block)
        self.assertIn("{{ $labels.seat }}", block)

    def test_forgebrokertargetdown_remediation_query_surfaces_a_real_zero(self):
        # review-bot round, ailab#467, reviewer-claude, important/medium: `count by (seat)
        # (up == 1)` returns NO row for a dark seat (never a 0), so the runbook text must point
        # the operator at `sum by (seat) (up{job="agentforge-broker"})`, which reads 0 when
        # every discovered replica fails its scrape, and at the kube-state-metrics rule for the
        # case where the seat has no discovered replica at all.
        block = _alert_block("ForgeBrokerTargetDown")
        self.assertIn('sum by (seat) (up{job="agentforge-broker"})', block)
        self.assertIn("ForgeBrokerSeatReplicasUnavailable", block)
        self.assertNotIn("count by (seat)", block)

    def test_forgebrokerseatreplicasunavailable_compares_expected_vs_available_from_ksm(self):
        # review-bot rounds, ailab#467, reviewer-codex, important/high (x4): a replica that
        # vanishes from service discovery produces no `up` series at all, so neither `up == 0`
        # nor a `count by (seat) (up) < 2` proxy can see a fully dark seat. kube-state-metrics
        # reports spec vs available replicas for every Deployment INCLUDING one with 0 available,
        # so `available < spec` yields a row (value = available) for a partially unready seat
        # (1 < 2) AND for a fully dark one (0 < 2), keyed by the Deployment name.
        block = _alert_block("ForgeBrokerSeatReplicasUnavailable")
        self.assertIn('kube_deployment_status_replicas_available{namespace="agentforge-broker"}', block)
        self.assertIn('kube_deployment_spec_replicas{namespace="agentforge-broker"}', block)
        # available on the LEFT of `<`, spec on the right: the alert's value is the available count
        self.assertLess(
            block.index("kube_deployment_status_replicas_available"),
            block.index("kube_deployment_spec_replicas"),
            "available replicas must be the left-hand side of the `<` comparison",
        )
        self.assertRegex(block, r"\)\s*\n\s*<\s*\n")
        self.assertIn("max by (namespace, deployment)", block)
        self.assertIn('"seat", "$1", "deployment", "(.+)"', block)
        self.assertIn("label_replace(", block)
        self.assertIn("for: 5m", block)
        self.assertIn("severity: critical", block)
        self.assertIn("{{ $labels.seat }}", block)
        self.assertIn("{{ $value }}", block)

    def test_the_discovery_count_proxy_rule_is_retired(self):
        # `count by (seat) (up{job="agentforge-broker"}) < 2` used discovery data as a readiness
        # proxy and could not see a seat with zero discovered replicas; it is superseded by the
        # kube-state-metrics comparison above and must not linger as a second, weaker copy.
        text = RULES_FILE.read_text(encoding="utf-8")
        self.assertNotIn("- alert: ForgeBrokerSeatReplicaMissing", text)
        self.assertNotIn('count by (seat) (up{job="agentforge-broker"}) < 2', text)

    def test_targetdown_precedes_seatreplicasunavailable_in_reading_order(self):
        # documented order: "discovered but unscrapeable" (ForgeBrokerTargetDown) before
        # "fewer available replicas than the Deployment asks for" (ForgeBrokerSeatReplicasUnavailable)
        # — see the latter's own preceding comment block, which narrates it as complementary
        text = RULES_FILE.read_text(encoding="utf-8")
        self.assertLess(
            text.index("- alert: ForgeBrokerTargetDown"),
            text.index("- alert: ForgeBrokerSeatReplicasUnavailable"),
            "ForgeBrokerTargetDown must precede ForgeBrokerSeatReplicasUnavailable",
        )

    def test_both_alerts_live_in_the_agentforge_rule_group(self):
        # a `- alert:` entry pasted outside spec.groups[].rules would render but never be picked
        # up by the operator's ruleSelector the way the rest of this file is
        text = RULES_FILE.read_text(encoding="utf-8")
        group_start = text.index("groups:")
        for name in ("ForgeBrokerTargetDown", "ForgeBrokerSeatReplicasUnavailable"):
            self.assertGreater(
                text.index(f"- alert: {name}"),
                group_start,
                f"{name} must appear after `groups:` (i.e. inside a rule group)",
            )


if __name__ == "__main__":
    unittest.main()
