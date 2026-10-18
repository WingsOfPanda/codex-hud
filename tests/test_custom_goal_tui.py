"""Plus goal PTY gate with private stock daemon, real helper and loopback provider."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fixtures.custom_goal_server import CustomGoalServer
from native_smoke import Terminal, assert_compact_hud, composer_ready, wait
from test_background_integration import wait as wait_server
from test_rust_runtime_stock_integration import stop_rust_observers


def run(binary, runtime, stock, evidence):
    f = CustomGoalServer(runtime, stock, evidence)
    terminal = None
    passed = False
    try:
        seed, _ = f.start_turn(text='Seed a resumable private goal UI thread')
        assert f.terminal(seed)['status'] == 'completed'
        f.rpc.call('thread/name/set', {'threadId': f.parent, 'name': 'plus-goal-ui-acceptance'})
        f.goal('provenance', '--cwd', f.work, '--load-kind', 'fresh')
        f.goal('start', '--objective', 'Verify visible Plus goal controls', '--paused')
        terminal = Terminal(binary, f.home, f.work, parent=f.parent)
        wait([terminal], lambda: composer_ready(terminal.text) and 'Goal paused' in terminal.text,
             'paused Plus goal absent from actual stock-daemon resume', 30)
        before = f.provider.count
        os.write(terminal.master, b'goal-ui-draft')
        wait([terminal], lambda: 'goal-ui-draft' in terminal.text, 'goal status blocked composer')
        replies = terminal.resize(12, 65)
        wait([terminal], lambda: terminal.resized_frame(replies) and 'goal-ui-draft' in terminal.text
             and 'Goal paused' in terminal.text, 'narrow goal footer lost draft or label')
        replies = terminal.resize(32, 120)
        wait([terminal], lambda: terminal.resized_frame(replies), 'normal resize did not settle')
        # Advisor readiness is the existing single activity row; the goal adds none.
        assert_compact_hud(terminal.text, rows=1)
        assert f.provider.count == before, 'goal display or draft invoked the model'
        (evidence / 'paused-editable-screen.txt').write_text(terminal.text + '\n')
        os.write(terminal.master, b'\x15/goal')
        wait([terminal], lambda: '/goal' in terminal.text, 'goal command did not enter composer')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: 'Plus goal' in terminal.text and ('Resume' in terminal.text or 'resume' in terminal.text),
             '/goal did not show Plus-owned controls with native goals disabled')
        assert f.provider.count == before, 'opening goal menu invoked the model'
        (evidence / 'goal-menu-screen.txt').write_text(terminal.text + '\n')
        os.write(terminal.master, b'\x1b[27u')
        wait([terminal], lambda: composer_ready(terminal.text), 'goal menu did not close')
        goal = f.goal()['goal']
        f.mutate('complete', goal, '--checkpoint', json.dumps({'evidence': ['fixture:goal-ui-verified']}))
        wait([terminal], lambda: 'Goal complete' in terminal.text, 'completed goal indicator not refreshed')
        os.write(terminal.master, b'An unrelated ordinary question')
        wait([terminal], lambda: 'An unrelated ordinary question' in terminal.text, 'ordinary input was not editable')
        os.write(terminal.master, b'\r')
        wait([terminal], lambda: f.provider.count == before + 1 and 'Goal complete' not in terminal.text,
             'ordinary user input was not admitted/did not dismiss completed label', 30)
        wait_server(lambda: f.thread()['status']['type'] == 'idle', 'ordinary user turn did not settle')
        wait([terminal], lambda: composer_ready(terminal.text), 'completed ordinary turn blocked composer')
        snapshot = f.goal()
        assert snapshot['goal'] is None, snapshot
        assert f.goal('history')['history'][-1]['state'] == 'complete', snapshot
        assert snapshot['pending_input_state'] is None, snapshot
        assert f.provider.count == before + 1, 'input admission triggered an extra goal/model turn'
        (evidence / 'ordinary-input-screen.txt').write_text(terminal.text + '\n')
        f.report.update(ui_binary=str(binary), ui_model_requests=1, display_model_requests=0,
                        native_goals_disabled=True, scenario='custom-goal-tui')
        passed = True
    finally:
        if terminal:
            (evidence / 'tui.ansi').write_bytes(terminal.output)
            (evidence / 'last-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        f.close(passed)
    print(json.dumps({'passed': passed, 'scenario': 'custom-goal-tui', 'evidence': str(evidence)}))


def restart_recovery(binary, runtime, stock, evidence):
    """A real cold TUI resume must restart observation without a manual observe call."""
    f = CustomGoalServer(runtime, stock, evidence)
    terminal = None
    passed = False
    try:
        seed, _ = f.start_turn(text='Seed the retained goal before a client restart')
        assert f.terminal(seed)['status'] == 'completed'
        f.rpc.call('thread/name/set', {'threadId': f.parent, 'name': 'goal-restart-acceptance'})
        f.goal('provenance', '--cwd', f.work, '--load-kind', 'fresh')
        f.goal('start', '--objective', 'Retained goal must report before old follow-up actions')
        stop_rust_observers(f.home)
        # Deterministically represent a same-boot unwatched gap in this private fixture.
        with sqlite3.connect(f.home / 'codex-plus/background.sqlite') as db:
            proof = json.loads(db.execute('SELECT value FROM goal_provenance WHERE root=?',
                                         (f.parent,)).fetchone()[0])
            proof['last_observed_uptime'] = float(Path('/proc/uptime').read_text().split()[0]) - 45
            db.execute('UPDATE goal_provenance SET value=? WHERE root=?',
                       (json.dumps(proof), f.parent))
        f.rpc.call('thread/unsubscribe', {'threadId': f.parent})
        wait_server(lambda: f.thread()['status']['type'] == 'notLoaded', 'root did not cold-unload')
        f.supervisor_lock.close()
        f.supervisor_lock = None
        before = f.provider.count
        terminal = Terminal(binary, f.home, f.work, parent=f.parent)
        wait([terminal], lambda: composer_ready(terminal.text)
             and f.provider.count == before + 1 and 'Goal needs input' in terminal.text,
             'cold TUI resume did not restore observation and report-only recovery', 30)
        wait_server(lambda: f.thread()['status']['type'] == 'idle', 'recovery report did not finish')
        snapshot = wait_server(lambda: (state := f.goal())['report_delivered'] == 1 and state,
                               'recovered report admission was not positively attributed', timeout=8)
        assert snapshot['report_barrier'] == 1, snapshot
        assert snapshot['goal']['state'] == 'needs_input', snapshot
        report = f.goal('history')['dispatches']
        assert len(report) == 1 and report[0]['kind'] == 'report', report
        assert report[0]['message'].startswith('REPORT ONLY:'), report
        f.quiet()
        assert f.provider.count == before + 1, 'reconnect resumed old actions after reporting'
        (evidence / 'restart-report-screen.txt').write_text(terminal.text + '\n')
        f.report.update(scenario='custom-goal-tui-restart', ui_binary=str(binary),
                        observation_started_by_client=True, manual_observe_calls=0,
                        restart_provider_requests=1, final_snapshot=snapshot)
        passed = True
    finally:
        if terminal:
            (evidence / 'tui.ansi').write_bytes(terminal.output)
            (evidence / 'last-screen.txt').write_text(terminal.text + '\n')
            terminal.close()
        f.close(passed)
    print(json.dumps({'passed': passed, 'scenario': 'custom-goal-tui-restart', 'evidence': str(evidence)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--stock-binary', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--scenario', choices=('display', 'restart', 'all'), default='all')
    args = parser.parse_args()
    if args.scenario in ('display', 'all'):
        run(args.binary.resolve(), args.runtime.resolve(), args.stock_binary.resolve(),
            args.evidence.resolve() / 'display')
    if args.scenario in ('restart', 'all'):
        restart_recovery(args.binary.resolve(), args.runtime.resolve(), args.stock_binary.resolve(),
                         args.evidence.resolve() / 'restart')
