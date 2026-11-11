"""Check the offline activation patch's protected-file mount relationship."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "build-router-retirement-activation.py"
SPEC = importlib.util.spec_from_file_location("retirement_activation", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
DIGEST = "a" * 64
ROOT = "/etc/router/protected"


class ActivationMountTest(unittest.TestCase):
    def test_process_owned_files_have_one_read_only_app_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            def path(name):
                return ROOT + "/" + name
            def pin(name):
                return {"path": path(name), "sha256": DIGEST}
            def connection(key):
                return {"ca": pin("source-ca.pem"), "keyFile": path(key)}

            server = {"version": 1, "host": "0.0.0.0", "port": 8788,
                      "ca": pin("server-ca.pem"), "certificate": pin("server-cert.pem"),
                      "keyFile": path("server.key"), "clientCertificateSha256": DIGEST}
            manifest = {"version": 1, "trust": pin("trust.json"),
                        "encryptionKeyFile": path("encryption.key"),
                        "entries": [{"policy": pin("policy.json"),
                                     "connection": connection("source.key")}],
                        "cleanup": [{"authority": pin("authority.json"),
                                     "readerConnection": connection("reader.key"),
                                     "transitionConnection": connection("transition.key")}]}
            def local(name, value):
                file = directory / name
                file.write_text(json.dumps(value))
                return {"path": path(name), "sha256": MODULE.sha256(file),
                        "localFile": str(file)}

            names = ["server.json", "manifest.json", "trust.json", "encryption.key",
                     "policy.json", "authority.json", "source-ca.pem", "source.key",
                     "reader.key", "transition.key", "server-ca.pem", "server-cert.pem",
                     "server.key", "budget-dsn", "budget-ca.pem", "budget-witness.json"]
            private = {"encryption.key", "source.key", "reader.key", "transition.key",
                       "server.key", "budget-dsn"}
            receipt = {"version": 1,
                       "routerImage": "registry.chifor.me/llm-router/router@sha256:" + DIGEST,
                       "routerSourceCommit": "b" * 40, "release": "retirement-test",
                       "materialSecret": "retirement-test", "budgetDeploymentDigest": DIGEST,
                       "serverConfig": local("server.json", server),
                       "sourceManifest": local("manifest.json", manifest),
                       "files": [{"secretKey": name, "path": path(name),
                                  "private": name in private} for name in names]}
            patch = MODULE.build(receipt)
            spec = patch["spec"]["template"]["spec"]
            app = spec["containers"][0]
            env = {item["name"]: item["value"] for item in app["env"]}
            self.assertEqual(env["ROUTER_PROTECTED_FILES_DIR"], ROOT)
            self.assertEqual(app["volumeMounts"],
                             [{"name": "retirement-files", "mountPath": ROOT, "readOnly": True}])
            self.assertEqual({item["path"] for item in
                              spec["volumes"][0]["secret"]["items"]}, set(names))
            self.assertTrue(all(item["path"].startswith(ROOT + "/")
                                for item in receipt["files"]))
            self.assertEqual(spec["initContainers"][0]["volumeMounts"][1],
                             {"name": "retirement-files", "mountPath": "/out"})


if __name__ == "__main__":
    unittest.main()
