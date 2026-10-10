#!/usr/bin/env python3
"""Opt-in notification components against their pinned Node and ntfy images.

Uses only disposable files/accounts/SQLite and loopback HTTP. No cluster access,
live topic publication, production secrets or root-owned host files. Scratch data
stays under this checkout's out/ and containers are removed, with volumes, finally.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPONENTS = ROOT / "kubernetes/components/relay-notifications"
KUSTOMIZE = "registry.k8s.io/kustomize/kustomize:v5.4.3@sha256:6dd0a67e2a8634a5d1aabd9c5e888ff220663e979b55bc17fe4b3a845718bb10"
KUBECONFORM = "ghcr.io/yannh/kubeconform:v0.6.7@sha256:0925177fb05b44ce18574076141b5c3d83235e1904d3f952182ac99ddc45762c"
# Public fixture verifier from ntfy's documentation, never a deployment credential.
HASH = "$2a$10$YLiO8U21sX1uhZamTLJXHuxgVC0Z/GKISibrKCLohPgtG7yIxSk4C"
TENANT = "00000000-0000-4000-8000-000000000001"


def docker(*args, check=True, timeout=90, env=None, data=None):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=timeout, env=env)
    if check and result.returncode:
        # Do not echo a failed native command's arbitrary credential-bearing output.
        # Pull/build/schema commands consume only static repository content.
        diagnostic = result.stderr.decode(errors="replace")[-4000:] if (
            args[0] == "pull" or KUSTOMIZE in args or KUBECONFORM in args) else ""
        raise AssertionError("Docker notification fixture failed (exit %d): %s" % (result.returncode, diagnostic))
    return result


def token():
    return "tk_" + uuid.uuid4().hex[:29]


def documents(path):
    return list(yaml.safe_load_all(path.read_text()))


def named(docs, kind, name):
    return next(d for d in docs if d and d["kind"] == kind and d["metadata"]["name"] == name)


class Notifications(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.getuid() == 0:
            raise AssertionError("Run the fixtures as an unprivileged user")
        (ROOT / "out").mkdir(exist_ok=True)
        cls.scratch = tempfile.TemporaryDirectory(prefix="relay-notifications-", dir=ROOT / "out")
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        cls.rendered = {}
        docker("pull", KUSTOMIZE, timeout=300)
        docker("pull", KUBECONFORM, timeout=300)
        for name, base in [("relay", "apps/relay"), ("ntfy", "infrastructure/monitoring")]:
            wrapper = cls.root / name
            wrapper.mkdir()
            k = {
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": [os.path.relpath(ROOT / "kubernetes/apps" / base, wrapper)],
                "components": [os.path.relpath(COMPONENTS / name, wrapper)],
            }
            (wrapper / "kustomization.yaml").write_text(yaml.safe_dump(k))
            raw = docker("run", "--rm", "-v", f"{ROOT}:/work:ro", "-w", "/work", KUSTOMIZE,
                         "build", str(wrapper.relative_to(ROOT))).stdout
            docs = list(yaml.safe_load_all(raw))
            cls.rendered[name] = docs
            deployment = named(docs, "Deployment", name)["spec"]["template"]["spec"]
            init = next(c for c in deployment["initContainers"] if c["name"] in
                        ["notification-files", "relay-notification-config"])
            cls.node_image = init["image"]
            setup = next(v["configMap"]["name"] for v in deployment["volumes"]
                         if v["name"] == "notification-setup")
            checked = [named(docs, "Deployment", name), named(docs, "ConfigMap", setup)]
            docker("run", "--rm", "-i", KUBECONFORM, "-strict", "-summary",
                   data=yaml.safe_dump_all(checked).encode())
            script = named(docs, "ConfigMap", setup)["data"]["materialize.mjs"]
            (wrapper / "materialize.mjs").write_text(script)
        native = named(cls.rendered["ntfy"], "Deployment", "ntfy")["spec"]["template"]["spec"]
        cls.native = next(c for c in native["containers"] if c["name"] == "ntfy")
        cls.ntfy_image = cls.native["image"]
        cls.base = named(cls.rendered["ntfy"], "ConfigMap", "ntfy-config")["data"]
        for image in [cls.node_image, cls.ntfy_image]:
            docker("pull", image, timeout=300)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        for directory in ["input", "output", "base", "db", "templates"]:
            (self.path / directory).mkdir()
        self.publisher, self.subscriber = token(), token()
        self.secret_values = [self.publisher, self.subscriber, HASH]
        self.seed_inputs()
        (self.path / "base/server.yml").write_text(self.base["server.yml"])
        (self.path / "templates/ailab-alertmanager.yml").write_text(self.base["ailab-alertmanager.yml"])
        self.container = "relay-notifications-test-" + uuid.uuid4().hex[:10]
        self.running = False
        self.addCleanup(self.stop)

    def seed_inputs(self):
        for key, value in {
            "publisher-token": self.publisher, "subscriber-token": self.subscriber,
            "publisher-hash": HASH, "subscriber-hash": HASH, "tenant-id": TENANT,
        }.items():
            (self.path / "input" / key).write_text(value)

    def materialize(self, mode, good=True):
        result = docker("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                        "-v", f"{self.root / mode}:/setup:ro", "-v", f"{self.path / 'input'}:/input:ro",
                        "-v", f"{self.path / 'base'}:/base:ro", "-v", f"{self.path / 'output'}:/output",
                        self.node_image, "node", "/setup/materialize.mjs", check=False)
        self.assertEqual(result.returncode == 0, good, "unexpected materializer result")
        self.no_secrets(result.stdout + result.stderr)
        return result

    def no_secrets(self, output):
        for value in self.secret_values:
            self.assertNotIn(value.encode(), output, "credential leaked into fixture output")

    def start(self, provisioned=True):
        config = self.path / ("output" if provisioned else "base") / "server.yml"
        docker("run", "--rm", "-d", "--name", self.container, "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", f"/var/cache/ntfy:uid={os.getuid()},gid={os.getgid()}",
               "-v", f"{config}:/etc/ntfy/server.yml:ro", "-v", f"{self.path / 'db'}:/var/lib/ntfy",
               "-v", f"{self.path / 'templates'}:/etc/ntfy/templates:ro",
               "-p", "127.0.0.1::80", self.ntfy_image, "serve")
        self.running = True
        port = docker("port", self.container, "80/tcp").stdout.decode().strip().rsplit(":", 1)[1]
        self.url = f"http://127.0.0.1:{port}"
        for _ in range(80):
            try:
                if self.request("/v1/health")[0] == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        else:
            self.fail("native ntfy did not become healthy")
        # Run the exact existing postStart hook alongside native provisioning:
        # a regression here would crash the shared alerting service.
        env = os.environ | {"NTFY_ADMIN_PASSWORD": "fixture-admin-password", "NTFY_QNAP_PASSWORD": "fixture-qnap-password"}
        result = docker("exec", "-e", "NTFY_ADMIN_PASSWORD", "-e", "NTFY_QNAP_PASSWORD", self.container,
                        *self.native["lifecycle"]["postStart"]["exec"]["command"], env=env)
        self.no_secrets(result.stdout + result.stderr)

    def stop(self):
        if not self.running:
            return
        try:
            logs = docker("logs", self.container, check=False)
            self.no_secrets(logs.stdout + logs.stderr)
        finally:
            docker("rm", "-fv", self.container, check=False)
            self.running = False

    def request(self, path, credential=None, publish=False):
        headers = {}
        if credential:
            headers["Authorization"] = ("Basic " + base64.b64encode(credential.encode()).decode()
                                         if ":" in credential else "Bearer " + credential)
        req = urllib.request.Request(self.url + path, headers=headers,
                                     data=b"An agent needs human review. Open Relay." if publish else None)
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def assert_scopes(self):
        self.assertEqual(self.request("/relay-actions", self.publisher, True)[0], 200)
        self.assertEqual(self.request("/relay-actions/json?poll=1", self.subscriber)[0], 200)
        for path, credential, publish in [
            ("/relay-actions/json?poll=1", self.publisher, False),
            ("/relay-actions", self.subscriber, True),
            ("/unrelated", self.publisher, True),
            ("/unrelated/json?poll=1", self.subscriber, False),
            ("/relay-actions", None, True),
            ("/relay-actions/json?poll=1", None, False),
        ]:
            self.assertEqual(self.request(path, credential, publish)[0], 403)
        self.assertEqual(self.request("/ailab-alerts", "ailab:fixture-admin-password", True)[0], 200)
        self.assertEqual(self.request("/qnap-alerts", "qnap:fixture-qnap-password", True)[0], 200)
        self.assertEqual(self.request("/qnap-alerts/json?poll=1", "qnap:fixture-qnap-password")[0], 403)

    def test_render_preserves_backup_gates_and_is_opt_in(self):
        deployment = named(self.rendered["relay"], "Deployment", "relay")["spec"]["template"]["spec"]
        base = named(documents(ROOT / "kubernetes/apps/apps/relay/relay.yaml"), "Deployment", "relay")["spec"]["template"]["spec"]
        self.assertEqual([c for c in deployment["initContainers"] if c["name"] != "notification-files"], base["initContainers"])
        app = next(c for c in deployment["containers"] if c["name"] == "relay")
        self.assertNotIn("RELAY_AGENT_CONTROL_PLANE", [e["name"] for e in app["env"]])
        self.assertEqual(app["image"], next(c for c in base["containers"] if c["name"] == "relay")["image"])
        initializer = next(c for c in deployment["initContainers"] if c["name"] == "notification-files")
        self.assertEqual(deployment["securityContext"]["fsGroup"], initializer["securityContext"]["runAsGroup"])
        self.assertEqual(deployment["securityContext"]["runAsUser"], initializer["securityContext"]["runAsUser"])
        for name, docs in self.rendered.items():
            pod = named(docs, "Deployment", name)["spec"]["template"]["spec"]
            volumes = {v["name"] for v in pod["volumes"]}
            for container in pod.get("initContainers", []) + pod["containers"]:
                for mount in container.get("volumeMounts", []):
                    self.assertIn(mount["name"], volumes, "mounted volume is absent from rendered pod")
        for directory in ["apps/relay", "infrastructure/monitoring"]:
            base = ROOT / "kubernetes/apps" / directory
            k = yaml.safe_load((base / "kustomization.yaml").read_text())
            for component in k.get("components", []):
                self.assertFalse((base / component).resolve().is_relative_to(COMPONENTS),
                                 "notification opt-in requires a separate rollout PR")
        mount = next(m for m in self.native["volumeMounts"] if m["mountPath"] == "/etc/ntfy/server.yml")
        self.assertEqual(mount["name"], "notification-config")
        self.assertEqual(len([m for m in self.native["volumeMounts"] if m["mountPath"] == "/etc/ntfy/server.yml"]), 1)

    def test_private_relay_files_rotation_and_interrupted_init(self):
        # Match Kubernetes' ..data/key projection, including a generation switch.
        projected = self.path / "input/publisher-token"
        projected.unlink()
        data = self.path / "input/..data"
        data.mkdir()
        (data / "publisher-token").write_text(self.publisher)
        projected.symlink_to("..data/publisher-token")
        self.materialize("relay")
        private = self.path / "output/private"
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        for name in ["publisher-token", "destinations.json"]:
            with os.fdopen(os.open(private / name, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
                s = os.fstat(stream.fileno())
                self.assertTrue(stat.S_ISREG(s.st_mode))
                self.assertEqual((s.st_uid, s.st_nlink, stat.S_IMODE(s.st_mode)), (os.getuid(), 1, 0o600))
        self.assertEqual(json.loads((private / "destinations.json").read_text()), [{
            "tenantId": TENANT, "url": "https://ntfy.chifor.me/relay-actions",
            "tokenFile": "/run/relay-notifications/private/publisher-token",
        }])
        replacement = token()
        self.secret_values.append(replacement)
        (data / "publisher-token").write_text(replacement)
        (private / "publisher-token.new").write_text("partial")
        self.materialize("relay")
        self.assertEqual((private / "publisher-token").read_text(), replacement)

    def test_invalid_relay_input_fails_without_publishing_or_disclosure(self):
        for key, value in [("tenant-id", "malformed-tenant"), ("publisher-token", "sensitive-canary:\nrole: admin"),
                           ("publisher-token", "A" * 129)]:
            self.seed_inputs()
            self.secret_values.append(value)
            (self.path / "input" / key).write_text(value)
            self.materialize("relay", good=False)
            self.assertFalse((self.path / "output/private/destinations.json").exists())

    def test_invalid_ntfy_input_fails_before_native_parser(self):
        for key, value in [("publisher-token", "sensitive-canary:\nrole: admin"),
                           ("subscriber-token", self.publisher), ("publisher-hash", HASH.replace("$10$", "$09$")),
                           ("publisher-hash", "X" * 129)]:
            self.seed_inputs()
            self.secret_values.append(value)
            (self.path / "input" / key).write_text(value)
            self.materialize("ntfy", good=False)
            self.assertFalse((self.path / "output/server.yml").exists())
        self.seed_inputs()
        for base in [self.base["server.yml"] + "\nauth-users: []\n",
                     self.base["server.yml"].replace('"deny-all"', '"read-write"')]:
            (self.path / "base/server.yml").write_text(base)
            self.materialize("ntfy", good=False)

    def test_equivalent_deny_all_formatting_and_value_free_diagnostics(self):
        for value in ['deny-all', "'deny-all'", '"deny-all"  # restricted']:
            base = self.base["server.yml"].replace('"deny-all"', value)
            (self.path / "base/server.yml").write_text(base)
            self.materialize("ntfy")
            self.assertEqual(yaml.safe_load((self.path / "output/server.yml").read_text())["auth-default-access"], "deny-all")
        (self.path / "input/publisher-token").unlink()
        result = self.materialize("relay", good=False)
        self.assertIn(b'read publisher-token (ENOENT)', result.stderr)
        self.seed_inputs()
        (self.path / "input/subscriber-hash").write_text("private-malformed-verifier")
        self.secret_values.append("private-malformed-verifier")
        result = self.materialize("ntfy", good=False)
        self.assertIn(b'validate subscriber-hash (INVALID_INPUT)', result.stderr)

    def test_native_accounts_reconcile_rotate_restore_and_withdraw(self):
        # Start with the existing shared service and its real reconciliation hook.
        self.start(provisioned=False)
        # A public unrelated topic must not expand either restricted identity.
        docker("exec", self.container, "ntfy", "access", "*", "unrelated", "read-write")
        self.stop()
        self.materialize("ntfy")
        self.start()
        self.assert_scopes()
        self.stop()
        # Idempotent restart and repair of drift in provisioned rows.
        with sqlite3.connect(self.path / "db/user.db") as db:
            db.execute("UPDATE user SET role='admin' WHERE user='relay-publisher'")
        self.start()
        self.assert_scopes()
        self.stop()
        old = self.publisher
        self.publisher = token()
        self.secret_values.append(self.publisher)
        self.seed_inputs()
        (self.path / "output/server.yml.new").write_text("partial")
        self.materialize("ntfy")
        self.start()
        self.assertEqual(self.request("/relay-actions", old, True)[0], 401)
        self.assert_scopes()
        self.stop()
        # Rebuilding the auth volume recreates accounts/ACLs/tokens from configuration.
        shutil.rmtree(self.path / "db")
        (self.path / "db").mkdir()
        self.start()
        self.assert_scopes()
        self.stop()
        # Removing the component removes only provisioned accounts on next start.
        self.start(provisioned=False)
        self.assertEqual(self.request("/relay-actions", self.publisher, True)[0], 401)
        self.assertEqual(self.request("/relay-actions/json?poll=1", self.subscriber)[0], 401)
        self.assertEqual(self.request("/ailab-alerts", "ailab:fixture-admin-password", True)[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
