#!/usr/bin/env python3
"""Unit tests for the provisioner scrape wiring (ailab#293).

SUBJECT: the three-way contract between
  * kubernetes/apps/infrastructure/security/openbao/provisioner-deploy.yaml — the named `metrics`
    containerPort and the AF_PROVISIONER_METRICS_PORT that must agree with it,
  * kubernetes/apps/infrastructure/security/openbao/provisioner-podmonitor.yaml — the PodMonitor
    whose selector must match the Deployment's POD TEMPLATE labels (not the Deployment's own) and
    whose endpoint must name a port the container actually declares,
  * kubernetes/apps/infrastructure/monitoring/agentforge-provisioner-rules.yaml — every rule of
    which reads `job="agentforge-provisioner"`, the value the PodMonitor relabels to.

WHY THESE ASSERTIONS AND NOT OTHERS. Each one pins a way this wiring has ALREADY failed somewhere in
this estate rather than a way it might:
  * ailab#284: a monitor whose selector did not match the pods it named. A PodMonitor with a
    plausible-looking selector discovers zero targets and reports nothing — it looks identical to a
    healthy estate. So the selector is checked against the pod template it must match, both ways.
  * ailab#293's own option B caveat: binding a port by NUMBER breaks silently if the port moves.
    The named form is used here, which means the NAME must exist on the container — a PodMonitor
    naming a port no container declares also discovers zero targets, silently.
  * agentforge-rules.yaml's header: five rules pinned to a job label with zero targets. So the job
    label the PodMonitor writes and the job label every rule selects on are asserted equal.
  * agentforge-workers/worker-podmonitor.yaml's own note: the Prometheus adopts a monitor by the
    `release: kube-prometheus-stack` label and looks for it in ns `monitoring`. A monitor missing
    either is applied by Flux, reconciles clean, and is never loaded.
  * The registration half: a rules file Flux never applies is the same defect as a rule that cannot
    fire (this repo shipped exactly that once), so kustomization membership is asserted too.

Nothing here talks to a cluster (BRIEFING.md), and nothing here re-checks what promtool already
proves — expression validity and firing behaviour live in
monitoring/agentforge-provisioner-rules.test.yaml, run by scripts/rules-lint.sh.

STDLIB ONLY, and the parsing helpers are BORROWED from scripts/gen-broker-inventory.py rather than
re-implemented: the CI runner installs no PyYAML (see .gitea/workflows/broker-inventory.yaml), and a
second hand-rolled parser could disagree with the one that actually derives the broker inventory.
Mirrors test_broker_servicemonitor.py, the sibling test for the broker's ServiceMonitor.

    python -m unittest discover -s scripts/tests -p "test_*.py"
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
OPENBAO_DIR = REPO / "kubernetes/apps/infrastructure/security/openbao"
DEPLOY = OPENBAO_DIR / "provisioner-deploy.yaml"
PODMONITOR = OPENBAO_DIR / "provisioner-podmonitor.yaml"
OPENBAO_KUSTOMIZATION = OPENBAO_DIR / "kustomization.yaml"
MONITORING_DIR = REPO / "kubernetes/apps/infrastructure/monitoring"
RULES = MONITORING_DIR / "agentforge-provisioner-rules.yaml"
RULES_TEST = MONITORING_DIR / "agentforge-provisioner-rules.test.yaml"
MONITORING_KUSTOMIZATION = MONITORING_DIR / "kustomization.yaml"

#: The job label the PodMonitor relabels to and every rule selects on. One value, two files.
JOB = "agentforge-provisioner"
#: The kube-prometheus-stack Prometheus adopts monitors/rules carrying this label, in ns monitoring.
RELEASE_LABEL = ("release", "kube-prometheus-stack")

_MOD_PATH = REPO / "scripts" / "gen-broker-inventory.py"
_spec = importlib.util.spec_from_file_location("gen_broker_inventory", _MOD_PATH)
gbi = importlib.util.module_from_spec(_spec)
sys.modules["gen_broker_inventory"] = gbi
_spec.loader.exec_module(gbi)  # must NOT perform any I/O at import time


def _doc_of_kind(path: pathlib.Path, kind: str) -> str:
    """The single YAML document of `kind` in `path`, failing closed on 0 or >1."""
    text = path.read_text(encoding="utf-8")
    docs = [d for d in gbi._docs(text) if gbi._kind(d) == kind]
    assert len(docs) == 1, f"{path.name}: expected exactly 1 {kind}, found {len(docs)}"
    return docs[0]


def _labels_at(block: str, indent: int) -> dict[str, str]:
    """Every `key: value` at exactly `indent` spaces in `block` (a labels/matchLabels mapping)."""
    out: dict[str, str] = {}
    for line in block.splitlines():
        m = re.match(rf"^[ ]{{{indent}}}([A-Za-z0-9_./-]+):[ \t]*(\S.*)$", line)
        if m:
            out[m.group(1)] = re.sub(r"\s+#.*$", "", m.group(2)).strip().strip('"').strip("'")
    return out


def _pod_template_labels() -> dict[str, str]:
    """spec.template.metadata.labels of the provisioner Deployment — what a PodMonitor selects on.

    Deliberately NOT metadata.labels: a PodMonitor matches POD labels, and the two mappings are
    separate YAML blocks that can drift apart without anything else noticing.
    """
    spec = gbi._top_block(_doc_of_kind(DEPLOY, "Deployment"), "spec")
    template = gbi._sub_block(spec, "template", 2)
    metadata = gbi._sub_block(template, "metadata", 4)
    return _labels_at(gbi._sub_block(metadata, "labels", 6), 8)


def _container_block() -> str:
    """The single container entry of the provisioner Deployment's pod template."""
    spec = gbi._top_block(_doc_of_kind(DEPLOY, "Deployment"), "spec")
    template = gbi._sub_block(spec, "template", 2)
    pod_spec = gbi._sub_block(template, "spec", 4)
    containers = gbi._sub_block(pod_spec, "containers", 6)
    names = re.findall(r"(?m)^[ ]{8}-[ ]name:[ \t]*(\S+)[ \t]*$", containers)
    assert names == ["provisioner"], f"expected one container named provisioner, found {names}"
    return containers


