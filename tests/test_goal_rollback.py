"""Quiet protocol-3 ledger rollback using the actual previous native helper."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixtures.custom_goal_server import CustomGoalServer
from test_custom_goals_stock import partial_two_job_completion
from test_rust_runtime_stock_integration import stop_rust_observers


def snapshot(home):
    path = home / 'codex-plus/background.sqlite'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'goal*' ORDER BY name")]
        return {name: sorted(db.execute(f'SELECT * FROM {name}').fetchall(), key=repr)
                for name in tables}


def run(package, previous, stock, evidence):
    current = json.loads((package / 'codex-plus.json').read_text())
    old = json.loads((previous / 'codex-plus.json').read_text())
    assert current['coordinator_protocol'] == 3 and old['coordinator_protocol'] == 2
    assert current['upstream'] == old['upstream'] and current['target'] == old['target']
    new_runtime, old_runtime = (p / 'bin/codex-plus-runtime' for p in (package, previous))
    f = CustomGoalServer(new_runtime, stock, evidence)
    passed = False
    try:
        partial_two_job_completion(f)
        stop_rust_observers(f.home)
        finished = f.goal()['goal']
        f.mutate('start', finished, '--objective',
                 'Retained paused objective after quiet rollback', '--paused')
        stop_rust_observers(f.home)
        state = f.goal('history')
        assert state['goal']['state'] == 'paused' and state['running_jobs'] == 0
        assert not state['open_dispatches'] and state['report_barrier'] == 0
        callbacks = f.cli('park', '--status', '--parent', f.parent)['callbacks']
        assert callbacks and all(row['state'] == 'goal_settled' for row in callbacks)
        before, requests = snapshot(f.home), f.provider.count
        result = subprocess.run([str(old_runtime), '--package-root', str(previous),
            'observe', '--parent', f.parent], env=f.env, cwd=previous,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        (evidence / 'previous-helper.stdout').write_text(result.stdout)
        (evidence / 'previous-helper.stderr').write_text(result.stderr)
        assert result.returncode == 0, (result.returncode, result.stderr)
        assert snapshot(f.home) == before, 'previous helper changed retained goal tables'
        assert f.provider.count == requests, 'previous helper dispatched dormant goal work'
        after = f.cli('park', '--status', '--parent', f.parent)['callbacks']
        assert [(r['id'], r['state'], r['client'], r['message']) for r in after] == [
            (r['id'], r['state'], r['client'], r['message']) for r in callbacks]
        final = f.goal('history')
        assert final['goal']['id'] == state['goal']['id'] and final['goal']['state'] == 'paused'
        f.report.update(quiet_rollback=True, previous_release=old['release'],
            previous_runtime_sha256=hashlib.sha256(old_runtime.read_bytes()).hexdigest(),
            current_release=current['release'], previous_helper_exit=result.returncode,
            goal_tables_unchanged=True, callback_markers_unchanged=True,
            additional_model_requests=0, final_snapshot=final)
        passed = True
    finally:
        f.close(passed)
    print(json.dumps({'passed': passed, 'evidence': str(evidence),
        'previous_release': old['release'], 'additional_model_requests': 0}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package', type=Path, required=True)
    p.add_argument('--previous', type=Path, required=True)
    p.add_argument('--stock-binary', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    args = p.parse_args()
    run(args.package.resolve(strict=True), args.previous.resolve(strict=True),
        args.stock_binary.resolve(strict=True), args.evidence.resolve())
