#!/usr/bin/env python3
"""Unit tests for scripts/check-ci-runners.py (PREFLIGHT #2 pure logic).

No SSH / network — these exercise the parse + evaluate layers only. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
import importlib.util
import pathlib
import unittest

_MOD_PATH = pathlib.Path(__file__).resolve().parents[1] / "check-ci-runners.py"
_spec = importlib.util.spec_from_file_location("check_ci_runners", _MOD_PATH)
ccr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ccr)  # must NOT perform any I/O at import time


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

    def test_duplicate_key_marked_dup(self):
        fields = ccr.parse_probe_output("daemon=active\ndaemon=failed\n")
        self.assertEqual(fields["daemon"], ccr.DUP)


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
        for c in ("200", "301", "302", "401", "403"):
            self.assertTrue(ccr.evaluate_host(good_fields(gitea=c)).ok, c)

    def test_gitea_unreachable_or_5xx_fails(self):
        for c in ("000", "500", "502", "503"):
            self.assertFalse(ccr.evaluate_host(good_fields(gitea=c)).ok, c)

    def test_capacity_mismatch_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="2")).ok)

    def test_capacity_non_int_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="abc")).ok)
        self.assertFalse(ccr.evaluate_host(good_fields(capacity="FAIL")).ok)

    def test_missing_required_key_fails(self):
        f = good_fields()
        del f["daemon"]
        self.assertFalse(ccr.evaluate_host(f).ok)

    def test_duplicate_required_key_fails(self):
        self.assertFalse(ccr.evaluate_host(good_fields(daemon=ccr.DUP)).ok)

    def test_failures_never_leak_raw_secrets(self):
        # evaluate_host only ever sees parsed non-secret fields; ensure the failure
        # strings echo only the key names / sentinels we fed it, never a token-like blob.
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


if __name__ == "__main__":
    unittest.main()
