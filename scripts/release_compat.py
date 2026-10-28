"""Local native compatibility recipes, extracted once from the qualified closure.

Preparation writes data only. The native owner reviews and launches the returned
argv through its existing managed executor. No host transport or installation.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys

RECIPE = Path(__file__).resolve().parent / 'release_recipes/compat'
UPSTREAM = {'version': '0.163.0', 'tag': 'rust-v0.163.0',
            'commit': '83086188ae0614bd882818ff1aa23f3194962012'}
UPSTREAM_165 = {'version': '0.165.0', 'tag': 'rust-v0.165.0',
                'commit': '360319d8dee62d87e2d59fc0c21805d79519cfed'}
GATES = ('quiet-rollback', 'advisor-nine-checks', 'actual-old-tui-root-result', 'packaged-cursor-v2', 'packaged-recovery-policy')
PAIRED_GATE = 'paired-stock-transition'
PAIRED_FIXTURE = 'tests/fixtures/paired_stock_transition.py'


def gate_names(revision):
    require(type(revision) is int and revision in (1, 2), 'Unsupported compatibility recipe')
    return GATES + ((PAIRED_GATE,) if revision == 2 else ())


def upstreams(config):
    revision = config['recipe_revision']
    gate_names(revision)
    candidate = config['upstream']
    previous = config.get('previous_upstream', candidate)
    if revision == 1:
        require(candidate == UPSTREAM, 'New upstream requires recipe qualification')
        require(previous == candidate and 'previous_stock_binary_sha256' not in config and
                'previous_stock_binary' not in config,
                'Rollback upstream differs or unexpected previous stock')
    else:
        require(candidate == UPSTREAM_165 and previous == UPSTREAM, 'Unsupported paired upstream transition')
        pins = config.get('previous_stock_binary_sha256', {})
        require(set(pins) == {'x86_64', 'aarch64'} and
            set(config['stock_binary_sha256']) == set(pins) and
            all(re.fullmatch('[0-9a-f]{64}', value) and value != '0' * 64 and
                re.fullmatch('[0-9a-f]{64}', config['stock_binary_sha256'][arch]) and
                config['stock_binary_sha256'][arch] != '0' * 64 and value != config['stock_binary_sha256'][arch]
                for arch, value in pins.items()), 'Missing or equal previous stock identity pin')
    return previous, candidate


RECOVERY_CASES = {
    'detached': ('cleanup-recovery-success', 'cleanup-recovery-failure', 'cleanup-recovery-old-reader',
        'goal-cleanup-recovery', 'goal-cleanup-recovery-old-reader', 'daemon-graceful',
        'observation-gap', 'cancellation', 'watch-recovery'),
    'goals': ('input-epoch', 'paused-recovery', 'replaced-recovery',
        'mixed-independent-priority', 'pre-report-recovery-barrier')}
RECOVERY_FIXTURES = ('tests/test_detached_jobs_stock.py', 'tests/test_custom_goals_stock.py',
    'tests/fixtures/detached_jobs.py', 'tests/fixtures/custom_goal_server.py')


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')


def validate_recovery_reports(evidence, config):
    """Read every case; a successful command or aggregate cannot replace its proof."""
    evidence = Path(evidence)
    path = evidence / 'report.json'
    require(path.is_file() and not path.is_symlink(), 'Recovery report missing')
    report = json.loads(path.read_text())
    identities = {'runtime_sha256': digest(Path(config['new_package']) / 'bin/codex-plus-runtime'),
        'old_runtime_sha256': digest(Path(config['old_package']) / 'bin/codex-plus-runtime'),
        'stock_sha256': config['stock_binary_sha256'][config['architecture']]}
    require(report.get('schema') == 1 and report.get('passed') is True and
        report.get('source_commit') == config['source_commit'] and
        all(report.get(key) == value for key, value in identities.items()), 'Recovery identity/result mismatch')
    if config['recipe_revision'] == 2:
        require(report.get('stock_role') == 'candidate' and report.get('stock_upstream') == config['upstream'],
                'Recovery daemon role mismatch')
    short = Path(json.loads((Path(config['scope']) / 'short-binding.json').read_text())['path'])
    require(short.parent == Path(config['short_root']) and len(short.name) == 2 and short.name.isalnum(),
        'Recovery short allocation mismatch')
    owned_short = short.resolve()
    expected = [(suite, scenario) for suite, cases in RECOVERY_CASES.items() for scenario in cases]
    require([(row.get('suite'), row.get('scenario')) for row in report.get('cases', [])] == expected,
        'Recovery case inventory incomplete')
    bindings = {'report.json': digest(path)}
    for row in report['cases']:
        name = f"{row['suite']}/{row['scenario']}/report.json"
        path = evidence / name
        require(row.get('report') == name and path.is_file() and not path.is_symlink() and
            digest(path) == row.get('sha256'), 'Recovery case report missing or changed')
        case = json.loads(path.read_text())
        if config['recipe_revision'] == 2:
            require(case.get('stock_role') == 'candidate' and case.get('stock_upstream') == config['upstream'],
                    'Recovery case daemon role mismatch')
        require(case.get('passed') is True and case.get('scenario') == row['scenario'] and
            case.get('runtime_sha256') == identities['runtime_sha256'] and
            case.get('stock_sha256') == identities['stock_sha256'] and
            case.get('stock_version') == 'codex-cli ' + config['upstream']['version'] and
            case.get('remote_model_requests') == 0 and case.get('private_home_removed') is True,
            'Recovery case failed, wrong helper/daemon, paid request, or unresolved cleanup')
        home = Path(case.get('private_home', ''))
        resolved_home = home.resolve()
        require(home.is_absolute() and '..' not in home.parts and resolved_home != owned_short and
            resolved_home.is_relative_to(owned_short) and not home.exists(),
            'Recovery private home escaped owned scope or survived cleanup')
        scenario = row['scenario']
        old_reader = scenario.endswith('old-reader')
        if old_reader:
            require(case.get('old_runtime_sha256') == identities['old_runtime_sha256'],
                'Recovery old helper mismatch')
        notification = 'REPORT ONLY:' if old_reader else 'RECOVERY REQUIRED:'
        if scenario.startswith('cleanup-recovery'):
            require(case.get('recovery_policy') == dict(exit_code=0 if scenario.endswith('success') else 7,
                old_reader=old_reader, notification=notification, one_workload=True,
                original_result_preserved=True, claims_held_until_verified_resolution=True),
                'Recovery execution policy mismatch')
        elif scenario.startswith('goal-cleanup-recovery'):
            require(case.get('goal_recovery') == dict(old_reader=old_reader, notification=notification,
                report_barrier=int(old_reader), original_result_preserved=True, one_workload=True,
                cleanup_claim_retained=old_reader, recovered_in_same_callback=not old_reader),
                'Recovery goal policy mismatch')
        bindings[name] = digest(path)
    return bindings


def validate_config(config):
    require(config['schema'] == 1, 'Unsupported compatibility recipe')
    previous_upstream, candidate_upstream = upstreams(config)
    require(config['architecture'] in ('x86_64', 'aarch64'), 'Unsupported native architecture')
    for key in ('source_commit', 'fixture_source_commit', 'frontend_compile_origin'):
        require(re.fullmatch('[0-9a-f]{40}', config[key]) and config[key] == config['source_commit'], 'Wrong source identity: ' + key)
    for key in ('new_manifest_sha256', 'new_archive_sha256', 'qualification_evidence_sha256', 'frontend_patch_sha256'):
        require(re.fullmatch('[0-9a-f]{64}', config[key]) and config[key] != '0' * 64, 'Invalid digest: ' + key)
    for key in ('scope', 'source', 'fixture_source', 'old_package', 'new_package', 'new_archive', 'stock_binary', 'qualification_evidence', 'short_root'):
        require(Path(config[key]).is_absolute() and '..' not in Path(config[key]).parts, 'Invalid absolute path: ' + key)
    require(config['source'] == config['fixture_source'], 'Fixtures must come from the accepted source')
    require(not Path(config['patch_path']).is_absolute() and '..' not in Path(config['patch_path']).parts, 'Unsafe patch path')
    require(config['root_accepted_final_package'] is True, 'Root package acceptance required')
    require(config['previous_release'] != config['release'] and config['release'].startswith(candidate_upstream['version'] + '-plus.'), 'Wrong release identity')
    require(config['previous_release'].startswith(previous_upstream['version'] + '-plus.'), 'Rollback upstream differs')
    require(re.fullmatch('[0-9a-f]{40}', config['previous_identity']['commit']), 'Invalid previous source')
    for mapping in (config['previous_identity']['manifests'], config['stock_binary_sha256']):
        require(config['architecture'] in mapping and all(re.fullmatch('[0-9a-f]{64}', value) for value in mapping.values()), 'Missing native identity pin')
    if config['recipe_revision'] == 2:
        require(Path(config['previous_stock_binary']).is_absolute() and '..' not in Path(config['previous_stock_binary']).parts,
                'Invalid absolute path: previous_stock_binary')
        require(PAIRED_FIXTURE in config['fixture_files'], 'Paired fixture closure missing')
    require(bool(config['fixture_files']), 'Fixture closure missing')
    for name, sha in config['fixture_files'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and re.fullmatch('[0-9a-f]{64}', sha), 'Unsafe fixture binding')
    require(set(RECOVERY_FIXTURES) <= config['fixture_files'].keys(), 'Recovery fixture closure missing')
    require(config['recipe_hashes'] == {path.name: digest(path) for path in RECIPE.iterdir() if path.is_file()}, 'Recipe bytes changed')
    return config


def validate_paired_report(path, config):
    """Validate the dedicated paired proof again at execution and export seams."""
    path = Path(path)
    require(config['recipe_revision'] == 2 and path.is_file() and not path.is_symlink(),
            'Paired transition report missing')
    report = json.loads(path.read_text())
    for key in ('source_commit', 'release', 'previous_release', 'upstream', 'previous_upstream', 'architecture'):
        require(report.get(key) == config[key], 'Paired transition identity mismatch: ' + key)
    require(report.get('configuration_sha256') == digest(Path(config['scope']) / 'native.config.json') and
        report.get('new_manifest_sha256') == config['new_manifest_sha256'] and
        report.get('old_manifest_sha256') == config['previous_identity']['manifests'][config['architecture']],
        'Paired package/configuration mismatch')
    require(report.get('schema') == 1 and all(report.get(key) is True for key in
        ('passed', 'old_stock_readable', 'old_stock_resumed_inference', 'original_result_preserved',
         'rows_unchanged', 'private_home_removed')) and
        report.get('snapshot_restore') is False and report.get('remote_model_requests') == 0 and
        report.get('provider_requests_from_reads_holds_restart') == 0 and report.get('workload_runs') == 1 and
        report.get('callbacks') == 1 and report.get('owned_survivors_after_cleanup') == [],
        'Paired transition failed, duplicated, restored snapshot, or cleanup incomplete')
    short = Path(json.loads((Path(config['scope']) / 'short-binding.json').read_text())['path']).resolve()
    home = Path(report.get('private_home', ''))
    require(home.is_absolute() and '..' not in home.parts and home.resolve().is_relative_to(short) and
        home.resolve() != short and not home.exists(), 'Paired private home escaped allocation or survived cleanup')
    phases = report.get('phases', [])
    require([row.get('name') for row in phases] == ['old-before', 'candidate', 'old-rollback'],
        'Paired phase inventory mismatch')
    for row, role in zip(phases, ('old', 'new', 'old')):
        upstream = config['previous_upstream'] if role == 'old' else config['upstream']
        pins = config['previous_stock_binary_sha256'] if role == 'old' else config['stock_binary_sha256']
        require(row.get('stock_sha256') == pins[config['architecture']] and
            row.get('stock_version') == 'codex-cli ' + upstream['version'] and
            row.get('runtime_sha256') == digest(Path(config[role + '_package']) / 'bin/codex-plus-runtime'),
            'Paired daemon/helper role mismatch')
    require(len({row.get('generation') for row in phases}) == 3 and all(row.get('generation') for row in phases),
        'Paired daemon generations missing or reused')
    before, after = report.get('ledger_before'), report.get('ledger_after')
    require(isinstance(before, dict) and before == after and
        all(before.get('tables', {}).get(name) for name in
            ('jobs', 'callbacks', 'goals', 'ultracode_turns', 'ultracode_submissions')),
        'Paired original ledger rows/schema changed or missing')
    before, after = report.get('execution_before_sha256', {}), report.get('execution_after_sha256', {})
    require({'manifest.json', 'state.json', 'result.json', 'observation.json'} <= before.keys() and
        before.keys() == after.keys() and all(before[name] == after[name] for name in before if name != 'state.json'),
        'Paired immutable execution records changed')
    before, after = report.get('execution_state_before', {}), report.get('execution_state_after', {})
    require(before.get('bytes_sha256') and before.get('bytes_sha256') == after.get('bytes_sha256') and
        type(before.get('clock')) in (int, float) and type(after.get('clock')) in (int, float) and
        math.isfinite(before['clock']) and math.isfinite(after['clock']) and
        before['clock'] <= after['clock'], 'Paired execution state changed beyond maintenance timestamp')
    holds = report.get('holds', [])
    require(len(holds) == 2 and holds[0]['entered']['id'] != holds[1]['entered']['id'],
        'Paired bidirectional holds missing')
    for row in holds:
        entered, observed, left = (row[name] for name in ('entered', 'observed', 'left'))
        require(entered['id'] == observed['id'] == left['id'] and left['held'] is False and
            all(value.get(name) is True for value in (entered, observed)
                for name in ('held', 'admission_quiet', 'execution_quiet')), 'Paired hold identity/quiet mismatch')
    require(report.get('new_turn') and report.get('rollback_turn') and
        report['new_turn'] != report['rollback_turn'], 'Paired resumed inference turns missing')
    fixture_path = path.parent / 'fixture/report.json'
    require(fixture_path.is_file() and not fixture_path.is_symlink() and
        digest(fixture_path) == report.get('fixture_report_sha256'), 'Paired fixture evidence changed or missing')
    fixture = json.loads(fixture_path.read_text())
    require(fixture.get('passed') is True and fixture.get('remote_model_requests') == 0 and
        fixture.get('stock_sha256') == config['previous_stock_binary_sha256'][config['architecture']] and
        fixture.get('stock_version') == 'codex-cli ' + config['previous_upstream']['version'] and
        fixture.get('runtime_sha256') == digest(Path(config['old_package']) / 'bin/codex-plus-runtime'),
        'Paired original fixture identity/result mismatch')
    requests = fixture.get('provider_requests', [])
    require(len(requests) >= 2 and [row['body'].get('reasoning', {}).get('effort') for row in requests[-2:]] ==
        ['xhigh', 'high'] and all(row.get('thread') == fixture.get('parent') for row in requests[-2:]),
        'Paired candidate effort/old-stock inference wire proof missing')
    execution, parent = report.get('execution_id', ''), fixture.get('parent', '')
    require(all(re.fullmatch('[0-9a-f-]{36}', value) for value in (execution, parent)),
        'Paired execution capture identity invalid')
    for label, directory in (('before', path.parent / 'fixture/execution-baseline'),
            ('after', path.parent / 'fixture/executions/v1' / parent / execution)):
        require(directory.is_dir() and not directory.is_symlink(), 'Paired execution capture missing')
        files = {}
        for captured in directory.rglob('*'):
            require(not captured.is_symlink(), 'Paired execution capture contains symlink')
            if captured.is_file() and captured.suffix == '.json':
                files[captured.relative_to(directory).as_posix()] = digest(captured)
        require(files == report['execution_' + label + '_sha256'], 'Paired execution capture bytes changed')
        raw = (directory / 'state.json').read_bytes()
        state = json.loads(raw)
        clock = state.get('updated_at')
        match = re.search(rb'("updated_at":)(-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)(?=[,}])', raw)
        require(type(clock) in (int, float) and math.isfinite(clock) and
            raw.count(b'"updated_at"') == 1 and match and json.loads(match[2]) == clock,
            'Paired execution timestamp capture malformed')
        normalized = raw[:match.start(2)] + b'<updated_at>' + raw[match.end(2):]
        require(report['execution_state_' + label] == dict(clock=clock,
            bytes_sha256=hashlib.sha256(normalized).hexdigest()), 'Paired execution timestamp proof mismatch')
    workload = path.parent / 'fixture/work/job/runs'
    require(workload.is_file() and not workload.is_symlink() and workload.read_text() == 'run\n',
        'Paired workload replayed or original evidence missing')
    return report


def accepted_package(config):
    """Check the approval before creating any private fixture directories."""
    require(digest(config['qualification_evidence']) == config['qualification_evidence_sha256'], 'Approval digest mismatch')
    approved = json.loads(Path(config['qualification_evidence']).read_text())
    require(all(approved.get(name) is True for name in ('accepted', 'cleanup_passed', 'native')), 'Package is not accepted and clean')
    for actual, expected in (('source', 'source_commit'), ('architecture', 'architecture'),
                             ('archive_sha256', 'new_archive_sha256'), ('manifest_sha256', 'new_manifest_sha256')):
        require(approved[actual] == config[expected], 'Wrong approval ' + actual)
    require(digest(Path(config['new_package']) / 'codex-plus.json') == config['new_manifest_sha256'], 'Package manifest mismatch')
    manifest = json.loads((Path(config['new_package']) / 'codex-plus.json').read_text())
    require(manifest['release'] == config['release'] and manifest['plus_commit'] == config['source_commit'], 'Wrong package source/release')
    require(manifest['target'] == config['architecture'] + '-unknown-linux-gnu', 'Wrong package architecture')
    require(manifest['coordinator_protocol'] == 5 and manifest['execution_record_format'] == 1 and manifest['upstream'] == config['upstream'] and
        config['upstream'] == (UPSTREAM if config['recipe_revision'] == 1 else UPSTREAM_165), 'Unsupported package protocol')


def fixture_inputs(plan, profile):
    """Check compatibility's stable fixture and gate contract before allocation."""
    try:
        upstreams(dict(plan['compat'], upstream=plan['upstream'],
            previous_upstream=plan['previous_release'].get('upstream', plan['upstream'])))
    except ValueError as exc:
        raise ValueError('Unsupported compatibility recipe/upstream: ' + str(exc)) from exc
    if plan['compat']['recipe_revision'] == 2:
        require(PAIRED_FIXTURE in plan['compat']['fixture_files'], 'Paired fixture closure missing')
    fixtures = plan['compat']['fixture_files']
    require(set(RECOVERY_FIXTURES) <= fixtures.keys(), 'Recovery fixture closure missing')
    for name, sha in fixtures.items():
        require(digest(Path(profile['source_root']) / name) == sha, 'Wrong fixture bytes: ' + name)
    gates = plan['gates'][profile['architecture']]
    require(tuple(gates['compat']['checks']) == gate_names(plan['compat']['recipe_revision']) and
            gates['captures']['checks'] == ['native-cursor-states'], 'Unsupported compatibility checks')


