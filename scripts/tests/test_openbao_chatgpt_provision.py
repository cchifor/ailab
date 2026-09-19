#!/usr/bin/env python3
"""Gates for the OpenBao ChatGPT-subscription provisioning Job (ADR 0026).

WHY THIS EXISTS. The Job replaces TWO live OpenBao policies with a breakglass
token and writes an auth role. The failure shapes that matter are invisible to
`kustomize build` or `kubeconform`:

  * IT CAN WEDGE ITSELF. Each guard accepts only the reviewed baseline and the
    document the script itself writes. If a DESIRED constant drifts from its
    heredoc, the Job writes the heredoc once and then refuses every later run,
    reading back its own output as unreviewed drift.

  * IT CAN BREAK THE PUBLISHER. `dsh-codex-publisher` is the reviewer host's
    AppRole policy: it was hand-installed with `patch` on af/dsh/credentials
    and this Job takes it over. Dropping that grant stops the DSH Codex token
    projection -- silently, until the token expires days later. The grant must
    be in the heredoc, asserted here rather than assumed.

  * IT CAN LEAVE THE ESO TEMPLATE UNRENDERABLE. The ExternalSecret template
    references four CHATGPT_* keys, and ESO v2 errors on an absent key, so the
    first `kv put` must seed every one of them.

Run:

    python3 -m unittest scripts.tests.test_openbao_chatgpt_provision -v
"""
import os
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
    / "chatgpt-provision-job.yaml"
)

#: The publisher grants that must survive any rewrite (the live projection).
PUBLISHER_REQUIRED = {"af/data/dsh/credentials", "af/metadata/dsh/credentials"}
#: The grants the feature adds.
PUBLISHER_FEATURE = {"af/data/litellm/chatgpt", "af/metadata/litellm/chatgpt"}
#: The reader's single grant.
APP_GRANT = "af/data/litellm/chatgpt"
#: Every key the ExternalSecret template references; all must be seeded.
TEMPLATE_KEYS = ("CHATGPT_ACCESS_TOKEN", "CHATGPT_ACCOUNT_ID", "CHATGPT_ACCOUNT_EMAIL", "CHATGPT_EXPIRES_AT")

SHIM_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "openbao-chatgpt-provision"

#: The publisher policy exactly as the 2026-09-17 hand ceremony installed it and
#: as `bao policy read dsh-codex-publisher` returned it on 2026-09-19 -- comment
#: lines included, which is why the guard strips comments before comparing.
LIVE_PUBLISHER = (
    "# The publisher cannot read DSH's credential values or replace/delete the document.\n"
    "# OpenBao ACLs apply per document: patch permits any field in this one document.\n"
    'path "af/data/dsh/credentials" { capabilities = ["patch"] }\n'
    'path "af/metadata/dsh/credentials" { capabilities = ["read"] }\n'
)


def _script():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap":
            return doc["data"]["provision.sh"]
    raise AssertionError("no ConfigMap carrying provision.sh")


def _code(script):
    """The script with full-line comments removed, so assertions about what it
    DOES are not satisfied or broken by what it SAYS."""
    return "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))


def _heredocs(script):
    """The bodies of the `bao policy write` heredocs, in order (publisher, app)."""
    bodies = []
    for chunk in script.split("<<'EOF'")[1:]:
        bodies.append(chunk.split("\nEOF", 1)[0].strip().splitlines())
    return bodies


def _paths(lines):
    return set(re.findall(r'path "([^"]+)"', "\n".join(lines)))


def _normalised(text):
    kept = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    return "".join("\n".join(kept).split())


def _guard_constant(script, name):
    m = re.search(rf"^{name}='([^']*)'", script, re.M)
    assert m, f"{name} not found in the script"
    return m.group(1)


