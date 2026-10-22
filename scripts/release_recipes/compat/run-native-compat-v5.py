#!/usr/bin/env python3
"""One short native batch: strict previous rollback, helper checks, actual old-TUI result."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import traceback

sys.dont_write_bytecode = True
AREA = Path(__file__).resolve().parent
SOURCE = CONFIG['source_commit']
ORIGIN = CONFIG['source_commit']
PATCH = CONFIG['frontend_patch_sha256']


def validate_config(config):
    assert type(config['schema']) is int and config['schema'] == 1
    assert config['architecture'] in ('x86_64', 'aarch64')
    assert config['source_commit'] == SOURCE and config['frontend_compile_origin'] == ORIGIN
    assert config['fixture_source_commit'] == CONFIG['source_commit']
    assert config['frontend_patch_sha256'] == PATCH
    assert config['root_accepted_final_package'] is True, 'native package acceptance remains a root prerequisite'
    for name in ('new_manifest_sha256', 'new_archive_sha256',
                 'qualification_evidence_sha256'):
        value = config[name]
        assert isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) and value != '0'*64, name
    for name in ('source', 'fixture_source', 'old_package', 'new_package', 'new_archive', 'stock_binary',
                 'qualification_evidence'):
        assert isinstance(config[name], str) and Path(config[name]).is_absolute(), name


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    config = json.loads(args.config.read_text())
    validate_config(config)
    assert platform.machine() == config['architecture'], 'run this gate natively on its target architecture'
    gate = load('gate', AREA / 'verify-mixes.py')
    assert gate.digest(AREA/'old-tui-root-result-v5.py') == CONFIG['recipe_hashes']['old-tui-root-result-v5.py'], 'reviewed recipe changed'
    archives = load('archives', AREA / 'archive-identity.py')
    source = Path(config['source']).resolve(strict=True)
    gate.validate_source_commit(config['source_commit'])
    gate.source_identity(source, SOURCE)
    fixture_source = Path(config['fixture_source']).resolve(strict=True)
    fixtures = gate.fixture_identity(fixture_source)
    assert gate.digest(Path(config['qualification_evidence'])) == config['qualification_evidence_sha256']
    final = json.loads(Path(config['qualification_evidence']).read_text())
    assert final['accepted'] is True and final['cleanup_passed'] is True and final['native'] is True
    assert final['source'] == SOURCE and final['architecture'] == config['architecture']
    assert final['archive_sha256'] == config['new_archive_sha256'] and final['manifest_sha256'] == config['new_manifest_sha256']
    pin = gate.OLD_IDENTITIES[OLD_VERSION]
    architecture = config['architecture']
    old = gate.package_identity(Path(config['old_package']), OLD_VERSION, pin['commit'], pin['manifests'][architecture])
    new = gate.package_identity(Path(config['new_package']), NEW_VERSION, SOURCE, config['new_manifest_sha256'])
    gate.distinct_packages(dict(old=old, new=new))
    archive = archives.archive_identity(config['new_archive'], architecture, NEW_VERSION, SOURCE,
                                        config['new_manifest_sha256'])
    assert archive['archive_sha256'] == config['new_archive_sha256']
    assert archive['helper_sha256'] == new['helper_sha256'] and archive['tree_sha256'] == new['tree_sha256']
    assert gate.digest(source / CONFIG['patch_path']) == PATCH
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = dict(schema=1, passed=False, architecture=architecture, source_commit=SOURCE,
                  fixture_source=fixtures,
                  frontend_compile_origin=ORIGIN, rollback_floor=OLD_VERSION,
                  configuration_sha256=gate.digest(args.config), packages=dict(old=old, new=new), archive=archive,
                  qualification_evidence=config['qualification_evidence'],
                  qualification_evidence_sha256=config['qualification_evidence_sha256'],
                  gate_sha256=gate.digest(AREA/'verify-mixes.py'), stages=[],
                  external_provider_requests=0, crosshost_advisor='separate root gate pending')

    def save():
        (evidence/'report.json').write_text(json.dumps(report, indent=2)+'\n')

    def stage(name, command, timeout):
        row = dict(name=name, argv=list(map(str, command)), passed=False)
        report['stages'].append(row)
        save()
        with (evidence/(name+'.log')).open('x') as log:
            result = subprocess.run(row['argv'], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, timeout=timeout, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        row.update(exit_code=result.returncode, log=str(evidence/(name+'.log')))
        save()
        assert result.returncode == 0, name+' failed; preserve this attempt'
        row['passed'] = True
        save()

    def timeout(signum, frame):
        raise TimeoutError('native combined compatibility gate exceeded 720 seconds')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(720)
    save()
    try:
        stage('quiet-rollback', [sys.executable, AREA/'verify-mixes.py', '--source', source,
            '--fixture-source', fixture_source,
            '--source-commit', SOURCE, '--old', config['old_package'], '--new', config['new_package'],
            '--new-manifest-sha256', config['new_manifest_sha256'], '--stock-binary', config['stock_binary'],
            '--gate-sha256', report['gate_sha256'], '--evidence', evidence/'rollback'], 330)
        advisor_command = [sys.executable]
        advisor_command.append(AREA/'bootstrap-advisor.py')
        stage('advisor-nine-checks', [*advisor_command, AREA/'check-advisor-mixes.py', '--source', source,
            '--old-runtime', Path(config['old_package'])/'bin/codex-plus-runtime',
            '--new-runtime', Path(config['new_package'])/'bin/codex-plus-runtime',
            '--report', evidence/'advisor-nine-checks.json'], 120)
        stage('actual-old-tui-root-result', [sys.executable, AREA/'old-tui-root-result-v5.py', '--source', source,
            '--old-package', config['old_package'], '--new-package', config['new_package'],
            '--new-manifest-sha256', config['new_manifest_sha256'], '--stock-binary', config['stock_binary'],
            '--evidence', evidence/'old-tui-root-result'], 210)
        stage('packaged-cursor-v2', [sys.executable, AREA/'run-cursor.py', '--config', args.config,
            '--footer-contract', DATA/'footer-contract.json',
            '--footer-contract-sha256', gate.digest(DATA/'footer-contract.json'),
            '--evidence', evidence/'cursor-v2'], 300)
        rollback = json.loads((evidence/'rollback/gate.json').read_text())
        advisor = json.loads((evidence/'advisor-nine-checks.json').read_text())
        tui = json.loads((evidence/'old-tui-root-result/report.json').read_text())
        assert rollback['passed'] and rollback['rollback_qualified'] and len(rollback['checks']) == 7
        assert rollback['architecture'] == architecture and rollback['gate_sha256'] == report['gate_sha256']
        assert rollback['fixture_source']['commit'] == config['fixture_source_commit']
        assert advisor['passed'] and advisor['tests_run'] == 9 and advisor['source_commit'] == SOURCE
        assert advisor['runtimes']['new']['sha256'] == new['helper_sha256']
        assert advisor['runtimes']['old']['sha256'] == old['helper_sha256']
        assert tui['passed'] and tui['source_commit'] == SOURCE and not tui['owned_survivors_after_cleanup']
        assert tui['old_claude_display_observed'] and tui['explicit_context_only']
        assert tui['root_tool_result']['exit_code'] == 0 and tui['root_requests'] == 2
        cursor = json.loads((evidence/'cursor-v2/manifest.json').read_text())
        integrity = json.loads((evidence/'cursor-v2/integrity.json').read_text())
        assert cursor['candidate_acceptance'] and cursor['capture_complete'] and len(cursor['captures']) == 20
        assert integrity['passed'] and not integrity['baseline_only'] and not integrity['development_pre_package']
        assert gate.package_identity(Path(config['new_package']), NEW_VERSION, SOURCE, config['new_manifest_sha256']) == new
        gate.source_identity(source, SOURCE)
        assert gate.fixture_identity(fixture_source) == fixtures
        report.update(passed=True, rollback_qualified=True, rollback_floor=OLD_VERSION,
            nine_advisor_checks_passed=True, actual_old_tui_new_cli_root_result_proved=True,
            fresh_packaged_cursor_v2_passed=True, ap_replay='separate root gate pending',
            restart_required_before_production_claude=False,
            evidence_sha256={str(path.relative_to(evidence)):gate.digest(path) for path in (
                evidence/'rollback/gate.json', evidence/'advisor-nine-checks.json', evidence/'old-tui-root-result/report.json')})
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        signal.alarm(0)
        save()
    print(json.dumps(dict(passed=report['passed'], architecture=architecture, evidence=str(evidence/'report.json'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args())
