"""Deterministic integration fixtures; these are NOT model-evaluation predictions."""
import contextlib
import copy
import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from test_pipeline import ROOT, load_module
from runtime_io import read_json, write_json
from validate_outputs import validate


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_module('runner_integration', ROOT / 'tools/run_demo.py')

    def prepare(self, folder):
        config = folder / 'config.json'
        write_json(config, {'mode': 'mock'})
        database = folder / 'fixture.sqlite'
        database.touch()  # L0 must never open this intentionally invalid DB.
        run = folder / 'run'
        args = Namespace(input=str(ROOT / 'examples/high-risk-input.json'), config=str(config),
                         database=str(database), output_dir=str(run))
        with patch('fetch_evidence.collect_catalog', return_value={'terms': [], 'errors': [], 'calls': []}), contextlib.redirect_stdout(io.StringIO()):
            self.runner.prepare(args)
        return run

    def decision(self, run):
        return {'run_id': read_json(run / 'manifest.json')['run_id'], 'phase': 'query',
                'producer': {'type': 'agent', 'model': 'test-fixture-not-a-live-model'},
                'profile': {'risk': 'L0', 'attributes': dict.fromkeys([
                    'brand_name', 'generic_name', 'dosage_form', 'pack_count', 'pack_unit', 'audience']),
                    'attribute_evidence': {}, 'reason': 'TEST FIXTURE: admission exclusion',
                    'rule_refs': ['ADM-002'], 'evidence_refs': ['evidence/task.json#/raw_query']},
                'rewrites': []}

    def test_high_risk_finishes_six_artifacts_without_sql_or_item_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); run = self.prepare(folder)
            decision = folder / 'decision.json'; write_json(decision, self.decision(run))
            with patch('query_history.analyze', side_effect=AssertionError('L0 cannot run SQL')), patch('fetch_evidence.collect_candidates', side_effect=AssertionError('L0 cannot fetch items')), contextlib.redirect_stdout(io.StringIO()):
                self.runner.resume_query(Namespace(run_dir=str(run), decision=str(decision)))
            manifest = read_json(run / 'manifest.json')
            self.assertEqual(manifest['phase'], 'complete')
            self.assertEqual(len(manifest['model_calls']), 1)
            self.assertEqual(len(list((run / 'steps').glob('*.json'))), 6)
            self.assertEqual(manifest['steps']['2']['status'], 'skipped')
            self.assertEqual(manifest['steps']['6']['status'], 'skipped')
            self.assertEqual(read_json(run / 'steps/40_final_review.json')['data']['recommendation'], 'reject')
            self.assertEqual(validate(run), [])

    def test_modified_evidence_is_blocked_before_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.prepare(Path(tmp)); manifest = read_json(run / 'manifest.json')
            task = read_json(run / 'evidence/task.json'); task['raw_query'] = 'tampered'
            write_json(run / 'evidence/task.json', task)
            with self.assertRaisesRegex(ValueError, 'Evidence changed'):
                self.runner.check_frozen(run, manifest)

    def test_modified_skill_fingerprint_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.prepare(Path(tmp)); manifest = read_json(run / 'manifest.json')
            fake = copy.deepcopy(manifest['source_hashes']); fake['SKILL.md'] = 'changed'
            with patch.object(self.runner, 'source_hashes', return_value=fake), self.assertRaisesRegex(ValueError, 'Skill resources changed'):
                self.runner.check_frozen(run, manifest)


if __name__ == '__main__':
    unittest.main()
