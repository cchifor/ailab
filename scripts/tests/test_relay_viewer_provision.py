"""scripts/relay-viewer-provision.py: the seed edit, the shim's order, and verify's invariants."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("relay_viewer", ROOT / "scripts" / "relay-viewer-provision.py")
rv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rv)

DOC = "\n".join([
    "# header comment",
    "stringData:",
    "    common.json: '{\"a\": \"x\"}'",
    "    dev-worker-4.json: '{\"gitea_pat\": \"it''s\"}'",
    "    dev-worker-5.json: \"{\\\"b\\\": 1}\"",
    "sops:",
    "    version: 3.9.4",
    "",
])

HEALTHY = {
    "seedUser": rv.EMAIL, "passwordLength": 43,
    "keycloakUser": {"username": rv.EMAIL, "enabled": True, "emailVerified": True, "requiredActions": []},
    "realmRoles": ["default-roles-relay"], "membership": "viewer", "login": 200,
}


class EditSeed(unittest.TestCase):
    def test_sets_fields_in_one_scope_and_keeps_every_other_line(self):
        out = rv.edit_seed(DOC, "dev-worker-4.json", {"relay_viewer_user": "u", "relay_viewer_password": "p"})
        before, after = DOC.split("\n"), out.split("\n")
        self.assertEqual(len(before), len(after))
        self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b], [3])
        _, seed, quote = rv.seed_line(out, "dev-worker-4.json")
        self.assertEqual(quote, "'")
        self.assertEqual(seed, {"gitea_pat": "it's", "relay_viewer_user": "u", "relay_viewer_password": "p"})

    def test_double_quoted_scope_round_trips(self):
        out = rv.edit_seed(DOC, "dev-worker-5.json", {"c": 2})
        _, seed, quote = rv.seed_line(out, "dev-worker-5.json")
        self.assertEqual((quote, seed), ('"', {"b": 1, "c": 2}))

    def test_a_missing_or_duplicate_scope_refuses(self):
        with self.assertRaises(SystemExit):
            rv.seed_line(DOC, "dev-worker-9.json")
        with self.assertRaises(SystemExit):
            rv.seed_line(DOC + "    common.json: '{}'\n", "common.json")

    def test_an_unquoted_scalar_refuses(self):
        with self.assertRaises(SystemExit):
            rv.seed_line("    x.json: {}\n", "x.json")


class Shim(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(DOC)
        self.addCleanup(os.unlink, self.path)

    def seeded_password(self):
        with open(self.path, encoding="utf-8") as f:
            return rv.seed_line(f.read(), rv.SCOPE)[1].get("relay_viewer_password")

    def test_the_seed_is_written_before_keycloak_gets_the_same_password(self):
        seen = {}

        def kc(*args, stdin=None):
            seen["file_password_at_call"] = self.seeded_password()
            seen["sent"] = rv.json.loads(stdin)["value"]
            return b""
        with mock.patch.object(rv, "need_uid", return_value="u1"), mock.patch.object(rv, "kc", side_effect=kc):
            rv.shim(self.path)
        self.assertIsNotNone(seen["file_password_at_call"])
        self.assertEqual(seen["file_password_at_call"], seen["sent"])

    def test_a_keycloak_refusal_fails_the_editor_so_sops_discards_the_buffer(self):
        with mock.patch.object(rv, "need_uid", return_value="u1"), \
                mock.patch.object(rv, "kc", side_effect=SystemExit("relay-viewer: refused")):
            with self.assertRaises(SystemExit):
                rv.shim(self.path)

    def test_an_unexpected_file_refuses_before_keycloak(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("stringData:\n    other.json: '{}'\n")
        kc = mock.Mock()
        with mock.patch.object(rv, "need_uid", return_value="u1"), mock.patch.object(rv, "kc", kc):
            with self.assertRaises(SystemExit):
                rv.shim(self.path)
        kc.assert_not_called()


class Check(unittest.TestCase):
    def test_a_healthy_account_passes(self):
        self.assertEqual(rv.check(HEALTHY), [])

    def test_each_broken_invariant_fails(self):
        cases = {
            "rejected login": {"login": 401},
            "unreachable": {"login": "unreachable (timed out)"},
            "absent membership": {"membership": None},
            "elevated membership": {"membership": "admin"},
            "failed membership query": {"membership": "query failed: x"},
            "extra realm role": {"realmRoles": ["default-roles-relay", "admin"]},
            "roles unknown": {"realmRoles": None},
            "short password": {"passwordLength": 0},
            "wrong seeded user": {"seedUser": "someone@else"},
            "disabled user": {"keycloakUser": {**HEALTHY["keycloakUser"], "enabled": False}},
            "required action": {"keycloakUser": {**HEALTHY["keycloakUser"], "requiredActions": ["UPDATE_PASSWORD"]}},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertNotEqual(rv.check({**HEALTHY, **change}), [], name)


class Main(unittest.TestCase):
    def test_usage_without_arguments_or_without_a_shim_path(self):
        for argv in (["p"], ["p", "shim"], ["p", "nope"]):
            with self.subTest(argv), self.assertRaises(SystemExit) as e:
                rv.main(argv)
            self.assertIn("usage", str(e.exception))


if __name__ == "__main__":
    unittest.main()
