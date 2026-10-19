"""Regression coverage for the independent v1 review findings."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
from unittest.mock import patch
from test_pipeline import ROOT, load_module
from runtime_io import read_json, write_json


class ReviewFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.runner=load_module('review_fixes_runner',ROOT/'tools/run_demo.py')

    def prepare(self,folder,catalog):
        config=folder/'config.json';write_json(config,{'mode':'mock'})
        db=folder/'source.sqlite';db.touch()
        args=Namespace(input=str(ROOT/'examples/case-input.json'),config=str(config),database=str(db),output_dir=str(folder/'run'))
        with patch('fetch_evidence.collect_catalog',return_value=catalog),contextlib.redirect_stdout(io.StringIO()):
            self.runner.prepare(args)

    def test_catalog_failure_blocks_before_agent_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with self.assertRaisesRegex(ValueError,'run blocked'):
                self.prepare(folder,{'terms':[],'errors':[{'code':'CONNECTION_ERROR'}],'calls':[]})
            run=folder/'run';m=read_json(run/'manifest.json')
            self.assertEqual(m['phase'],'blocked')
            self.assertEqual(m['steps']['1']['status'],'blocked')
            self.assertFalse((run/'handoff.json').exists())
            self.assertEqual(read_json(run/'evidence/catalog.json')['errors'][0]['code'],'CONNECTION_ERROR')

    def test_runtime_paths_are_not_in_public_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);self.prepare(folder,{'terms':[],'errors':[],'calls':[]})
            run=folder/'run';m=read_json(run/'manifest.json')
            self.assertNotIn('config_path',m);self.assertNotIn('database_path',m)
            self.assertNotIn(str(folder),(run/'manifest.json').read_text())
            self.assertEqual(Path(read_json(run/'.runtime.json')['database_path']), (folder/'source.sqlite').resolve())

    def test_report_distinguishes_plan_and_offer_counts(self):
        run=ROOT/'outputs/normal-v1-1'
        report=(run/'review-report.md').read_text()
        self.assertIn('双通过计划数：2',report)
        self.assertIn('去重后的可用供给数：1',report)
        self.assertIn('| T0001 / A00001 | O00001 |',report)
        self.assertIn('| T0001 / A00999 | O00001 |',report)

    def test_snapshot_is_small_and_has_one_bounded_history_array(self):
        run=ROOT/'outputs/normal-v1-1'
        self.assertLess(sum(p.stat().st_size for p in run.rglob('*') if p.is_file()),1_000_000)
        history=read_json(run/'evidence/history.json')
        self.assertNotIn('candidate_gap_evidence',history)
        self.assertEqual(len(history['evidence_rows']),500)
        self.assertEqual(history['verified_gap_count'],6000)
        data=read_json(run/'steps/00_input.json')['data']
        self.assertNotIn('history',data)
        self.assertEqual(data['history_ref']['path'],'evidence/history.json')

    def test_rebuild_does_not_fabricate_new_model_calls(self):
        m=read_json(ROOT/'outputs/normal-v1-1/manifest.json')
        self.assertEqual(m['revision']['new_model_calls'],0)
        self.assertEqual(m['revision']['new_product_api_calls'],0)
        self.assertEqual(len(m['model_calls']),2)

if __name__=='__main__':unittest.main()
