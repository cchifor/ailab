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
`agentforge/**` never had its tags pruned and the four repos grew unbounded (184/184/183/240 tags
at incident time). The four repos do NOT all push the same tag shape (agentforge/
.gitea/workflows/images.yml vs. agentforge-platform/.gitea/workflows/images.yml, both re-checked
2026-09-02 after a review round found the first draft of this policy covered only three of the
four repos):
  - orchestrator, sandbox, p1-worker push ONE bare `<short-sha>` tag (7-12 hex, no `sha-` prefix)
    plus `latest` per merge.
  - agentforge-platform pushes the FULL 40-hex `github.sha` tag AND the 7-hex short-sha tag per
    build, and never pushes `latest` at all.

This test renders the REAL template with the REAL role defaults via python3-jinja2. That is a NEW
runtime assumption this PR introduces for scripts/tests (broker-inventory.yaml's "Script unit
tests" step comment documents only the PyYAML assumption, not jinja2); the noble cloud-image
runners install ansible as part of provisioning, which pulls python3-jinja2 as a dependency, so
CI is expected to have it, but that has not yet been confirmed by an actual green CI run of this
step — treat the first "Script unit tests" run on this PR's head as the confirmation, not this
docstring. If it turns out missing, add it to the workflow step comment / apt list in a follow-up
rather than skipping this test. Assertions:

  1. the rendered config is valid JSON (`json.loads`, the same check `python -m json.tool` performs);
  2. a `storage.retention` policy matching `agentforge/**` exists, and it precedes the `**`
     catch-all in `policies` (Zot evaluates policies in order — see the strive/** policy, which is
     ordered the same way) — a `**` policy anywhere before it would keep every agentforge tag;
  3. that policy sets `deleteUntagged: true`, keeps `^latest$`, and keeps
     `registry_zot_agentforge_keep_recent` most-recently-pushed tags under TWO separate
     patterns — one for the bare short-sha shape all four repos push, one for
     agentforge-platform's full 40-hex `github.sha` shape — each checked against real tag strings
     rather than re-deriving the template's own string (that would only prove the template said
     what it said, not that it matches what images.yml actually pushes);
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

# A real agentforge-platform full-sha tag (images.yml: `docker push "$IMAGE:${{ github.sha }}"`,
# always 40 hex chars) — the actual sha named in the review finding this test was extended for.
_FULL_SHA = "01e41b240e7dcf086e3377e378a4186d2709ae7d"


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
        # TWO count-based rules: agentforge-platform pushes BOTH a bare short-sha tag AND a full
        # 40-hex `github.sha` tag per build (and no `latest` at all) — orchestrator/sandbox/
        # p1-worker push only the bare short-sha tag + `latest`. One rule per tag shape, each
        # keyed off the same knob, so agentforge-platform's full-sha builds are covered too (a
        # single merged pattern would double-count both tags of one build against a shared
        # window, halving the platform's effective retention).
        self.assertEqual(
            len(recent_entries),
            2,
            f"expected exactly two mostRecentlyPushedCount entries (short-sha shape + "
            f"agentforge-platform's full-sha shape) in {policy['keepTags']!r}",
        )
        for entry in recent_entries:
            self.assertEqual(len(entry["patterns"]), 1)
        compiled_patterns = [re.compile(entry["patterns"][0]) for entry in recent_entries]

        def _matched_by_any(value: str) -> bool:
            return any(c.fullmatch(value) for c in compiled_patterns)

        # This is the regex the deployed defaults render, checked against real tag shapes rather
        # than re-deriving the same string the template contains (that would only prove the
        # template said what it said, not that it matches what images.yml actually pushes).
        self.assertTrue(
            _matched_by_any(_BARE_SHORT_SHA),
            f"agentforge/** keepTags must match a bare short-sha tag like {_BARE_SHORT_SHA!r} "
            "(agentforge/.gitea/workflows/images.yml pushes VER=$(git rev-parse --short HEAD) "
            "with NO 'sha-' prefix, unlike strive/**): patterns="
            f"{[e['patterns'][0] for e in recent_entries]!r}",
        )
        self.assertTrue(
            _matched_by_any(_FULL_SHA),
            f"agentforge/** keepTags must match a full 40-hex sha tag like {_FULL_SHA!r} "
            "(agentforge-platform/.gitea/workflows/images.yml pushes "
            "\"$IMAGE:${{ github.sha }}\" — the FULL sha, not just the short one, and never "
            f"pushes 'latest'): patterns={[e['patterns'][0] for e in recent_entries]!r}",
        )
        self.assertFalse(
            _matched_by_any(_STRIVE_STYLE_TAG),
            "agentforge/** keepTags must NOT also swallow strive-style "
            f"{_STRIVE_STYLE_TAG!r} tags (that would be the wrong repo's shape): patterns="
            f"{[e['patterns'][0] for e in recent_entries]!r}",
        )
        self.assertFalse(
            _matched_by_any("latest"),
            "the sha-shaped keepTags patterns must not also match the literal tag 'latest'",
        )
        # A 41-char string of hex digits is neither the 7-12 char short shape nor the exact
        # 40-char full-sha shape — pins each pattern to a fixed width rather than "40 or more".
        self.assertFalse(
            _matched_by_any("0" * 41),
            "the sha-shaped keepTags patterns must not match a 41-hex-char string (each pattern "
            "must be anchored to its exact tag width, not open-ended)",
        )

        defaults = _load_defaults()
        expected_n = defaults["registry_zot_agentforge_keep_recent"]
        for entry in recent_entries:
            self.assertEqual(
                entry["mostRecentlyPushedCount"],
                expected_n,
                "every mostRecentlyPushedCount entry must come from the "
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
        # Anchor on the DECLARATION line (`registry_zot_agentforge_keep_recent: 40`), not the
        # first occurrence of the name anywhere in the file — a cross-reference in an earlier
        # comment (e.g. from the strive knob's block) would otherwise make text.find() walk back
        # from the wrong place and pass or fail for the wrong reason.
        match = re.search(r"^registry_zot_agentforge_keep_recent:", text, re.M)
        self.assertIsNotNone(match, "no `registry_zot_agentforge_keep_recent:` declaration line")
        idx = match.start()
        # Look at the comment block immediately preceding the declaration.
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
