#!/usr/bin/env python3
"""Build an offline Router Deployment patch after checking nonsecret pin receipts.

This emits JSON (valid Kubernetes YAML) to stdout. It never reads or emits
Secret values and does not apply anything to the cluster. The named Secret must
be provisioned and tested separately before the patch is included in Flux.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

DIGEST = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
KEY = re.compile(r"[A-Za-z0-9_.-]+\Z")
ROOT = "/etc/router/protected"


def fail(message):
    raise SystemExit(f"retirement activation receipt: {message}")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value, field):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        fail(f"{field} must be a lowercase SHA-256 digest")
    return value


def name(value, field):
    if not isinstance(value, str) or not NAME.fullmatch(value) or len(value) > 63:
        fail(f"{field} must be a Kubernetes DNS label")
    return value


def material_path(value):
    if not isinstance(value, str) or not value.startswith(ROOT + "/"):
        fail("material paths must be under " + ROOT)
    leaf = value[len(ROOT) + 1 :]
    if not KEY.fullmatch(leaf) or leaf in (".", ".."):
        fail("material path must contain one safe filename")
    return value


def pinned_file(value, files, field):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        fail(f"{field} needs exactly path and sha256")
    path = material_path(value["path"])
    digest(value["sha256"], field + ".sha256")
    if path not in files or files[path]["private"]:
        fail(f"{field} must map to a public prepared file")


def build(receipt):
    required = {"version", "routerImage", "routerSourceCommit", "release", "materialSecret", "serverConfig", "sourceManifest",
                "budgetDeploymentDigest", "files"}
    if not isinstance(receipt, dict) or set(receipt) != required or receipt["version"] != 1:
        fail("receipt must have exactly the v1 fields")
    image = receipt["routerImage"]
    if not isinstance(image, str) or not re.fullmatch(
        r"registry\.chifor\.me/llm-router/router@sha256:[a-f0-9]{64}", image
    ):
        fail("routerImage must be a digest-pinned Router image")
    source_commit = receipt["routerSourceCommit"]
    if not isinstance(source_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        fail("routerSourceCommit must be a full source SHA")
    release = receipt["release"]
    if not isinstance(release, str) or not re.fullmatch(r"[a-z0-9][-a-z0-9]{1,62}", release):
        fail("release must be a short immutable label")
    secret = name(receipt["materialSecret"], "materialSecret")
    deployment_digest = digest(receipt["budgetDeploymentDigest"], "budgetDeploymentDigest")
    entries = receipt["files"]
    if not isinstance(entries, list) or not 8 <= len(entries) <= 32:
        fail("files must contain 8 to 32 explicit Secret key mappings")
    files = {}
    keys = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"secretKey", "path", "private"}:
            fail("each file needs exactly secretKey, path, private")
        key, path = entry["secretKey"], material_path(entry["path"])
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in keys or path in files:
            fail("Secret keys and material paths must be unique safe filenames")
        if not isinstance(entry["private"], bool):
            fail("private must be a boolean")
        keys.add(key)
        files[path] = entry
    for field in ("serverConfig", "sourceManifest"):
        value = receipt[field]
        if not isinstance(value, dict) or set(value) != {"path", "sha256", "localFile"}:
            fail(f"{field} needs path, sha256, localFile")
        pinned_file({"path": value["path"], "sha256": value["sha256"]}, files, field)
        if sha256(value["localFile"]) != value["sha256"]:
            fail(f"{field} local file does not match its pin")
    server = json.loads(Path(receipt["serverConfig"]["localFile"]).read_text())
    if not isinstance(server, dict) or set(server) != {"version", "host", "port", "ca",
                                                    "certificate", "keyFile", "clientCertificateSha256"}:
        fail("serverConfig has unexpected or missing fields")
    if server["version"] != 1 or server["host"] != "0.0.0.0" or server["port"] != 8788:
        fail("serverConfig must bind v1 to 0.0.0.0:8788")
    for field in ("ca", "certificate"):
        pinned_file(server[field], files, "serverConfig." + field)
    key_path = material_path(server["keyFile"])
    if key_path not in files or not files[key_path]["private"]:
        fail("server private key mapping is missing")
    digest(server["clientCertificateSha256"], "clientCertificateSha256")
    manifest = json.loads(Path(receipt["sourceManifest"]["localFile"]).read_text())
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not manifest.get("cleanup"):
        fail("sourceManifest must be v1 with cleanup entries")
    if len(manifest.get("entries", [])) != len(manifest["cleanup"]):
        fail("sourceManifest needs one cleanup entry per source entry")
    for field in ("trust",):
        pinned_file(manifest[field], files, "sourceManifest." + field)
    for field in ("encryptionKeyFile",):
        path = material_path(manifest[field])
        if path not in files or not files[path]["private"]:
            fail(f"sourceManifest.{field} needs a private file mapping")
    for group in ("entries", "cleanup"):
        for entry in manifest[group]:
            if group == "entries":
                pinned_file(entry["policy"], files, "entry.policy")
                connections = [entry["connection"]]
            else:
                pinned_file(entry["authority"], files, "cleanup.authority")
                connections = [entry["readerConnection"], entry["transitionConnection"]]
            for connection in connections:
                pinned_file(connection["ca"], files, "connection.ca")
                path = material_path(connection["keyFile"])
                if path not in files or not files[path]["private"]:
                    fail("connection key needs a private file mapping")
    budget = {"ROUTER_WORKER_BUDGET_DSN_FILE": "budget-dsn",
              "ROUTER_WORKER_BUDGET_CA_FILE": "budget-ca.pem",
              "ROUTER_WORKER_BUDGET_WITNESS_FILE": "budget-witness.json"}
    for field, leaf in budget.items():
        if ROOT + "/" + leaf not in files:
            fail(f"{field} needs {leaf} in the Secret mapping")
    if not files[ROOT + "/budget-dsn"]["private"]:
        fail("budget DSN must be mapped as private")
    mapping = [{"key": e["secretKey"], "name": e["path"].removeprefix(ROOT + "/")}
               for e in entries]
    permissions = {e["path"].removeprefix(ROOT + "/"): (0o400 if e["private"] else 0o444)
                   for e in entries}
    # The projected Secret is mounted only in this init container. Its files
    # are symlinks; copying yields regular files owned by uid 1000. The app
    # mounts only the memory-backed output, read-only.
    init_code = ("const fs=require('node:fs');const modes=JSON.parse(process.env.FILE_MODES);"
                 "for(const [name,mode] of Object.entries(modes)){"
                 "const src='/in/'+name,dst='/out/'+name;"
                 "fs.copyFileSync(src,dst);fs.chmodSync(dst,mode);}")
    env = [{"name": "ROUTER_DRAIN_MS", "value": "40000"},
           {"name": "ROUTER_PROVENANCE_FILE", "value": receipt["sourceManifest"]["path"]},
           {"name": "ROUTER_PROVENANCE_SHA256", "value": receipt["sourceManifest"]["sha256"]},
           {"name": "ROUTER_RETIREMENT_SERVER_CONFIG_FILE", "value": receipt["serverConfig"]["path"]},
           {"name": "ROUTER_RETIREMENT_SERVER_CONFIG_SHA256", "value": receipt["serverConfig"]["sha256"]},
           {"name": "ROUTER_WORKER_BUDGET_DEPLOYMENT_DIGEST", "value": deployment_digest}]
    env += [{"name": field, "value": ROOT + "/" + leaf} for field, leaf in budget.items()]
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "llm-router", "namespace": "llm-router"},
            "spec": {"template": {"metadata": {"annotations": {
                "chifor.me/source-commit": source_commit, "chifor.me/release": release}},
                "spec": {
                "terminationGracePeriodSeconds": 60,
                "initContainers": [{"name": "prepare-retirement-files", "image": image,
                    "command": ["node", "-e", init_code],
                    "env": [{"name": "FILE_MODES", "value": json.dumps(permissions, sort_keys=True)}],
                    "securityContext": {"allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "retirement-secret", "mountPath": "/in", "readOnly": True},
                                     {"name": "retirement-files", "mountPath": "/out"}]}],
                "containers": [{"name": "router", "image": image,
                    "ports": [{"name": "retirement-https", "containerPort": 8788, "protocol": "TCP"}],
                    "env": env,
                    "volumeMounts": [{"name": "retirement-files", "mountPath": ROOT, "readOnly": True}]}],
                "volumes": [{"name": "retirement-secret", "secret": {"secretName": secret,
                                                              "defaultMode": 0o444,
                                                              "items": mapping}},
                            {"name": "retirement-files", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}]
            }}}}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: build-router-retirement-activation.py RECEIPT.json")
    try:
        result = build(json.loads(Path(sys.argv[1]).read_text()))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        fail(type(error).__name__ + " while validating receipt")
    print(json.dumps(result, indent=2, sort_keys=True))
