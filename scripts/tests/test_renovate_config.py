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


class AnsibleRequirementsTest(unittest.TestCase):
    def test_no_trailing_comment_on_name_lines(self):
        # Renovate's ansible-galaxy manager takes the rest of the `name:` line as the collection name.
        text = (REPO / 'ansible/requirements.yml').read_text(encoding='utf-8')
        for line in text.splitlines():
            if re.match(r'\s*-?\s*name:', line):
                self.assertNotIn('#', line, line)


if __name__ == '__main__':
    unittest.main()
