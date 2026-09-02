#!/usr/bin/env python3
"""Unit tests for PR C-P0-16: an `agentforge/**` retention policy in the Zot registry role.

Subject: ansible/roles/registry_zot/templates/config.json.j2 +
ansible/roles/registry_zot/defaults/main.yml.

WHY THIS EXISTS. agentforge's images.yml build-push started failing on every retry with
`error from registry: blob upload unknown to registry` while reads still answered 200 — the exact
symptom ADR 0014 / docs/runbooks/registry-cache.md attribute to a full zot store. The role already
has a `storage.retention` policy for `strive/**` (keep `latest` + the N most-recently-pushed
`sha-*` tags) and a catch-all `**` policy that keeps EVERY tag (deliberately, to protect
digest-pinned mirror/cache repos — see ADR 0014). Because the catch-all matches everything,
`agentforge/**` (orchestrator, sandbox, p1-worker, agentforge-platform: one bare `<short-sha>` tag
+ `latest` per merge, per agentforge/.gitea/workflows/images.yml) never had its tags pruned and the
repos grew unbounded (184/184/183/240 tags at incident time).

This test renders the REAL template with the REAL role defaults via python3-jinja2 (the runner's
system package, per .gitea/workflows/broker-inventory.yaml's own "Script unit tests" step comment
— no ansible install needed) and asserts:

  1. the rendered config is valid JSON (`json.loads`, the same check `python -m json.tool` performs);
  2. a `storage.retention` policy matching `agentforge/**` exists, and it precedes the `**`
     catch-all in `policies` (Zot evaluates policies in order — see the strive/** policy, which is
     ordered the same way) — a `**` policy anywhere before it would keep every agentforge tag;
  3. that policy sets `deleteUntagged: true`, keeps `^latest$`, and keeps the
     `registry_zot_agentforge_keep_recent` most-recently-pushed tags matching the BARE short-sha
     pattern agentforge's workflow actually pushes (`VER=$(git rev-parse --short HEAD)`, tagged as
     `<repo>:${VER}` with NO `sha-` prefix — unlike the strive/** policy's `^sha-.*`, so a
     copy-pasted `^sha-.*` pattern here would keep zero agentforge tags and this test pins that
     regex against real short-sha and `sha-`-prefixed strings);
  4. `registry_zot_agentforge_keep_recent` defaults to 40 (documented rationale: enough for the
     current + previous ailab digest pin plus rollback headroom — a GC'd build is
     un-rollback-able because ailab pins by digest);
  5. the strive/** policy is unchanged (still precedes the catch-all, still uses its own pattern)
     and the catch-all itself is unchanged (protects everything else, i.e. the mirror/cache repos).

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import json
import pathlib
import re
import unittest

import jinja2
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROLE_DIR = REPO_ROOT / "ansible" / "roles" / "registry_zot"
TEMPLATE_PATH = ROLE_DIR / "templates" / "config.json.j2"
DEFAULTS_PATH = ROLE_DIR / "defaults" / "main.yml"

#: Vars the template needs that are NOT in defaults/main.yml (they come from SOPS/group_vars at
#: real render time). Fake, non-secret placeholders — this test never touches the real secrets
#: store, matching manifest-lint.sh's "must NEVER decrypt anything" posture for this repo.
_EXTRA_VARS = {
    "registry_ci_password": "test-render-only-not-a-secret",
    "registry_oidc_client_secret": "test-render-only-not-a-secret",
}

# A real agentforge tag (images.yml: `git rev-parse --short HEAD`, default 7 hex chars, pushed as
# `<repo>:${VER}` with no prefix) vs. the strive-style `sha-<hex>` shape it must NOT match.
_BARE_SHORT_SHA = "b80a376"
_STRIVE_STYLE_TAG = "sha-b80a376"


def _load_defaults() -> dict:
    with DEFAULTS_PATH.open() as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"{DEFAULTS_PATH} did not parse to a mapping"
    return data


def _render_config() -> str:
    context = _load_defaults()
    context.update(_EXTRA_VARS)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    template = env.from_string(TEMPLATE_PATH.read_text())
    return template.render(**context)


def _policy_index(policies: list, repo_pattern: str) -> int:
    for i, policy in enumerate(policies):
        if policy.get("repositories") == [repo_pattern]:
            return i
    raise AssertionError(
        f"no retention policy with repositories == [{repo_pattern!r}] in {policies!r}"
    )


class RegistryZotRetentionTest(unittest.TestCase):
    def setUp(self):
        for p in (TEMPLATE_PATH, DEFAULTS_PATH):
            self.assertTrue(p.is_file(), f"missing: {p}")
        self.rendered_text = _render_config()

    def test_rendered_config_is_valid_json(self):
        # Equivalent to `python3 -m json.tool` (spec's verification step): json.loads raises
        # json.JSONDecodeError on malformed JSON, exactly as json.tool would exit non-zero.
        parsed = json.loads(self.rendered_text)
        self.assertIsInstance(parsed, dict)

    def test_agentforge_policy_precedes_catchall(self):
        parsed = json.loads(self.rendered_text)
        policies = parsed["storage"]["retention"]["policies"]
        agentforge_idx = _policy_index(policies, "agentforge/**")
        catchall_idx = _policy_index(policies, "**")
        self.assertLess(
            agentforge_idx,
            catchall_idx,
            "the agentforge/** retention policy must precede the ** catch-all "
            "(Zot evaluates storage.retention.policies in order) or every agentforge "
            "tag is kept forever, which is the bug this PR fixes",
        )

    def test_agentforge_policy_shape(self):
        parsed = json.loads(self.rendered_text)
        policies = parsed["storage"]["retention"]["policies"]
        policy = policies[_policy_index(policies, "agentforge/**")]

        self.assertTrue(
            policy["deleteUntagged"],
            "agentforge/** must reclaim untagged manifests (deleteUntagged: true) or GC never "
            "collects the blobs a pruned tag leaves behind",
        )

        self.assertTrue(
            any(entry.get("patterns") == ["^latest$"] for entry in policy["keepTags"]),
            f"agentforge/** must keep ^latest$ (every engine merge repushes it): {policy['keepTags']!r}",
        )

        recent_entries = [
            entry for entry in policy["keepTags"] if "mostRecentlyPushedCount" in entry
        ]
        self.assertEqual(
            len(recent_entries),
            1,
            f"expected exactly one mostRecentlyPushedCount entry in {policy['keepTags']!r}",
        )
        recent = recent_entries[0]

        # This is the regex the deployed defaults render, checked against real tag shapes rather
        # than re-deriving the same string the template contains (that would only prove the
        # template said what it said, not that it matches what images.yml actually pushes).
        self.assertEqual(len(recent["patterns"]), 1)
        compiled = re.compile(recent["patterns"][0])
        self.assertTrue(
            compiled.fullmatch(_BARE_SHORT_SHA),
            f"agentforge/** keepTags pattern {recent['patterns'][0]!r} must match a bare "
            f"short-sha tag like {_BARE_SHORT_SHA!r} (images.yml pushes VER=$(git rev-parse "
            "--short HEAD) with NO 'sha-' prefix, unlike strive/**)",
        )
        self.assertFalse(
            compiled.fullmatch(_STRIVE_STYLE_TAG),
            f"agentforge/** keepTags pattern {recent['patterns'][0]!r} must NOT also swallow "
            f"strive-style {_STRIVE_STYLE_TAG!r} tags (that would be the wrong repo's shape)",
        )
        self.assertFalse(
            compiled.fullmatch("latest"),
            "the short-sha keepTags pattern must not also match the literal tag 'latest'",
        )

        defaults = _load_defaults()
        expected_n = defaults["registry_zot_agentforge_keep_recent"]
        self.assertEqual(
            recent["mostRecentlyPushedCount"],
            expected_n,
            "the rendered mostRecentlyPushedCount must come from the "
            "registry_zot_agentforge_keep_recent knob",
        )

    def test_agentforge_keep_recent_default_is_40(self):
        defaults = _load_defaults()
        self.assertIn(
            "registry_zot_agentforge_keep_recent",
            defaults,
            "defaults/main.yml must declare registry_zot_agentforge_keep_recent",
        )
        self.assertEqual(
            defaults["registry_zot_agentforge_keep_recent"],
            40,
            "40 covers the current + previous ailab digest pin plus rollback headroom "
            "(too low silently makes a live digest un-rollback-able — see the defaults comment)",
        )

    def test_defaults_comment_explains_the_knob(self):
        # The strive knob's rationale is explained inline (defaults/main.yml, "Was 25 — too
        # shallow..."); the spec asks for the agentforge knob to be explained the same way,
        # including that a GC'd build is un-rollback-able because ailab pins by digest.
        text = DEFAULTS_PATH.read_text()
        idx = text.find("registry_zot_agentforge_keep_recent")
        self.assertGreater(idx, -1)
        # Look at the comment block immediately preceding the first (declaration) occurrence.
        preceding = text[:idx]
        comment_lines = []
        for line in reversed(preceding.splitlines()):
            stripped = line.strip()
            if stripped.startswith("#"):
                comment_lines.append(stripped)
                continue
            break
        comment = "\n".join(reversed(comment_lines))
        self.assertIn(
            "digest",
            comment.lower(),
            "the registry_zot_agentforge_keep_recent comment must explain the digest-pin / "
            "rollback rationale (mirrors the registry_zot_strive_keep_recent comment above it)",
        )
        self.assertIn(
            "rollback",
            comment.lower(),
            "the registry_zot_agentforge_keep_recent comment must mention rollback headroom",
        )

    def test_strive_and_catchall_policies_unchanged(self):
        parsed = json.loads(self.rendered_text)
        policies = parsed["storage"]["retention"]["policies"]

        strive_idx = _policy_index(policies, "strive/**")
        catchall_idx = _policy_index(policies, "**")
        self.assertLess(strive_idx, catchall_idx)

        catchall = policies[catchall_idx]
        self.assertFalse(catchall["deleteUntagged"])
        self.assertEqual(catchall["keepTags"], [{"patterns": [".*"]}])


if __name__ == "__main__":
    unittest.main()
