#!/usr/bin/env python3
"""Source gates for the Relay researcher internal TLS terminators (kubernetes/apps/relay-internal-tls,
owner decision G3, relay#277 comment 122379).

Workers resolve relay.chifor.me and router.chifor.me through measured hostAliases to the two pinned
Services here. These tests pin that contract: the staged inclusion, the addresses (and their network
plan reservation), one ailab-ca certificate per public name, Pods that only managed workers of the
researcher slot can reach, the exact upstream each terminator proxies to (checked against the live
relay and llm-router manifests), the client-address overwrite in both Caddyfiles, and the additive
upstream policies with the comments that guard them. The rendered tree is
scripts/tests/test-relay-internal-tls-render.sh; Caddy's runtime behaviour is
scripts/tests/relay_internal_tls_runtime.py.
"""
from __future__ import annotations

import ipaddress
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "kubernetes/apps/relay-internal-tls"
CLUSTER = ROOT / "kubernetes/apps/clusters/ai"
NS = "relay-internal-tls"
SLOT = "relay-researcher-1"
MANAGED = {"relay.infrastructure/managed": "true"}
# name -> (public name, pinned ClusterIP, Caddyfile, upstream namespace, upstream Pod labels, port, Service)
ENDPOINTS = {
    "relay-hub-tls": ("relay.chifor.me", "10.96.0.40", "hub.Caddyfile", "relay", {"app": "relay"}, 8788,
                      "http://relay.relay.svc.cluster.local:80"),
    "relay-router-tls": ("router.chifor.me", "10.96.0.41", "router.Caddyfile", "llm-router",
                         {"app.kubernetes.io/name": "llm-router-edge"}, 8080,
                         "http://llm-router-edge.llm-router.svc.cluster.local:80"),
}
STRIPPED = ["-Forwarded", "-X-Forwarded-*", "-X-Real-Ip", "-True-Client-Ip", "-Cdn-Loop", "-Remote-*", "-Cf-*"]
HEADER_UP = [("CF-Connecting-IP", "{remote_host}"), ("X-Real-IP", "{remote_host}"),
             ("X-Forwarded-For", "{remote_host}"), ("X-Forwarded-Proto", "https"), ("X-Forwarded-Host", "{host}")]


def docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def objects() -> dict:
    return {(d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
            for f in sorted(TREE.glob("*.yaml")) if f.name != "kustomization.yaml" for d in docs(f)}


def flux_manifest() -> Path:
    """The inclusion step `git mv`s the file from flux/ (staged) to clusters/ai (active): exactly one copy
    exists at any time, and it keeps the reviewed shape in either place."""
    copies = [p for p in (TREE / "flux/relay-internal-tls.yaml", CLUSTER / "relay-internal-tls.yaml") if p.is_file()]
    assert len(copies) == 1, f"expected exactly one of flux/ or clusters/ai, found {copies}"
    return copies[0]


def selects(selector: dict, labels: dict) -> bool:
    assert set(selector) <= {"matchLabels"}, f"matchLabels-only selectors expected: {selector}"
    return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())


class Inclusion(unittest.TestCase):
    def test_staged_flux_kustomization(self):
        doc = docs(flux_manifest())[0]
        self.assertEqual(doc["kind"], "Kustomization")
        self.assertEqual(doc["metadata"], {"name": "relay-internal-tls", "namespace": "flux-system"})
        spec = doc["spec"]
        self.assertEqual(spec["path"], "./kubernetes/apps/relay-internal-tls")
        self.assertEqual(spec["sourceRef"], {"kind": "GitRepository", "name": "flux-system"})
        self.assertEqual(spec["dependsOn"], [{"name": "cert-manager-config"}])
        self.assertIs(spec["prune"], True)
        self.assertIs(spec["wait"], True)

    def test_nothing_else_points_flux_at_the_tree(self):
        for manifest in sorted(CLUSTER.glob("*.yaml")):
            if manifest.name == "relay-internal-tls.yaml":
                continue
            for doc in docs(manifest):
                self.assertNotIn("relay-internal-tls", str(doc.get("spec", {}).get("path", "")), manifest)

    def test_kustomization_lists_every_manifest(self):
        k = yaml.safe_load((TREE / "kustomization.yaml").read_text(encoding="utf-8"))
        files = sorted(f.name for f in TREE.glob("*.yaml") if f.name != "kustomization.yaml")
        self.assertEqual(sorted(k["resources"]), files)
        gens = {g["name"]: g for g in k["configMapGenerator"]}
        self.assertEqual(gens["relay-hub-tls-caddy"]["files"], ["Caddyfile=hub.Caddyfile", "cert-reloader.sh"])
        self.assertEqual(gens["relay-router-tls-caddy"]["files"], ["Caddyfile=router.Caddyfile", "cert-reloader.sh"])
        self.assertTrue(all(g["namespace"] == NS for g in gens.values()))


