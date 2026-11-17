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

strive/ci/** (cchifor/platform e2e build-once, platform#2823) adds three classes below: its
retention policy (first match, ordered before strive/**), the scoped push user `strive-ci-push`
(read/create/update on strive/ci/** only, no delete anywhere, every other identity's effective
rights unchanged), and the role's skip-until-the-SOPS-key-exists task logic.

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
    # Ansible's `to_json` is json.dumps with default arguments; plain jinja2 does not ship it.
    env.filters["to_json"] = json.dumps
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


# ---- strive/ci/** : cchifor/platform e2e build-once (platform#2823) ----

_STRIVE_CI_USER = "strive-ci-push"
_STRIVE_CI_REPOS = "strive/ci/**"
# The tag platform's build-once step pushes: `ci-<40-hex commit>`.
_CI_TAG = "ci-" + _FULL_SHA
# Repositories that exercise every accessControl / retention pattern in the rendered config,
# including a prefix look-alike (strive/cix/...) that must NOT fall under strive/ci/**.
_SAMPLE_REPOS = [
    "strive/ci/platform-e2e-local",
    "strive/ci/nested/image",
    "strive/cix/foo",
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


def _first_retention_policy(policies: list, repo: str) -> dict:
    # pkg/retention getRepoPolicy: the FIRST policy with a matching repositories glob; not merged.
    for policy in policies:
        if any(_glob(p).match(repo) for p in policy["repositories"]):
            return policy
    raise AssertionError(f"no retention policy matches {repo!r}")


def _effective_actions(access: dict, repo: str, user: str = "", groups=()) -> set:
    """zot pkg/api/authz.go `can`/`isPermitted`: only the LONGEST matching repositories pattern
    counts; a policy grants by user or group; defaultPolicy applies to any authenticated user;
    anonymousPolicy ONLY when the username is empty; then the adminPolicy."""
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


class RegistryZotStriveCiRetentionTest(unittest.TestCase):
    """strive/ci/** keeps the 10 most-recently-pushed `ci-<40-hex>` tags per repo, and is listed
    before strive/**, which would otherwise match first and keep no ci- tag at all."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.policies = json.loads(_render_config())["storage"]["retention"]["policies"]

    def test_knob_defaults_to_10(self):
        self.assertEqual(self.defaults["registry_zot_strive_ci_keep_recent"], 10)

    def test_strive_ci_policy_shape(self):
        policy = self.policies[_policy_index(self.policies, _STRIVE_CI_REPOS)]
        self.assertEqual(
            policy,
            {
                "repositories": [_STRIVE_CI_REPOS],
                "deleteUntagged": True,
                "keepTags": [
                    {"patterns": ["^ci-[0-9a-f]{40}$"], "mostRecentlyPushedCount": 10},
                ],
            },
        )

    def test_keep_pattern_matches_only_the_full_commit_tag(self):
        policy = self.policies[_policy_index(self.policies, _STRIVE_CI_REPOS)]
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

    def test_strive_ci_precedes_strive_and_catchall(self):
        ci_idx = _policy_index(self.policies, _STRIVE_CI_REPOS)
        self.assertLess(ci_idx, _policy_index(self.policies, "strive/**"))
        self.assertLess(ci_idx, _policy_index(self.policies, "**"))

    def test_first_match_routes_each_repo_to_the_intended_policy(self):
        expected = {
            "strive/ci/platform-e2e-local": _STRIVE_CI_REPOS,
            "strive/ci/nested/image": _STRIVE_CI_REPOS,
            "strive/cix/foo": "strive/**",
            "strive/platform-api": "strive/**",
            "agentforge/orchestrator": "agentforge/**",
            "library/python": "**",
            "trueswarm/api": "**",
        }
        for repo, want in expected.items():
            got = _first_retention_policy(self.policies, repo)["repositories"]
            self.assertEqual(got, [want], f"{repo} must be governed by {want}")

    def test_strive_policy_would_drop_a_ci_tag(self):
        # The reason for the ordering: under strive/** a ci- tag matches no keepTags entry, so zot
        # deletes it on the next GC run (pkg/retention: "no tag policy found ... will be gc'ed").
        strive = self.policies[_policy_index(self.policies, "strive/**")]
        patterns = [p for entry in strive["keepTags"] for p in entry["patterns"]]
        self.assertFalse(any(re.search(p, _CI_TAG) for p in patterns), patterns)

    def test_other_retention_policies_unchanged(self):
        others = [p for p in self.policies if p["repositories"] != [_STRIVE_CI_REPOS]]
        self.assertEqual(
            [p["repositories"] for p in others], [["strive/**"], ["agentforge/**"], ["**"]]
        )
        self.assertEqual(
            others[0],
            {
                "repositories": ["strive/**"],
                "deleteUntagged": True,
                "keepTags": [
                    {"patterns": ["^latest$"]},
                    {"patterns": ["^ailab$"]},
                    {"patterns": ["^ailab-prev\\d*$"]},
                    {"patterns": ["^sha-.*"], "mostRecentlyPushedCount": 100},
                    {"patterns": ["^ailab-[0-9a-f]{12}([0-9a-f]{52})?$"]},
                ],
            },
        )
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


class RegistryZotStriveCiPushAccessTest(unittest.TestCase):
    """`strive-ci-push`: read/create/update on strive/ci/** and nothing else; every other
    identity's effective rights are what they were without it."""

    def setUp(self):
        self.defaults = _load_defaults()
        self.config = json.loads(_render_config())
        self.access = self.config["http"]["accessControl"]
        self.baseline = json.loads(_render_config(_without_strive_ci_user(self.defaults)))

    def test_user_is_declared_for_strive_ci_only(self):
        entries = [
            p for p in self.defaults["registry_zot_scoped_push_users"] if p["user"] == _STRIVE_CI_USER
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repositories"], _STRIVE_CI_REPOS)

    def test_exact_grant_on_strive_ci(self):
        entry = self.access["repositories"][_STRIVE_CI_REPOS]
        mine = [g for g in entry["policies"] if _STRIVE_CI_USER in g.get("users", [])]
        self.assertEqual(mine, [{"users": [_STRIVE_CI_USER], "actions": ["read", "create", "update"]}])
        for repo in ("strive/ci/platform-e2e-local", "strive/ci/nested/image"):
            self.assertEqual(
                _effective_actions(self.access, repo, _STRIVE_CI_USER), {"read", "create", "update"}
            )

    def test_no_rights_outside_strive_ci_and_no_delete_anywhere(self):
        for repo in _SAMPLE_REPOS:
            actions = _effective_actions(self.access, repo, _STRIVE_CI_USER)
            self.assertNotIn("delete", actions, repo)
            if not repo.startswith("strive/ci/"):
                # Not even read: zot's anonymousPolicy covers only unauthenticated requests.
                self.assertEqual(actions, set(), f"{_STRIVE_CI_USER} must have no rights on {repo}")
        for pattern, entry in self.access["repositories"].items():
            for g in entry["policies"]:
                if _STRIVE_CI_USER in g.get("users", []):
                    self.assertEqual(pattern, _STRIVE_CI_REPOS)
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
        # Spot-check the absolute values too, not only "same as the baseline render".
        self.assertEqual(_effective_actions(self.access, "strive/ci/x", ""), {"read"})
        self.assertEqual(_effective_actions(self.access, "strive/ci/x", "pull"), {"read"})
        self.assertEqual(
            _effective_actions(self.access, "strive/ci/x", "ci"),
            {"read", "create", "update", "delete"},
        )

    def test_only_the_strive_ci_entries_differ_from_the_baseline_render(self):
        after = json.loads(json.dumps(self.config))
        del after["http"]["accessControl"]["repositories"][_STRIVE_CI_REPOS]
        after["storage"]["retention"]["policies"] = [
            p for p in after["storage"]["retention"]["policies"]
            if p["repositories"] != [_STRIVE_CI_REPOS]
        ]
        # The retention policy renders regardless of the user list, so drop it from both.
        before = json.loads(json.dumps(self.baseline))
        before["storage"]["retention"]["policies"] = [
            p for p in before["storage"]["retention"]["policies"]
            if p["repositories"] != [_STRIVE_CI_REPOS]
        ]
        self.assertEqual(after, before)
        self.assertNotIn(_STRIVE_CI_REPOS, self.baseline["http"]["accessControl"]["repositories"])


class RegistryZotScopedPushPasswordTaskTest(unittest.TestCase):
    """The role reads each scoped password from registry_scoped_push_passwords (SOPS). An entry
    marked `password_optional` is skipped while its key is absent, so the change can merge before
    the operator adds the secret; every other entry still fails the play without its key."""

    def setUp(self):
        tasks = yaml.safe_load((ROLE_DIR / "tasks" / "main.yml").read_text())
        self.tasks = {t["name"]: t for t in tasks}
        self.assert_task = self.tasks[
            "Every scoped push user has a password in SOPS (registry_scoped_push_passwords)"
        ]
        self.skip_task = self.tasks["Report scoped push users skipped for want of a SOPS password"]
        self.htpasswd_task = self.tasks["Generate bcrypt htpasswd for the scoped per-project push users"]
        self.env = jinja2.Environment()
        self.env.filters["bool"] = bool
        self.users = {p["user"]: p for p in _load_defaults()["registry_zot_scoped_push_users"]}

    def _eval(self, expr: str, item: dict, passwords) -> bool:
        ctx = {"item": item}
        if passwords is not None:
            ctx["registry_scoped_push_passwords"] = passwords
        return bool(self.env.compile_expression(expr)(**ctx))

    def test_only_strive_ci_push_is_optional(self):
        optional = [u for u, p in self.users.items() if p.get("password_optional")]
        self.assertEqual(optional, [_STRIVE_CI_USER])

    def test_assert_fails_without_key_unless_optional(self):
        that = self.assert_task["ansible.builtin.assert"]["that"]
        for passwords in (None, {}, {"unrelated": "x"}):
            self.assertTrue(self._eval(that, self.users[_STRIVE_CI_USER], passwords))
            self.assertFalse(self._eval(that, self.users["trueswarm-ci"], passwords))
            self.assertFalse(self._eval(that, self.users["workbricks-ci"], passwords))
        present = {u: "x" for u in self.users}
        for item in self.users.values():
            self.assertTrue(self._eval(that, item, present))

    def test_htpasswd_runs_exactly_when_the_key_exists(self):
        when = self.htpasswd_task["when"]
        skip_when = self.skip_task["when"]
        item = self.users[_STRIVE_CI_USER]
        for passwords in (None, {}, {"trueswarm-ci": "x"}):
            self.assertFalse(self._eval(when, item, passwords))
            self.assertTrue(self._eval(skip_when, item, passwords))
        self.assertTrue(self._eval(when, item, {_STRIVE_CI_USER: "x"}))
        self.assertFalse(self._eval(skip_when, item, {_STRIVE_CI_USER: "x"}))

    def test_password_flow_matches_the_other_scoped_users(self):
        module = self.htpasswd_task["community.general.htpasswd"]
        self.assertEqual(module["password"], "{{ registry_scoped_push_passwords[item.user] }}")
        self.assertEqual(module["path"], "{{ registry_zot_config_dir }}/htpasswd")
        self.assertEqual(module["crypt_scheme"], "bcrypt")
        self.assertIs(self.htpasswd_task["no_log"], True)
        self.assertEqual(self.htpasswd_task["loop"], "{{ registry_zot_scoped_push_users }}")

    def test_sops_rule_encrypts_the_password_map(self):
        # The key must go UNDER registry_scoped_push_passwords: the file's encrypted_regex covers
        # that map, and a new top-level key would be written in plaintext.
        # Both the creation rule AND the encrypted file's own metadata (which `sops edit` keeps)
        # are checked; reading the metadata decrypts nothing.
        secrets_path = "ansible/secrets/registry.sops.yaml"
        rules = yaml.safe_load((REPO_ROOT / ".sops.yaml").read_text())["creation_rules"]
        rule = next(r for r in rules if re.search(r.get("path_regex", "^$"), secrets_path))
        self.assertTrue(re.search(rule["encrypted_regex"], "registry_scoped_push_passwords"))
        meta = yaml.safe_load((REPO_ROOT / secrets_path).read_text())["sops"]
        self.assertTrue(re.search(meta["encrypted_regex"], "registry_scoped_push_passwords"))


if __name__ == "__main__":
    unittest.main()
