"""One private home through the exact old/new/old daemon and helper pair.

Scripted loopback inference proves resume; it is never paid provider traffic.
The caller supplies the existing strict ledger/execution byte oracle.
"""
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid

from fixtures.upstream_0163 import message
from fixtures.detached_jobs import DetachedJobServer
from test_background_integration import wait
from test_detached_jobs_stock import completion
from test_rust_runtime_stock_integration import stop_rust_observers


class PairedStockServer(DetachedJobServer):
    """Opt this fixture into the real OpenAI effort-update gate before first launch."""
    def launch(self, **kwargs):
        catalog = self.home / 'paired-models.json'
        if not catalog.exists():
            catalog.write_text(json.dumps({'models': [dict(
                slug='gpt-6.1-sol', display_name='gpt-6.1-sol', description='paired loopback model',
                default_reasoning_level='high', supported_reasoning_levels=[
                    {'effort': effort, 'description': effort} for effort in ('high', 'xhigh')],
                shell_type='shell_command', visibility='list', minimal_client_version=[0, 1, 0],
                supported_in_api=True, priority=0, upgrade=None,
                model_messages={'instructions_template': 'Read private paired fixture evidence.',
                                'approvals': None, 'auto_review': None, 'permissions': None},
                support_verbosity=False, default_verbosity=None, apply_patch_tool_type=None,
                truncation_policy={'mode': 'bytes', 'limit': 10000},
                supports_image_detail_original=False, context_window=272000,
                max_context_window=272000, experimental_supported_tools=[],
                supports_reasoning_effort_updates=True)]}))
            path = self.home / 'config.toml'
            config = path.read_text().replace('name="loopback custom goal acceptance"',
                'name="OpenAI"\nsupports_websockets=false')
            assert 'features.reasoning_effort_override' not in config
            path.write_text(f'model_catalog_json={json.dumps(str(catalog))}\n' + config)
        super().launch(**kwargs)

    def close(self, passed):
        for name in ('paired-models.json', 'config.toml'):
            if (self.home / name).is_file():
                shutil.copy2(self.home / name, self.evidence / name)
        super().close(passed)