class Addresses(unittest.TestCase):
    def test_services_are_pinned_distinct_and_reserved(self):
        objs = objects()
        plan = (ROOT / "docs/network-plan.md").read_text(encoding="utf-8")
        static = ipaddress.ip_network("10.96.0.0/24")
        seen = set()
        for name, (_, ip, *_rest) in ENDPOINTS.items():
            svc = objs[("Service", NS, name)]["spec"]
            self.assertEqual(svc, {
                "type": "ClusterIP", "clusterIP": ip, "clusterIPs": [ip], "ipFamilyPolicy": "SingleStack",
                "ipFamilies": ["IPv4"], "selector": {"app.kubernetes.io/name": name},
                "ports": [{"name": "https", "protocol": "TCP", "port": 443, "targetPort": 443}]})
            self.assertIn(ipaddress.ip_address(ip), static)
            self.assertNotIn(ip, {"10.96.0.1", "10.96.0.10"})
            self.assertRegex(plan, rf"\| `{re.escape(ip)}` \| `{NS}/{name}`")
            seen.add(ip)
        self.assertEqual(len(seen), len(ENDPOINTS))


class Terminators(unittest.TestCase):
    def setUp(self):
        self.objs = objects()

    def test_namespace_is_restricted_retained_and_default_deny(self):
        ns = self.objs[("Namespace", None, NS)]["metadata"]
        for mode in ("enforce", "audit", "warn"):
            self.assertEqual(ns["labels"][f"pod-security.kubernetes.io/{mode}"], "restricted")
        self.assertEqual(ns["annotations"], {"kustomize.toolkit.fluxcd.io/prune": "disabled"})
        self.assertEqual(self.objs[("NetworkPolicy", NS, "relay-default-deny")]["spec"],
                         {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []})
        self.assertEqual({k[0] for k in self.objs if k[1] == NS} | {"Namespace"},
                         {"Namespace", "NetworkPolicy", "Certificate", "Deployment", "PodDisruptionBudget", "Service"})

    def test_one_ailab_ca_certificate_per_public_name(self):
        certs = {k[2]: v for k, v in self.objs.items() if k[0] == "Certificate"}
        self.assertEqual(set(certs), set(ENDPOINTS))
        for name, (public, *_rest) in ENDPOINTS.items():
            spec = certs[name]["spec"]
            self.assertEqual(spec["dnsNames"], [public])
            self.assertNotIn("ipAddresses", spec)
            self.assertNotIn("isCA", spec)
            self.assertEqual(spec["issuerRef"], {"name": "ailab-ca", "kind": "ClusterIssuer", "group": "cert-manager.io"})
            self.assertEqual(spec["privateKey"], {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"})
            self.assertEqual(spec["usages"], ["server auth", "digital signature"])
            self.assertEqual(spec["secretName"], f"{name}-cert")

    def test_deployments(self):
        edge = next(d for d in docs(ROOT / "kubernetes/apps/apps/llm-router/edge.yaml") if d["kind"] == "Deployment")
        edge_image = edge["spec"]["template"]["spec"]["containers"][0]["image"]
        for name in ENDPOINTS:
            dep = self.objs[("Deployment", NS, name)]
            self.assertEqual(dep["spec"]["selector"], {"matchLabels": {"app.kubernetes.io/name": name}})
            self.assertEqual(dep["spec"]["replicas"], 2)
            tmpl = dep["spec"]["template"]
            self.assertEqual(tmpl["metadata"]["labels"]["app.kubernetes.io/name"], name)
            pod = tmpl["spec"]
            self.assertIs(pod["automountServiceAccountToken"], False)
            self.assertIs(pod["enableServiceLinks"], False)
            for field in ("hostNetwork", "hostPID", "hostIPC", "serviceAccountName", "hostAliases"):
                self.assertNotIn(field, pod)
            self.assertEqual(pod["securityContext"], {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                                      "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}})
            containers = {c["name"]: c for c in pod["containers"]}
            self.assertEqual(set(containers), {"caddy", "cert-reloader"})
            for c in containers.values():
                # The same image as llm-router-edge (and the edge proxy): Renovate moves them together.
                self.assertEqual(c["image"], edge_image)
                self.assertRegex(c["image"], r"^caddy:[\w.-]+@sha256:[0-9a-f]{64}$")
                self.assertEqual(c["securityContext"], {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                        "capabilities": {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]}})
                self.assertEqual({m["name"]: (m["mountPath"], m.get("readOnly", False)) for m in c["volumeMounts"]},
                                 {"caddyfile": ("/etc/caddy", True), "certs": ("/certs", True),
                                  "data": ("/data", False), "config": ("/config", False)})
                self.assertIn("limits", c["resources"])
            caddy = containers["caddy"]
            self.assertEqual(caddy["command"], ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"])
            self.assertEqual(caddy["ports"], [{"name": "https", "containerPort": 443, "protocol": "TCP"}])
            self.assertEqual(containers["cert-reloader"]["command"], ["sh", "/etc/caddy/cert-reloader.sh"])
            vols = {v["name"]: v for v in pod["volumes"]}
            self.assertEqual(vols["caddyfile"], {"name": "caddyfile", "configMap": {"name": f"{name}-caddy"}})
            self.assertEqual(vols["certs"]["secret"], {"secretName": f"{name}-cert", "defaultMode": 0o440})
            pdb = self.objs[("PodDisruptionBudget", NS, name)]["spec"]
            self.assertEqual(pdb, {"minAvailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}})

    def test_ingress_only_from_managed_workers_of_the_slot_and_exact_egress(self):
        for name, (_, _, _, up_ns, up_labels, up_port, _) in ENDPOINTS.items():
            spec = self.objs[("NetworkPolicy", NS, name)]["spec"]
            self.assertEqual(spec["podSelector"], {"matchLabels": {"app.kubernetes.io/name": name}})
            self.assertEqual(spec["policyTypes"], ["Ingress", "Egress"])
            self.assertEqual(spec["ingress"], [{
                "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": SLOT}},
                          "podSelector": {"matchLabels": MANAGED}}],
                "ports": [{"protocol": "TCP", "port": 443}]}])
            self.assertEqual(spec["egress"], [
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": up_ns}},
                         "podSelector": {"matchLabels": up_labels}}],
                 "ports": [{"protocol": "TCP", "port": up_port}]},
                {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                         "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                 "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}])
        # Each terminator's policy selects only its own Pods: the slot's hub and Router rules stay separable.
        for name in ENDPOINTS:
            for other in ENDPOINTS:
                labels = self.objs[("Deployment", NS, other)]["spec"]["template"]["metadata"]["labels"]
                self.assertEqual(selects(self.objs[("NetworkPolicy", NS, name)]["spec"]["podSelector"], labels),
                                 name == other)

    def test_upstreams_match_the_live_relay_and_router_manifests(self):
        relay = {(d["kind"], d["metadata"]["name"]): d for d in docs(ROOT / "kubernetes/apps/apps/relay/relay.yaml")}
        edge = {(d["kind"], d["metadata"]["name"]): d for d in docs(ROOT / "kubernetes/apps/apps/llm-router/edge.yaml")}
        for svc, dep, name in ((relay[("Service", "relay")], relay[("Deployment", "relay")], "relay-hub-tls"),
                               (edge[("Service", "llm-router-edge")], edge[("Deployment", "llm-router-edge")],
                                "relay-router-tls")):
            _, _, _, up_ns, up_labels, up_port, url = ENDPOINTS[name]
            self.assertEqual(svc["metadata"]["namespace"], up_ns)
            self.assertEqual(url, f"http://{svc['metadata']['name']}.{up_ns}.svc.cluster.local:{svc['spec']['ports'][0]['port']}")
            pod_labels = dep["spec"]["template"]["metadata"]["labels"]
            self.assertTrue(selects({"matchLabels": up_labels}, pod_labels), name)
            ports = {p["containerPort"] for c in dep["spec"]["template"]["spec"]["containers"] for p in c.get("ports", [])}
            self.assertIn(up_port, ports, name)

    def test_additive_upstream_ingress(self):
        for key, (up_labels, name, port) in {
                ("NetworkPolicy", "relay", "relay-internal-tls-ingress"): ({"app": "relay"}, "relay-hub-tls", 8788),
                ("NetworkPolicy", "llm-router", "llm-router-edge-internal-tls-ingress"):
                    ({"app.kubernetes.io/name": "llm-router-edge"}, "relay-router-tls", 8080)}.items():
            self.assertEqual(self.objs[key]["spec"], {
                "podSelector": {"matchLabels": up_labels}, "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}},
                                       "podSelector": {"matchLabels": {"app.kubernetes.io/name": name}}}],
                             "ports": [{"protocol": "TCP", "port": port}]}]})
        # The guards next to the trusted-header settings name these extra sources.
        self.assertIn("relay-internal-tls-ingress",
                      (ROOT / "kubernetes/apps/apps/relay/network-policy.yaml").read_text(encoding="utf-8"))
        self.assertIn("llm-router-edge-internal-tls-ingress",
                      (ROOT / "kubernetes/apps/apps/llm-router/edge.yaml").read_text(encoding="utf-8"))
        self.assertIn("relay-router-tls", (ROOT / "kubernetes/apps/apps/llm-router/router.yaml").read_text(encoding="utf-8"))


