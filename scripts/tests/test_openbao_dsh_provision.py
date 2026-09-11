#!/usr/bin/env python3
"""Gates for the OpenBao dsh provisioning Job.

WHY THIS EXISTS. The Job replaces a live OpenBao policy with a breakglass
token. Two failure shapes matter, and neither is visible to `kustomize build`
or `kubeconform`:

  * IT CAN WEDGE ITSELF. The script refuses to run when the live policy grants
    a path its allow-list does not know, so that a grant added by hand is never
    silently dropped. If someone adds a path to the policy heredoc without
    adding it to that allow-list, the Job succeeds once, writes the new grant,
    and then refuses every subsequent run -- because the grant it wrote is now
    "unknown" drift. The allow-list must therefore be a SUPERSET of what the
    policy writes.

  * IT CAN CAUSE AN OUTAGE SEVERAL DAYS LATER. `bao policy write` replaces the
    document. Dropping the estate/litellm grant stops dsh-litellm syncing, and
    because a Secret change never reaches an already-running container's
    environment, dsh keeps working on a stale credential until it is revoked.
    The grant must be present in the heredoc, asserted here rather than assumed.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = (
    ROOT
    / "kubernetes"
    / "apps"
    / "infrastructure"
    / "security"
    / "openbao"
    / "dsh-provision-job.yaml"
)

#: Grants that must survive any rewrite. dsh-litellm is the live model
#: credential; losing it is the delayed outage described above.
REQUIRED_GRANTS = {"af/data/estate/litellm", "af/metadata/estate/litellm"}

#: The grant the feature needs. ESO's `dataFrom.extract` reads the KV-v2 DATA
#: endpoint only, so no metadata grant is expected here.
FEATURE_GRANT = "af/data/dsh/credentials"


def _script():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap":
            return doc["data"]["provision.sh"]
    raise AssertionError("no ConfigMap carrying provision.sh")


def _code(script):
    """The script with full-line comments removed.

    Assertions about what the script DOES must not be satisfied or broken by
    what it SAYS: this file's own comments name `kv patch` precisely to explain
    why it is absent, which a raw substring check reads as its presence.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _policy_paths(script):
    """Paths inside the `bao policy write` heredoc."""
    body = script.split("<<'EOF'", 1)[1].split("\nEOF", 1)[0]
    return set(re.findall(r'path "([^"]+)"', body))


def _allowlist(script):
    """Paths the drift guard tolerates, from its `case` arms."""
    arm = re.search(r"\n\s*(af/[^)]*?)\)\s*;;", script)
    assert arm, "drift-guard case arm not found"
    return {p.strip() for p in arm.group(1).split("|") if p.strip()}


class ProvisionScript(unittest.TestCase):
    def setUp(self):
        self.script = _script()

    def test_shell_parses(self):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no /bin/sh")
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(self.script)
            f.flush()
            proc = subprocess.run([sh, "-n", f.name], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_required_grants_survive(self):
        self.assertTrue(REQUIRED_GRANTS <= _policy_paths(self.script))

    def test_feature_grant_present(self):
        self.assertIn(FEATURE_GRANT, _policy_paths(self.script))

    def test_guard_allows_everything_the_policy_writes(self):
        # Otherwise the Job writes a grant and then refuses every later run,
        # treating its own output as unreviewed drift.
        written = _policy_paths(self.script)
        allowed = _allowlist(self.script)
        self.assertTrue(
            written <= allowed,
            f"policy writes {sorted(written - allowed)} which the drift guard would reject",
        )

    def test_guard_protects_the_required_grants(self):
        self.assertTrue(REQUIRED_GRANTS <= _allowlist(self.script))

    def test_kv_contents_are_not_seeded_from_git(self):
        # The point of the feature is that a credential added in OpenBao reaches
        # dsh with no per-machine step. A `kv patch` here would make the Job
        # reassert git contents daily and defeat that.
        code = _code(self.script)
        self.assertNotIn("kv patch", code)
        self.assertIn("-cas=0", code)

    def test_auth_role_is_asserted_not_written(self):
        # A role bound to the right ServiceAccount but carrying the wrong
        # policies authenticates fine and then reads nothing.
        code = _code(self.script)
        self.assertNotIn("bao write auth/kubernetes/role", code)
        self.assertIn("token_policies", code)


class JobShape(unittest.TestCase):
    def setUp(self):
        self.job = [
            d for d in yaml.safe_load_all(MANIFEST.read_text()) if d and d.get("kind") == "Job"
        ][0]

    def test_breakglass_secret_is_not_optional(self):
        env = self.job["spec"]["template"]["spec"]["containers"][0]["env"]
        tok = [e for e in env if e["name"] == "BAO_TOKEN"][0]
        self.assertIs(tok["valueFrom"]["secretKeyRef"]["optional"], False)

    def test_force_annotation_present(self):
        # A Job's spec.template is immutable; without this a script edit fails
        # "field is immutable" and takes the whole openbao Kustomization False.
        self.assertEqual(
            self.job["metadata"]["annotations"].get("kustomize.toolkit.fluxcd.io/force"),
            "enabled",
        )


if __name__ == "__main__":
    unittest.main()
