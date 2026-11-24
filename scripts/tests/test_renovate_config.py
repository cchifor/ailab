"""Pins the Renovate lookup fixes (renovate.json, the renovate-config ConfigMap, ansible/requirements.yml).

Each of these failed silently before: Renovate logs a lookup failure or drops a rule without failing
its run, and renovate-config-validator accepts every one of them.
"""
import json
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[2]
CONFIGMAP = REPO / 'kubernetes/apps/apps/renovate/config-configmap.yaml'
TAGLESS_PINS = {
    'registry.chifor.me/llm-router/router',
    'registry.chifor.me/relay/control-plane',
    'registry.chifor.me/headlamp-k8s/headlamp-plugin-flux',
}


def config_js():
    text = CONFIGMAP.read_text(encoding='utf-8')
    return text.split('config.js: |', 1)[1]


def reject_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError('duplicate JSON keys: %s' % dupes)
    return dict(pairs)


class GlobalConfigTest(unittest.TestCase):
    def test_single_host_rules_key(self):
        # A JS object literal keeps only the LAST of two equal keys, so a second `hostRules:` would
        # silently drop the registry.chifor.me grant (or the other rules). Add host rules to the one array.
        keys = re.findall(r'''^\s*['"]?hostRules['"]?\s*:''', config_js(), re.M)
        self.assertEqual(len(keys), 1, 'config.js must define hostRules exactly once')

    def test_registry_grant_is_scoped_to_docker(self):
        self.assertRegex(
            config_js(),
            r"\{\s*matchHost:\s*'registry\.chifor\.me',\s*hostType:\s*'docker',\s*allowInternal:\s*true\s*\}")

    def test_topic_discovery_keeps_a_repository_floor(self):
        # Discovery by topic alone fails silently: a broken topic search or a lost grant makes Renovate run zero
        # repositories and exit 0. With `repositories` next to it, an empty discovery keeps that list instead.
        js = config_js()
        self.assertRegex(js, r"(?m)^\s*autodiscover:\s*true,")
        self.assertRegex(js, r"(?m)^\s*autodiscoverTopics:\s*\['renovate'\],")
        m = re.search(r"(?m)^\s*repositories:\s*\[([^\]]*)\],", js)
        self.assertIsNotNone(m, 'config.js must keep a repositories floor next to autodiscover')
        self.assertIn("'cchifor/ailab'", m.group(1))
        for key in ('autodiscover', 'repositories'):
            self.assertEqual(len(re.findall(r"(?m)^\s*%s:" % key, js)), 1, key)


class RepoConfigTest(unittest.TestCase):
    def setUp(self):
        text = (REPO / 'renovate.json').read_text(encoding='utf-8')
        self.config = json.loads(text, object_pairs_hook=reject_duplicate_keys)

    def test_tagless_registry_pins_are_disabled(self):
        disabled = set()
        for rule in self.config['packageRules']:
            if rule.get('enabled') is False and not rule.get('matchFileNames'):
                disabled.update(rule.get('matchPackageNames', []))
        self.assertLessEqual(TAGLESS_PINS, disabled)

    def test_tagged_registry_mirrors_stay_tracked(self):
        for rule in self.config['packageRules']:
            if rule.get('enabled') is False:
                for name in rule.get('matchPackageNames', []):
                    self.assertFalse(name in ('registry.chifor.me/**', '/^registry\\.chifor\\.me\\//'),
                                     'a rule disables the whole registry: %s' % name)


    def test_campaign_freeze_holds_relay_paths(self):
        # The Relay qualification campaign freeze (R1 to the canary): the last rule disables every
        # Relay, Router, journal, ledger and researcher path, so no earlier rule re-enables them.
        rule = self.config['packageRules'][-1]
        # No other matcher may narrow it (matchManagers, matchDatasources, matchUpdateTypes, ...).
        self.assertEqual(set(rule), {'description', 'matchFileNames', 'enabled'})
        self.assertIs(rule['enabled'], False)
        frozen = {'kubernetes/apps/apps/relay/**', 'kubernetes/apps/apps/llm-router/**',
                  'kubernetes/apps/relay-*/**', 'kubernetes/components/relay-*/**',
                  'kubernetes/components/llm-router-*/**', 'kubernetes/optional/relay-*/**'}
        # The two OpenBao Jobs that provision the Relay budget and Router credentials: an image bump would
        # re-run a provisioning Job inside the campaign.
        jobs = {'kubernetes/apps/infrastructure/security/openbao/relay-budget-provision-job.yaml',
                'kubernetes/apps/infrastructure/security/openbao/relay-router-provision-job.yaml'}
        self.assertEqual(set(rule['matchFileNames']), frozen | jobs)
        for path in sorted(jobs):
            self.assertTrue((REPO / path).is_file(), path)
        # Every pattern must match real manifests, so a typo or a rename cannot lift the freeze quietly.
        for pattern in sorted(frozen):
            # pathlib's trailing '**' yields directories only; '**/*' yields the files under them.
            files = REPO.glob(pattern[:-2] + '**/*')
            self.assertTrue(any(f.is_file() and f.suffix in ('.yaml', '.yml') for f in files),
                            'no manifest matches %s' % pattern)
        # The files Renovate #1932 changed (relay-postgres and backup-database) are inside the freeze.
        for path in ('kubernetes/apps/apps/relay/postgres.yaml', 'kubernetes/apps/apps/relay/backup.yaml',
                     'kubernetes/apps/apps/relay/relay.yaml'):
            self.assertTrue((REPO / path).is_file(), path)
            self.assertTrue(path.startswith('kubernetes/apps/apps/relay/'))

    def test_litellm_held_at_its_audited_minor(self):
        # LiteLLM 1.110 changes the chatgpt/ route's wire body and drops accounting tables (#1968): the hold
        # stays until that audit lands, and the three gateway manifests stay on a tag the hold allows.
        holds = [r for r in self.config['packageRules'] if r.get('matchPackageNames') == ['ghcr.io/berriai/litellm']]
        self.assertEqual(len(holds), 1)
        self.assertEqual(set(holds[0]), {'description', 'matchPackageNames', 'allowedVersions'})
        allowed = holds[0]['allowedVersions']
        self.assertTrue(allowed.startswith('/') and allowed.endswith('/'), allowed)
        pattern = re.compile(allowed[1:-1])
        for name in ('litellm.yaml', 'litellm-local.yaml', 'litellm-vkeys.yaml'):
            text = (REPO / 'kubernetes/apps/apps/ai' / name).read_text(encoding='utf-8')
            tags = re.findall(r'ghcr\.io/berriai/litellm:([^@\s]+)@sha256:[0-9a-f]{64}', text)
            self.assertTrue(tags, name)
            for tag in tags:
                self.assertRegex(tag, pattern, '%s pins %s outside the hold' % (name, tag))
        self.assertIsNone(pattern.search('1.110.3'), 'the hold must exclude the unaudited 1.110 line')
        self.assertIsNone(pattern.search('v1.110.3'), 'the hold must exclude the unaudited 1.110 line')


class AnsibleRequirementsTest(unittest.TestCase):
    def test_no_trailing_comment_on_name_lines(self):
        # Renovate's ansible-galaxy manager takes the rest of the `name:` line as the collection name.
        text = (REPO / 'ansible/requirements.yml').read_text(encoding='utf-8')
        for line in text.splitlines():
            if re.match(r'\s*-?\s*name:', line):
                self.assertNotIn('#', line, line)


if __name__ == '__main__':
    unittest.main()