def prepare(plan, profile, attempt, *, config, helper_binding):
    """Bind accepted packages and committed recipes; return the local launch data."""
    attempt = Path(attempt).resolve(strict=True)
    arch = profile['architecture']
    previous = plan['previous_release']
    compat = plan['compat']
    values = dict(config, schema=1, recipe_revision=compat['recipe_revision'],
        architecture=arch, source_commit=plan['plus_source_commit'],
        fixture_source_commit=plan['plus_source_commit'], frontend_compile_origin=plan['plus_source_commit'],
        source=profile['source_root'], fixture_source=profile['source_root'],
        release=plan['release'], previous_release=previous['tag'], upstream=plan['upstream'],
        patch_path=plan['patch']['path'], frontend_patch_sha256=plan['patch']['sha256'],
        short_root=profile['short_root'], scope=str(attempt / 'compat'),
        previous_identity={'commit': compat['previous_source_commit'],
            'manifests': {key: row['manifest_sha256'] for key, row in previous['targets'].items()}},
        stock_binary_sha256=compat['stock_binary_sha256'], fixture_files=compat['fixture_files'],
        gate_checks=plan['gates'][arch],
        recipe_hashes={path.name: digest(path) for path in sorted(RECIPE.iterdir()) if path.is_file()})
    if compat['recipe_revision'] == 2:
        values.update(previous_upstream=previous['upstream'],
            previous_stock_binary=profile['native_build']['previous_official_binary'],
            previous_stock_binary_sha256=compat['previous_stock_binary_sha256'])
    elif 'upstream' in previous:
        values['previous_upstream'] = previous['upstream']
    validate_config(values)
    accepted_package(values)
    require(digest(values['new_archive']) == values['new_archive_sha256'], 'Archive digest mismatch')
    require(digest(values['stock_binary']) == values['stock_binary_sha256'][arch], 'Stock binary digest mismatch')
    if compat['recipe_revision'] == 2:
        require(digest(values['previous_stock_binary']) == values['previous_stock_binary_sha256'][arch] ==
            profile['native_build']['previous_official_sha256'], 'Previous stock binary digest mismatch')
    source = Path(values['source'])
    require(subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == values['source_commit'], 'Wrong source checkout')
    require(digest(source / values['patch_path']) == values['frontend_patch_sha256'], 'Wrong frontend patch')
    fixture_inputs(plan, profile)
    require(digest(helper_binding['helper']) == helper_binding['sha256'], 'Owned supervisor binding mismatch')
    scope = Path(values['scope'])
    scope.mkdir(mode=0o700)
    write(scope / 'native.config.json', values)
    write(scope / 'helper-binding.json', helper_binding)
    footer = json.loads((RECIPE / 'footer-contract.json').read_text())
    footer.update(release=plan['release'], frontend_patch_sha256=plan['patch']['sha256'], ap_release=compat['ap_version'])
    write(scope / 'footer-contract.json', footer)
    return {'scope': str(scope), 'argv': [profile['python']['path'], str(Path(__file__).resolve()), 'run', '--scope', str(scope)],
            'gates': list(gate_names(compat['recipe_revision'])), 'native_gates_run': False}