class Caddyfiles(unittest.TestCase):
    @staticmethod
    def body(name: str) -> str:
        return "\n".join(line for line in (TREE / ENDPOINTS[name][2]).read_text(encoding="utf-8").splitlines()
                         if not line.startswith("#"))

    def test_one_site_one_upstream_and_header_overwrite(self):
        for name, (public, _, _, _, _, _, url) in ENDPOINTS.items():
            text = self.body(name)
            self.assertEqual(re.findall(r"^(\S+) \{$", text, re.M), [f"https://{public}:443"])
            self.assertEqual(re.findall(r"reverse_proxy (\S+)", text), [url])
            self.assertIn("\ttls /certs/tls.crt /certs/tls.key\n", text)
            self.assertIn("\t\tstrict_sni_host on\n", text)
            self.assertIn("\tadmin localhost:2019\n", text)
            self.assertIn("\tauto_https off\n", text)
            stripped = re.findall(r"^\t\trequest_header (\S+)$", text, re.M)
            self.assertEqual(stripped, STRIPPED)
            self.assertEqual(re.findall(r"^\t\t\theader_up (\S+) (\S+)$", text, re.M), HEADER_UP)
            # Both the site's access log and Caddy's default logger (http.log.error carries the request
            # of a failed upstream) drop credentials and query strings.
            self.assertIn("\tlog default {\n", text)
            for header in ("Authorization", "Proxy-Authorization", "Cookie", "X-Api-Key"):
                self.assertEqual(text.count(f"request>headers>{header} delete"), 2, header)
            self.assertEqual(text.count("request>uri regexp `\\?.*$` `?REDACTED`"), 2)
            self.assertIn("\t\toutput stdout\n", text)
            # The strip comes before the proxy in the same route.
            self.assertLess(text.index("request_header -Cf-*"), text.index("reverse_proxy"))

    def test_hub_and_router_differ_only_in_name_and_upstream(self):
        hub, router = (self.body(n) for n in ENDPOINTS)
        hub = hub.replace("relay.chifor.me", "NAME").replace(ENDPOINTS["relay-hub-tls"][6], "UPSTREAM")
        router = router.replace("router.chifor.me", "NAME").replace(ENDPOINTS["relay-router-tls"][6], "UPSTREAM")
        self.assertEqual(hub, router)

    def test_reloader_forces_a_reload_only_after_validation(self):
        text = (TREE / "cert-reloader.sh").read_text(encoding="utf-8")
        self.assertIn("caddy validate --config \"$CONFIG\" --adapter caddyfile", text)
        self.assertIn("caddy reload --force --config \"$CONFIG\" --adapter caddyfile --address localhost:2019", text)
        self.assertLess(text.index("caddy validate"), text.index("caddy reload"))


if __name__ == "__main__":
    unittest.main()
