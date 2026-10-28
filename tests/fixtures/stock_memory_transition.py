"""Private stock memory writer/rollback diagnostic; every inference is loopback."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback

from fixtures.upstream_0163 import message
from fixtures.turn_completion import wait_for_turn_completion
from test_advisor_integration import official_stock_identity
from test_background_integration import RecordingClient, stop, wait


MODEL = 'gpt-6.1-sol'
TABLES = ('stage1_outputs', 'jobs', 'consolidation_progress', '_sqlx_migrations')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summary(marker):
    return ('v1\n\n## User Profile\nPrivate fixture\n\n## User preferences\nDeterministic\n'
            "\n## General Tips\nKeep evidence\n\n## What's in Memory\n" + marker + '\n')


def snapshot(home):
    stores = {}
    for name in ('memories_1.sqlite', 'memories_v2_1.sqlite'):
        path = home / name
        if path.exists():
            with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                stores[name] = {table: sorted([dict(row) for row in db.execute(f'SELECT * FROM {table}')],
                    key=lambda row: json.dumps(row, sort_keys=True, default=str)) for table in TABLES}
                for row in stores[name]['_sqlx_migrations']:
                    row['checksum'] = row['checksum'].hex()
    artifacts = {str(path.relative_to(home)): sha(path)
        for directory in ('memories', 'memories_v2') for path in (home / directory).rglob('*')
        if path.is_file() and not path.is_symlink() and '.git' not in path.relative_to(home / directory).parts}
    return dict(stores=stores, artifacts=artifacts)


def owned_survivors(home):
    found = []
    for path in Path('/proc').glob('[0-9]*'):
        try:
            if f'CODEX_HOME={home}'.encode() in (path / 'environ').read_bytes().split(b'\0'):
                found.append(dict(pid=int(path.name), start=(path / 'stat').read_text().rsplit(')', 1)[1].split()[19],
                    command=(path / 'cmdline').read_bytes().decode(errors='replace').replace('\0', ' ')))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def response_item(body, version, root, marker, consolidation_calls):
    """Dispatch by the extraction contract, never concurrent request ordering."""
    properties = body.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
    if 'rollout_summary' in properties:
        output = dict(rollout_summary=marker + ' rollout summary', rollout_slug='memory-transition')
        if 'raw_memory' in properties:
            output['raw_memory'] = marker + ' raw memory'
        return 'extraction', message(json.dumps(output))
    if 'MEMORY-PROBE' in json.dumps(body.get('input', [])) or 'SEED-SOURCE' in json.dumps(body.get('input', [])):
        return 'probe', message('private memory probe completed')
    if consolidation_calls:
        return 'consolidation-final', message('memory artifacts written')
    files = {'memory_summary.md': summary(marker)}
    if version == 'v1':
        files['MEMORY.md'] = marker + ' consolidated memory\n'
    code = 'from pathlib import Path; root=Path(' + repr(str(root)) + '); ' + '; '.join(
        '(root / ' + repr(name) + ').write_text(' + repr(value) + ')' for name, value in files.items())
    command = shlex.quote(shutil.which('python3')) + ' -c ' + shlex.quote(code)
    tools = {tool.get('name') for tool in body.get('tools', [])}
    assert 'exec_command' in tools, f'Consolidation exec_command absent: {tools}'
    return 'consolidation-write', dict(type='function_call', name='exec_command', call_id='memory-artifacts',
        arguments=json.dumps(dict(cmd=command, yield_time_ms=1000, max_output_tokens=100, login=False)))


class Provider:
    def __init__(self, version, root, marker):
        self.requests, self.errors, self.consolidation_calls = [], [], 0
        self.lock = threading.Lock()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    assert self.path == '/v1/responses', self.path
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    with fixture.lock:
                        assert len(fixture.requests) < 10, 'Finite loopback request budget exceeded'
                        role, item = response_item(body, version, root, marker, fixture.consolidation_calls)
                        if role.startswith('consolidation'):
                            fixture.consolidation_calls += 1
                            assert fixture.consolidation_calls <= 2, 'Unexpected repeated consolidation'
                        fixture.requests.append(dict(role=role, body=body, thread=self.headers.get('thread-id')))
                        number = len(fixture.requests)
                    events = [dict(type='response.created', response={'id': f'memory-{number}'}),
                        dict(type='response.output_item.done', item=item),
                        dict(type='response.completed', response={'id': f'memory-{number}',
                            'usage': dict(input_tokens=0, output_tokens=0, total_tokens=0)})]
                    payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception:
                    fixture.errors.append(traceback.format_exc())
                    self.send_error(500, 'private memory fixture failed')

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def write_config(home, provider):
    catalog = home / 'models.json'
    catalog.write_text(json.dumps({'models': [dict(slug=MODEL, display_name=MODEL, description='private memory model',
        default_reasoning_level='low', supported_reasoning_levels=[dict(effort=e, description=e) for e in ('low', 'medium')],
        shell_type='shell_command', visibility='list', minimal_client_version=[0, 1, 0], supported_in_api=True,
        priority=0, upgrade=None, model_messages=dict(instructions_template='Use the private memory fixture.',
            approvals=None, auto_review=None, permissions=None), support_verbosity=False, default_verbosity=None,
        apply_patch_tool_type=None, truncation_policy=dict(mode='bytes', limit=10000),
        supports_image_detail_original=False, context_window=272000, max_context_window=272000,
        experimental_supported_tools=[])]}))
    (home / 'config.toml').write_text(f'''model={json.dumps(MODEL)}
model_catalog_json={json.dumps(str(catalog))}
model_provider="isolated"
model_reasoning_effort="low"
sandbox_mode="danger-full-access"
approval_policy="never"
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
features.apps=false
features.memories=true
features.unified_exec=true
features.shell_tool=true
features.code_mode=false
memories.generate_memories=true
memories.use_memories=true
memories.extract_model={json.dumps(MODEL)}
memories.consolidation_model={json.dumps(MODEL)}
memories.max_rollouts_per_startup=1
memories.max_raw_memories_for_consolidation=1
memories.min_rollout_idle_hours=0
[model_providers.isolated]
name="OpenAI"
base_url={json.dumps(provider.url)}
wire_api="responses"
requires_openai_auth=false
supports_websockets=false
request_max_retries=0
stream_max_retries=0
''')


class StockServer:
    def __init__(self, binary, home, work, evidence):
        self.log = evidence.open('w')
        self.rpc = None
        self.process = None
        env = {key: value for key, value in os.environ.items()
            if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
        env.update(CODEX_HOME=str(home), RUST_LOG='warn')
        env.pop('CODEX_THREAD_ID', None)
        env.pop('CODEX_SQLITE_HOME', None)
        try:
            self.process = subprocess.Popen([str(binary), 'app-server', '--listen', 'unix://'], env=env,
                cwd=work, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log, start_new_session=True)
            def connect():
                assert self.process.poll() is None, 'Private stock app-server exited'
                try:
                    return RecordingClient(home / 'app-server-control/app-server-control.sock')
                except OSError:
                    return None
            self.rpc = wait(connect, 'Private stock socket unavailable', timeout=15)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.rpc:
            self.rpc.close()
        if self.process:
            stop(self.process)
        self.log.close()


def start(rpc, work, **kwargs):
    return rpc.call('thread/start', dict(cwd=str(work), model=MODEL, approvalPolicy='never',
        sandbox='danger-full-access', **kwargs))['thread']['id']


def probe(rpc, work, provider, config=None):
    ident = start(rpc, work, ephemeral=True, **({'config': config} if config else {}))
    before = len(provider.requests)
    turn = rpc.call('turn/start', dict(threadId=ident, input=[dict(type='text', text='MEMORY-PROBE')]))['turn']['id']
    wait_for_turn_completion(rpc, ident, turn)
    assert len(provider.requests) == before + 1, provider.requests
    return dict(thread=ident, request=provider.requests[-1])


def run_lane(old, new, evidence, lane):
    evidence.mkdir(parents=True, mode=0o700)
    report = dict(schema=1, lane=lane, passed=False, remote_model_requests=0, writer='stock165 startup pipeline',
        fixture_sha256=sha(__file__), stocks={},
        input_preparation='Real stock source turn; SQL only normalizes source/idle timestamp/memory eligibility.',
        limitations=['Scripted local provider only; no quality or production-provider claim.',
            'Explicit v2 reader is not automatic-default fleet rollback.'])
    root = Path(tempfile.mkdtemp(prefix='mm-'))
    home, work = root / 'home', root / 'work'
    home.mkdir(mode=0o700)
    work.mkdir()
    version = 'v1' if lane == 'existing-v1' else 'v2'
    memory_root = home / ('memories' if version == 'v1' else 'memories_v2')
    marker = 'memory-transition-' + version
    provider = Provider(version, memory_root, marker)
    server = None
    report['private_home'] = str(home)
    try:
        for role, binary, stock_version in (('previous', old, '0.163.0'), ('candidate', new, '0.165.0')):
            observed, digest = official_stock_identity(binary)
            assert observed == 'codex-cli ' + stock_version, observed
            report['stocks'][role] = dict(path=str(binary), sha256=digest, version=observed)
        if version == 'v1':
            memory_root.mkdir()
            (memory_root / 'memory_summary.md').write_text(summary('existing-v1-marker'))
            (memory_root / 'MEMORY.md').write_text('existing-v1-marker\n')
        write_config(home, provider)
        report['config_sha256'], report['catalog_sha256'] = sha(home / 'config.toml'), sha(home / 'models.json')
        shutil.copy2(home / 'config.toml', evidence / 'config.toml')
        shutil.copy2(home / 'models.json', evidence / 'models.json')
        server = StockServer(new, home, work, evidence / '165-seed.log')
        source = start(server.rpc, work, config={'features.memories': False})
        turn = server.rpc.call('turn/start', dict(threadId=source, input=[dict(type='text', text='SEED-SOURCE remember this task')]))['turn']['id']
        wait_for_turn_completion(server.rpc, source, turn)
        server.close()
        server = None
        with sqlite3.connect(home / 'state_5.sqlite') as db:
            idle = int((time.time() - 7200) * 1000)
            changed = db.execute("UPDATE threads SET source='cli', memory_mode='enabled', updated_at_ms=? WHERE id=?", (idle, source)).rowcount
            assert changed == 1, 'Real source thread metadata absent'
            db.row_factory = sqlite3.Row
            report['prepared_source_metadata'] = dict(db.execute(
                'SELECT id, source, memory_mode, updated_at_ms, rollout_path FROM threads WHERE id=?', (source,)).fetchone())
        report['source_thread'] = source
        assert not (home / 'memories_v2_1.sqlite').exists(), 'Input setup generated v2 state'
        server = StockServer(new, home, work, evidence / '165-writer.log')
        report['writer_thread'] = start(server.rpc, work)
        writer_turn = server.rpc.call('turn/start', dict(threadId=report['writer_thread'],
            input=[dict(type='text', text='MEMORY-PROBE start genuine memory startup')]))['turn']['id']
        wait_for_turn_completion(server.rpc, report['writer_thread'], writer_turn)

        def generated():
            assert not provider.errors, provider.errors
            try:
                state = snapshot(home)
            except sqlite3.OperationalError:
                return False  # The concurrently opened memory store may still be migrating.
            store = state['stores'].get('memories_1.sqlite' if version == 'v1' else 'memories_v2_1.sqlite', {})
            return (store.get('consolidation_progress', [{}])[0].get('max_thread_count', 0) >= 1 and
                memory_root.joinpath('memory_summary.md').is_file() and
                marker in memory_root.joinpath('memory_summary.md').read_text())
        wait(generated, 'Stock165 memory writer did not complete; inspect writer log and provider errors', timeout=90)
        rows = snapshot(home)['stores']['memories_1.sqlite' if version == 'v1' else 'memories_v2_1.sqlite']['stage1_outputs']
        assert len(rows) == 1 and rows[0]['thread_id'] == source and marker in rows[0]['rollout_summary'], rows
        assert rows[0]['selected_for_phase2'] == 1, rows
        assert rows[0]['raw_memory'] == (marker + ' raw memory' if version == 'v1' else ''), rows
        assert [r['role'] for r in provider.requests].count('extraction') == 1
        assert provider.consolidation_calls == 2
        candidate = probe(server.rpc, work, provider)
        assert marker in json.dumps(candidate['request']['body']['input']), candidate
        if version == 'v2':
            assert not (home / 'memories').exists(), 'Fresh165 default unexpectedly used v1'
        server.close()
        server = None
        before = snapshot(home)
        report['before_rollback'] = before
        server = StockServer(old, home, work, evidence / '163-rollback.log')
        status = server.rpc.call('memory/status', dict(minConsolidatedThreads=1))
        default = probe(server.rpc, work, provider)
        default_has_marker = marker in json.dumps(default['request']['body']['input'])
        assert default_has_marker == (version == 'v1'), default
        if version == 'v2':
            explicit = probe(server.rpc, work, provider, {'memories.version': 'v2'})
            assert marker in json.dumps(explicit['request']['body']['input']), explicit
            assert status['v2ConsolidatedThreads'] == 1 and status['v2Ready'] is True, status
            report['explicit_v2_probe'] = explicit
        server.close()
        server = None
        after = snapshot(home)
        for name, store in before['stores'].items():
            assert after['stores'].get(name) == store, f'Rollback modified original memory store: {name}'
        assert all(after['artifacts'].get(name) == value for name, value in before['artifacts'].items()), 'Rollback changed memory artifacts'
        report.update(passed=True, candidate_probe=candidate, old_default_probe=default,
            old_default_reads_written_summary=default_has_marker, old_default_version='v1',
            transparent_default_rollback=(version == 'v1'), memory_status=status, after_rollback=after,
            real_extraction_and_consolidation=True, original_memory_store_preserved=True)
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        if server:
            server.close()
        try:
            report['final_memory_snapshot'] = snapshot(home)
            with sqlite3.connect(f'file:{home / "state_5.sqlite"}?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                report['final_source_metadata'] = [dict(row) for row in db.execute(
                    'SELECT id, source, memory_mode, updated_at_ms, rollout_path FROM threads')]
            for name in report['final_memory_snapshot']['stores']:
                with sqlite3.connect(f'file:{home / name}?mode=ro', uri=True) as source_db:
                    with sqlite3.connect(evidence / name) as evidence_db:
                        source_db.backup(evidence_db)
            for name in report['final_memory_snapshot']['artifacts']:
                destination = evidence / 'memory-artifacts' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(home / name, destination)
        except sqlite3.Error as error:
            report['passed'] = False
            report['final_snapshot_error'] = str(error)
        report.update(provider_requests=provider.requests, provider_errors=provider.errors)
        provider.close()
        report['owned_survivors_after_cleanup'] = owned_survivors(home)
        if not report['owned_survivors_after_cleanup']:
            shutil.rmtree(root)
        else:
            report['passed'] = False
        report['private_home_removed'] = not home.exists()
        (evidence / 'report.json').write_text(json.dumps(report, indent=2, default=str) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old', type=Path, required=True)
    parser.add_argument('--new', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--lane', choices=('existing-v1', 'fresh-v2'), required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = run_lane(args.old.resolve(), args.new.resolve(), args.evidence.resolve(), args.lane)
    assert result['passed'] and result['private_home_removed'], 'Memory diagnostic or cleanup failed'
    print(json.dumps(dict(passed=result['passed'], lane=args.lane, report=str(args.evidence / 'report.json'))))


if __name__ == '__main__':
    main()