def _container_ports() -> dict[str, int]:
    """name -> containerPort for every port the provisioner container declares (inline-map form)."""
    out: dict[str, int] = {}
    for line in _container_block().splitlines():
        m = re.match(
            r"^\s*-\s*\{\s*name:\s*([A-Za-z0-9-]+),\s*containerPort:\s*(\d+)", line
        )
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _container_env() -> dict[str, str]:
    """name -> value for every inline `- { name: X, value: "Y" }` env entry (valueFrom skipped)."""
    out: dict[str, str] = {}
    for line in _container_block().splitlines():
        m = re.match(r"^\s*-\s*\{\s*name:\s*([A-Za-z0-9_]+),\s*value:\s*(\S.*?)\s*\}\s*$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def _podmonitor_spec() -> str:
    return gbi._top_block(_doc_of_kind(PODMONITOR, "PodMonitor"), "spec")


def _exprs() -> list[str]:
    """Every rule `expr:` in the rules manifest, plain and `|-` block forms alike.

    Line-based rather than one multiline regex: the block form's body is whatever is indented
    further than the `expr:` key, which a regex has to guess at and which this reads directly.
    """
    lines = RULES.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s+)expr:[ \t]*(\S.*)?$", lines[i])
        i += 1
        if not m:
            continue
        indent, inline = len(m.group(1)), (m.group(2) or "")
        if inline and inline.strip() not in ("|-", "|", ">-", ">"):
            out.append(inline)
            continue
        body: list[str] = []
        while i < len(lines):
            line = lines[i]
            if line.strip() and (len(line) - len(line.lstrip(" "))) <= indent:
                break
            body.append(line)
            i += 1
        out.append("\n".join(body))
    return out


