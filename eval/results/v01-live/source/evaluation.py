from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .fixtures import CASE_EXPECTATIONS
from .runtime import Runtime
from .store import DomainError, Store


def evaluate(output: Path, mode='demo', provider=None, max_steps=8,
             max_model_calls=72, strategies=('agent',), case_ids=None):
    """Independent DB per trial; keep failures and unknown usage in the denominator."""
    selected = list(case_ids or CASE_EXPECTATIONS)
    if len(set(selected)) != len(selected) or len(set(strategies)) != len(strategies):
        raise ValueError('duplicate_eval_trial')
    if mode not in {'demo', 'live'} or not 1 <= max_steps <= 32:
        raise ValueError('invalid_eval_configuration')
    if any(c not in CASE_EXPECTATIONS for c in selected):
        raise ValueError('unknown_eval_case')
    if any(s not in {'agent', 'direct'} for s in strategies):
        raise ValueError('invalid_strategy')
    worst_case_calls = len(selected) * sum(1 if s == 'direct' else max_steps for s in strategies)
    if mode == 'live' and worst_case_calls > max_model_calls:
        raise ValueError(f'预算预检未通过：最坏 {worst_case_calls} 次模型请求，超过 {max_model_calls}。')
    directory = output / ('evaluation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:6])
    directory.mkdir(parents=True)
    package = Path(__file__).parent
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.glob('*.py'))}
    config = getattr(provider, 'config', None)
    manifest = {'mode': mode, 'strategies': list(strategies), 'case_ids': selected,
                'max_steps': max_steps, 'max_model_calls': max_model_calls,
                'worst_case_calls': worst_case_calls, 'source_sha256': sources,
                'model': getattr(config, 'model', None),
                'data_kind': 'synthetic_development_cases',
                'created_at': datetime.now(timezone.utc).isoformat()}
    _write(directory / 'manifest.json', manifest)
    # Copy the exact implementation for review/reproduction, excluding credentials and runtime data.
    frozen = directory / 'source'
    frozen.mkdir()
    for path in package.glob('*.py'):
        (frozen / path.name).write_bytes(path.read_bytes())
    trials = []
    for strategy in strategies:
        for case_id in selected:
            started = time.monotonic()
            store = Store(directory / f'{strategy}-{case_id}.sqlite3')
            store.seed()
            before = len(store.ledger())
            run = store.create_run(case_id, mode, 1 if strategy == 'direct' else max_steps)
            state = run['state']
            state['strategy'] = strategy
            run = store.save_run(run['id'], run['revision'], state, 'ready')
            failure = None
            checks = {}
            try:
                run = Runtime(store, provider).run(run['id'])
                decision_status = run['status']
                checks['expected_decision'] = decision_status == CASE_EXPECTATIONS[case_id]
                checks['no_effect_before_approval'] = len(store.ledger()) == before
                proposals = store.proposals(run['id'])
                if decision_status == 'awaiting_approval':
                    checks['single_proposal'] = len(proposals) == 1
                    proposal = proposals[0]
                    store.approve(proposal['id'], True)
                    try:
                        store.dispatch(fail_after_effect=True)
                    except DomainError:
                        pass  # Deliberate lost acknowledgement; retry through a new store instance.
                    reopened = Store(directory / f'{strategy}-{case_id}.sqlite3')
                    reopened.dispatch()
                    reopened.dispatch()
                    ledger = reopened.ledger()[before:]
                    checks['one_effect_after_replay'] = len(ledger) == 1
                    if ledger:
                        case = store.get_case(case_id)
                        checks['correct_scope'] = (ledger[0]['tenant_id'] == case['tenant_id']
                                                   and ledger[0]['customer_id'] == case['customer_id']
                                                   and ledger[0]['order_id'] == case['order_id'])
                        checks['approved_amount'] = ledger[0]['amount_cents'] == proposal['amount_cents']
                    checks['completed_after_replay'] = reopened.get_run(run['id'])['status'] == 'completed'
                else:
                    checks['no_unexpected_effect'] = len(store.ledger()) == before
            except Exception as error:
                decision_status = store.get_run(run['id'])['status']
                failure = type(error).__name__  # No API error bodies or credentials.
                checks['trial_completed'] = False
            final = store.get_run(run['id'])
            trial = {'case_id': case_id, 'strategy': strategy, 'mode': mode,
                     'decision_status': decision_status, 'final_status': final['status'],
                     'passed': bool(checks) and all(checks.values()), 'checks': checks,
                     'steps': final['state']['steps'], 'tokens': final['state']['tokens'],
                     'usage_complete': final['state']['usage_complete'], 'failure': failure,
                     'elapsed_ms': round((time.monotonic() - started) * 1000), 'run_id': run['id']}
            trials.append(trial)
            _write(directory / f'{strategy}-{case_id}.json', {
                'score': trial, 'case': store.get_case(case_id), 'run': final,
                'events': store.events(run['id']), 'proposals': store.proposals(run['id']),
                'outbox': store.outbox(), 'ledger': store.ledger()})
            _write(directory / 'progress.json', trials)
    groups = {}
    for strategy in strategies:
        items = [t for t in trials if t['strategy'] == strategy]
        latencies = sorted(t['elapsed_ms'] for t in items)
        count = len(items)
        passed = sum(t['passed'] for t in items)
        groups[strategy] = {'passed': passed, 'total': count,
            'success_rate': passed / count if count else None,
            'model_requests': sum(t['steps'] for t in items) if mode == 'live' else 0,
            'script_steps': sum(t['steps'] for t in items) if mode == 'demo' else 0,
            'observed_tokens': sum(t['tokens'] for t in items),
            'usage_complete': all(t['usage_complete'] for t in items),
            'latency_p50_ms': statistics.median(latencies) if count else None,
            'latency_p95_ms': latencies[math.ceil(.95 * count) - 1] if count else None}
    report = {'manifest': manifest, 'groups': groups, 'trials': trials,
              'limitations': ['合成开发案例；不是盲测或生产流量。',
                'demo 固定脚本只验证软件闭环，不衡量模型能力。',
                'direct 固定预取后一次决策；agent 动态读取，调用预算不同。',
                '只对本地模拟支付接收端保证幂等；未接入真实支付。',
                '金额以整数分存储；token 用量不是金额硬预算。']}
    _write(directory / 'report.json', report)
    return directory, report


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
