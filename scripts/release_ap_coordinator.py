"""Prepare an original replay-ap leaf and compose its verified external receipt.

This coordinator never launches a workload. The original run-ap proposal remains
unchanged and retired; the executor runs an original-API supervised replay leaf.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import release_native as native
import release_protocol as protocol
sys.path.insert(0, str(SCRIPTS / 'release_recipes/compat'))
import workflow

LEAF_CODE = '''import sys
from pathlib import Path
root, execution, script = map(Path, sys.argv[1:])
sys.path[:0] = [str(script.parent / 'release_recipes/compat'), str(script.parent)]
import workflow
import release_native as native
context, plan, profile = workflow.verify_ap(root)
workflow.bound_execution(execution, profile, sys.orig_argv)
argv = [profile['python']['path'], '-B', str(script), 'replay-ap', '--attempt', str(root)]
env = native.python_env(profile) | {'CODEX_PLUS_RELEASE_QA_SCOPE': str(root)}
sys.exit(native.owned_run(argv, helper=Path(profile['executor_package_root']) / 'bin/codex-plus-runtime',
    python=profile['python']['path'], cwd=root, env=env, stdout=sys.stdout, timeout=300))
'''

REPLAY = ('ap-home', 'codex-home', 'release.log', 'observer.log', 'release-vitest.json',
          'observer-vitest.json', 'observed-classifier.json', 'replay-report.json',
          'leaf-independent-cleanup.json', 'leaf-executor-review.json')
WRAPPER = ('ap-started.json', 'ap-command.json', 'ap-report.json', 'cleanup.json', 'managed.log')
LEAF = ('leaf-prelaunch.json', 'leaf-job-proposal.json', 'leaf-ap-report.json',
        'leaf-execution.json', 'leaf-executed.json', 'leaf-provenance.json', 'leaf-records', 'ap-receipt.json')
INPUTS = ('ap-preparation.json', 'ap-profile.json', 'native-plan.json', 'native-captures.json',
          'native-capture-inventory.json', 'ownership-baseline.json')


def coordinator_identity():
    identity = native.checkout_identity(SCRIPTS.parent)
    return {'commit': identity['head'], 'files': {name: protocol.file_record(SCRIPTS / name)
        for name in ('release_ap_coordinator.py', 'release_protocol.py', 'release_recipes/compat/workflow.py')}}


def inventory(root):
    return {path.relative_to(root).as_posix(): protocol.file_record(path)
            for path in root.rglob('*') if path.is_file()}


def validate_original(root, profile):
    original = protocol.load(root / 'ap-job-proposal.json')
    script = str(Path(profile['tooling_root']) / 'scripts/release_compat.py')
    env = native.python_env(profile)
    protocol.require(original['argv'] == [profile['python']['path'], '-B', script, 'run-ap',
        '--attempt', str(root), '--execution', '{execution}'] and
        original['cwd'] == profile['tooling_root'] and original['env'] == env and
        original['exclusive_keys'] == ['release-compat-' + protocol.digest(str(root))[:24]] and
        original['deadline_seconds'] == 600 and original['cancel_grace_seconds'] == 90,
        'Original AP proposal binding differs')
    check = {'argv': [profile['python']['path'], '-B', script, 'verify-ap', '--attempt', str(root)],
             'cwd': profile['tooling_root'], 'env': env, 'deadline_seconds': 180}
    cleanup = dict(check, argv=[profile['python']['path'], '-B', script, 'cleanup-ap', '--attempt',
        str(root), '--output', str(root / 'job-cleanup.json')])
    protocol.require(len(original['external_dependencies']) == 1 and
        original['external_dependencies'][0]['precondition'] == check and original['cleanup'] == cleanup,
        'Original AP precondition/cleanup differs')
    expected_inputs = set(INPUTS) | set(protocol.load(root / 'ap-preparation.json')['capture_data'])
    protocol.require(all(row == {'source': str(root / row['path']), 'path': row['path']}
        for row in original['inputs']) and
        {row['path'] for row in original['inputs']} == expected_inputs and
        len(original['inputs']) == len(expected_inputs), 'Original AP inputs differ')
    return original


def leaf_proposal(root, original, profile):
    proposal = copy.deepcopy(original)
    proposal.update(topic='Replay pinned AP leaf', argv=[profile['python']['path'], '-B', '-c', LEAF_CODE,
        str(root), '{execution}', str(Path(profile['tooling_root']) / 'scripts/release_compat.py')],
        cwd=str(root), env=native.python_env(profile) | {'CODEX_PLUS_RELEASE_QA_SCOPE': str(root)})
    # The inventory precedes the proposal, so neither record hashes itself.
    proposal['inputs'] += [{'source': str(root / name), 'path': name}
                           for name in ('ap-job-proposal.json', 'leaf-prelaunch.json')]
    return proposal


def prepare_leaf(coordinator):
    root = Path(coordinator).resolve(strict=True)
    context, plan, profile = workflow.verify_ap(root, preparation=True)
    protocol._ap_binding(context['native_root'])
    original = validate_original(root, profile)
    absent = REPLAY + WRAPPER + LEAF + ('job-cleanup.json', 'process-journal.jsonl')
    protocol.require(not any((root / name).exists() or (root / name).is_symlink() for name in absent),
                     'Preexisting AP replay or partial coordinator output; preserve this attempt')
    prelaunch = {'schema': 1, 'coordinator': str(root), 'inventory': inventory(root), 'absent': list(absent),
        'native_binding': protocol._ap_binding(context['native_root']), 'coordinator_code': coordinator_identity(),
        'execution_tooling_commit': plan['tooling_commit'], 'run_ap_not_launched': True,
        'retired_proposal': {'path': 'ap-job-proposal.json', 'sha256': native.sha(root / 'ap-job-proposal.json')}}
    proposal = leaf_proposal(root, original, profile)
    protocol.write_once(root / 'leaf-prelaunch.json', prelaunch)
    protocol.write_once(root / 'leaf-job-proposal.json', proposal)
    return proposal


def check_command(frozen, expected):
    spec = frozen['spec']
    protocol.require(spec == {name: expected[name] for name in ('argv', 'cwd', 'env', 'deadline_seconds')} |
        {'env_refs': {}, 'secret_refs': {}}, 'Managed command binding mismatch')
    executable = frozen['executable']
    path = Path(spec['argv'][0]).resolve(strict=True)
    info = path.stat()
    protocol.require(executable == {'path': str(path), 'dev': info.st_dev, 'ino': info.st_ino,
        'size': info.st_size, 'sha256': native.sha(path)}, 'Managed executable identity mismatch')
    info = Path(spec['cwd']).stat()
    protocol.require(frozen['cwd_dev'] == info.st_dev and frozen['cwd_ino'] == info.st_ino and
                     frozen['env_hashes'] == {}, 'Managed cwd/environment identity mismatch')


def validate_job(directory, prepared_path, proposal, profile):
    records = {name: protocol.load(directory / name) for name in ('state.json', 'result.json', 'manifest.json')}
    state, result, manifest = (records[name] for name in ('state.json', 'result.json', 'manifest.json'))
    prepared = protocol.load(prepared_path)
    protocol.require(prepared == manifest and manifest['state'] == 'prepared', 'Prepared manifest changed')
    for record in (state, result, manifest):
        protocol.require(record['id'] == directory.name and record['root'] == directory.parent.name,
                         'Managed execution directory/identity mismatch')
    protocol.require(state['manifest'] == manifest['manifest'] and state['review_digest'] == manifest['review_digest'] and
        state['origin'] == manifest['origin'], 'Managed prepared binding changed')
    material = {'schema': 1, 'id': manifest['id'], 'root': manifest['root'],
                'origin': manifest['origin'], 'manifest': manifest['manifest']}
    review = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    protocol.require(review == manifest['review_digest'], 'Managed review digest mismatch')
    helper = Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'
    info = helper.stat()
    protocol.require(state['package'] == profile['executor_package_root'] and state['runtime'] ==
        {'path': str(helper), 'dev': info.st_dev, 'ino': info.st_ino, 'size': info.st_size, 'sha256': native.sha(helper)},
        'Managed executor package/runtime mismatch')
    protocol.require(protocol.recovery_results_match(state['result'], result) and
        state['state'] == result['state'] == 'completed' and
        state['log_complete'] is True and state['output_closed'] is True and state['error'] is None and
        state['resolution'] is None and result['workload_attempted'] is True and result['exit_code'] == 0 and
        result['signal'] is None and result['termination'] is None and result['error'] is None and
        result['log_complete'] is True and result['workload'] is not None and result['executor'] is not None and
        state['workload'] == result['workload'] and state['executor'] == result['executor'],
        'Managed workload did not complete successfully')
    protocol.require(result['cleanup'] == {'status': 'completed', 'exit_code': 0, 'signal': None,
        'log_complete': True, 'error': None}, 'Managed cleanup did not complete successfully')
    frozen = state['manifest']
    actual_proposal = dict(proposal, argv=[arg.replace('{execution}', str(directory)) for arg in proposal['argv']])
    check_command(frozen['command'], actual_proposal)
    check_command(frozen['cleanup'], proposal['cleanup'])
    protocol.require(len(frozen['external_dependencies']) == len(proposal['external_dependencies']), 'Managed preconditions differ')
    for actual, expected in zip(frozen['external_dependencies'], proposal['external_dependencies']):
        protocol.require(actual['identity'] == expected['identity'], 'Managed dependency identity differs')
        check_command(actual['precondition'], expected['precondition'])
    for name in ('schema', 'topic', 'exclusive_keys', 'expected_duration_seconds', 'cancel_grace_seconds'):
        protocol.require(frozen[name] == proposal[name], 'Managed proposal differs: ' + name)
    protocol.require(len(frozen['inputs']) == len(proposal['inputs']), 'Managed input inventory differs')
    for actual, expected in zip(frozen['inputs'], proposal['inputs']):
        source = Path(expected['source'])
        info = source.stat()
        protocol.require(actual == {'path': expected['path'], 'sha256': native.sha(source),
            'source': {'path': str(source.resolve()), 'dev': info.st_dev, 'ino': info.st_ino,
                       'size': info.st_size, 'sha256': native.sha(source)}} and
            native.sha(directory / 'inputs' / expected['path']) == actual['sha256'], 'Managed frozen input changed')
    for name in ('stdout.log', 'cleanup.log'):
        protocol.file_record(directory / name)
    protocol.require(protocol.load(directory / 'stdout.log') ==
        {'passed': True, 'tests': 23, 'captures': 20, 'busy_pairs': 2}, 'Replay stdout differs')
    return state, records


def compose(coordinator, execution, prepared_state, audit_path, executor_review):
    root, directory = Path(coordinator).resolve(strict=True), Path(execution).resolve(strict=True)
    context, plan, profile = workflow.verify_ap(root, preparation=True)
    original = validate_original(root, profile)
    prelaunch = protocol.load(root / 'leaf-prelaunch.json')
    proposal = protocol.load(root / 'leaf-job-proposal.json')
    protocol.require(prelaunch['coordinator'] == str(root) and prelaunch['run_ap_not_launched'] is True and
        prelaunch['execution_tooling_commit'] == plan['tooling_commit'] and
        prelaunch['coordinator_code'] == coordinator_identity() and
        prelaunch['native_binding'] == protocol._ap_binding(context['native_root']) and
        prelaunch['retired_proposal'] == {'path': 'ap-job-proposal.json', 'sha256': native.sha(root / 'ap-job-proposal.json')} and
        prelaunch['absent'] == list(REPLAY + WRAPPER + LEAF + ('job-cleanup.json', 'process-journal.jsonl')),
        'Prelaunch provenance changed')
    protocol.require(all(protocol.file_record(protocol.relative_file(root, name)) == row
        for name, row in prelaunch['inventory'].items()), 'Prelaunch input changed')
    protocol.require(not any((root / name).exists() or (root / name).is_symlink()
        for name in WRAPPER + LEAF[2:]), 'Partial/old AP composition exists')
    expected = leaf_proposal(root, original, profile)
    protocol.require(proposal == expected, 'Leaf proposal changed')
    state, records = validate_job(directory, prepared_state, proposal, profile)
    journal_path = root / 'process-journal.jsonl'
    protocol.file_record(journal_path)
    journal = [json.loads(line) for line in journal_path.read_text().splitlines()]
    protocol.require(len(journal) == 2 and workflow.ownership.verify_guard_journal(journal, state['workload']['boot_id']),
                     'Replay supervision journal is incomplete')
    replay_argv = [profile['python']['path'], '-B',
        str(Path(profile['tooling_root']) / 'scripts/release_compat.py'), 'replay-ap', '--attempt', str(root)]
    guard = journal[0]['argv']
    protocol.require(len(guard) > 5 and guard[:3] == [profile['python']['path'], '-B', '-c'] and guard[4].isdigit() and
        guard[5:] == [str(Path(profile['executor_package_root']) / 'bin/codex-plus-runtime'),
                       'advisor', '--owned-child', '--', *replay_argv], 'Replay guard command binding differs')
    protocol.require((root / 'ap-home').is_dir() and not (root / 'ap-home').is_symlink(), 'Replay home missing')
    for name in ('release.log', 'observer.log'):
        protocol.file_record(root / name)
    source = Path(profile['source_root'])
    recipe = Path(profile['tooling_root']) / 'scripts/release_recipes/compat'
    commands = [{'argv': [profile['ap_replay']['node'], str(Path(profile['ap_replay']['node_modules']) / 'vitest/vitest.mjs'),
        'run', str(test), '--config', str(recipe / 'vitest.replay.config.ts'), '--reporter', 'json',
        '--outputFile', str(root / (label + '-vitest.json'))], 'exit_code': 0}
        for label, test in (('release', source / 'tests/codex-plus-footer.test.ts'),
                            ('observer', recipe / 'ap-release-replay-observer.test.ts'))]
    protocol.require(protocol.load(root / 'replay-report.json') == {'passed': True, 'candidate_acceptance': True,
        'ap_version': plan['compat']['ap_version'], **plan['compat']['ap_replay'], 'commands': commands,
        'captures': 20, 'busy_pairs': 2, 'tests': 23, 'live_nudge_invoked': False,
        'classifier_sha256': native.sha(source / 'src/commands/nudge.ts'),
        'cursor_sha256': native.sha(source / 'src/core/tmux.ts'),
        'observer_sha256': native.sha(recipe / 'ap-release-replay-observer.test.ts')}, 'Replay report failed or differs')
    for label, count in (('release', 22), ('observer', 1)):
        tests = protocol.load(root / (label + '-vitest.json'))
        protocol.require(tests['success'] is True and tests['numPassedTests'] == count and
            all(tests[name] == 0 for name in ('numFailedTests', 'numPendingTests', 'numTodoTests')), 'Vitest counts differ')
    observed = protocol.load(root / 'observed-classifier.json')
    protocol.require(observed['passed'] is True and len(observed['captures']) == 20 and
                     len(observed['busy_pairs']) == 2, 'AP observer evidence incomplete')
    cleanup = protocol.load(root / 'job-cleanup.json')
    audit = protocol.load(audit_path)
    for raw in (cleanup, audit):
        protocol.require(raw['passed'] is True and raw['outcome'] == 'clean' and
            raw['owned_remaining'] == [] and raw['unknown'] == [] and raw['signals_sent'] == 0 and
            raw['completed_local_supervision'] is True,
            'AP cleanup audit is not clean')
    review = protocol.load(executor_review)
    protocol.require(review['scope'] == str(root) and review['job_id'] == state['id'] and
        review['execution'] == str(directory) and review['state_sha256'] == native.sha(directory / 'state.json') and
        review['result_sha256'] == native.sha(directory / 'result.json') and
        review['audit_sha256'] == native.sha(audit_path) and
        all(review[name] is True for name in ('identities_absent', 'groups_absent', 'sessions_absent', 'executor_lock_free')),
        'Independent executor review binding differs')
    metadata, _, _ = protocol.read_attempt(context['native_root'])
    raw = {name: native.sha(root / name) for name in
        ('replay-report.json', 'release-vitest.json', 'observer-vitest.json', 'observed-classifier.json')}
    report = {'architecture': metadata['architecture'], 'plus_source_commit': plan['plus_source_commit'],
        'ap_version': plan['compat']['ap_version'], 'package_sha256': context['package_sha256'],
        'ap_replay': plan['compat']['ap_replay'], 'capture_manifest_sha256': context['capture_manifest_sha256'],
        'checks': {name: {'outcome': 'passed', 'evidence': raw}
                   for name in plan['gates'][metadata['architecture']]['ap']['checks']}}
    execution_record = {'job_id': state['id'], 'executor_package_root': profile['executor_package_root'],
        'executor_sha256': state['runtime']['sha256'], 'architecture': profile['architecture'],
        'plus_source_commit': plan['plus_source_commit'], 'tooling_commit': plan['tooling_commit'],
        'outcome': 'passed', 'exit_code': 0, 'argv': state['manifest']['command']['spec']['argv'], 'cwd': str(root), 'env': proposal['env'],
        'replay_profile_sha256': protocol.digest(profile)}
    executed = workflow.executed_origins(profile, [path for path in context['closure']
        if Path(path) != Path(profile['ap_replay']['installed_registry']).resolve()])
    protocol._execution_identity(execution_record, profile, plan)
    protocol._executed(profile, executed, root=root, plan=plan)
    # All trust-boundary checks precede writing any passing artifact.
    records_dir = root / 'leaf-records'
    records_dir.mkdir(mode=0o700)
    for name in (*records, 'stdout.log', 'cleanup.log'):
        shutil.copyfile(directory / name, records_dir / name)
    shutil.copyfile(prepared_state, records_dir / 'prepared-state.json')
    shutil.copyfile(audit_path, records_dir / 'independent-audit.json')
    shutil.copyfile(executor_review, records_dir / 'executor-review.json')
    protocol.write_once(root / 'leaf-ap-report.json', report)
    protocol.write_once(root / 'leaf-execution.json', execution_record)
    protocol.write_once(root / 'leaf-executed.json', executed)
    protocol.write_once(root / 'leaf-provenance.json', {'schema': 1,
        'coordinator_code': prelaunch['coordinator_code'], 'execution_tooling_commit': plan['tooling_commit'],
        'original_proposal_sha256': native.sha(root / 'ap-job-proposal.json'),
        'leaf_proposal_sha256': native.sha(root / 'leaf-job-proposal.json'), 'job_id': state['id'],
        'execution': str(directory), 'review_digest': state['review_digest'], 'run_ap_not_launched': True,
        'wrapper_sha256': hashlib.sha256(LEAF_CODE.encode()).hexdigest(),
        'child_replay_argv': replay_argv,
        'diff_scope': 'read-only cleanup verifier, pure AP preparation and leaf receipt coordinator'})
    protocol.write_ap_receipt(context['native_root'], root, inputs=list(INPUTS), executed=executed,
        outputs=list(inventory(root)), evidence='leaf-ap-report.json', execution=execution_record,
        cleanup={'outcome': 'clean', 'owned_remaining': 0, 'unknown': 0, 'report': 'job-cleanup.json'}, replay_profile=profile)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare-leaf', 'compose'))
    parser.add_argument('--coordinator', type=Path, required=True)
    parser.add_argument('--execution', type=Path)
    parser.add_argument('--prepared-state', type=Path)
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--executor-review', type=Path)
    args = parser.parse_args()
    if args.command == 'prepare-leaf':
        result = prepare_leaf(args.coordinator)
    else:
        if any(value is None for value in (args.execution, args.prepared_state, args.audit, args.executor_review)):
            parser.error('compose requires --execution, --prepared-state, --audit and --executor-review')
        result = compose(args.coordinator, args.execution, args.prepared_state, args.audit, args.executor_review)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
