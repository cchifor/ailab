#!/usr/bin/env python3
"""Restore a real rollout backup using the deployment's pinned, restricted images.

All data and PostgreSQL roles are disposable fixtures; no cluster access or
production credentials are used. Docker and PyYAML are required.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "kubernetes/apps/apps/relay"
backup = list(yaml.safe_load_all((MANIFESTS / "backup.yaml").read_text()))
scripts = next(d for d in backup if d["kind"] == "ConfigMap")["data"]
deployment = next(d for d in yaml.safe_load_all((MANIFESTS / "relay.yaml").read_text()) if d["kind"] == "Deployment")
database, artifacts, migrate = deployment["spec"]["template"]["spec"]["initContainers"]
assert [database["name"], artifacts["name"], migrate["name"]] == ["backup-database", "backup-artifacts", "migrate"]
name = "relay-rollout-backup-test-" + uuid.uuid4().hex[:10]
fixture_user = f"{os.getuid()}:{os.getgid()}"
assert os.getuid() != 0, "Run the restricted backup fixture as an unprivileged user"


def docker(*args, data=None, check=True, timeout=90):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Docker fixture failed: " + result.stderr.decode(errors="replace")[-2500:])
    return result


def sql(statement, database_name="relay", role="postgres", check=True):
    return docker("exec", "-i", name, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", role,
                  "-d", database_name, data=statement.encode(), check=check)


# Separate network fetch time from the bounded fixture commands on cold runners.
for image in [database["image"], artifacts["image"]]:
    docker("pull", image, timeout=300)

with tempfile.TemporaryDirectory(prefix="relay-rollout-backup-") as temporary:
    temporary = Path(temporary)
    temporary.chmod(0o755)
    for child in ["scripts", "dumps", "artifacts", "tmp"]:
        (temporary / child).mkdir()
    for filename, content in scripts.items():
        (temporary / "scripts" / filename).write_text(content)
    manifest = {"id": "rollout-test", "version": "1.0.0", "description": "Unicode λ and escaped\nline"}
    source = "export default () => {};\n"
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sha = hashlib.sha256((canonical + "\n" + source).encode()).hexdigest()
    plugin = temporary / "artifacts" / "plugins" / sha
    plugin.mkdir(parents=True)
    (plugin / "index.mjs").write_text(source)
    restricted = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
    mounts = ["-v", f"{temporary / 'dumps'}:/dumps", "-v", f"{temporary / 'scripts'}:/scripts:ro", "-v", f"{temporary / 'tmp'}:/tmp"]
    try:
        docker("network", "create", name)
        docker("run", "-d", "--name", name, "--network", name, "--user", "70:70", *restricted,
               "--tmpfs", "/var/lib/postgresql/data:uid=70,gid=70,mode=0770", "--tmpfs", "/var/run/postgresql:uid=70,gid=70,mode=0770",
               "--tmpfs", "/tmp", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=relay", database["image"])
        for _ in range(60):
            if docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", "relay", check=False).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        sql("CREATE ROLE relay_backup LOGIN BYPASSRLS; CREATE ROLE relay_migrator LOGIN; "
            "CREATE TABLE plugin_states(sha256 text,manifest jsonb,previous jsonb); "
            "CREATE TABLE preserved(id integer PRIMARY KEY,body text); "
            "INSERT INTO preserved VALUES(1,'before migration'),(2,'Unicode λ'); "
            "INSERT INTO plugin_states VALUES('" + sha + "',$manifest$" + canonical + "$manifest$,NULL); "
            "GRANT USAGE ON SCHEMA public TO relay_backup; "
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO relay_backup;")
        assert sql("DELETE FROM preserved;", role="relay_backup", check=False).returncode != 0
        # Execute the exact deployment init command with unprivileged fixture ownership.
        docker("run", "--rm", "--network", name, "--user", fixture_user, *restricted, *mounts,
               "-e", "PGHOST=" + name, "-e", "PGUSER=relay_backup", "-e", "PGDATABASE=relay", "-e", "PGCONNECT_TIMEOUT=15",
               "-e", "POD_UID=rollout-test", database["image"], *database["command"])
        docker("run", "--rm", "--network", "none", "--user", fixture_user, *restricted, *mounts,
               "-v", f"{temporary / 'artifacts'}:/artifacts:ro", "-e", "POD_UID=rollout-test", artifacts["image"], *artifacts["command"])
        proof = json.loads((temporary / "tmp" / "relay-pre-migration-backup.json").read_text())
        generation = temporary / "dumps" / proof["generation"]
        archive = (generation / "relay.dump").read_bytes()
        assert hashlib.sha256(archive).hexdigest() == proof["dumpSha256"]
        sql("CREATE DATABASE relay_restore OWNER relay_migrator;")
        docker("exec", "-i", name, "pg_restore", "-U", "relay_migrator", "-d", "relay_restore",
               "--no-owner", "--no-acl", "--exit-on-error", data=archive)
        assert sql("SELECT * FROM preserved ORDER BY id", "relay_restore").stdout == sql("SELECT * FROM preserved ORDER BY id").stdout
        assert sql("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='public'", "relay_restore").stdout.strip() == b"relay_migrator"
        assert (generation / "plugins" / sha / "index.mjs").read_text() == source
        # Re-enter the completed publisher with no staging tree: init retry is idempotent.
        docker("run", "--rm", "--network", "none", "--user", fixture_user, *restricted, *mounts,
               "-v", f"{temporary / 'artifacts'}:/artifacts:ro", "-e", "POD_UID=rollout-test", artifacts["image"], *artifacts["command"])
        assert json.loads((temporary / "tmp" / "relay-pre-migration-backup.json").read_text()) == proof
        # Run the script-level corruption/retention/retry suite under the exact Node image too.
        gate_tests = ROOT / "scripts/tests/test-relay-backup.py"
        with tempfile.TemporaryDirectory(prefix="relay-node-wrapper-") as wrapper_dir:
            wrapper = Path(wrapper_dir) / "node"
            wrapper.write_text("#!/bin/sh\ncase_root=${DUMP_ROOT%/dumps}\nexec docker run --rm --network none --user " + fixture_user +
                               " --read-only --cap-drop ALL --security-opt no-new-privileges -v \"$case_root:$case_root\" -e DUMP_ROOT -e ARTIFACT_ROOT -e POD_UID -e BACKUP_PUBLISHER -e BACKUP_RECEIPT " + artifacts["image"] + " node \"$@\"\n")
            wrapper.chmod(0o700)
            subprocess.run(["python3", str(gate_tests)], env={**os.environ, "RELAY_NODE_BIN": str(wrapper)}, check=True, timeout=300)
        print("PASS: restricted pre-migration dump, artifact gate, checksum validation, real restore and retry invariants")
    finally:
        docker("rm", "-f", name, check=False)
        docker("network", "rm", name, check=False)
