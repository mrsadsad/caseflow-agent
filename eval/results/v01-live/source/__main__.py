from __future__ import annotations

import argparse
import json
from pathlib import Path

from .provider import Config, Provider, load_env
from .runtime import Runtime
from .store import DomainError, Store


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='CaseFlow · 可恢复的售后业务 Agent')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'run', 'resume', 'list', 'approve', 'dispatch', 'evaluate', 'doctor'):
        sub = commands.add_parser(name)
        sub.add_argument('--db', type=Path, default=Path('runs/caseflow.sqlite3'))
        sub.add_argument('--env-file', type=Path, default=Path('.env'))
        if name in ('run', 'evaluate'):
            sub.add_argument('--mode', choices=['demo', 'live'], default='demo')
            sub.add_argument('--max-steps', type=int, default=8)
        if name == 'serve':
            sub.add_argument('--port', type=int, default=8780)
        elif name == 'run':
            sub.add_argument('--case', default='refund_valid')
            sub.add_argument('--strategy', choices=['agent', 'direct'], default='agent')
        elif name == 'resume':
            sub.add_argument('run_id')
        elif name == 'approve':
            sub.add_argument('proposal_id')
            sub.add_argument('--reject', action='store_true')
        elif name == 'dispatch':
            sub.add_argument('--crash-after-effect', action='store_true')
        elif name == 'evaluate':
            sub.add_argument('--output', type=Path, default=Path('runs'))
            sub.add_argument('--max-model-calls', type=int, default=72)
            sub.add_argument('--strategy', choices=['agent', 'direct', 'both'], default='both')
            sub.add_argument('--cases', nargs='+')
    args = parser.parse_args(argv)
    load_env(args.env_file)
    config = Config.from_env()
    try:
        if hasattr(args, 'max_steps') and not 1 <= args.max_steps <= 32:
            raise ValueError('max_steps must be between 1 and 32')
        if args.command == 'doctor':
            _print({'python_runtime': 'OK', 'core_dependencies': 'stdlib only',
                    'model_configured': bool(config.api_key), 'model': config.model,
                    'database': str(args.db.resolve()), 'payment_backend': 'local simulator'})
            return 0
        if args.command == 'evaluate':
            from .evaluation import evaluate
            provider = Provider(config) if args.mode == 'live' else None
            directory, report = evaluate(args.output, args.mode, provider, args.max_steps,
                args.max_model_calls, ('direct', 'agent') if args.strategy == 'both' else (args.strategy,), args.cases)
            _print({'report': str((directory / 'report.json').resolve()), 'groups': report['groups']})
            return 0 if all(t['passed'] for t in report['trials']) else 1
        store = Store(args.db)
        store.seed()
        if args.command == 'serve':
            from .server import serve
            store.recover()
            provider = Provider(config) if config.api_key else None
            serve(store, lambda: Runtime(store, provider), port=args.port)
        elif args.command == 'list':
            _print({'cases': store.list_cases(), 'runs': store.list_runs(),
                    'proposals': store.proposals(), 'outbox': store.outbox(), 'ledger': store.ledger()})
        elif args.command in ('run', 'resume'):
            if args.command == 'run':
                run = store.create_run(args.case, args.mode, 1 if args.strategy == 'direct' else args.max_steps)
                state = run['state']
                state['strategy'] = args.strategy
                run = store.save_run(run['id'], run['revision'], state, 'ready')
            else:
                store.recover()
                run = store.get_run(args.run_id)
            print('run_id=' + run['id'], flush=True)
            provider = Provider(config) if run['mode'] == 'live' else None
            result = Runtime(store, provider).run(run['id'])
            _print({'id': result['id'], 'status': result['status'],
                    'steps': result['state']['steps'], 'proposals': store.proposals(run['id'])})
            return 1 if result['status'] in {'error', 'budget_exhausted'} else 0
        elif args.command == 'approve':
            _print(store.approve(args.proposal_id, not args.reject))
        elif args.command == 'dispatch':
            _print(store.dispatch(fail_after_effect=args.crash_after_effect))
        return 0
    except (DomainError, ValueError, RuntimeError, KeyError) as error:
        # Application exceptions contain only safe codes. Never print raw provider errors.
        print(f'{type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