class ProvisionerPodMonitorTest(unittest.TestCase):
    """The PodMonitor selects the provisioner pod and binds a port the container declares."""

    def test_selector_matches_the_pod_template_labels(self) -> None:
        """ailab#284's failure: a selector that matches no pod discovers zero targets, silently."""
        selector = _labels_at(gbi._sub_block(_podmonitor_spec(), "selector", 2), 6)
        self.assertTrue(selector, "the PodMonitor declares no selector.matchLabels")
        pod_labels = _pod_template_labels()
        for key, value in selector.items():
            self.assertIn(key, pod_labels, f"selector key {key!r} is not on the pod template")
            self.assertEqual(
                value,
                pod_labels[key],
                f"selector {key}={value!r} does not match the pod template's {pod_labels[key]!r}",
            )

    def test_namespace_selector_names_the_deployments_namespace(self) -> None:
        """The object lives in `monitoring`; its target does not, so the selector must be explicit."""
        ns_block = gbi._sub_block(_podmonitor_spec(), "namespaceSelector", 2)
        m = re.search(r"matchNames:[ \t]*\[([^\]]*)\]", ns_block)
        self.assertIsNotNone(m, f"namespaceSelector.matchNames not found in:\n{ns_block}")
        names = {item.strip().strip('"').strip("'") for item in m.group(1).split(",") if item.strip()}
        deploy_ns = gbi._field(
            gbi._top_block(_doc_of_kind(DEPLOY, "Deployment"), "metadata"), "namespace", 2
        )
        self.assertEqual(deploy_ns, "openbao")
        self.assertEqual(names, {deploy_ns})

    def test_endpoint_binds_a_named_port_the_container_declares(self) -> None:
        """A named port that no container declares discovers zero targets, exactly like a bad
        selector — and the named form is why this is checkable at all (ailab#293 option B)."""
        endpoints = gbi._sub_block(_podmonitor_spec(), "podMetricsEndpoints", 2)
        ports = {
            re.sub(r"\s+#.*$", "", v).strip().strip('"').strip("'")
            for v in re.findall(r"(?m)^[ \t]*-?[ \t]*port:[ \t]*(\S.*)$", endpoints)
        }
        self.assertEqual(ports, {"metrics"}, "expected exactly one endpoint on port `metrics`")
        self.assertIn(
            "metrics",
            _container_ports(),
            "the provisioner container declares no `metrics` containerPort for the endpoint to bind",
        )
        self.assertNotIn(
            "portNumber",
            _podmonitor_spec(),
            "bind by NAME, not portNumber: a numeric bind breaks silently when the port moves",
        )

    def test_container_port_and_env_state_the_same_port(self) -> None:
        """The engine reads AF_PROVISIONER_METRICS_PORT (default 9465) and binds start_http_server
        to it. If the env and the containerPort disagree, the PodMonitor binds a port nothing
        listens on and every target is permanently down — a WORSE state than the absence #293
        describes, because it looks like a broken provisioner rather than a missing monitor."""
        self.assertEqual(_container_ports().get("metrics"), 9465)
        self.assertEqual(_container_env().get("AF_PROVISIONER_METRICS_PORT"), "9465")

    def test_podmonitor_is_adoptable_by_kube_prometheus(self) -> None:
        """ns monitoring + `release: kube-prometheus-stack`, or the Prometheus never loads it."""
        metadata = gbi._top_block(_doc_of_kind(PODMONITOR, "PodMonitor"), "metadata")
        self.assertEqual(gbi._field(metadata, "namespace", 2), "monitoring")
        labels = _labels_at(gbi._sub_block(metadata, "labels", 2), 4)
        self.assertEqual(labels.get(RELEASE_LABEL[0]), RELEASE_LABEL[1])

    def test_podmonitor_is_applied_by_flux(self) -> None:
        """An unlisted manifest is never applied — the same class of defect as an inert rule."""
        listed = OPENBAO_KUSTOMIZATION.read_text(encoding="utf-8")
        self.assertRegex(listed, rf"(?m)^\s*-\s*{re.escape(PODMONITOR.name)}\b")


