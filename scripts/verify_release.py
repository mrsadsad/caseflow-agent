"""Install a wheel into an isolated target and test bundled data/UI and business flow."""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import tempfile


SMOKE = r'''
from pathlib import Path
import json
import threading
import urllib.request
import caseflow
from caseflow.runtime import Runtime
from caseflow.store import Store, DomainError
from caseflow.server import make_server
assert Path(caseflow.__file__).resolve().is_relative_to(Path('site').resolve())
store = Store('smoke.sqlite3')
store.seed()
assert len(store.list_cases()) == 9
run = Runtime(store).run(store.create_run('refund_valid', 'demo')['id'])
assert run['status'] == 'awaiting_approval'
proposal = store.proposals(run['id'])[0]
store.approve(proposal['id'], True)
try:
    store.dispatch(fail_after_effect=True)
except DomainError as error:
    assert error.code == 'simulated_ack_loss'
assert len(store.ledger()) == 1
store.dispatch()
assert len(store.ledger()) == 1
assert store.get_run(run['id'])['status'] == 'completed'
server = make_server(store, lambda: Runtime(store), port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with urllib.request.urlopen(server.origin) as response:
        assert 'CaseFlow' in response.read().decode('utf-8')
    with urllib.request.urlopen(server.origin + '/api/bootstrap') as response:
        assert len(json.load(response)['cases']) == 9
finally:
    server.shutdown()
    server.server_close()
    thread.join()
print('PASS: isolated wheel import, bundled fixtures/UI, approval, crash replay, HTTP')
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='caseflow-release-') as temp:
        target = Path(temp) / 'site'
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-compile',
                        '--target', str(target), str(wheel)], check=True, cwd=temp)
        env = {**os.environ, 'PYTHONPATH': str(target), 'PYTHONIOENCODING': 'utf-8'}
        subprocess.run([sys.executable, '-c', SMOKE], check=True, cwd=temp, env=env)


if __name__ == '__main__':
    main()