class ProvisionScript(unittest.TestCase):
    def setUp(self):
        self.script = _script()
        self.publisher_body, self.app_body = _heredocs(self.script)

    def test_shell_parses(self):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no /bin/sh")
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(self.script)
            f.flush()
            proc = subprocess.run([sh, "-n", f.name], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_publisher_keeps_its_live_grants_and_gains_the_feature(self):
        self.assertTrue(PUBLISHER_REQUIRED <= _paths(self.publisher_body))
        self.assertTrue(PUBLISHER_FEATURE <= _paths(self.publisher_body))

    def test_app_policy_is_read_only_on_one_path(self):
        self.assertEqual(_paths(self.app_body), {APP_GRANT})
        self.assertNotIn("list", "\n".join(self.app_body))
        self.assertNotIn("patch", "\n".join(self.app_body))

    def test_desired_constants_match_what_the_heredocs_write(self):
        # THE self-wedge invariant, for both policies.
        self.assertEqual(
            _normalised("\n".join(self.publisher_body)),
            _guard_constant(self.script, "PUBLISHER_DESIRED"),
        )
        self.assertEqual(
            _normalised("\n".join(self.app_body)),
            _guard_constant(self.script, "APP_DESIRED"),
        )

    def test_publisher_baseline_is_the_live_policy(self):
        # The pre-ownership arm has to match what is in the vault (comments stripped) or the very
        # first run refuses.
        self.assertEqual(
            _guard_constant(self.script, "PUBLISHER_BASELINE"), _normalised(LIVE_PUBLISHER)
        )

    def test_guard_strips_comments_then_whitespace(self):
        self.assertIn("tr -d '[:space:]'", self.script)
        self.assertIn("sed -e 's/^[[:space:]]*#.*$//'", self.script)
        self.assertNotIn("grep -oE 'path", self.script)

    def test_maintenance_rule_is_stated_beside_the_constants(self):
        # No test can enforce "move the outgoing DESIRED into BASELINE" across revisions; the
        # file must at least SAY it where the editor will look.
        self.assertIn("MAINTENANCE RULE", self.script)
        self.assertIn("OUTGOING", self.script)

    def test_kv_seeds_every_template_key_and_nothing_else_later(self):
        code = _code(self.script)
        self.assertNotIn("kv patch", code)
        self.assertIn("-cas=0", code)
        put = code.split("kv put", 1)[1].split("2>&1", 1)[0]
        for key in TEMPLATE_KEYS:
            self.assertIn(f"{key}=", put)

    def test_auth_role_is_written_with_the_policy(self):
        code = _code(self.script)
        self.assertIn("bao write auth/kubernetes/role/af-app-litellm", code.replace('"$ROLE"', "auth/kubernetes/role/af-app-litellm"))
        self.assertIn("bound_service_account_names=litellm-eso", code)
        self.assertIn("bound_service_account_namespaces=ai", code)
        self.assertIn("alias_name_source=serviceaccount_uid", code)
        for field in ("token_policies", "bound_service_account_names", "bound_service_account_namespaces"):
            self.assertIn(f"role_field_has {field}", code)


class JobShape(unittest.TestCase):
    def setUp(self):
        self.job = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d and d.get("kind") == "Job"][0]

    def test_breakglass_secret_is_not_optional(self):
        env = self.job["spec"]["template"]["spec"]["containers"][0]["env"]
        tok = [e for e in env if e["name"] == "BAO_TOKEN"][0]
        self.assertIs(tok["valueFrom"]["secretKeyRef"]["optional"], False)

    def test_force_annotation_present(self):
        self.assertEqual(
            self.job["metadata"]["annotations"].get("kustomize.toolkit.fluxcd.io/force"),
            "enabled",
        )

    def test_listed_in_the_kustomization(self):
        kustomization = yaml.safe_load((MANIFEST.parent / "kustomization.yaml").read_text())
        self.assertIn("chatgpt-provision-job.yaml", kustomization["resources"])


