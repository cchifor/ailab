"""Offline tests for scripts/relay-operator.py: the just-in-time admin grant is always inside the cleanup
scope, the cleanup never raises, and the subject never enters SQL text. No kubectl, docker or network."""
import importlib.util
import io
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "relay-operator.py"
_spec = importlib.util.spec_from_file_location("relay_operator", _MOD_PATH)
ro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ro)  # no I/O at import time

SUBJECT = "https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f"
UID = "72e99c0a-fce0-42f1-8e2a-af1363b5aa4f"
CREDS = {"subject": SUBJECT, "username": "relay-operator@ailab.invalid", "password": "SENTINEL_PW_do_not_leak",
         "keycloak_user_id": UID}


class CallCleanup(unittest.TestCase):
    def setUp(self):
        self.revoke = mock.patch.object(ro, "revoke", return_value=ro.REVOKED).start()
        self.end_sso = mock.patch.object(ro, "end_sso").start()
        mock.patch.object(ro, "creds", return_value=dict(CREDS)).start()
        mock.patch.object(sys, "stderr", io.StringIO()).start()
        # `call` holds the fenced Lease (relay_operator_session.py); these tests are about the grant/revoke
        # cleanup, so the Lease is faked as always ours (its own behaviour is tested in test_relay_operator_session)
        ros = ro._session_module()
        mock.patch.object(ros, "lease_acquire", return_value=None).start()
        mock.patch.object(ros, "lease_secure", return_value=True).start()
        mock.patch.object(ros, "lease_release", return_value=True).start()
        mock.patch.object(ro, "run", return_value=b"").start()  # the named-container removal: no real docker
        self.addCleanup(mock.patch.stopall)

    def test_grant_failure_still_revokes(self):
        # member.js may commit the membership and still report a transport error
        mock.patch.object(ro, "grant", side_effect=SystemExit("kubectl exec failed")).start()
        self.assertEqual(ro.call(["me"]), 1)
        # the orphan clear before the grant, then the cleanup's revoke (relay-operator.py `call`)
        self.assertEqual(self.revoke.call_args_list, [mock.call(SUBJECT), mock.call(SUBJECT)])
        self.end_sso.assert_called_once_with(UID)

    def test_grant_failure_with_unconfirmed_revoke_exits_3(self):
        # the cleanup's verdict is not lost behind the grant's own failure
        grant = mock.patch.object(ro, "grant", side_effect=SystemExit("kubectl exec failed")).start()
        self.revoke.side_effect = [ro.REVOKED, "1 memberships, 0 sessions"]
        self.assertEqual(ro.call(["me"]), 3)
        grant.assert_called_once()
        self.assertEqual(self.revoke.call_args_list, [mock.call(SUBJECT), mock.call(SUBJECT)])

    def test_count_failure_after_grant_still_revokes(self):
        mock.patch.object(ro, "grant").start()
        mock.patch.object(ro, "counts", side_effect=SystemExit("psql failed")).start()
        self.assertEqual(ro.call(["me"]), 1)
        # the orphan clear before the grant, then the cleanup's revoke (relay-operator.py `call`)
        self.assertEqual(self.revoke.call_args_list, [mock.call(SUBJECT), mock.call(SUBJECT)])
        self.end_sso.assert_called_once_with(UID)

    def test_normal_call_revokes_and_ends_sso(self):
        mock.patch.object(ro, "grant").start()
        mock.patch.object(ro, "counts", return_value="1 memberships, 0 sessions").start()
        done = subprocess.CompletedProcess([], 0, stdout=b"{}", stderr=b"")
        run = mock.patch.object(ro.subprocess, "run", return_value=done).start()
        out = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=out)):
            self.assertEqual(ro.call(["me"]), 0)
        # the orphan clear before the grant, then the cleanup's revoke (relay-operator.py `call`)
        self.assertEqual(self.revoke.call_args_list, [mock.call(SUBJECT), mock.call(SUBJECT)])
        self.end_sso.assert_called_once_with(UID)
        self.assertNotIn("SENTINEL_PW", " ".join(run.call_args.args[0]))  # the password is never in argv
        self.assertEqual(run.call_args.kwargs["input"], b"SENTINEL_PW_do_not_leak\n")

    def test_unconfirmed_revoke_is_rc3_and_failed_sso_is_rc5(self):
        self.revoke.return_value = "1 memberships, 0 sessions"
        self.assertEqual(ro.cleanup(SUBJECT, UID, 0), 3)
        self.revoke.return_value = ro.REVOKED
        self.end_sso.side_effect = SystemExit("kcadm failed")
        self.assertEqual(ro.cleanup(SUBJECT, UID, 0), 5)

    def test_cleanup_never_raises_when_revoke_fails(self):
        self.revoke.side_effect = SystemExit("psql down")
        self.assertEqual(ro.cleanup(SUBJECT, UID, 0), 3)
        self.end_sso.assert_called_once_with(UID)


class SqlParameters(unittest.TestCase):
    def test_subject_is_a_psql_variable_not_sql_text(self):
        evil = "x'); DROP TABLE users; --"
        with mock.patch.object(ro, "run", return_value=b"0 memberships, 0 sessions\n") as run:
            ro.revoke(evil)
        for call in run.call_args_list:
            argv, sql = call.args[0], call.args[1].decode()
            self.assertNotIn(evil, sql)
            self.assertIn(":'subject'", sql)
            self.assertIn(f"subject={evil}", argv)


class AdminCallLogoutRc(unittest.TestCase):
    def test_unclosed_logout_sets_rc4(self):
        spec = importlib.util.spec_from_file_location(
            "relay_admin_call", pathlib.Path(__file__).resolve().parents[1] / "relay-admin-call.py")
        rac = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rac)
        with mock.patch.object(sys, "argv", ["relay-admin-call.py", "--username", "u@x", "me"]), \
                mock.patch.object(sys, "stdin", io.StringIO("pw\n")), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.object(rac, "login"), \
                mock.patch.object(rac.Client, "api", return_value=(200, {"role": "admin"})), \
                mock.patch.object(rac, "logout", return_value=("logout 403 (NOT CLOSED)", False)):
            self.assertEqual(rac.main(), 4)


if __name__ == "__main__":
    unittest.main()
