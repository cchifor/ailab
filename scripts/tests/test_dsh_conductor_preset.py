"""Safety contract for the human-controlled conductor front-door preset."""
import pathlib
import unittest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/apps/dsh'
TEAMS = APP / 'agent-teams'
PRESET = 'team-dsh-conductor'

class ConductorPresetTests(unittest.TestCase):
    def test_distinct_discoverable_metadata(self):
        meta = yaml.safe_load((TEAMS / f'{PRESET}.preset.yml').read_text())
        self.assertEqual(meta['name'], 'Team Conductor (0.1.0)')
        self.assertIn('/conductor', meta['description'])
        legacy = yaml.safe_load((TEAMS / 'team-conductor.preset.yml').read_text())
        self.assertEqual(legacy['name'], 'Conductor')

    def test_composition_registers_only_a_scoped_persona(self):
        rows = yaml.safe_load((TEAMS / f'{PRESET}.agent.cordis.yml').read_text())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '@deepseek-ai/dsh-persona')
        self.assertFalse(rows[0]['config'].get('complete', False))
        self.assertFalse(rows[0].get('disabled', False))
        # No service, tool, model or registry rows can be added accidentally.
        self.assertNotIn('group', rows[0])
        self.assertNotIn('isolate', rows[0])

    def test_explicit_start_and_ledger_scope_are_explained(self):
        rows = yaml.safe_load((TEAMS / f'{PRESET}.agent.cordis.yml').read_text())
        text = rows[0]['config']['prefix']
        for phrase in ['/conductor status', '/conductor start', '/conductor pause',
                       '/conductor resume', '40', '40M', 'one production run',
                       'cchifor/dsh-team-conductor', 'outside', 'Never start']:
            self.assertIn(phrase, text)

    def test_existing_projection_accepts_both_files(self):
        document = yaml.safe_load((APP / 'kustomization.yaml').read_text())
        generators = document['configMapGenerator']
        files = [file for generator in generators for file in generator.get('files', [])]
        for suffix in ['preset.yml', 'agent.cordis.yml']:
            self.assertIn(f'agent-teams/{PRESET}.{suffix}', files)
            for legacy in ['team-solo', 'team-conductor', 'team-review']:
                self.assertIn(f'agent-teams/{legacy}.{suffix}', files)
        deployment = (APP / 'deployment.yaml').read_text()
        self.assertIn('for f in /seed/team-*.preset.yml', deployment)
        self.assertIn('mountPath: /dsh-teams', deployment)

if __name__ == '__main__':
    unittest.main()
