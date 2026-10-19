"""Private projection-only PTY acceptance; no runtime or stock-daemon claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--color-level', choices=['0', '1', '2', '3'], default='3')
    parser.add_argument('--status-line', choices=['mixed', 'empty'], default='mixed')
    args = parser.parse_args()
    source, binary, evidence = args.source.resolve(), args.binary.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source / 'tests'))
    from fixtures.native_server import AppServer
    from native_smoke import Terminal, composer_ready, wait, worker_rows, write_health
    from test_ultracode_tui import helper_script, calls

    os.environ.pop('NO_COLOR', None)
    os.environ['FORCE_COLOR'] = args.color_level
    if args.color_level == '0':
        os.environ['NO_COLOR'] = '1'
    root = '00000000-0000-0000-0012-000000000001'
    selection = ['model-with-reasoning', 'current-dir', 'context-remaining'] if args.status_line == 'mixed' else []

    class GoalServer(AppServer):
        def reply(self, method, params):
            result = super().reply(method, params)
            if method == 'config/read':
                result['result']['config']['tui'] = {'status_line': selection}
            return result

    report = dict(passed=False, fixture=__doc__, binary=str(binary),
                  binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                  color_level=args.color_level, status_line=args.status_line, stages=[], violations=[])
    terminal = server = None
    with tempfile.TemporaryDirectory(prefix='plus-goal-placement-') as temporary:
        directory = Path(temporary)
        home, work, package = directory / 'home', directory / 'workspace', directory / 'package'
        for path in (home, work, package):
            path.mkdir(mode=0o700)
        native = package / 'codex'
        shutil.copy2(binary, native)
        host = binary.with_name('codex-code-mode-host')
        if host.exists():
            (package / host.name).symlink_to(host)
        helper = package / 'codex-plus-runtime'
        helper.write_text(helper_script((source / 'docs/ultracode-bootstrap.txt').read_text()))
        helper.chmod(0o700)
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
            'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
            '[tui]\nstatus_line=' + json.dumps(selection) + '\n')
        (home / 'auth.json').write_text('{"OPENAI_API_KEY":"isolated-fixture-only"}')
        socket = home / 'app-server-control/app-server-control.sock'
        socket.parent.mkdir(mode=0o700)
        server = GoalServer(root, work, 2, socket_path=socket)
        socket.chmod(0o600)
        projection = home / 'codex-plus/goals' / f'{root}.json'
        projection.parent.mkdir(parents=True)

        def publish(state, jobs=0):
            write_health(home, root, 'idle')
            value = dict(version=1, root=root, goal=None, attention=None)
            if state is not None:
                value['goal'] = dict(id='private-projection-goal', revision=1, state=state,
                    objective='Private display fixture only', running_jobs=jobs)
            pending = projection.with_suffix('.tmp')
            pending.write_text(json.dumps(value))
            pending.replace(projection)

        def capture(name, label=None, check_goal=True):
            display = terminal.screen.display
            composers = [i for i, line in enumerate(display) if line.lstrip().startswith('›')]
            assert composers, terminal.text
            composer = composers[-1]
            goal_rows = [i for i, line in enumerate(display) if 'Goal ' in line]
            data = dict(name=name, composer_row=composer, cursor_row=terminal.screen.cursor.y,
                        goal_rows=goal_rows, worker_rows=[i for i, line in enumerate(display)
                            if line.strip() in worker_rows(terminal.text)], foregrounds=[])
            if label:
                matches = [(i, line.index(label)) for i, line in enumerate(display) if label in line]
                assert len(matches) == 1, (label, matches, terminal.text)
                row, column = matches[0]
                data['foregrounds'] = sorted({terminal.screen.buffer[row][x].fg
                    for x in range(column, column + len(label))})
                if check_goal:
                    if not (goal_rows == [row] and row < composer):
                        report['violations'].append(f'{name}: goal must appear once above input; {data}')
                    allowed = {'default'} if args.color_level == '0' else {'89b4fa', '87afff', 'blue', '0000ee'}
                    if not set(data['foregrounds']) <= allowed:
                        report['violations'].append(f'{name}: expected blue foreground; {data}')
            elif check_goal and goal_rows:
                report['violations'].append(f'{name}: cleared goal remained visible; {data}')
            footer = [(i, line.strip()) for i, line in enumerate(display)
                if i > composer and line.strip() and line.strip() not in worker_rows(terminal.text)]
            data['footer_rows'] = footer
            expected_footer = 2 if selection else 0
            if name not in ('help', 'tiny') and len(footer) != expected_footer:
                report['violations'].append(f'{name}: expected {expected_footer} status rows below input; {footer}')
            if name not in ('help', 'tiny') and len(data['worker_rows']) != 2:
                report['violations'].append(f'{name}: expected two worker rows; {data}')
            if data['worker_rows'] and footer and min(data['worker_rows']) <= max(i for i, _ in footer):
                report['violations'].append(f'{name}: workers must follow status rows; {data}')
            (evidence / f'{name}.txt').write_text(terminal.text + '\n')
            report['stages'].append(data)

        try:
            publish(None)
            terminal = Terminal(native, home, work)
            wait([terminal], lambda: composer_ready(terminal.text) and len(worker_rows(terminal.text)) == 2
                and 'Watcher unavailable' not in terminal.text
                and any(c['args'][:2] == ['ultracode', 'provenance'] for c in calls(home)),
                'private frontend did not settle', 30)
            os.write(terminal.master, b'goal-placement-draft')
            wait([terminal], lambda: 'goal-placement-draft' in terminal.text, 'draft not editable')
            capture('initial-cleared')
            for name, state, jobs, label in [
                ('active', 'active', 0, 'Goal active'),
                ('waiting', 'waiting', 0, 'Goal waiting'),
                ('waiting-jobs', 'waiting', 2, 'Goal waiting · 2 jobs'),
                ('waiting-long', 'waiting', 18446744073709551615, 'Goal waiting · 18446744073709551615 jobs'),
                ('needs-input', 'needs_input', 0, 'Goal needs input'),
                ('paused', 'paused', 0, 'Goal paused'),
                ('complete', 'complete', 0, 'Goal complete'),
            ]:
                publish(state, jobs)
                wait([terminal], lambda: label in terminal.text, f'{name} projection not rendered')
                capture(name, label)
            publish('paused')
            wait([terminal], lambda: 'Goal paused' in terminal.text, 'paused goal not restored')
            replies = terminal.resize(12, 65)
            wait([terminal], lambda: terminal.resized_frame(replies) and 'Goal paused' in terminal.text
                and 'goal-placement-draft' in terminal.text, 'narrow resize lost goal or draft')
            os.write(terminal.master, b'-narrow')
            wait([terminal], lambda: 'goal-placement-draft-narrow' in terminal.text, 'narrow draft not editable')
            capture('narrow', 'Goal paused')
            replies = terminal.resize(4, 24)
            wait([terminal], lambda: terminal.resized_frame(replies) and composer_ready(terminal.text),
                 'tiny terminal lost composer priority')
            capture('tiny', check_goal=False)
            replies = terminal.resize(32, 120)
            wait([terminal], lambda: terminal.resized_frame(replies) and 'Goal paused' in terminal.text,
                 'goal not restored after tiny resize')
            os.write(terminal.master, b'\x15?')
            wait([terminal], lambda: 'Keyboard shortcuts' in terminal.text, 'help not opened')
            capture('help', 'Goal paused')
            os.write(terminal.master, b'\x1b')
            wait([terminal], lambda: 'Keyboard shortcuts' not in terminal.text, 'help not closed')
            projection.write_text('{invalid private projection')
            wait([terminal], lambda: 'Goal needs input' in terminal.text, 'unavailable projection not surfaced')
            capture('unavailable', 'Goal needs input')
            publish(None)
            wait([terminal], lambda: 'Goal ' not in terminal.text, 'cleared goal not removed')
            capture('cleared')
            forbidden = {'turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start'}
            assert not [request for request in server.requests if request['method'] in forbidden]
            assert not server.errors, server.errors
            report.update(display_model_requests=0, passed=not report['violations'])
        finally:
            if terminal:
                (evidence / 'terminal.ansi').write_bytes(terminal.output)
                terminal.close()
            server.close()
            (evidence / 'rpc.json').write_text(json.dumps(server.requests, indent=2) + '\n')
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    assert report['passed'], report['violations']


if __name__ == '__main__':
    main()