class ProvisionerRulesWiringTest(unittest.TestCase):
    """The rules read the job label the PodMonitor writes, and Flux applies them."""

    def test_relabeled_job_matches_every_rule_selector(self) -> None:
        """agentforge-rules.yaml's header records five rules pinned to a job with zero targets. The
        job written by the monitor and the job selected by the rules are ONE value, asserted equal
        rather than two copies that happen to agree today."""
        endpoints = gbi._sub_block(_podmonitor_spec(), "podMetricsEndpoints", 2)
        replacements = re.findall(r"(?m)^[ \t]*-?[ \t]*replacement:[ \t]*(\S+)[ \t]*$", endpoints)
        self.assertEqual(replacements, [JOB], "expected exactly one job relabeling")
        rules_text = RULES.read_text(encoding="utf-8")
        selectors = set(re.findall(r'job="([^"]+)"', rules_text))
        self.assertTrue(selectors, "no rule in the file selects on a job label")
        self.assertEqual(selectors, {JOB})

    def test_every_alert_reads_a_series_this_wiring_provides(self) -> None:
        """Each rule's expr must read `up`, kube-state-metrics, or an af_provisioner_* family the
        provisioner actually exports — the three families ProvisionerMetrics registers plus the two
        independent sources. A rule reading anything else is one no scrape here can ever feed."""
        known = (
            "af_provisioner_ops_total",
            "af_provisioner_alerts_total",
            "af_provisioner_seats",
            "kube_pod_container_status_ready",
            "up{",
        )
        exprs = _exprs()
        self.assertEqual(len(exprs), 7, f"expected 7 exprs, parsed {len(exprs)}")
        for expr in exprs:
            self.assertTrue(
                any(name in expr for name in known),
                f"expr reads no series this wiring provides: {expr.strip()!r}",
            )

    def test_no_counter_rule_uses_the_rate_over_the_hold_shape(self) -> None:
        """A `rate(<counter>[W]) > 0` rule held `for: W` does NOT mean "the condition is standing".

        rate() stays > 0 for the WHOLE window after the LAST increment, so the `for:` hold keeps
        accumulating over a condition that already cleared: measured on this file's own first
        revision, samples 1,2,3,4,4,4,... (increments stopped at minute 3) still paged at minute 11
        under a description asserting the failure had been happening "continuously for >10m". The
        firing behaviour is proven by the promtool fixture's "a burst that has already recovered
        must NOT page"; this pins the SOURCE SHAPE so the fixture cannot be satisfied by re-tuning a
        window back into that class. Every counter rule reads a SHORT trailing `increase()` window
        instead, which reflects a current condition.
        """
        text = RULES.read_text(encoding="utf-8")
        self.assertNotIn(
            "rate(af_provisioner_",
            text,
            "a counter rule is back on the rate()-over-the-hold shape (see this test's docstring)",
        )
        counter_exprs = [e for e in _exprs() if "af_provisioner_alerts_total{reason" in e]
        self.assertEqual(len(counter_exprs), 3, "expected the three reason-family counter rules")
        for expr in counter_exprs:
            self.assertRegex(
                expr,
                r"increase\(af_provisioner_alerts_total\{reason[^}]*\}\[2m\]\)",
                f"counter rule does not read a short trailing increase window: {expr.strip()!r}",
            )

    def test_a_stall_detector_reads_progress_and_not_just_scraping(self) -> None:
        """`up`/kube-state-metrics cannot see a provisioner that is up and reconciling NOTHING.

        prometheus_client serves /metrics from a thread separate from the reconcile loop, so a
        wedged or wholly-failing loop keeps `up` at 1 and the pod Ready while both counters freeze —
        and a whole-pass failure increments NEITHER counter (it only logs). An earlier revision of
        the rules header claimed TargetDown and Unscraped covered that; they cannot. This asserts
        the group still carries a rule that joins a live target against ABSENT PROGRESS in both
        counter families, with the anti-join form that also covers a counter which never appeared at
        all. Its firing behaviour is the fixture's "the pre-fix blind spot" test.
        """
        exprs = [e for e in _exprs() if "unless" in e and "increase(af_provisioner_ops_total" in e]
        self.assertEqual(len(exprs), 1, "expected exactly one stall detector")
        expr = exprs[0]
        self.assertIn(f'up{{job="{JOB}"}} == 1', expr, "the stall rule must require a LIVE target")
        # START-UP BOUND. `increase(X[10m])` is satisfied by two samples, not by ten minutes of
        # them, so without an arm requiring the window to have been OBSERVED the rule is true from
        # a replacement pod's first scrape and pages at 5m during initialisation (measured against
        # the unbounded form). `up offset 10m == 1` on the same (namespace, pod) is that arm; the
        # promtool fixture's start-up test is the behavioural half.
        self.assertIn(
            f'up{{job="{JOB}"}} offset 10m == 1',
            expr,
            "the stall rule must require the [10m] window to have been observed for this pod",
        )
        self.assertIn("increase(af_provisioner_ops_total[10m])", expr)
        self.assertIn("increase(af_provisioner_alerts_total[10m])", expr)
        # One `sum by` over the UNION of both families, never `sum(A) or sum(B)`: `or` drops its
        # right operand wherever the left has a sample, so a frozen ops_total beside an advancing
        # alerts_total would read 0 and page falsely.
        self.assertEqual(expr.count("sum by (namespace, pod)"), 1)
        self.assertIn("ForgeProvisionerNotProgressing", RULES.read_text(encoding="utf-8"))

    def test_seat_and_brokerseat_matchers_are_disjoint(self) -> None:
        """PromQL matchers are fully anchored, so `seat-.*` must not be written in a form that also
        catches `brokerseat-*`. The firing behaviour is proven by the promtool fixture; this asserts
        the source text never grows a leading wildcard that would silently merge the two families."""
        text = RULES.read_text(encoding="utf-8")
        self.assertIn('reason=~"seat-.*"', text)
        self.assertIn('reason=~"brokerseat-.*"', text)
        self.assertNotIn('reason=~".*seat-', text)

    def test_rules_are_applied_by_flux_and_the_fixture_is_not(self) -> None:
        """The rules file must be a listed resource; the promtool fixture must NOT be (it is not a
        Kubernetes object and kustomize build would fail on it)."""
        listed = MONITORING_KUSTOMIZATION.read_text(encoding="utf-8")
        self.assertRegex(listed, rf"(?m)^\s*-\s*{re.escape(RULES.name)}\b")
        self.assertNotRegex(listed, rf"(?m)^\s*-\s*{re.escape(RULES_TEST.name)}\b")

    def test_fixture_names_the_extracted_spec(self) -> None:
        """rules-lint.sh hands promtool the extracted `spec:` under the MANIFEST's basename, and
        scripts/promtest-refs.py fails closed on a fixture whose rule_files resolve to nothing."""
        self.assertRegex(
            RULES_TEST.read_text(encoding="utf-8"),
            rf"(?m)^\s*-\s*{re.escape(RULES.name)}\s*$",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