def export_reports(scope):
    """Adapt passed raw native evidence to the coordinator's existing gate schema."""
    scope = Path(scope).resolve(strict=True)
    config = validate_config(json.loads((scope / 'native.config.json').read_text()))
    evidence = scope / 'evidence'
    raw = json.loads((evidence / 'report.json').read_text())
    require(raw['passed'] is True and raw['architecture'] == config['architecture'] and
            raw['source_commit'] == config['source_commit'], 'Native report identity mismatch')
    require(tuple(row['name'] for row in raw['stages']) == gate_names(config['recipe_revision']) and
            all(row['passed'] is True and row['exit_code'] == 0 for row in raw['stages']), 'Incomplete native gates')
    require(raw['configuration_sha256'] == digest(scope / 'native.config.json') and
            raw['gate_sha256'] == config['recipe_hashes']['verify-mixes.py'], 'Native recipe/configuration mismatch')
    require(raw['archive']['archive_sha256'] == config['new_archive_sha256'] and
            raw['packages']['new']['manifest_sha256'] == config['new_manifest_sha256'], 'Native package binding mismatch')
    for name, sha in raw['evidence_sha256'].items():
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and digest(evidence / name) == sha, 'Native dedicated report changed')
    recovery = {'recovery-policy/' + name: sha for name, sha in
                validate_recovery_reports(evidence / 'recovery-policy', config).items()}
    require(all(raw['evidence_sha256'].get(name) == sha for name, sha in recovery.items()),
            'Recovery dedicated reports missing from native evidence')
    if config['recipe_revision'] == 2:
        validate_paired_report(evidence / 'paired-stock-transition/report.json', config)
        name = 'paired-stock-transition/report.json'
        require(raw['evidence_sha256'].get(name) == digest(evidence / name), 'Paired dedicated report missing')
    root = scope.parent
    reports = ('report.json', 'rollback/gate.json', 'advisor-nine-checks.json', 'old-tui-root-result/report.json')
    if config['recipe_revision'] == 2:
        reports += ('paired-stock-transition/report.json',)
    raw_bindings = {str((evidence / name).relative_to(root)): digest(evidence / name) for name in reports}
    raw_bindings.update({str((evidence / name).relative_to(root)): sha for name, sha in recovery.items()})
    captures = evidence / 'cursor-v2'
    manifest = json.loads((captures / 'manifest.json').read_text())
    integrity = json.loads((captures / 'integrity.json').read_text())
    require(manifest['candidate_acceptance'] is True and manifest['capture_complete'] is True and
            len(manifest['captures']) == 20 and integrity['passed'] is True, 'Incomplete native captures')
    files = {}
    for path in sorted(captures.rglob('*')):
        require(not path.is_symlink(), 'Capture contains a symlink')
        if path.is_file():
            info = path.stat()
            files[str(path.relative_to(root))] = {'sha256': digest(path), 'size': info.st_size,
                                                  'mode': f'{info.st_mode & 0o777:04o}'}
    inventory = scope / 'capture-manifest.json'
    write(inventory, {'architecture': config['architecture'], 'package_sha256': config['new_archive_sha256'], 'files': files})
    capture_bindings = {str(path.relative_to(root)): digest(path) for path in (captures / 'manifest.json', captures / 'integrity.json', inventory)}
    common = {'architecture': config['architecture'], 'plus_source_commit': config['source_commit']}
    compat_report = dict(common, checks={name: {'outcome': 'passed', 'evidence': raw_bindings | capture_bindings}
        for name in config['gate_checks']['compat']['checks']})
    captures_report = dict(common, package_sha256=config['new_archive_sha256'],
        capture_manifest=str(inventory.relative_to(root)), capture_manifest_sha256=digest(inventory),
        checks={name: {'outcome': 'passed', 'evidence': capture_bindings}
                for name in config['gate_checks']['captures']['checks']})
    write(scope / 'compat-report.json', compat_report)
    write(scope / 'captures-report.json', captures_report)
    return {'compat': str(scope / 'compat-report.json'), 'captures': str(scope / 'captures-report.json'),
            'capture_manifest': str(inventory)}


