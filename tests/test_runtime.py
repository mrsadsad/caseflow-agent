from __future__ import annotations

import copy
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from caseflow.provider import ProviderError
from caseflow.runtime import Runtime, SYSTEM
from caseflow.store import DomainError, Store


def tool_message(name, arguments, call_id='call_1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append({'messages': copy.deepcopy(messages), 'tools': copy.deepcopy(tools)})
        if not self.responses:
            raise AssertionError('Unexpected extra model request')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return {'message': copy.deepcopy(response), 'tokens': 5}


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'runtime.sqlite3')
        self.store.seed()
        self.case = self.store.get_case('refund_valid')

    def make_run(self, max_steps=4, strategy=None):
        run = self.store.create_run(self.case['id'], 'live', max_steps=max_steps)
        if strategy:
            run['state']['strategy'] = strategy
            run = self.store.save_run(run['id'], run['revision'], run['state'], run['status'])
        return run

    def results(self, run_id):
        return [event['payload'] for event in self.store.events(run_id)
                if event['kind'] == 'tool_feedback']

    def test_text_cannot_complete_business_task_and_is_bounded(self):
        response = {'role': 'assistant', 'content': '已经退款成功。'}
        provider = ScriptedProvider(response, response)
        run = self.make_run(max_steps=2)
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['state']['steps'], 2)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(self.store.proposals(run['id']), [])
        self.assertEqual(self.store.ledger(), [])

    def test_unknown_request_is_charged_against_budget_after_resume(self):
        run = self.make_run(max_steps=1)
        failed = Runtime(self.store, ScriptedProvider(
            ProviderError('provider_connection_failed'))).run(run['id'])
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['state']['steps'], 1)
        self.assertFalse(failed['state']['usage_complete'])
        self.assertTrue(failed['state']['in_flight'])
        provider = ScriptedProvider()
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['state']['steps'], 1)
        self.assertFalse(result['state']['usage_complete'])
        self.assertEqual(provider.calls, [])
        self.assertIn('uncertain_model_request',
                      [event['kind'] for event in self.store.events(run['id'])])

    def test_resume_uses_only_remaining_budget_and_keeps_unknown_usage(self):
        run = self.make_run(max_steps=2)
        Runtime(self.store, ScriptedProvider(
            ProviderError('provider_connection_failed'))).run(run['id'])
        provider = ScriptedProvider(tool_message('escalate', {'reason': '请人工核验订单。'}))
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(result['state']['steps'], 2)
        self.assertFalse(result['state']['usage_complete'])
        self.assertEqual(len(provider.calls), 1)

    def test_saved_tool_response_replays_without_another_model_request(self):
        run = self.make_run(max_steps=1)
        message = tool_message('escalate', {'reason': '持久化回复待处理，转人工。'})
        state = run['state']
        state.update({
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': json.dumps(self.case)}, message],
            'steps': 1, 'pending_calls': message['tool_calls'],
        })
        self.store.save_run(run['id'], run['revision'], state, 'interrupted')
        provider = ScriptedProvider()
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(provider.calls, [])
        self.assertEqual(result['state']['pending_calls'], [])
        self.assertEqual(result['state']['steps'], 1)
        self.assertEqual(self.results(run['id'])[0]['name'], 'escalate')

    def test_changed_protocol_refuses_resume_before_request(self):
        run = self.make_run()
        run['state']['protocol_hash'] = 'previous-runtime-definition'
        self.store.save_run(run['id'], run['revision'], run['state'], 'interrupted')
        provider = ScriptedProvider()
        with self.assertRaisesRegex(DomainError, 'runtime_protocol_changed'):
            Runtime(self.store, provider).run(run['id'])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.get_run(run['id'])['status'], 'interrupted')

    def test_proposal_created_before_checkpoint_replays_without_duplication(self):
        run = self.make_run(max_steps=1)
        policies = self.store.search_policy(run['id'], '退款')
        order = self.store.get_order(run['id'], self.case['order_id'])
        arguments = {'order_id': order['id'], 'amount_cents': order['amount_cents'],
                     'reason': '本次申请全部剩余可退金额。',
                     'policy_ids': [next(p['id'] for p in policies if p['kind'] == 'refund')]}
        # Model response is durable, but its business tool finished before the runtime checkpoint.
        message = tool_message('propose_refund', arguments)
        state = run['state']
        state.update({
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': json.dumps(self.case)}, message],
            'steps': 1, 'pending_calls': message['tool_calls'],
        })
        self.store.save_run(run['id'], run['revision'], state, 'interrupted')
        original = self.store.create_proposal(run['id'], **arguments)
        provider = ScriptedProvider()
        result = Runtime(self.store, provider).run(run['id'])
        proposals = self.store.proposals(run['id'])
        self.assertEqual(result['status'], 'awaiting_approval')
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]['id'], original['id'])
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.store.ledger(), [])

    def test_failed_dispatch_cannot_restart_the_model_business_action(self):
        run = self.store.create_run(self.case['id'], 'demo')
        Runtime(self.store).run(run['id'])
        proposal = self.store.proposals(run['id'])[0]
        self.store.approve(proposal['id'], True)
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute('UPDATE orders SET version=version+1 WHERE id=?',
                               (self.case['order_id'],))
            connection.commit()
        delivery = self.store.dispatch()
        self.assertEqual(delivery['status'], 'failed')
        self.assertEqual(self.store.get_run(run['id'])['status'], 'error')
        provider = ScriptedProvider()
        with self.assertRaisesRegex(DomainError, 'business_action_requires_manual_review'):
            Runtime(self.store, provider).run(run['id'])
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(self.store.proposals(run['id'])), 1)
        self.assertEqual(self.store.ledger(), [])

    def test_direct_prefetches_same_sources_and_makes_one_decision(self):
        provider = ScriptedProvider(tool_message('escalate', {'reason': '转人工核验。'}))
        run = self.make_run(max_steps=8, strategy='direct')
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(len(provider.calls), 1)
        call = provider.calls[0]
        prefetched = [m for m in call['messages'] if m['role'] == 'tool']
        self.assertEqual(len(prefetched), 2)
        self.assertIn('order', json.loads(prefetched[0]['content']))
        self.assertIn('policies', json.loads(prefetched[1]['content']))
        self.assertFalse({'get_order', 'search_policy'} &
                         {tool['function']['name'] for tool in call['tools']})
        self.assertEqual(result['state']['steps'], 1)

    def test_direct_cannot_get_extra_model_calls_from_larger_run_budget(self):
        provider = ScriptedProvider({'role': 'assistant', 'content': '暂时无法确定。'})
        run = self.make_run(max_steps=8, strategy='direct')
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['state']['steps'], 1)
        self.assertEqual(len(provider.calls), 1)

    def test_agent_reads_sources_in_its_own_tool_loop(self):
        provider = ScriptedProvider(
            tool_message('get_order', {'order_id': self.case['order_id']}, 'read-order'),
            tool_message('search_policy', {'query': '退款'}, 'read-policy'),
            tool_message('escalate', {'reason': '转人工核验。'}, 'decision'),
        )
        run = self.make_run(max_steps=3, strategy='agent')
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual(len(provider.calls), 3)
        self.assertFalse(any(m['role'] == 'tool' for m in provider.calls[0]['messages']))
        self.assertEqual([r['name'] for r in self.results(run['id'])],
                         ['get_order', 'search_policy', 'escalate'])

    def test_invalid_arguments_return_feedback_without_tool_effect(self):
        for malformed in ('{', '[]', '{"amount_cents": NaN}'):
            with self.subTest(arguments=malformed):
                message = tool_message('propose_refund', {})
                message['tool_calls'][0]['function']['arguments'] = malformed
                provider = ScriptedProvider(message,
                    tool_message('escalate', {'reason': '参数错误后转人工。'}, 'next'))
                run = self.make_run(max_steps=2)
                result = Runtime(self.store, provider).run(run['id'])
                self.assertEqual(result['status'], 'escalated')
                self.assertEqual(self.results(run['id'])[0]['result']['error'], 'invalid_arguments')
                self.assertEqual(self.store.proposals(run['id']), [])
        self.assertEqual(self.store.ledger(), [])

    def test_terminal_action_stops_extra_bundled_tools(self):
        message = tool_message('escalate', {'reason': '人工处理。'}, 'terminal')
        message['tool_calls'].extend(tool_message('get_order',
            {'order_id': self.case['order_id']}, 'must-not-execute')['tool_calls'])
        provider = ScriptedProvider(message)
        run = self.make_run()
        result = Runtime(self.store, provider).run(run['id'])
        self.assertEqual(result['status'], 'escalated')
        self.assertEqual([r['name'] for r in self.results(run['id'])], ['escalate'])
        self.assertEqual([event['payload']['name'] for event in self.store.events(run['id'])
                          if event['kind'] == 'tool_call'], ['escalate'])
        self.assertEqual(result['state']['pending_calls'], [])
        self.assertEqual(self.store.proposals(run['id']), [])


if __name__ == '__main__':
    unittest.main()
