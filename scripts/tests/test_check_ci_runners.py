#!/usr/bin/env python3
"""Unit tests for scripts/check-ci-runners.py (PREFLIGHT #2).

Pure-logic tests use no I/O; transport/main tests MOCK paramiko/urllib so still no real SSH/network. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "check-ci-runners.py"
_spec = importlib.util.spec_from_file_location("check_ci_runners", _MOD_PATH)
ccr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccr)  # must NOT perform any I/O at import time

TOKEN_SENTINEL = "SENTINEL_TOKEN_ab12cd34ef56_do_not_leak"  # asserted absent from all output/errors


def good_fields(**over):
    f = {
        "daemon": "active",
        "docker": "29.6.2",
        "label": "self-hosted-hv:host",
        "address": "https://git.chifor.me",
        "registry": "200",
        "gitea": "403",
        "capacity": "1",
    }
    f.update(over)
    return f


class ParseProbeOutput(unittest.TestCase):
    def test_basic_key_values(self):
        fields = ccr.parse_probe_output("daemon=active\ndocker=29.6.2\n")
        self.assertEqual(fields["daemon"], "active")
        self.assertEqual(fields["docker"], "29.6.2")

    def test_blank_and_malformed_lines_skipped(self):
        fields = ccr.parse_probe_output("\n  \nnot-a-kv-line\ndaemon=active\n")
        self.assertEqual(fields.get("daemon"), "active")
        self.assertNotIn("not-a-kv-line", fields)

    def test_value_may_contain_equals(self):
        fields = ccr.parse_probe_output("x=a=b\n")
        self.assertEqual(fields["x"], "a=b")

    def test_value_is_stripped(self):
        fields = ccr.parse_probe_output("daemon=  active  \n")
        self.assertEqual(fields["daemon"], "active")

    def test_duplicate_key_differing_value_marked_dup(self):
        fields = ccr.parse_probe_output("daemon=active\ndaemon=failed\n")
        self.assertEqual(fields["daemon"], ccr.DUP)

    def test_duplicate_key_identical_value_still_marked_dup(self):
        # fail-closed: a repeated key is ambiguous even when the value matches
        fields = ccr.parse_probe_output("capacity=1\ncapacity=1\n")
        self.assertEqual(fields["capacity"], ccr.DUP)


class EvaluateHost(unittest.TestCase):
    def test_all_good(self):
        r = ccr.evaluate_host(good_fields())
        self.assertTrue(r.ok, r.failures)
        self.assertEqual(r.failures, [])

    def test_daemon_inactive_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(daemon="inactive")).ok)

    def test_docker_empty_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(docker="")).ok)

    def test_docker_sentinels_fail(self):
        for v in ("FAIL", "SUDO_DENIED", "SOCKET_DENIED"):
            self.assertFalse(ccr.evaluate_host(good_fields(docker=v)).ok, v)

    def test_docker_must_look_like_version(self):
        self.assertFalse(ccr.evaluate_host(good_fields(docker="Cannot connect")).ok)

    def test_label_missing_host_schema_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(label="self-hosted-hv")).ok)

    def test_label_docker_schema_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(label="self-hosted-hv:docker://node:20")).ok)

    def test_label_present_among_others_ok(self):
        self.assertTrue(ccr.evaluate_host(good_fields(label="ubuntu-latest,self-hosted-hv:host")).ok)

    def test_address_trailing_slash_normalized_ok(self):
        self.assertTrue(ccr.evaluate_host(good_fields(address="https://git.chifor.me/")).ok)

    def test_address_wrong_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(address="https://evil.example")).ok)

    def test_address_empty_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(address="")).ok)

    def test_registry_non_200_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(registry="404")).ok)
        self.assertFalse(ccr.evaluate_host(good_fields(registry="000")).ok)

    def test_gitea_reachable_codes_ok(self):
        for c in ("200", "401", "403"):  # exactly the finalized policy set
            self.assertTrue(ccr.evaluate_host(good_fields(gitea=c)).ok, c)

    def test_gitea_redirect_or_5xx_or_unreachable_fails(self):
        # 3xx redirects are NOT accepted (fail-closed), nor 000/5xx
        for c in ("000", "301", "302", "404", "500", "502", "503"):
            self.assertFalse(ccr.evaluate_host(good_fields(gitea=c)).ok, c)

    def test_capacity_mismatch_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="2")).ok)

    def test_capacity_non_int_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="abc")).ok)
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="FAIL")).ok)

    def test_invalid_runner_file_fails(self):
        # remote python3 failing to parse .runner emits label=FAIL / address=FAIL (fail-closed)
        self.assertFalse(ccr.evaluate_host(good_fields(label="FAIL", address="FAIL")).ok)

    def test_missing_required_key_fails(self):
        f = good_fields()
        del f["daemon"]
        self.assertFalse(ccr.evaluate_host(f).ok)

    def test_duplicate_required_key_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(daemon=ccr.DUP)).ok)

    def test_failures_reference_only_field_names(self):
        # evaluate_host only ever sees parsed non-secret fields; failure strings echo the key names, not blobs
        r = ccr.evaluate_host(good_fields(address="https://evil.example"))
        self.assertTrue(any("address" in f for f in r.failures))


class EvaluateApi(unittest.TestCase):
    EXPECTED = {"ci-runner-1", "ci-runner-2", "ci-runner-3", "ci-runner-4", "ci-runner-5"}

    @staticmethod
    def runner(name, status="online"):
        return {"name": name, "status": status, "busy": False,
                "labels": [{"name": "self-hosted-hv", "type": "custom"}]}

    def all_online(self):
        return [self.runner(n) for n in sorted(self.EXPECTED)]

    def test_all_online_ok(self):
        r = ccr.evaluate_api(self.all_online(), self.EXPECTED)
        self.assertTrue(r.ok, r.failures)
        self.assertEqual(r.warnings, [])

    def test_busy_is_still_online_ok(self):
        runners = self.all_online()
        runners[0]["busy"] = True
        self.assertTrue(ccr.evaluate_api(runners, self.EXPECTED).ok)

    def test_expected_offline_fails(self):
        runners = self.all_online()
        runners[2]["status"] = "offline"
        r = ccr.evaluate_api(runners, self.EXPECTED)
        self.assertFalse(r.ok)
        self.assertTrue(any("ci-runner-3" in f for f in r.failures))

    def test_expected_missing_fails(self):
        runners = [self.runner(n) for n in sorted(self.EXPECTED) if n != "ci-runner-4"]
        r = ccr.evaluate_api(runners, self.EXPECTED)
        self.assertFalse(r.ok)
        self.assertTrue(any("ci-runner-4" in f for f in r.failures))

    def test_extra_stale_runner_warns_not_fails(self):
        runners = self.all_online() + [self.runner("old-hv-runner-9", status="offline")]
        r = ccr.evaluate_api(runners, self.EXPECTED)
        self.assertTrue(r.ok, r.failures)
        self.assertTrue(any("old-hv-runner-9" in w for w in r.warnings))

    def test_malformed_item_missing_status_fails(self):
        runners = self.all_online()
        del runners[1]["status"]
        self.assertFalse(ccr.evaluate_api(runners, self.EXPECTED).ok)

    def test_non_list_fails(self):
        self.assertFalse(ccr.evaluate_api(None, self.EXPECTED).ok)
        self.assertFalse(ccr.evaluate_api({"runners": []}, self.EXPECTED).ok)

    def test_duplicate_name_fails(self):
        # a stale duplicate registration could mask an offline one → schema failure
        runners = self.all_online() + [self.runner("ci-runner-2", status="offline")]
        r = ccr.evaluate_api(runners, self.EXPECTED)
        self.assertFalse(r.ok)
        self.assertTrue(any("duplicate" in f.lower() for f in r.failures))

    def test_non_string_name_or_status_fails(self):
        runners = self.all_online()
        runners[0]["name"] = 123
        self.assertFalse(ccr.evaluate_api(runners, self.EXPECTED).ok)
        runners = self.all_online()
        runners[0]["status"] = None
        self.assertFalse(ccr.evaluate_api(runners, self.EXPECTED).ok)


# ---- transport + main: mocked I/O (no real SSH/network); secret-redaction assertions ----
def _fake_http_resp(payload):
    """A context-manager stand-in for urlopen returning JSON `payload`."""
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    cm.__exit__.return_value = False
    return cm


class QueryGiteaRunnersTransport(unittest.TestCase):
    def test_validate_token_rejects_empty_or_whitespace(self):
        for bad in ("", "   ", "tok en", "tok\nen"):
            with self.assertRaises(RuntimeError):
                ccr._validate_token(bad)
        self.assertEqual(ccr._validate_token("  goodtoken  "), "goodtoken")

    def test_single_page(self):
        page = {"runners": [{"name": "ci-runner-1", "status": "online"}], "total_count": 1}
        with mock.patch.object(ccr._OPENER, "open", return_value=_fake_http_resp(page)) as op:
            runners = ccr.query_gitea_runners(TOKEN_SENTINEL, page_size=50)
        self.assertEqual(len(runners), 1)
        op.assert_called_once()  # short page → no second request

    def test_pagination_fetches_all_pages(self):
        full = [{"name": f"r{i}", "status": "online"} for i in range(3)]
        pages = [{"runners": full}, {"runners": [{"name": "r3", "status": "online"}]}]
        with mock.patch.object(ccr._OPENER, "open", side_effect=[_fake_http_resp(p) for p in pages]):
            runners = ccr.query_gitea_runners(TOKEN_SENTINEL, page_size=3)
        self.assertEqual([r["name"] for r in runners], ["r0", "r1", "r2", "r3"])

    def test_http_error_is_sanitized(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        self.addCleanup(err.close)
        with mock.patch.object(ccr._OPENER, "open", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                ccr.query_gitea_runners(TOKEN_SENTINEL)
        self.assertIn("403", str(cm.exception))
        self.assertNotIn(TOKEN_SENTINEL, str(cm.exception))

    def test_malformed_json_is_sanitized(self):
        buf = io.BytesIO(b"<html>redirect</html>")
        cm_resp = mock.MagicMock()
        cm_resp.__enter__.return_value = buf
        cm_resp.__exit__.return_value = False
        with mock.patch.object(ccr._OPENER, "open", return_value=cm_resp):
            with self.assertRaises(RuntimeError) as cm:
                ccr.query_gitea_runners(TOKEN_SENTINEL)
        self.assertNotIn(TOKEN_SENTINEL, str(cm.exception))

    def test_unexpected_exception_is_sanitized(self):
        # an exception outside the handled classes must NOT surface a verbatim message
        with mock.patch.object(ccr._OPENER, "open", side_effect=RuntimeError(TOKEN_SENTINEL)):
            with self.assertRaises(RuntimeError) as cm:
                ccr.query_gitea_runners(TOKEN_SENTINEL)
        self.assertNotIn(TOKEN_SENTINEL, str(cm.exception))

    def test_missing_runners_key_schema_fail(self):
        with mock.patch.object(ccr._OPENER, "open", return_value=_fake_http_resp({"total_count": 0})):
            with self.assertRaises(RuntimeError):
                ccr.query_gitea_runners(TOKEN_SENTINEL)


class MainIntegration(unittest.TestCase):
    """Drive main() with mocked transport; assert exit codes + that the token never reaches stdout."""

    GOOD_PROBE = ("daemon=active\ndocker=29.6.2\nlabel=self-hosted-hv:host\n"
                  "address=https://git.chifor.me\nregistry=200\ngitea=403\ncapacity=1\n")

    def _run(self, argv, env, probe=None, probe_exc=None, runners=None, query_exc=None):
        out = io.StringIO()
        pm = (mock.patch.object(ccr, "run_probe", side_effect=probe_exc) if probe_exc
              else mock.patch.object(ccr, "run_probe", return_value=probe or self.GOOD_PROBE))
        qm = (mock.patch.object(ccr, "query_gitea_runners", side_effect=query_exc) if query_exc
              else mock.patch.object(ccr, "query_gitea_runners", return_value=runners or []))
        with mock.patch.dict("os.environ", env, clear=True), pm, qm, contextlib.redirect_stdout(out):
            rc = ccr.main(argv)
        return rc, out.getvalue()

    def test_skip_api_all_good_passes(self):
        rc, text = self._run(["--skip-api", "192.168.0.14"], {})
        self.assertEqual(rc, 0)
        self.assertIn("PASS", text)

    def test_missing_token_fails_closed(self):
        rc, text = self._run(["192.168.0.14"], {})  # no GITEA_TOKEN in env
        self.assertEqual(rc, 1)
        self.assertIn("no GITEA_TOKEN", text)

    def test_unreachable_host_fails(self):
        rc, text = self._run(["--skip-api", "192.168.0.14"], {}, probe_exc=OSError("no route"))
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", text)

    def test_full_pass_and_token_never_printed(self):
        runners = [{"name": f"ci-runner-{i}", "status": "online"} for i in range(1, 6)]
        rc, text = self._run([], {"GITEA_TOKEN": TOKEN_SENTINEL}, runners=runners)
        self.assertEqual(rc, 0)
        self.assertIn("5/5 expected runners online", text)
        self.assertNotIn(TOKEN_SENTINEL, text)  # token must never reach stdout

    def test_api_error_token_never_printed(self):
        rc, text = self._run([], {"GITEA_TOKEN": TOKEN_SENTINEL},
                             query_exc=RuntimeError("Gitea API HTTP 403 (auth/scope?)"))
        self.assertEqual(rc, 1)
        self.assertNotIn(TOKEN_SENTINEL, text)

    def test_api_subpool_still_gates_full_pool(self):
        # probing one host but only 4 runners online -> API must still FAIL (whole pool is the gate)
        runners = [{"name": f"ci-runner-{i}", "status": "online"} for i in range(1, 5)]
        rc, text = self._run(["192.168.0.14"], {"GITEA_TOKEN": TOKEN_SENTINEL}, runners=runners)
        self.assertEqual(rc, 1)
        self.assertIn("ci-runner-5", text)


class HostKeyPolicy(unittest.TestCase):
    def test_run_probe_uses_autoadd_and_no_system_known_hosts(self):
        # assert the transport never loads/saves the user's known_hosts (idempotent trust state)
        fake_client = mock.MagicMock()
        stdout = mock.MagicMock()
        stdout.read.return_value = MainIntegration.GOOD_PROBE.encode()
        fake_client.exec_command.return_value = (mock.MagicMock(), stdout, mock.MagicMock())
        fake_paramiko = mock.MagicMock()
        fake_paramiko.SSHClient.return_value = fake_client
        with mock.patch.dict("sys.modules", {"paramiko": fake_paramiko}):
            ccr.run_probe("192.168.0.14")
        fake_client.load_system_host_keys.assert_not_called()
        fake_client.save_host_keys.assert_not_called()
        fake_client.set_missing_host_key_policy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
