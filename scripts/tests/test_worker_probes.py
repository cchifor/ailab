#!/usr/bin/env python3
"""Unit tests for PR C-P0-06: startup/liveness/readiness probes + the downward-API
AF_WORKER_INSTANCE env on the two operator-managed agentforge-workers Deployments.

Subject: kubernetes/apps/infrastructure/agentforge-workers/{worker,dispatcher}-deployment.yaml.

Two live hazards this closes (see specs/C-P0-06.json, specs/WS-C-context.json
G3_performant_stable_deterministic_delivery):

  1. AF_WORKER_NAME is a FIXED literal shared by every KEDA replica of the tenant-zero worker
     (the claim-lock owner — must stay fixed) — but nothing hands a replica ITS OWN identity, so
     WorkerInstance (agentforge Settings.worker_instance / AF_WORKER_INSTANCE) falls back to that
     same shared literal on every pod. A hand-typed literal `value:` here would be worse than
     nothing: identical across replicas, silently wrong the moment there are 2+. The fix is the
     downward API (`valueFrom.fieldRef.fieldPath: metadata.name`), resolved per-pod by the
     kubelet at start — see agentforge-platform renderer.py's `_instance_env` for the upstream
     precedent this mirrors.
  2. Neither Deployment carries a startup/liveness/readiness probe, so kubelet cannot see a
     wedged worker or dispatcher process, and the Services in front of them
     (worker-webhook-service.yaml, dispatcher-service.yaml) never learn a pod stopped answering.

Probe shape mirrors agentforge-platform renderer.py's WORKER_* constants (403-411) byte-for-byte
for periodSeconds/timeoutSeconds/failureThreshold; the worker's API port (Settings.port=8700,
agentforge infra/settings.py) is the container port named `webhook` here (this repo's naming,
not the platform renderer's `http`), and the dispatcher's webhook_port=8710 is likewise named
`webhook`.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v

Every test loads the REAL manifest via PyYAML — no kustomize build, no cluster, no fixtures — so
this cannot drift from what Flux would actually apply for these two (both ARE listed in
kustomization.yaml; see that file's "BOTH Deployments ARE listed and live" note).
"""
from __future__ import annotations

import pathlib
import unittest

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKERS_DIR = REPO / "kubernetes" / "apps" / "infrastructure" / "agentforge-workers"
WORKER_MANIFEST = WORKERS_DIR / "worker-deployment.yaml"
DISPATCHER_MANIFEST = WORKERS_DIR / "dispatcher-deployment.yaml"

# The renderer.py WORKER_* constants this PR mirrors (agentforge-platform
# adapters/gitops/renderer.py:403-411).
STARTUP_PERIOD_S = 5
LIVENESS_PERIOD_S = 30
READINESS_PERIOD_S = 10
PROBE_TIMEOUT_S = 3
STARTUP_FAILURE_THRESHOLD = 24
LIVENESS_FAILURE_THRESHOLD = 10
READINESS_FAILURE_THRESHOLD = 3

HEALTHZ = "/healthz"
READYZ = "/readyz"


def load_deployment(path: pathlib.Path) -> dict:
    with open(path) as f:
        doc = yaml.safe_load(f)
    assert doc["kind"] == "Deployment", f"{path}: expected a single Deployment document"
    return doc


def first_container(deployment: dict) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1, "these Deployments are single-container by design"
    return containers[0]


def container_port_names(container: dict) -> set[str]:
    return {p["name"] for p in container.get("ports", [])}


def env_entry(container: dict, name: str) -> dict:
    for e in container.get("env", []):
        if e.get("name") == name:
            return e
    raise AssertionError(f"no env entry named {name!r} on container {container.get('name')!r}")


class AfWorkerInstanceDownwardApi(unittest.TestCase):
    """AF_WORKER_INSTANCE must be the pod's OWN name via the downward API on BOTH Deployments —
    never a literal `value:`, which would be identical (and silently wrong) across every KEDA
    replica."""

    def _assert_downward_api_instance(self, deployment: dict) -> None:
        container = first_container(deployment)
        entry = env_entry(container, "AF_WORKER_INSTANCE")
        self.assertNotIn(
            "value",
            entry,
            "AF_WORKER_INSTANCE must not carry a literal value — a literal is IDENTICAL across "
            "every KEDA replica and is the bug this PR fixes, not a weaker version of the fix",
        )
        self.assertEqual(
            entry.get("valueFrom", {}).get("fieldRef", {}).get("fieldPath"),
            "metadata.name",
            "AF_WORKER_INSTANCE must resolve via valueFrom.fieldRef.fieldPath: metadata.name "
            "(kubelet-resolved per pod at start)",
        )

    def test_worker_deployment_carries_downward_api_instance(self) -> None:
        self._assert_downward_api_instance(load_deployment(WORKER_MANIFEST))

    def test_dispatcher_deployment_carries_downward_api_instance(self) -> None:
        self._assert_downward_api_instance(load_deployment(DISPATCHER_MANIFEST))