def exercise(f, new_runtime, old_binary, new_binary, oracle):
    report = dict(phases=[], holds=[], remote_model_requests=0,
        limitations=['Private quiet paired cutover only; no running executor transfer.',
            'Inherited fixture explicitly disables apps and memories; fleet memory-v2/default behavior remains uncovered.',
            'Scripted loopback effort-update wire/history proof only; no production provider or cache-hit claim.'])
    old_runtime = f.runtime
    instructions = (Path(__file__).resolve().parents[2] / 'docs/ultracode-bootstrap.txt').read_text()

    def mode(action='status', *args, model=False, stdin=None):
        env = dict(f.env)
        if model:
            env['CODEX_THREAD_ID'] = f.parent
        result = subprocess.run([str(f.runtime), 'ultracode', action, '--thread', f.parent,
            *map(str, args), '--json'], input=stdin, env=env, cwd=f.work,
            capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout)

    def maintenance(action):
        nonlocal records
        before = oracle.execution_record_bytes(f.job_directory(ident))
        value = f.cli('job', 'maintenance', action)
        records = oracle.execution_record_bytes(f.job_directory(ident))
        oracle.validate_execution_record_transition(before, records, ('job', 'maintenance', action))
        return value

    def phase(name, binary, runtime):
        report['phases'].append(dict(name=name, stock_sha256=oracle.digest(binary),
            stock_version=subprocess.check_output([str(binary), '--version'], text=True).strip(),
            runtime_sha256=oracle.digest(runtime), generation=f.rpc.generation))

    mode('provenance', '--load-kind', 'fresh', '--developer-instructions-stdin', stdin=instructions)
    mode('on')
    completion(f)
    execution = f.report['verified_results'][-1]
    ident = execution['id']
    stop_rust_observers(f.home)
    wait(lambda: not f.coordinator_processes(), 'private observers did not stop')
    f.supervisor_lock = (f.home / 'codex-plus' / f'{f.parent}.supervisor.lock').open('w')
    fcntl.flock(f.supervisor_lock, fcntl.LOCK_EX)
    f.goal('provenance', '--cwd', f.work, '--load-kind', 'fresh')
    paused = f.goal('start', '--objective', 'Preserve paired transition rows', '--paused')
    submission = str(uuid.uuid4())
    f.goal('input', '--submission-id', submission)
    gate = f.provider.gate()
    f.provider.root_steps.append((message('old stock admitted snapshot'), gate))
    before = f.provider.count
    origin = f.rpc.call('turn/start', dict(threadId=f.parent, clientUserMessageId=submission,
        input=[dict(type='text', text='private old-stock seed')], effort='high'))['turn']['id']
    wait(lambda: f.provider.count == before + 1, 'old seed turn did not reach loopback')
    admitted = mode(model=True)
    assert admitted['effective']['turn'] == origin
    f.goal('input-settle', '--submission-id', submission, '--accepted', '--turn', origin)
    mode('off')
    gate.set()
    assert f.terminal(origin)['status'] == 'completed'
    wait(lambda: f.thread()['status']['type'] == 'idle', 'old seed turn stayed active')
    normalized = maintenance('enter')
    assert normalized['held'] and normalized['admission_quiet'] and normalized['execution_quiet']
    maintenance('leave')
    baseline = oracle.ledger_snapshot(f.home)
    for table in ('jobs', 'callbacks', 'goals', 'ultracode_turns', 'ultracode_submissions'):
        assert baseline['tables'][table], table
    records = oracle.execution_record_bytes(f.job_directory(ident))
    baseline_directory = f.evidence / 'execution-baseline'
    baseline_directory.mkdir()
    for name, raw in records.items():
        target = baseline_directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    report['execution_id'] = ident
    report['ledger_before'] = baseline
    report['execution_before_sha256'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in records.items()}
    clock, normalized = oracle.state_clock_bytes(records['state.json'])
    report['execution_state_before'] = dict(clock=clock, bytes_sha256=hashlib.sha256(normalized).hexdigest())
    phase('old-before', old_binary, old_runtime)
    first = maintenance('enter')

    def switch(binary, runtime):
        before = f.provider.count
        f.binary, f.runtime = binary, runtime
        f.restart_daemon()
        resumed = f.rpc.call('thread/resume', dict(threadId=f.parent))['thread']
        assert resumed['id'] == f.parent
        assert f.provider.count == before, 'restart/resume called provider'

    def check_hold(entered):
        before = f.provider.count
        observed = maintenance('status')
        assert observed['id'] == entered['id'] and observed['held']
        assert observed['admission_quiet'] and observed['execution_quiet']
        left = maintenance('leave')
        assert left['id'] == entered['id'] and not left['held']
        assert f.provider.count == before, 'hold read/leave called provider'
        report['holds'].append(dict(entered=entered, observed=observed, left=left))

    def read_unchanged():
        before = f.provider.count
        assert f.job(ident)['result'] == execution['result']
        assert f.goal()['goal']['id'] == paused['goal']['id']
        f.goal('history')
        f.cli('park', '--status', '--parent', f.parent)
        f.cli('job', 'list', '--parent', f.parent)
        mode()
        assert oracle.ledger_snapshot(f.home) == baseline, 'paired read changed original rows/schema'
        oracle.validate_execution_record_transition(records, oracle.execution_record_bytes(f.job_directory(ident)))
        assert f.provider.count == before, 'paired reads called provider'

    def inference(text, effort):
        before = f.provider.count
        f.provider.root_steps.append((message(text + ' completed'), None))
        turn = f.rpc.call('turn/start', dict(threadId=f.parent,
            input=[dict(type='text', text=text)], effort=effort))['turn']['id']
        assert f.terminal(turn)['status'] == 'completed'
        wait(lambda: f.thread()['status']['type'] == 'idle', 'resumed inference stayed active')
        assert f.provider.count == before + 1, 'inference duplicated provider request'
        return turn

    switch(new_binary, new_runtime)
    phase('candidate', new_binary, new_runtime)
    check_hold(first)
    read_unchanged()
    candidate_start = f.provider.count
    first_new_turn = inference('candidate first selected effort', 'high')
    new_turn = inference('candidate effort-changing turn', 'xhigh')
    bodies = [row['body'] for row in f.provider.requests[candidate_start:]]
    high = dict(type='configuration_update', reasoning=dict(effort='high'))
    xhigh = dict(type='configuration_update', reasoning=dict(effort='xhigh'))
    updates = [[item for item in body['input'] if item.get('type') == 'configuration_update'] for body in bodies]
    assert updates == [[high], [high, xhigh]], 'candidate did not exercise effort override updates'
    assert [body['reasoning']['effort'] for body in bodies] == ['high', 'high'], 'candidate request effort did not stay pinned'
    keys = [body['prompt_cache_key'] for body in bodies]
    assert keys[0] and keys[0] == keys[1], 'candidate cache key changed with selected effort'
    def completed_history():
        thread = f.rpc.call('thread/read', dict(threadId=f.parent, includeTurns=True))['thread']
        completed = {turn['id'] for turn in thread['turns'] if turn['status'] == 'completed'}
        return thread if {first_new_turn, new_turn} <= completed else None
    full_history = wait(completed_history, 'candidate completed turns were not readable from full stock history')
    full_history_path = f.evidence / 'stock-read-before-rollback.json'
    full_history_path.write_text(json.dumps(full_history, indent=2) + '\n')
    rollouts = list(f.home.glob(f'sessions/**/*{f.parent}*.jsonl'))
    assert len(rollouts) == 1, 'exact private stock rollout missing or ambiguous'
    history_path = rollouts[0]
    history_before = history_path.read_bytes()
    persisted = [row['payload'] for line in history_before.splitlines()
        if (row := json.loads(line)).get('type') == 'response_item' and
        row.get('payload', {}).get('type') == 'configuration_update']
    assert persisted == [high, xhigh], 'candidate effort updates were not persisted'
    (f.evidence / 'stock-history-before-rollback.jsonl').write_bytes(history_before)
    report['effort_override'] = dict(selected_efforts=['high', 'xhigh'], request_efforts=['high', 'high'],
        configuration_updates=updates, persisted_updates=persisted,
        stable_prompt_cache_key=keys[0], candidate_turns=[first_new_turn, new_turn],
        stock_read_before_sha256=oracle.digest(full_history_path),
        history_before_sha256=hashlib.sha256(history_before).hexdigest())
    read_unchanged()
    second = maintenance('enter')
    assert second['id'] != first['id']
    switch(old_binary, old_runtime)
    phase('old-rollback', old_binary, old_runtime)
    check_hold(second)
    read_unchanged()
    turns = f.rpc.call('thread/turns/list', dict(threadId=f.parent))['data']
    assert any(row['id'] == new_turn and row['status'] == 'completed' for row in turns)
    rollback_turn = inference('old stock resumed candidate-written history', 'high')
    assert f.provider.requests[-1]['body']['reasoning']['effort'] == 'high'
    history_after = history_path.read_bytes()
    assert history_after.startswith(history_before), 'old stock rewrote candidate configuration-update history'
    (f.evidence / 'stock-history-after-rollback.jsonl').write_bytes(history_after)
    report['effort_override'].update(history_after_sha256=hashlib.sha256(history_after).hexdigest(),
        original_history_prefix_preserved=True, rollback_selected_effort='high', rollback_request_effort='high')
    read_unchanged()
    assert (f.work / 'job/runs').read_text() == 'run\n'
    assert f.delivered() and not f.coordinator_processes()
    report.update(passed=True, old_stock_readable=True, old_stock_resumed_inference=True,
        new_turn=new_turn, rollback_turn=rollback_turn, ledger_after=oracle.ledger_snapshot(f.home),
        execution_after_sha256={name: hashlib.sha256(raw).hexdigest()
            for name, raw in oracle.execution_record_bytes(f.job_directory(ident)).items()},
        workload_runs=1, original_result_preserved=True, rows_unchanged=True,
        provider_requests_from_reads_holds_restart=0, callbacks=len(baseline['tables']['callbacks']),
        snapshot_restore=False)
    clock, normalized = oracle.state_clock_bytes(records['state.json'])
    report['execution_state_after'] = dict(clock=clock, bytes_sha256=hashlib.sha256(normalized).hexdigest())
    return report
