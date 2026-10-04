#!/usr/bin/env python3
"""Two-gate OIDC clients never show a consent screen (kubernetes/apps/apps/auth/configuration.yml).

WHY THIS EXISTS. A host behind Cloudflare Access whose app ALSO does its own Authelia login makes
two OIDC round trips per visit: one for the Access gate (its IdP is an Authelia client whose
redirect_uri is the Zero Trust team domain) and one for the app. Both are the estate's own relying
parties, so a consent screen protects nothing — but a client that omits `consent_mode` gets
Authelia's default (`auto` = explicit without a pre-configured duration) and asks on EVERY login.

That is what made trueswarm-admin "ask twice": on 2026-10-03 Authelia's oauth2_consent_session
held 27 human-answered consents for `cloudflare-trueswarm-admin` and 29 for `trueswarm-admin` in
seven days, one pair per hourly Access re-check. The general `cloudflare-access` client had
already learned this ("no consent prompt between the two gates"); the admin clients were added
later without it. Nothing in kubeconform or Authelia's startup validation notices, so this pins it.

Run:

    python3 -m unittest scripts.tests.test_authelia_oidc_consent -v
"""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / "kubernetes" / "apps" / "apps" / "auth" / "configuration.yml"
ACCESS_CALLBACK_HOST = "cloudflareaccess.com/cdn-cgi/access/callback"

# Apps that sit BEHIND a Cloudflare Access gate and run their own Authelia login as well. Nothing in
# configuration.yml marks an app client as gated (the gate is in kubernetes/infra/cloudflare), so
# this cannot be auto-discovered like the gate IdPs above: ADD THE CLIENT HERE when you put another
# app with its own Authelia login behind Access, or this test will not catch the omission.
GATED_APP_CLIENTS = {"trueswarm-admin"}


def clients():
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    listed = config["identity_providers"]["oidc"]["clients"]
    by_id = {c["client_id"]: c for c in listed}
    # A duplicate client_id would silently shadow its earlier entry here (Authelia rejects it at
    # startup, but fail loudly in CI rather than check only one of the two).
    assert len(by_id) == len(listed), "duplicate client_id in configuration.yml"
    return by_id


class TwoGateConsent(unittest.TestCase):
    def test_every_access_gate_client_is_implicit(self):
        gates = {cid: c for cid, c in clients().items()
                 if any(ACCESS_CALLBACK_HOST in uri for uri in c.get("redirect_uris", []))}
        self.assertIn("cloudflare-access", gates)
        self.assertIn("cloudflare-trueswarm-admin", gates)
        for cid, client in gates.items():
            with self.subTest(client=cid):
                self.assertEqual(client.get("consent_mode"), "implicit",
                                 f"{cid} is a Cloudflare Access IdP: an explicit consent screen "
                                 "would be shown on every Access re-check")

    def test_every_gated_app_client_is_implicit(self):
        known = clients()
        for cid in sorted(GATED_APP_CLIENTS):
            with self.subTest(client=cid):
                self.assertIn(cid, known, f"{cid} vanished from configuration.yml; update this test")
                self.assertEqual(known[cid].get("consent_mode"), "implicit",
                                 f"{cid} is behind an Access gate: its login is the SECOND OIDC "
                                 "round trip of the visit and must be silent")


if __name__ == "__main__":
    unittest.main()
