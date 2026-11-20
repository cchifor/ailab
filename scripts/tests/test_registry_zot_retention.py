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

strive/ci-build/** (cchifor/platform e2e build-once, platform#2823) adds three classes below: its
retention policy (first match, ordered before strive/**, the existing strive/ci/* repos still under
strive/**), the scoped push user `strive-ci-push` (read/create/update on strive/ci-build/** only, no
delete anywhere, every other identity's effective rights unchanged), and the role's
skip-and-remove-until-the-SOPS-key-exists task logic.

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


def _render_config(overrides=None) -> str:
    context = _load_defaults()
    context.update(_EXTRA_VARS)
    context.update(overrides or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    # Ansible's `to_json` is json.dumps with default arguments; plain jinja2 does not ship it, nor ansible's `bool`.
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = _ansible_bool
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

class RegistryZotScopedPushAccessTest(unittest.TestCase):
    """registry_zot_scoped_push_users: per-project push identities (cchifor/trueswarm publish CI).

    zot authorizes a repository by its LONGEST matching accessControl pattern only -- policies are
    NOT merged with `**` -- so a scoped entry that forgot a base grant would silently revoke it
    (e.g. anonymous or `pull` reads of trueswarm/* images the cluster pulls)."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.repos = json.loads(_render_config())["http"]["accessControl"]["repositories"]

    def test_every_scoped_entry_repeats_the_base_grants(self):
        base = self.repos["**"]
        scoped = self.defaults["registry_zot_scoped_push_users"]
        self.assertTrue(scoped, "expected at least the trueswarm-ci entry")
        for p in scoped:
            entry = self.repos[p["repositories"]]
            self.assertEqual(entry["anonymousPolicy"], base["anonymousPolicy"])
            for grant in base["policies"]:
                self.assertIn(grant, entry["policies"], f"{p['repositories']} drops {grant}")

    def test_scoped_user_can_push_only_its_prefix_and_never_delete(self):
        for p in self.defaults["registry_zot_scoped_push_users"]:
            mine = [g for g in self.repos[p["repositories"]]["policies"] if g.get("users") == [p["user"]]]
            self.assertEqual(mine, [{"users": [p["user"]], "actions": ["read", "create", "update"]}])
            for pattern, entry in self.repos.items():
                if pattern != p["repositories"]:
                    self.assertFalse(
                        any(p["user"] in g.get("users", []) for g in entry["policies"]),
                        f"{p['user']} leaks into {pattern}",
                    )


# ---- strive/ci-build/** : cchifor/platform e2e build-once (platform#2823) ----
# No test here runs zot: retention and authorization are SIMULATED from the rendered config, with
# the semantics below read from the source of BOTH the pinned and the live release
# (v2.1.23 and v2.1.18 are identical here):
#   pkg/retention/retention.go  getRepoPolicy: the FIRST policy whose repositories doublestar glob
#     matches wins; getTagPolicy: the FIRST keepTags entry whose patterns match a tag; a tag with no
#     entry is skipped "(will be gc'ed)"; rules within an entry are OR-ed, and an entry with no rules
#     keeps every tag it matches. getRules turns mostRecentlyPushedCount into NewLatestPush and
#     pushedWithin (a *time.Duration) into NewDaysPush, and GetRetainedTagsFromMetaDB appends each
#     rule's retained candidates: "we retain candidates if any of the below rules are met (OR logic
#     between rules)".
#   pkg/api/authz.go  can: only the LONGEST matching repositories pattern counts; isPermitted:
#     anonymousPolicy only when username == "", defaultPolicy only when username != "".
# https://github.com/project-zot/zot/blob/v2.1.18/pkg/retention/retention.go (and .../v2.1.23/...)

_STRIVE_CI_USER = "strive-ci-push"
_CI_BUILD_REPOS = "strive/ci-build/**"
_CI_BUILD_KNOB = "registry_zot_strive_ci_build_keep_recent"
_CI_BUILD_WITHIN_KNOB = "registry_zot_strive_ci_build_keep_within"
# The one keepTags entry: 10 most-recently-pushed OR pushed within 24h (zot ORs an entry's rules).
_CI_BUILD_ENTRY = {
    "patterns": ["^ci-[0-9a-f]{40}$"],
    "mostRecentlyPushedCount": 10,
    "pushedWithin": "24h",
}
# The tag platform's build-once step pushes: `ci-<40-hex commit>`.
_CI_TAG = "ci-" + _FULL_SHA
# The 12 repositories that ALREADY live under strive/ci/ (registry catalog, 2026-11-17). They keep
# latest/ailab/sha- tags under strive/**, which is why build-once got a new namespace.
_EXISTING_STRIVE_CI_REPOS = [
    "strive/ci/platform-app-runner-latest",
    "strive/ci/platform-digest-test",
    "strive/ci/platform-doc-search-latest",
    "strive/ci/platform-e2e-local",
    "strive/ci/platform-echo-http",
    "strive/ci/platform-hatchet-init",
    "strive/ci/platform-integration",
    "strive/ci/platform-mock-mcp",
    "strive/ci/platform-mock-mcp-oauth",
    "strive/ci/platform-mock-model",
    "strive/ci/platform-sentinel-latest",
    "strive/ci/platform-stub-oauth",
]
# Repositories that exercise every accessControl / retention pattern in the rendered config,
# including prefix look-alikes that must NOT fall under strive/ci-build/**.
_SAMPLE_REPOS = _EXISTING_STRIVE_CI_REPOS + [
    "strive/ci-build",                       # the bare prefix: doublestar `a/**` matches `a` too
    "strive/ci-build/platform-e2e-local",
    "strive/ci-build/nested/image",
    "strive/ci-buildx/foo",
    "strive/ci",
    "strive/platform-api",
    "agentforge/orchestrator",
    "trueswarm/api",
    "workbricks/cell",
    "library/python",
    "keycloak/keycloak",
]


def _glob(pattern: str) -> "re.Pattern":
    """zot's repository globs (bmatcuk/doublestar) for the subset this config uses: `**`, a trailing
    `/**` (which also matches the bare prefix) and `*`. Anything else fails loudly, so the
    simulation below can never silently mis-model a new pattern."""
    if re.search(r"[?\[\]{}\\]", pattern):
        raise AssertionError(f"glob simulation does not model {pattern!r}; extend _glob")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("(?:/.*)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _in_ci_build(repo: str) -> bool:
    # What `strive/ci-build/**` matches under doublestar: the bare prefix and everything below it.
    return repo == "strive/ci-build" or repo.startswith("strive/ci-build/")


def _first_retention_policy(policies: list, repo: str) -> dict:
    for policy in policies:
        if any(_glob(p).match(repo) for p in policy["repositories"]):
            return policy
    raise AssertionError(f"no retention policy matches {repo!r}")


def _keep_entry(policies: list, repo: str, tag: str):
    """(policy, the keepTags entry that governs `tag` in `repo`, or None = deleted on the next GC)."""
    policy = _first_retention_policy(policies, repo)
    for entry in policy["keepTags"]:
        if any(re.search(p, tag) for p in entry["patterns"]):
            return policy, entry
    return policy, None


def _effective_actions(access: dict, repo: str, user: str = "", groups=()) -> set:
    repos = access["repositories"]
    matching = [p for p in repos if _glob(p).match(repo)]
    actions = set()
    if matching:
        group = repos[max(matching, key=len)]
        for pol in group.get("policies", []):
            if user and user in pol.get("users", []) or set(groups) & set(pol.get("groups", [])):
                actions |= set(pol["actions"])
        if user:
            actions |= set(group.get("defaultPolicy", []))
        else:
            actions |= set(group.get("anonymousPolicy", []))
    admin = access.get("adminPolicy", {})
    if user and user in admin.get("users", []) or set(groups) & set(admin.get("groups", [])):
        actions |= set(admin["actions"])
    return actions


def _without_strive_ci_user(defaults: dict) -> dict:
    return {
        "registry_zot_scoped_push_users": [
            p for p in defaults["registry_zot_scoped_push_users"] if p["user"] != _STRIVE_CI_USER
        ]
    }


# strive/**'s two bounded families (2026-11-20): the 10 most recently pushed OR pushed within 14 days, one entry each;
# an ailab-<digest> protect tag also while its digest was pulled within 14 days (the platform re-fetches pins daily).
_STRIVE_SHA_ENTRY = {"patterns": ["^sha-.*"], "mostRecentlyPushedCount": 10, "pushedWithin": "336h"}
_STRIVE_AILAB_DIGEST_ENTRY = {
    "patterns": ["^ailab-[0-9a-f]{12}([0-9a-f]{52})?$"], "mostRecentlyPushedCount": 10, "pushedWithin": "336h",
    "pulledWithin": "336h",
}
_STRIVE_POLICY = {
    "repositories": ["strive/**"],
    "deleteUntagged": True,
    "keepTags": [
        {"patterns": ["^latest$"]},
        {"patterns": ["^ailab$"]},
        {"patterns": ["^ailab-prev\\d*$"]},
        _STRIVE_SHA_ENTRY,
        _STRIVE_AILAB_DIGEST_ENTRY,
    ],
}


class RegistryZotStriveCiBuildRetentionTest(unittest.TestCase):
    """strive/ci-build/** keeps the 10 most-recently-pushed `ci-<40-hex>` tags per repo, is listed
    before strive/** (which would keep no ci- tag), and leaves the existing strive/ci/* repos under
    the unchanged strive/** policy."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.policies = json.loads(_render_config())["storage"]["retention"]["policies"]

    def test_knobs_default_to_10_or_24h(self):
        self.assertEqual(self.defaults[_CI_BUILD_KNOB], 10)
        self.assertEqual(self.defaults[_CI_BUILD_WITHIN_KNOB], "24h")
        # A Go duration string (zot decodes it like gcDelay), not a bare number of days.
        self.assertRegex(self.defaults[_CI_BUILD_WITHIN_KNOB], r"^[0-9]+h$")

    def test_ci_build_policy_shape(self):
        policy = self.policies[_policy_index(self.policies, _CI_BUILD_REPOS)]
        self.assertEqual(
            policy,
            {
                "repositories": [_CI_BUILD_REPOS],
                "deleteUntagged": True,
                # ONE entry carrying both rules, so zot ORs them: a tag survives if it is among the
                # 10 most recently pushed OR was pushed within 24h. Split into two entries, the
                # first would claim every ci- tag (getTagPolicy is first-match) and the second
                # would never apply.
                "keepTags": [_CI_BUILD_ENTRY],
            },
        )

    def test_bare_prefix_and_catalog_boundaries(self):
        # doublestar: `strive/ci-build/**` matches the bare `strive/ci-build` as well as everything
        # below it, and nothing that merely shares the prefix string.
        rx = _glob(_CI_BUILD_REPOS)
        self.assertTrue(rx.match("strive/ci-build"))
        self.assertFalse(rx.match("strive/ci-buildx"))
        self.assertFalse(rx.match("strive/ci"))
        self.assertIn("strive/ci-build", _SAMPLE_REPOS)
        for repo in _SAMPLE_REPOS:
            self.assertEqual(
                _first_retention_policy(self.policies, repo)["repositories"] == [_CI_BUILD_REPOS],
                _in_ci_build(repo),
                repo,
            )
        # None of the existing strive/ci/* repos (the catalog) falls inside the new namespace.
        for repo in _EXISTING_STRIVE_CI_REPOS:
            self.assertFalse(_in_ci_build(repo), repo)
            self.assertFalse(rx.match(repo), repo)

    def test_no_policy_targets_strive_ci(self):
        for policy in self.policies:
            for pattern in policy["repositories"]:
                self.assertFalse(
                    pattern.startswith("strive/ci/") or pattern == "strive/ci",
                    f"{pattern!r} would shadow strive/** for the existing strive/ci/* repos",
                )

    def test_keep_pattern_matches_only_the_full_commit_tag(self):
        policy = self.policies[_policy_index(self.policies, _CI_BUILD_REPOS)]
        (pattern,) = policy["keepTags"][0]["patterns"]
        rx = re.compile(pattern)
        self.assertTrue(rx.search(_CI_TAG))
        for tag in [
            "ci-" + _FULL_SHA[:39],          # 39 hex
            "ci-" + _FULL_SHA + "0",         # 41 hex
            "ci-" + _FULL_SHA.upper(),       # git prints lowercase; uppercase is not a commit tag
            "ci-" + _BARE_SHORT_SHA,         # short sha
            "sha-" + _FULL_SHA,              # strive main-build shape
            _FULL_SHA,                       # bare sha
            "latest",
            "buildcache",
        ]:
            self.assertFalse(rx.search(tag), f"{pattern!r} must not keep {tag!r}")

    def test_ci_build_precedes_strive_and_catchall(self):
        idx = _policy_index(self.policies, _CI_BUILD_REPOS)
        self.assertLess(idx, _policy_index(self.policies, "strive/**"))
        self.assertLess(idx, _policy_index(self.policies, "**"))

    def test_existing_strive_ci_repos_keep_their_strive_rules(self):
        # Regression for the first draft (strive/ci/**): every existing strive/ci/* repo must still
        # be governed by the UNCHANGED strive/** policy, so its latest / ailab / ailab-prev /
        # sha- / ailab-<digest> tags keep the entries they had.
        for repo in _EXISTING_STRIVE_CI_REPOS:
            for tag, want in [
                ("latest", {"patterns": ["^latest$"]}),
                ("ailab", {"patterns": ["^ailab$"]}),
                ("ailab-prev2", {"patterns": ["^ailab-prev\\d*$"]}),
                ("sha-" + _FULL_SHA, _STRIVE_SHA_ENTRY),
                ("ailab-" + _FULL_SHA[:12], _STRIVE_AILAB_DIGEST_ENTRY),
            ]:
                policy, entry = _keep_entry(self.policies, repo, tag)
                self.assertEqual(policy, _STRIVE_POLICY, f"{repo}:{tag} left strive/**")
                self.assertEqual(entry, want, f"{repo}:{tag}")

    def test_ci_build_tag_is_governed_by_the_new_policy(self):
        for repo in ("strive/ci-build", "strive/ci-build/x", "strive/ci-build/platform-e2e-local", "strive/ci-build/a/b"):
            policy, entry = _keep_entry(self.policies, repo, _CI_TAG)
            self.assertEqual(policy["repositories"], [_CI_BUILD_REPOS], repo)
            self.assertEqual(entry, _CI_BUILD_ENTRY)
            # Anything else pushed there is pruned on the next GC.
            for other in ("latest", "sha-" + _FULL_SHA, "buildcache"):
                self.assertIsNone(_keep_entry(self.policies, repo, other)[1], f"{repo}:{other}")

    def test_first_match_routes_each_repo_to_the_intended_policy(self):
        expected = {
            "strive/ci-build/platform-e2e-local": _CI_BUILD_REPOS,
            "strive/ci-build/nested/image": _CI_BUILD_REPOS,
            "strive/ci-build": _CI_BUILD_REPOS,
            "strive/ci-buildx/foo": "strive/**",
            "strive/ci/platform-e2e-local": "strive/**",
            "strive/ci": "strive/**",
            "strive/platform-api": "strive/**",
            "agentforge/orchestrator": "agentforge/**",
            "library/python": "**",
            "trueswarm/api": "**",
        }
        for repo, want in expected.items():
            got = _first_retention_policy(self.policies, repo)["repositories"]
            self.assertEqual(got, [want], f"{repo} must be governed by {want}")

    def test_strive_policy_would_drop_a_ci_tag(self):
        # The reason for the ordering: under strive/** a ci- tag matches no keepTags entry.
        policy, entry = _keep_entry(self.policies, "strive/platform-api", _CI_TAG)
        self.assertEqual(policy, _STRIVE_POLICY)
        self.assertIsNone(entry)

    def test_other_retention_policies_unchanged(self):
        # Ignore the deleteReferrers canary (its own test class below pins it), but not a widened strive/**.
        others = [p for p in self.policies if p["repositories"] != [_CI_BUILD_REPOS]
                  and not (p.get("deleteReferrers") and p["repositories"] != ["strive/**"])]
        self.assertEqual(
            [p["repositories"] for p in others], [["strive/**"], ["agentforge/**"], ["**"]]
        )
        self.assertEqual({k: v for k, v in others[0].items() if k != "deleteReferrers"}, _STRIVE_POLICY)
        self.assertEqual(
            others[1],
            {
                "repositories": ["agentforge/**"],
                "deleteUntagged": True,
                "keepTags": [
                    {"patterns": ["^latest$"]},
                    {"patterns": ["^[0-9a-f]{40}$"], "mostRecentlyPushedCount": 40},
                    {"patterns": ["^[0-9a-f]{7,12}$"], "mostRecentlyPushedCount": 40},
                    {"patterns": ["^[0-9a-f]{7,40}$"], "mostRecentlyPulledCount": 10},
                ],
            },
        )
        self.assertEqual(
            others[2],
            {"repositories": ["**"], "deleteUntagged": False, "keepTags": [{"patterns": [".*"]}]},
        )


_CANARY_KNOB = "registry_zot_strive_delete_referrers_repos"


def _policies(repos=None):
    overrides = None if repos is None else {_CANARY_KNOB: repos}
    return json.loads(_render_config(overrides))["storage"]["retention"]["policies"]


def _load_apply_helper():
    import importlib.util
    path = ROLE_DIR / "files" / "zot-config-apply.py"
    spec = importlib.util.spec_from_file_location("zot_config_apply", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The shipped deleteReferrers list: the strive/web canary plus batch B1 (2026-11-19). One place to update per batch.
_DEFAULT_REFERRER_REPOS = ["strive/web", "strive/e2e-runner", "strive/keycloak-realm-sync", "strive/notification"]


class RegistryZotStriveDeleteReferrersTest(unittest.TestCase):
    """deleteReferrers drops orphaned buildx attestation referrers (subject already deleted) without changing
    which TAGS are kept: the canary policy is strive/**'s exact rules plus deleteReferrers, between
    strive/ci-build/** and strive/**; widening to strive/** puts the flag on strive/** itself."""

    def test_default_is_the_canary_plus_batch_b1(self):
        # Widening in batches (2026-11-19): strive/web (canary) + B1. B2-B4 follow, then ["strive/**"].
        self.assertEqual(_load_defaults()[_CANARY_KNOB], _DEFAULT_REFERRER_REPOS)

    def test_canary_sits_between_ci_build_and_strive(self):
        repos = [p["repositories"] for p in _policies()]
        self.assertEqual(repos[:3], [[_CI_BUILD_REPOS], _DEFAULT_REFERRER_REPOS, ["strive/**"]])

    def test_canary_keeps_exactly_what_strive_keeps(self):
        canary = _first_retention_policy(_policies(), "strive/web")
        self.assertIs(canary["deleteReferrers"], True)
        self.assertEqual(canary["keepTags"], _STRIVE_POLICY["keepTags"])
        self.assertEqual(canary["deleteUntagged"], _STRIVE_POLICY["deleteUntagged"])
        self.assertEqual(_first_retention_policy(_policies(), "strive/platform-api"), _STRIVE_POLICY)

    def test_tag_decisions_are_identical_inside_and_outside_the_canary(self):
        pols = _policies()
        for tag in ("latest", "ailab", "ailab-prev2", "sha-b80a376c", "ailab-0123456789ab", "ci-" + _FULL_SHA, "x"):
            inside = _keep_entry(pols, "strive/web", tag)[1]
            outside = _keep_entry(pols, "strive/platform-api", tag)[1]
            self.assertEqual(inside, outside, tag)

    def test_first_match_routing(self):
        pols = _policies()
        expected = {"strive/web": _DEFAULT_REFERRER_REPOS, "strive/e2e-runner": _DEFAULT_REFERRER_REPOS,
                    "strive/profile": ["strive/**"],
                    "strive/webx": ["strive/**"], "strive/ci-build/a": [_CI_BUILD_REPOS],
                    "strive/platform-api": ["strive/**"], "agentforge/orchestrator": ["agentforge/**"],
                    "library/python": ["**"]}
        for repo, want in expected.items():
            self.assertEqual(_first_retention_policy(pols, repo)["repositories"], want, repo)

    def test_empty_list_renders_no_canary(self):
        pols = _policies([])
        self.assertFalse(any(p.get("deleteReferrers") for p in pols))
        self.assertEqual([p["repositories"] for p in pols],
                         [[_CI_BUILD_REPOS], ["strive/**"], ["agentforge/**"], ["**"]])

    def test_widening_sets_the_flag_on_strive_itself(self):
        pols = _policies(["strive/**"])
        self.assertEqual([p["repositories"] for p in pols],
                         [[_CI_BUILD_REPOS], ["strive/**"], ["agentforge/**"], ["**"]])
        strive = _first_retention_policy(pols, "strive/web")
        self.assertIs(strive["deleteReferrers"], True)
        self.assertEqual(strive["keepTags"], _STRIVE_POLICY["keepTags"])
        self.assertNotIn("deleteReferrers", _first_retention_policy(pols, "strive/ci-build/a"))
        self.assertNotIn("deleteReferrers", _first_retention_policy(pols, "agentforge/orchestrator"))

    def test_the_change_is_reload_safe(self):
        # PR 1's helper must classify adding the canary (or widening) as a live reload, not a 19 min restart.
        helper = _load_apply_helper()
        before = json.loads(_render_config({_CANARY_KNOB: []}))
        for repos in (["strive/web"], ["strive/**"]):
            after = json.loads(_render_config({_CANARY_KNOB: repos}))
            mode, paths, _ = helper.classify(before, after)
            self.assertEqual(mode, "reload", (repos, paths))
            self.assertEqual(paths, ["storage.retention.policies"])


class RegistryZotStriveCiPushAccessTest(unittest.TestCase):
    """`strive-ci-push`: read/create/update on strive/ci-build/** and nothing else; every other
    identity's effective rights are what they were without it."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.config = json.loads(_render_config())
        self.access = self.config["http"]["accessControl"]
        self.baseline = json.loads(_render_config(_without_strive_ci_user(self.defaults)))

    def test_user_is_declared_for_ci_build_only(self):
        entries = [
            p for p in self.defaults["registry_zot_scoped_push_users"] if p["user"] == _STRIVE_CI_USER
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repositories"], _CI_BUILD_REPOS)

    def test_exact_grant_on_ci_build(self):
        entry = self.access["repositories"][_CI_BUILD_REPOS]
        mine = [g for g in entry["policies"] if _STRIVE_CI_USER in g.get("users", [])]
        self.assertEqual(mine, [{"users": [_STRIVE_CI_USER], "actions": ["read", "create", "update"]}])
        for repo in ("strive/ci-build", "strive/ci-build/platform-e2e-local", "strive/ci-build/nested/image"):
            self.assertEqual(
                _effective_actions(self.access, repo, _STRIVE_CI_USER), {"read", "create", "update"}
            )

    def test_no_rights_outside_ci_build_and_no_delete_anywhere(self):
        for repo in _SAMPLE_REPOS:
            actions = _effective_actions(self.access, repo, _STRIVE_CI_USER)
            self.assertNotIn("delete", actions, repo)
            if not _in_ci_build(repo):
                # Not even read: zot's anonymousPolicy covers only unauthenticated requests. This
                # includes the existing strive/ci/* repos.
                self.assertEqual(actions, set(), f"{_STRIVE_CI_USER} must have no rights on {repo}")
        for pattern, entry in self.access["repositories"].items():
            for g in entry["policies"]:
                if _STRIVE_CI_USER in g.get("users", []):
                    self.assertEqual(pattern, _CI_BUILD_REPOS)
                    self.assertNotIn("delete", g["actions"])
            self.assertNotIn("defaultPolicy", entry, "a defaultPolicy would grant every user")
        self.assertNotIn(_STRIVE_CI_USER, self.access.get("adminPolicy", {}).get("users", []))

    def test_other_identities_effective_rights_unchanged(self):
        identities = [
            ("anonymous", "", ()),
            ("ci", self.defaults["registry_zot_ci_user"], ()),
            ("pull", self.defaults["registry_zot_pull_user"], ()),
            ("admins group", "sso-user", (self.defaults["registry_zot_admin_group"],)),
        ] + [
            (p["user"], p["user"], ())
            for p in self.defaults["registry_zot_scoped_push_users"]
            if p["user"] != _STRIVE_CI_USER
        ]
        before = self.baseline["http"]["accessControl"]
        for label, user, groups in identities:
            for repo in _SAMPLE_REPOS:
                self.assertEqual(
                    _effective_actions(self.access, repo, user, groups),
                    _effective_actions(before, repo, user, groups),
                    f"{label} on {repo} changed",
                )
        # Absolute values too, not only "same as the baseline render".
        for repo in ("strive/ci-build/x", "strive/ci/platform-e2e-local"):
            self.assertEqual(_effective_actions(self.access, repo, ""), {"read"})
            self.assertEqual(_effective_actions(self.access, repo, "pull"), {"read"})
            self.assertEqual(
                _effective_actions(self.access, repo, "ci"), {"read", "create", "update", "delete"}
            )

    def test_only_the_ci_build_entries_differ_from_the_baseline_render(self):
        def strip(cfg):
            cfg = json.loads(json.dumps(cfg))
            cfg["http"]["accessControl"]["repositories"].pop(_CI_BUILD_REPOS, None)
            cfg["storage"]["retention"]["policies"] = [
                p for p in cfg["storage"]["retention"]["policies"]
                if p["repositories"] != [_CI_BUILD_REPOS]
            ]
            return cfg

        self.assertEqual(strip(self.config), strip(self.baseline))
        self.assertNotIn(_CI_BUILD_REPOS, self.baseline["http"]["accessControl"]["repositories"])


def _ansible_bool(value):
    # ansible's `bool` filter, not Python's: strings are parsed ("no" is False).
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
    return value in ("yes", "on", "1", "true", 1)


class RegistryZotScopedPushPasswordTaskTest(unittest.TestCase):
    """The role reads each scoped password from registry_scoped_push_passwords (SOPS). An entry
    marked `password_optional` is skipped while its key is absent, and its htpasswd line removed,
    so the change can merge before the operator adds the secret; every other entry still fails the
    play without its key. These tests evaluate the tasks' real conditions with jinja2; they do not
    execute the tasks or the htpasswd module (a one-off ansible-core run is in the PR, ailab#1836)."""

    def setUp(self):
        tasks = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text())
        self.tasks = {t["name"]: t for t in tasks}
        self.assert_task = self.tasks[
            "Every scoped push user has a password in SOPS (registry_scoped_push_passwords)"
        ]
        self.skip_task = self.tasks["Report scoped push users skipped for want of a SOPS password"]
        self.remove_task = self.tasks[
            "Remove the htpasswd line of an optional scoped push user with no SOPS password"
        ]
        self.htpasswd_task = self.tasks["Generate bcrypt htpasswd for the scoped per-project push users"]
        self.stale_task = self.tasks["Report a stale password_optional flag (the SOPS key now exists)"]
        self.env = jinja2.Environment()
        self.env.filters["bool"] = _ansible_bool
        self.users = {p["user"]: p for p in _load_defaults()["registry_zot_scoped_push_users"]}
        # The optional-password mechanism is exercised with a synthetic entry: since the operator added
        # strive-ci-push's key (ailab#1839) no real user is optional, and every real one fails closed.
        self.optional = {"user": "optional-ci", "repositories": "example/**", "password_optional": True}

    def _eval(self, when, item: dict, passwords) -> bool:
        # Ansible ANDs a list of `when` conditions.
        ctx = {"item": item}
        if passwords is not None:
            ctx["registry_scoped_push_passwords"] = passwords
        exprs = when if isinstance(when, list) else [when]
        return all(bool(self.env.compile_expression(e)(**ctx)) for e in exprs)

    def _actions(self, item: dict, passwords) -> dict:
        return {
            "assert_ok": self._eval(self.assert_task["ansible.builtin.assert"]["that"], item, passwords),
            "skip_msg": self._eval(self.skip_task["when"], item, passwords),
            "stale_msg": self._eval(self.stale_task["when"], item, passwords),
            "remove": self._eval(self.remove_task["when"], item, passwords),
            "write": self._eval(self.htpasswd_task["when"], item, passwords),
        }

    def test_no_real_user_is_optional(self):
        # strive-ci-push's key is in SOPS (ailab#1839), so its password_optional flag is gone: every real
        # scoped user, strive-ci-push included, must fail the play without its key.
        optional = [u for u, p in self.users.items() if p.get("password_optional")]
        self.assertEqual(optional, [])
        self.assertIn(_STRIVE_CI_USER, self.users)

    def test_assert_fails_without_key_unless_optional(self):
        that = self.assert_task["ansible.builtin.assert"]["that"]
        for passwords in (None, {}, {"unrelated": "x"}):
            self.assertTrue(self._eval(that, self.optional, passwords))
            self.assertFalse(self._eval(that, self.users[_STRIVE_CI_USER], passwords))
            self.assertFalse(self._eval(that, self.users["trueswarm-ci"], passwords))
            self.assertFalse(self._eval(that, self.users["workbricks-ci"], passwords))
        present = {u: "x" for u in self.users}
        for item in self.users.values():
            self.assertTrue(self._eval(that, item, present))

    def test_present_key_then_absent_key(self):
        # Run 1: the operator added the key -> the line is written, nothing removed.
        item = self.optional
        self.assertEqual(
            self._actions(item, {u: "x" for u in list(self.users) + [item["user"]]}),
            {"assert_ok": True, "skip_msg": False, "stale_msg": True, "remove": False, "write": True},
        )
        # Run 2: the key is gone again (or never existed) -> no write, and the line from run 1 is
        # REMOVED, so "skipped" really means "cannot log in".
        for passwords in (None, {}, {"trueswarm-ci": "x", "workbricks-ci": "x"}):
            self.assertEqual(
                self._actions(item, passwords),
                {"assert_ok": True, "skip_msg": True, "stale_msg": False, "remove": True, "write": False},
            )

    def test_mandatory_users_are_never_removed_and_never_reported(self):
        # The SKIPPED and STALE messages are about optional users only; a mandatory user with no key
        # fails the assert instead.
        for user in ("trueswarm-ci", "workbricks-ci", _STRIVE_CI_USER):
            for passwords in (None, {}, {u: "x" for u in self.users}):
                for task in (self.remove_task, self.skip_task, self.stale_task):
                    self.assertFalse(self._eval(task["when"], self.users[user], passwords), task["name"])

    def test_stale_flag_message_fires_only_with_flag_and_key(self):
        item = self.optional
        self.assertTrue(self._eval(self.stale_task["when"], item, {item["user"]: "x"}))
        self.assertFalse(self._eval(self.stale_task["when"], item, {}))
        unflagged = {k: v for k, v in item.items() if k != "password_optional"}
        self.assertFalse(self._eval(self.stale_task["when"], unflagged, {item["user"]: "x"}))
        self.assertIn("STALE FLAG", self.stale_task["ansible.builtin.debug"]["msg"])

    def test_remove_task_shape(self):
        module = self.remove_task["community.general.htpasswd"]
        self.assertEqual(
            module,
            {"path": "{{ registry_zot_config_dir }}/htpasswd", "name": "{{ item.user }}", "state": "absent"},
        )
        self.assertIs(self.remove_task["no_log"], True)
        self.assertEqual(self.remove_task["notify"], "restart zot")
        self.assertEqual(self.remove_task["loop"], "{{ registry_zot_scoped_push_users }}")

    def test_password_flow_matches_the_other_scoped_users(self):
        module = self.htpasswd_task["community.general.htpasswd"]
        self.assertEqual(module["password"], "{{ registry_scoped_push_passwords[item.user] }}")
        self.assertEqual(module["path"], "{{ registry_zot_config_dir }}/htpasswd")
        self.assertEqual(module["crypt_scheme"], "bcrypt")
        self.assertIs(self.htpasswd_task["no_log"], True)
        self.assertEqual(self.htpasswd_task["loop"], "{{ registry_zot_scoped_push_users }}")

    def test_sops_rule_encrypts_the_password_map(self):
        # The key must go UNDER registry_scoped_push_passwords: the file's encrypted_regex covers
        # that map, and a new top-level key would be written in plaintext. Both the creation rule
        # AND the encrypted file's own metadata (which `sops edit` keeps) are checked; reading the
        # metadata decrypts nothing.
        secrets_path = "ansible/secrets/registry.sops.yaml"
        rules = yaml.safe_load((REPO_ROOT / ".sops.yaml").read_text())["creation_rules"]
        rule = next(r for r in rules if re.search(r.get("path_regex", "^$"), secrets_path))
        self.assertTrue(re.search(rule["encrypted_regex"], "registry_scoped_push_passwords"))
        meta = yaml.safe_load((REPO_ROOT / secrets_path).read_text())["sops"]
        self.assertTrue(re.search(meta["encrypted_regex"], "registry_scoped_push_passwords"))


# The keepTags strive/** had until 2026-11-20 (the live config at that date): 100 sha- builds, every ailab-<digest>.
_STRIVE_KEEP_TAGS_BEFORE = [
    {"patterns": ["^latest$"]},
    {"patterns": ["^ailab$"]},
    {"patterns": ["^ailab-prev\\d*$"]},
    {"patterns": ["^sha-.*"], "mostRecentlyPushedCount": 100},
    {"patterns": ["^ailab-[0-9a-f]{12}([0-9a-f]{52})?$"]},
]


class RegistryZotStriveTagRetentionTest(unittest.TestCase):
    """strive/** keeps latest / ailab / ailab-prev* forever, and the sha- builds and ailab-<digest> protect tags each
    by the N most recently pushed OR pushed within a window: both rules in ONE entry, which zot v2.1.18 ORs."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.policies = json.loads(_render_config())["storage"]["retention"]["policies"]

    def test_knobs(self):
        self.assertEqual(self.defaults["registry_zot_strive_keep_recent"], 10)
        self.assertEqual(self.defaults["registry_zot_strive_keep_within"], "336h")  # 14 days, a Go duration
        self.assertIs(self.defaults["registry_zot_retention_dry_run"], False)

    def test_strive_keep_tags(self):
        strive = self.policies[_policy_index(self.policies, "strive/**")]
        self.assertEqual(strive["keepTags"], _STRIVE_POLICY["keepTags"])
        for entry in (_STRIVE_SHA_ENTRY, _STRIVE_AILAB_DIGEST_ENTRY):
            # one entry carrying both rules: split in two, the first would claim every tag (first match) and the
            # second would never apply
            self.assertEqual(sum(1 for e in strive["keepTags"] if e["patterns"] == entry["patterns"]), 1)

    def test_every_strive_policy_shares_the_keep_tags(self):
        # the deleteReferrers list renders from the same macro, at any list value
        for repos in ([], ["strive/web"], ["strive/**"]):
            for policy in _policies(repos):
                if any(p.startswith("strive/") and not p.startswith("strive/ci-build") for p in policy["repositories"]):
                    self.assertEqual(policy["keepTags"], _STRIVE_POLICY["keepTags"], (repos, policy["repositories"]))

    def test_knobs_reach_both_families(self):
        pols = json.loads(_render_config({"registry_zot_strive_keep_recent": 25,
                                          "registry_zot_strive_keep_within": "48h"}))["storage"]["retention"]["policies"]
        strive = pols[_policy_index(pols, "strive/**")]
        bounded = [e for e in strive["keepTags"] if len(e) > 1]
        self.assertEqual(len(bounded), 2)
        for e in bounded:
            self.assertEqual((e["mostRecentlyPushedCount"], e["pushedWithin"]), (25, "48h"))
        sha, ailab_digest = bounded
        self.assertNotIn("pulledWithin", sha)  # a sha- build is kept by count or push age only
        self.assertEqual(ailab_digest["pulledWithin"], "48h")  # a pinned digest is re-fetched daily

    def test_other_policies_untouched(self):
        ci_build = self.policies[_policy_index(self.policies, _CI_BUILD_REPOS)]
        self.assertEqual(ci_build["keepTags"], [_CI_BUILD_ENTRY])
        agentforge = self.policies[_policy_index(self.policies, "agentforge/**")]
        self.assertNotIn("pushedWithin", json.dumps(agentforge))

    def test_dry_run_renders_a_json_boolean(self):
        # `-e registry_zot_retention_dry_run=true` arrives as the STRING "true": a bare to_json would render "true" in
        # quotes, which zot rejects (bool field), so the template goes through ansible's bool filter first.
        for value, want in ((None, False), (False, False), (True, True), ("true", True), ("yes", True),
                            ("false", False), ("no", False)):
            overrides = {} if value is None else {"registry_zot_retention_dry_run": value}
            rendered = _render_config(overrides)
            self.assertIs(json.loads(rendered)["storage"]["retention"]["dryRun"], want, value)

    def test_retention_changes_are_reloads(self):
        # the apply helper must hot-reload both steps of the rollout, never restart (19 min outage)
        helper = _load_apply_helper()
        live = json.loads(_render_config({}))
        for policy in live["storage"]["retention"]["policies"]:  # what ran until 2026-11-20
            if policy["repositories"] != [_CI_BUILD_REPOS] and policy["keepTags"] == _STRIVE_POLICY["keepTags"]:
                policy["keepTags"] = _STRIVE_KEEP_TAGS_BEFORE
        dry = json.loads(_render_config({"registry_zot_retention_dry_run": "true"}))
        mode, paths, _ = helper.classify(live, dry)
        self.assertEqual(mode, "reload", paths)
        self.assertEqual(paths, ["storage.retention.dryRun", "storage.retention.policies"])
        mode, paths, _ = helper.classify(dry, json.loads(_render_config({})))
        self.assertEqual((mode, paths), ("reload", ["storage.retention.dryRun"]))


if __name__ == "__main__":
    unittest.main()