def run(scope, *, setup_only=False):
    """Run complete setup before gates using the pinned existing child supervisor."""
    scope = Path(scope).resolve(strict=True)
    config = validate_config(json.loads((scope / 'native.config.json').read_text()))
    require(platform.machine() == config['architecture'], 'Compatibility must run on the native host')
    accepted_package(config)
    env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=str(scope), CODEX_PLUS_RELEASE_QA_SCOPE=str(scope), PYTHONDONTWRITEBYTECODE='1')
    # Import the same recipe adapter used by every setup and workload subprocess.
    os.environ.update(env)
    sys.path.insert(0, str(RECIPE))
    import owned_run
    from permission_cleanup import capture_baseline
    short_root = Path(config['short_root']).resolve(strict=True)
    require(not (scope / 'short-binding.json').exists(), 'Preserve previous attempt; prepare a fresh compatibility stage')
    for unused in range(4096):
        short = short_root / ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(2))
        try:
            short.mkdir(mode=0o700)
            break
        except FileExistsError:
            pass
    else:
        raise FileExistsError('No free short fixture directory')
    info = short.stat()
    write(scope / 'short-state.json', {'path': str(short), 'device': info.st_dev, 'inode': info.st_ino,
        'uid': info.st_uid, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'state': 'allocated'})
    baseline = capture_baseline([scope, short], scope)
    write(scope / 'unreadable-baseline.json', baseline)
    write(scope / 'short-binding.json', {'path': str(short), 'device': info.st_dev, 'inode': info.st_ino,
                                        'baseline_sha256': digest(scope / 'unreadable-baseline.json')})
    env['TMPDIR'] = str(short)
    os.environ['TMPDIR'] = str(short)
    (scope / 'setup-smoke').mkdir(mode=0o700)
    with (scope / 'setup-smoke.log').open('x') as log:
        result = owned_run.run([sys.executable, RECIPE / 'preflight.py'], cwd=scope, env=env, stdout=log, timeout=90)
    require(result.returncode == 0, 'Setup failed; preserve attempt and cleanup evidence')
    if setup_only:
        return {'passed': True, 'setup_only': True, 'native_gates_run': False, 'evidence': str(scope / 'preflight.json')}
    with (scope / 'native-owned.log').open('x') as log:
        result = owned_run.run([sys.executable, RECIPE / 'run-native-compat-v5.py', '--config', scope / 'native.config.json', '--evidence', scope / 'evidence'],
                               cwd=scope, env=env, stdout=log, timeout=930)
    require(result.returncode == 0, 'Native compatibility failed; preserve raw evidence')
    return {'passed': True, 'evidence': str(scope / 'evidence/report.json'), 'cleanup_required': True,
            'reports': export_reports(scope)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('precheck', 'precheck-ap', 'run', 'prepare-attempt', 'verify-attempt', 'run-attempt', 'cleanup-attempt',
        'prepare-ap', 'verify-ap', 'replay-ap', 'run-ap', 'cleanup-ap'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--cleanup-hook', type=Path)
    parser.add_argument('--scope', type=Path)
    parser.add_argument('--attempt', type=Path)
    parser.add_argument('--old-package', type=Path)
    parser.add_argument('--execution', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--coordinator', type=Path)
    parser.add_argument('--setup-only', action='store_true')
    args = parser.parse_args()
    if args.command in ('precheck', 'precheck-ap'):
        sys.dont_write_bytecode = True
    if args.command == 'run':
        require(args.scope is not None, '--scope is required')
        result = run(args.scope, setup_only=args.setup_only)
    else:
        sys.path.insert(0, str(RECIPE))
        import workflow
        if args.command in ('precheck', 'precheck-ap'):
            require(args.plan is not None and args.profile is not None, '--plan and --profile are required')
            plan, profile = (json.loads(path.read_text()) for path in (args.plan, args.profile))
            if args.command == 'precheck':
                require(args.old_package is not None and args.cleanup_hook is not None,
                        '--old-package and --cleanup-hook are required')
                result = workflow.precheck(plan, profile, args.old_package, json.loads(args.cleanup_hook.read_text()))
            else:
                result = workflow.precheck_ap(plan, profile)
        elif args.attempt is None:
            raise ValueError('--attempt is required')
        elif args.command == 'prepare-attempt':
            require(args.old_package is not None, '--old-package is required')
            result = workflow.prepare_attempt(args.attempt, args.old_package)
        elif args.command == 'verify-attempt':
            context = workflow.verify_attempt(args.attempt)
            result = {'verified': True, 'attempt': context['attempt']}
        elif args.command == 'cleanup-attempt':
            require(args.output is not None, '--output is required')
            result = workflow.cleanup_attempt(args.attempt, args.output)
        elif args.command == 'prepare-ap':
            require(args.profile is not None and args.coordinator is not None, '--profile and --coordinator are required')
            result = workflow.prepare_ap(args.attempt, args.coordinator, json.loads(args.profile.read_text()))
        elif args.command == 'verify-ap':
            context = workflow.verify_ap(args.attempt)[0]
            result = {'verified': True, 'native_attempt': context['native_attempt']}
        elif args.command == 'replay-ap':
            result = workflow.replay_ap(args.attempt)
        elif args.command == 'cleanup-ap':
            require(args.output is not None, '--output is required')
            result = workflow.cleanup_ap(args.attempt, args.output)
        elif args.command == 'run-ap':
            require(args.execution is not None, '--execution is required')
            result = workflow.run_ap(args.attempt, args.execution)
        else:
            require(args.execution is not None, '--execution is required')
            result = workflow.run_attempt(args.attempt, args.execution)
    print(json.dumps(result))
    if args.command in ('cleanup-attempt', 'cleanup-ap') and result['outcome'] != 'clean':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
