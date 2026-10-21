"""Single status invocation must settle brief empty history without replaying input."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--runtime', type=Path, required=True)
args = parser.parse_args()
source = args.source.resolve()
sys.path[:0] = [str(source / 'tests'), str(source)]
import test_rust_runtime_acceptance as fixture

fixture.RUNTIME = args.runtime.resolve()
f = fixture.PrivateRuntime()
try:
    f.env.pop('CODEX_THREAD_ID', None)
    f.fake.parent_turns = []
    subprocess.run([str(f.runtime), 'ultracode', 'provenance', '--thread', fixture.PARENT,
        '--load-kind', 'fresh', '--developer-instructions-stdin', '--json'], env=f.env,
        input=(source / 'docs/ultracode-bootstrap.txt').read_text(), text=True,
        capture_output=True, check=True, timeout=10)
    f.cli('ultracode', 'on', '--thread', fixture.PARENT, '--json')
    submission, turn = str(uuid.uuid4()), str(uuid.uuid4())
    f.cli('goal', 'input', '--thread', fixture.PARENT, '--submission-id', submission, '--json')
    f.fake.parent_turns = [{'id': turn, 'status': 'inProgress'}]
    f.fake.entries = [{'turnId': turn, 'item': {'type': 'userMessage', 'clientId': submission}}]
    original = f.server.reply
    reads = []

    def reply(method, params):
        if method == 'thread/items/list':
            reads.append(params)
            if len(reads) <= 2:
                return {'result': {'data': [], 'nextCursor': None}}
        return original(method, params)

    f.server.reply = reply
    before = len(f.server.requests)
    result = f.cli('ultracode', 'status', '--thread', fixture.PARENT, '--json')
    status = json.loads(result.stdout)
    assert status['availability'] == 'available' and status['effective'], status.get('unavailable_reason')
    assert status['effective']['enabled'] and status['effective']['submission'] == submission
    assert status['effective']['turn'] == turn and len(reads) == 3
    assert all(p['threadId'] == fixture.PARENT and p['turnId'] == turn for p in reads)
    projection = json.loads((f.home / 'codex-plus/ultracode' / (fixture.PARENT + '.json')).read_text())
    assert projection['effective'] == status['effective'] and projection['availability'] == 'available'
    assert not status['pending'] and not result.stderr
    assert all(r['method'] in ('initialize', 'initialized', 'thread/read', 'thread/turns/list',
        'thread/items/list') for r in f.server.requests[before:]), 'input replayed'
    print(json.dumps({'passed': True, 'entrypoint': 'status', 'empty_pages': 2,
        'item_reads': len(reads), 'exact_turn_bound': True, 'mutation_replays': 0}))
finally:
    f.close()
