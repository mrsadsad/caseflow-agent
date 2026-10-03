import json
import tempfile
import unittest
from pathlib import Path

from caseflow.evaluation import evaluate
from caseflow.fixtures import CASE_EXPECTATIONS


class EvaluationTests(unittest.TestCase):
    def test_demo_both_strategies_record_all_trials_and_replay_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, report = evaluate(Path(tmp), strategies=('direct', 'agent'))
            self.assertEqual(len(report['trials']), 2 * len(CASE_EXPECTATIONS))
            failures = [t for t in report['trials'] if not t['passed']]
            self.assertEqual(failures, [])
            self.assertEqual(report['groups']['agent']['model_requests'], 0)
            self.assertTrue((directory / 'source' / 'runtime.py').is_file())
            self.assertEqual(json.loads((directory / 'report.json').read_text(encoding='utf-8')), report)
            for trial in report['trials']:
                self.assertTrue(trial['checks']['no_effect_before_approval'])
                if trial['decision_status'] == 'awaiting_approval':
                    self.assertTrue(trial['checks']['one_effect_after_replay'])

    def test_live_budget_preflight_before_any_calls_or_output(self):
        class Never:
            def complete(self, *_):
                raise AssertionError('must not call model')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, '预算预检'):
                evaluate(Path(tmp), 'live', Never(), max_steps=8, max_model_calls=1)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_failures_remain_in_denominator(self):
        class Bad:
            def complete(self, *_):
                return {'message': {'role': 'assistant', 'content': '已经退款'}, 'tokens': None}
        with tempfile.TemporaryDirectory() as tmp:
            _, report = evaluate(Path(tmp), 'live', Bad(), max_steps=1,
                max_model_calls=1, case_ids=['refund_valid'])
            self.assertEqual(report['groups']['agent']['total'], 1)
            self.assertEqual(report['groups']['agent']['passed'], 0)
            self.assertEqual(report['trials'][0]['decision_status'], 'budget_exhausted')
            self.assertFalse(report['groups']['agent']['usage_complete'])

    def test_duplicate_cases_rejected_to_prevent_reusing_mutated_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'duplicate_eval_trial'):
                evaluate(Path(tmp), case_ids=['refund_valid', 'refund_valid'])


if __name__ == '__main__':
    unittest.main()
