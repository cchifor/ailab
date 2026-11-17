"""Offline tests for scripts/relay-admin-call.py (no network): login-form parsing, redaction, the
allowlist and argument validation. The live flow is exercised by `relay-operator.py call me`."""
import importlib.util
import io
import pathlib
import sys
import unittest
from unittest import mock

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "relay-admin-call.py"
_spec = importlib.util.spec_from_file_location("relay_admin_call", _MOD_PATH)
rac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rac)  # no I/O at import time

LOGIN_PAGE = """<html><body>
<div class="pf-v5-c-alert kc-feedback-text"><span id="input-error">Invalid username or password.</span></div>
<form id="kc-form-login" onsubmit="return true;"
 action="https://id.chifor.me/realms/relay/login-actions/authenticate?session_code=abc&amp;execution=e1&amp;client_id=relay&amp;tab_id=t1"
 method="post">
<input tabindex="1" id="username" name="username" type="text" autofocus>
<input id="password" name="password" type="password">
<input type="hidden" id="id-hidden-input" name="credentialId" value="">
<input type="hidden" name="rememberMe" value="off">
</form></body></html>"""


class LoginForm(unittest.TestCase):
    def test_action_unescaped_and_hidden_fields_kept(self):
        f = rac._LoginForm()
        f.feed(LOGIN_PAGE)
        self.assertEqual(
            f.action,
            "https://id.chifor.me/realms/relay/login-actions/authenticate?session_code=abc&execution=e1&client_id=relay&tab_id=t1")
        self.assertEqual(f.fields, {"credentialId": "", "rememberMe": "off"})  # visible inputs are not copied
        self.assertIn("Invalid username or password", f.feedback)

    def test_no_form(self):
        f = rac._LoginForm()
        f.feed("<html><form id='kc-update-profile-form' action='x'></form></html>")
        self.assertIsNone(f.action)


class Redaction(unittest.TestCase):
    def test_secret_and_generation_fields_redacted_recursively(self):
        out = rac.redact({"enrollmentToken": "SECRET", "recoveryGeneration": "GEN",
                          "items": [{"token": "T2", "state": "ready"}], "nullToken": None})
        flat = repr(out)
        for s in ("SECRET", "GEN", "T2"):
            self.assertNotIn(s, flat)
        self.assertEqual(out["items"][0]["state"], "ready")


class Arguments(unittest.TestCase):
    def run_main(self, *argv, stdin="pw\n"):
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["relay-admin-call.py", *argv]), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch.object(rac, "login", side_effect=AssertionError("must not reach login")):
            with self.assertRaises(SystemExit) as cm:
                rac.main()
        return cm.exception.code, err.getvalue()

    def test_op_outside_allowlist_refused(self):
        code, err = self.run_main("--username", "u@x", "approve-installation")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_bad_id_and_ttl_refused(self):
        self.assertEqual(self.run_main("--username", "u@x", "get-installation", "../../admin")[0], 2)
        self.assertEqual(self.run_main("--username", "u@x", "enrollment-token",
                                       "b380ff7f-0716-478d-8b01-3d39f30e750b", "--ttl", "5")[0], 2)

    def test_empty_password_refused(self):
        self.assertEqual(self.run_main("--username", "u@x", "me", stdin="\n")[0], 2)


if __name__ == "__main__":
    unittest.main()