class GuardBehaviour(unittest.TestCase):
    """Run provision.sh against a fake `bao` and assert what it actually does."""

    #: Sentinel: "the app policy already holds this Job's own output" -- the steady state, and
    #: what the shim's default policy LIST (which names af-app-litellm) has to be able to read.
    APP_AS_WRITTEN = object()

    def _run(self, publisher=LIVE_PUBLISHER, app=APP_AS_WRITTEN, **fake):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no /bin/sh")
        if app is self.APP_AS_WRITTEN:
            app = "\n".join(_heredocs(_script())[1]) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            state = work / "state"
            state.mkdir()
            if publisher is not None:
                (state / "policy-dsh-codex-publisher").write_text(publisher)
            if app is not None:
                (state / "policy-af-app-litellm").write_text(app)
            script = work / "provision.sh"
            script.write_text(_script())
            log = work / "log"
            log.touch()
            env = {
                "PATH": f"{SHIM_DIR}:{os.environ['PATH']}",
                "FAKE_LOG": str(log),
                "FAKE_STATE": str(state),
                "HOME": str(work),
            }
            env.update({k: str(v) for k, v in fake.items()})
            proc = subprocess.run([sh, str(script)], capture_output=True, text=True, env=env, timeout=60)
            return proc, log.read_text()

    def assertNoPolicyWrite(self, log):
        self.assertNotIn("policy write", log, "the guard failed but a policy was replaced anyway")

    # --- the states the guards must accept -----------------------------------
    def test_first_run_over_the_live_publisher_policy_proceeds(self):
        # app policy absent from the vault, publisher at its hand-installed form (with comments).
        proc, log = self._run(app=None, FAKE_POLICY_LIST='["default","dsh-codex-publisher"]', FAKE_KV_EXISTS=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write dsh-codex-publisher", log)
        self.assertIn("policy write af-app-litellm", log)
        self.assertIn("write auth/kubernetes/role/af-app-litellm", log)
        self.assertIn("kv put", log)
        for key in TEMPLATE_KEYS:
            self.assertIn(f"{key}=", log)

    def test_second_run_over_its_own_output_proceeds_without_kv_write(self):
        publisher_desired = "\n".join(_heredocs(_script())[0]) + "\n"
        app_desired = "\n".join(_heredocs(_script())[1]) + "\n"
        proc, log = self._run(publisher=publisher_desired, app=app_desired, FAKE_KV_EXISTS=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write dsh-codex-publisher", log)
        self.assertNotIn("kv put", log)

    def test_reworded_comments_on_the_live_policy_still_proceed(self):
        # A comment is not a grant; the guard must reject DRIFT, not prose.
        reworded = LIVE_PUBLISHER.replace("The publisher cannot read", "  # the publisher may not read")
        proc, log = self._run(publisher=reworded, app=None, FAKE_POLICY_LIST='["dsh-codex-publisher"]', FAKE_KV_EXISTS=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write", log)

    # --- the drift cases, which are the point --------------------------------
    def test_added_capability_on_the_publisher_aborts_before_any_write(self):
        drifted = LIVE_PUBLISHER.replace(
            'path "af/data/dsh/credentials" { capabilities = ["patch"] }',
            'path "af/data/dsh/credentials" { capabilities = ["patch", "read"] }',
        )
        proc, log = self._run(publisher=drifted)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSING", proc.stderr)
        self.assertNoPolicyWrite(log)
        self.assertNotIn("write auth", log)

    def test_drifted_app_policy_aborts_before_the_publisher_is_written(self):
        # Both guards run before either write, so a refusal on the second never half-applies.
        proc, log = self._run(app='path "af/data/litellm/chatgpt" { capabilities = ["read", "list"] }\n')
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_removed_publisher_grant_aborts(self):
        proc, log = self._run(publisher='path "af/data/dsh/credentials" { capabilities = ["patch"] }\n')
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    # --- read failures must not look like absence ----------------------------
    def test_unreadable_existing_policy_aborts_without_writing(self):
        proc, log = self._run(publisher=None, FAKE_READ_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not be read", proc.stderr)
        self.assertNoPolicyWrite(log)

    def test_failed_policy_list_aborts_without_writing(self):
        proc, log = self._run(FAKE_LIST_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_absent_policies_are_created(self):
        proc, log = self._run(publisher=None, app=None, FAKE_POLICY_LIST='["default"]', FAKE_KV_EXISTS=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write dsh-codex-publisher", log)
        self.assertIn("policy write af-app-litellm", log)

    # --- the remaining fail-closed paths -------------------------------------
    def test_rejected_token_aborts(self):
        proc, log = self._run(FAKE_TOKEN_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_role_write_failure_aborts_before_touching_kv(self):
        proc, log = self._run(FAKE_ROLE_WRITE_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("kv put", log)

    def test_role_carrying_a_similarly_named_policy_aborts(self):
        proc, log = self._run(FAKE_ROLE_POLICIES="af-app-litellm-ro")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("kv put", log)

    def test_role_bound_to_another_service_account_or_namespace_aborts(self):
        # The read-back covers WHO may log in, not only what they may read.
        for fake in ({"FAKE_ROLE_SA": "dsh-eso"}, {"FAKE_ROLE_NS": "dsh"}):
            with self.subTest(**fake):
                proc, log = self._run(**fake)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("not bound", proc.stderr)
                self.assertNotIn("kv put", log)

    def test_soft_deleted_kv_path_fails_loudly(self):
        proc, log = self._run(FAKE_KV_DELETED=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("operator action needed", proc.stderr)
        # The CLI's own message rides along, so a permission error or a sealed vault is not
        # misreported as a soft-delete (reviewer-claude, #791).
        self.assertIn("check-and-set parameter did not match", proc.stderr)
        self.assertIn("kv put", log)
        self.assertNotIn("created", proc.stdout)

    def test_kv_put_failure_message_reaches_the_operator(self):
        proc, log = self._run(FAKE_KV_PUT_FAIL=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("permission denied", proc.stderr)


if __name__ == "__main__":
    unittest.main()