class WorkerProbes(unittest.TestCase):
    """The worker (af-orch-playground-planner) carries startup+liveness on /healthz and
    readiness on /readyz, all three on the `webhook` containerPort (8700 == Settings.port)."""

    def setUp(self) -> None:
        self.deployment = load_deployment(WORKER_MANIFEST)
        self.container = first_container(self.deployment)
        self.port_names = container_port_names(self.container)

    def test_webhook_port_exists(self) -> None:
        self.assertIn("webhook", self.port_names)

    def test_startup_probe(self) -> None:
        probe = self.container.get("startupProbe")
        self.assertIsNotNone(probe, "worker must carry a startupProbe")
        self.assertEqual(probe["httpGet"]["path"], HEALTHZ)
        self.assertEqual(probe["httpGet"]["port"], "webhook")
        self.assertIn(probe["httpGet"]["port"], self.port_names)
        self.assertEqual(probe["periodSeconds"], STARTUP_PERIOD_S)
        self.assertEqual(probe["timeoutSeconds"], PROBE_TIMEOUT_S)
        self.assertEqual(probe["failureThreshold"], STARTUP_FAILURE_THRESHOLD)

    def test_liveness_probe(self) -> None:
        probe = self.container.get("livenessProbe")
        self.assertIsNotNone(probe, "worker must carry a livenessProbe")
        self.assertEqual(probe["httpGet"]["path"], HEALTHZ)
        self.assertEqual(probe["httpGet"]["port"], "webhook")
        self.assertIn(probe["httpGet"]["port"], self.port_names)
        self.assertEqual(probe["periodSeconds"], LIVENESS_PERIOD_S)
        self.assertEqual(probe["timeoutSeconds"], PROBE_TIMEOUT_S)
        self.assertEqual(probe["failureThreshold"], LIVENESS_FAILURE_THRESHOLD)

    def test_readiness_probe(self) -> None:
        probe = self.container.get("readinessProbe")
        self.assertIsNotNone(probe, "worker must carry a readinessProbe")
        self.assertEqual(probe["httpGet"]["path"], READYZ)
        self.assertEqual(probe["httpGet"]["port"], "webhook")
        self.assertIn(probe["httpGet"]["port"], self.port_names)
        self.assertEqual(probe["periodSeconds"], READINESS_PERIOD_S)
        self.assertEqual(probe["timeoutSeconds"], PROBE_TIMEOUT_S)
        self.assertEqual(probe["failureThreshold"], READINESS_FAILURE_THRESHOLD)


class DispatcherProbes(unittest.TestCase):
    """The dispatcher carries liveness+readiness (no startupProbe — the spec names only these
    two for the dispatcher) on the `webhook` containerPort (8710 == Settings.webhook_port).

    Liveness stays bound to /healthz rather than /readyz (review round 1, C-P0-06): the
    dispatch loop's only per-pass work (main.py::_dispatch_once) is a sequence of
    `await forge.list_issues(...)` calls, each riding a GiteaClient timeout-bounded httpx
    client (adapters/gitea/client.py DEFAULT_TIMEOUT_S=30s + bounded retries) with no lock
    and no other await — so a pass always either succeeds or raises within a bounded time,
    and _dispatch_loop's except-arm (main.py:1406-1420) records the failure before its
    backoff sleep. A "wedged loop, healthy /healthz" therefore requires a blocking code path
    with no timeout and no exception, and none exists in _dispatch_once — see the manifest
    comment above dispatcher-deployment.yaml's livenessProbe for the full derivation. Binding
    liveness to /readyz instead would crash-loop the single dispatcher replica through every
    ordinary bounded forge outage (readyz stays 503 for the outage's full duration by design),
    which is the CrashLoopBackOff dispatch_api.py's own /healthz docstring warns against."""

    def setUp(self) -> None:
        self.deployment = load_deployment(DISPATCHER_MANIFEST)
        self.container = first_container(self.deployment)
        self.port_names = container_port_names(self.container)

    def test_webhook_port_exists(self) -> None:
        self.assertIn("webhook", self.port_names)

    def test_liveness_probe(self) -> None:
        probe = self.container.get("livenessProbe")
        self.assertIsNotNone(probe, "dispatcher must carry a livenessProbe")
        self.assertEqual(probe["httpGet"]["path"], HEALTHZ)
        self.assertEqual(probe["httpGet"]["port"], "webhook")
        self.assertIn(probe["httpGet"]["port"], self.port_names)
        self.assertEqual(probe["periodSeconds"], LIVENESS_PERIOD_S)
        self.assertEqual(probe["timeoutSeconds"], PROBE_TIMEOUT_S)
        self.assertEqual(probe["failureThreshold"], LIVENESS_FAILURE_THRESHOLD)

    def test_readiness_probe(self) -> None:
        probe = self.container.get("readinessProbe")
        self.assertIsNotNone(probe, "dispatcher must carry a readinessProbe")
        self.assertEqual(probe["httpGet"]["path"], READYZ)
        self.assertEqual(probe["httpGet"]["port"], "webhook")
        self.assertIn(probe["httpGet"]["port"], self.port_names)
        self.assertEqual(probe["periodSeconds"], READINESS_PERIOD_S)
        self.assertEqual(probe["timeoutSeconds"], PROBE_TIMEOUT_S)
        self.assertEqual(probe["failureThreshold"], READINESS_FAILURE_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
