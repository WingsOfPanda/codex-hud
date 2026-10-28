"""Native frontend and focused harness for the qualified upstream recipe."""
import json
from pathlib import Path
import shutil



FILTERS = ('morandi', 'theme_picker', 'render::highlight', 'custom_terminal::',
    'insert_history::', 'style::contrast', 'bottom_pane::status_line_style::tests',
    'exec_cell::compact', 'worker_hud', 'execution_hud')


def filters(plan):
    if plan['upstream']['version'] == '0.165.0':
        return FILTERS + ('plus_goal', 'ultracode', 'shell_policy', 'command_display',
            'compact_display', 'daemon_menu', 'native_history', 'active_turn_steer',
            'chatwidget::tests::exec_flow')
    return FILTERS


def compiler_artifacts(output):
    artifacts = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('reason') == 'compiler-artifact':
            if type(row.get('fresh')) is not bool:
                raise ValueError('Cargo compiler-artifact freshness is missing or invalid')
            artifacts.append(row)
    if not artifacts:
        raise ValueError('Missing Cargo compiler-artifact records')
    return artifacts


def frontend(c):
    c.run('clone-upstream', ['/usr/bin/git', 'clone', '--shared', '--no-checkout',
        c.UPSTREAM_REPOSITORY, c.UPSTREAM])
    c.run('checkout-upstream', ['/usr/bin/git', '-C', c.UPSTREAM, 'checkout', '--detach',
        c.plan['upstream']['commit']])
    patch = c.SOURCE / c.plan['patch']['path']
    c.run('patch-clean-apply-check', ['/usr/bin/git', '-C', c.UPSTREAM, 'apply', '--check', patch])
    c.run('patch-apply', ['/usr/bin/git', '-C', c.UPSTREAM, 'apply', '--index', patch])
    c.run('packaging-preflight', [c.PYTHON, c.INPUTS / 'packaging-preflight.py',
        '--source', c.SOURCE, '--upstream-source', c.UPSTREAM, '--target', c.TARGET,
        '--license-cache', c.profile['native_build']['license_cache'],
        '--output-dir', c.BASE / 'evidence/packaging-preflight'], cwd=c.SOURCE, timeout=300)
    c.ENV['INSTA_WORKSPACE_ROOT'] = str(c.UPSTREAM / 'codex-rs')
    build = [c.CARGO, 'build', '--locked', '--offline', '--release', '--target', c.TARGET,
        '--jobs', str(c.JOBS), '--timings', '--message-format=json']
    bwrap_artifacts = compiler_artifacts(c.run('bwrap-build', build + ['-p', 'codex-bwrap', '--bin', 'bwrap'],
        cwd=c.UPSTREAM / 'codex-rs', timeout=1800))
    c.install_native('bwrap')
    c.ENV['CODEX_BWRAP_SHA256'] = c.digest(c.NATIVE / 'bwrap')
    code = ('import json;from pathlib import Path;from codex_package.targets import TARGET_SPECS;'
        'from codex_package.v8 import resolve_codex_v8_cargo_env;'
        'print(json.dumps(resolve_codex_v8_cargo_env(TARGET_SPECS[' + repr(c.TARGET) +
        '],cache_root=Path(' + repr(str(c.V8_CACHE)) + '))))')
    old = c.ENV.get('PYTHONPATH')
    c.ENV['PYTHONPATH'] = str(c.UPSTREAM / 'scripts')
    try:
        v8 = json.loads(c.run('v8-resolve', [c.PYTHON, '-c', code]))
    finally:
        if old is None:
            c.ENV.pop('PYTHONPATH', None)
        else:
            c.ENV['PYTHONPATH'] = old
    assert set(v8) >= {'RUSTY_V8_ARCHIVE', 'RUSTY_V8_SRC_BINDING_PATH'}
    assert all(Path(path).resolve().is_relative_to(c.V8_CACHE.resolve()) for path in v8.values())
    c.ENV.update(v8)
    c.REPORT['v8'] = {name: dict(path=path, sha256=c.digest(path)) for name, path in v8.items()}
    frontend_artifacts = compiler_artifacts(c.run('frontend-build', build + ['-p', 'codex-cli', '-p', 'codex-code-mode-host', '--bin', 'codex',
        '--bin', 'codex-code-mode-host'], cwd=c.UPSTREAM / 'codex-rs', timeout=5400))
    c.cargo_timing('cargo-frontend-build')
    for name in ('codex', 'codex-code-mode-host'):
        c.install_native(name)
    c.REPORT['native_binary_sha256'] = {name: c.digest(c.NATIVE / name) for name in ('codex', 'codex-code-mode-host', 'bwrap')}
    c.REPORT['cargo_artifact_counts'] = {name: {'fresh': sum(row['fresh'] for row in artifacts),
        'compiled': sum(not row['fresh'] for row in artifacts)}
        for name, artifacts in (('bwrap-build', bwrap_artifacts), ('frontend-build', frontend_artifacts))}


def harness(c):
    output = c.run('tui-harness-build', [c.CARGO, 'test', '--locked', '--offline',
        '--profile', 'dev-small', '--target', c.TARGET, '--jobs', str(c.JOBS), '--timings', '-p', 'codex-tui',
        '--lib', '--no-run', '--message-format=json'], cwd=c.UPSTREAM / 'codex-rs', timeout=5400)
    artifacts = [row for row in compiler_artifacts(output) if row.get('profile', {}).get('test') and
                 row.get('executable') and row['target']['name'] == 'codex_tui']
    assert len(artifacts) == 1, artifacts
    artifact = artifacts[0]
    path = Path(artifact['executable'])
    assert path.is_relative_to(c.CARGO_TARGET / c.TARGET / 'dev-small')
    saved = c.BASE / 'test-binaries/codex-tui'
    saved.parent.mkdir(mode=0o700)
    shutil.copy2(path, saved)
    shutil.copy2(c.NATIVE / 'codex-plus-runtime', saved.with_name('codex-plus-runtime'))
    c.record_harness(artifact, saved)
    c.REPORT['tui_harness'] = dict(cargo_artifact=artifact, retained=str(saved), sha256=c.digest(saved),
        source_commit=c.REPORT['source_commit'], frontend_patch_sha256=c.REPORT['frontend_patch_sha256'],
        profile='dev-small', target=c.TARGET, locked=True)
    (c.BASE / 'evidence/tui-harness.json').write_text(json.dumps(c.REPORT['tui_harness'], indent=2) + '\n')
    c.cargo_timing('cargo-tui-harness-build')
    c.REPORT['native_filters'] = {}
    for name in filters(c.plan):
        label = name.replace(':', '_')
        c.run('tui-list-' + label, [saved, name, '--list'], cwd=c.UPSTREAM / 'codex-rs')
        logfile = c.BASE / 'logs' / ('tui-' + label + '.libtest')
        c.run('tui-' + label, [saved, name, '--test-threads=' + str(c.TEST_THREADS), '--color', 'never', '--logfile', logfile],
            cwd=c.UPSTREAM / 'codex-rs', timeout=600)
        result = c.run('validate-tui-' + label, [c.PYTHON, c.SOURCE / 'scripts/native-results.py',
            '--name', name, '--list', c.BASE / 'logs' / ('tui-list-' + label + '.log'), '--logfile', logfile,
            '--stdout', c.BASE / 'logs' / ('tui-' + label + '.log'), '--exit-code',
            str(c.REPORT['checks']['tui-' + label]['exit_code'])])
        c.REPORT['native_filters'][name] = json.loads(result)
        assert c.REPORT['native_filters'][name]['passed'] > 0
    c.REPORT['focused_native_passed'] = True
    unique = {name for row in c.REPORT['native_filters'].values() for name in row['listed_names']}
    assert unique, 'Affected native inventory is empty'
    c.REPORT['unique_native_tests'] = len(unique)


def terminals(c):
    tests = [('native-smoke', 'native_smoke.py', [c.PACKAGE / 'bin/codex-plus']),
        ('morandi', 'test_morandi_tui.py', ['--binary', c.PACKAGE / 'bin/codex', '--expect', 'present']),
        ('command-display', 'test_command_display_tui.py', [c.PACKAGE / 'bin/codex', '--launcher', c.PACKAGE / 'bin/codex-plus']),
        ('command-display-morandi', 'test_command_display_tui.py', [c.PACKAGE / 'bin/codex', '--launcher', c.PACKAGE / 'bin/codex-plus', '--theme', 'morandi']),
        ('shell-policy', 'test_shell_policy_integration.py', [c.PACKAGE / 'bin/codex', '--stock-daemon', c.OFFICIAL, '--stock-launcher', c.PACKAGE / 'bin/codex-plus']),
        ('shell-topics', 'test_shell_topics_tui.py', ['--binary', c.PACKAGE / 'bin/codex']),
        ('detached-hud', 'test_detached_execution_hud_tui.py', ['--binary', c.PACKAGE / 'bin/codex']),
        ('worker-roles', 'test_worker_roles_tui.py', [c.PACKAGE / 'bin/codex']),
        ('footer-rows', 'test_footer_rows_tui.py', ['--source', c.SOURCE, '--binary', c.PACKAGE / 'bin/codex'])]
    for label, program, args in tests:
        if label in ('command-display', 'command-display-morandi'):
            command = [c.PYTHON, c.INPUTS / 'command-checks.py', '--source', c.SOURCE,
                '--binary', c.PACKAGE / 'bin/codex', '--launcher', c.PACKAGE / 'bin/codex-plus',
                '--short-root', c.SHORT.name, '--evidence', c.BASE / 'evidence' / label]
            if label.endswith('-morandi'):
                command += ['--theme', 'morandi']
        elif label in ('native-smoke', 'shell-policy', 'shell-topics', 'detached-hud', 'worker-roles'):
            code = ('import sys,tempfile;from pathlib import Path;sys.path.insert(0,' + repr(str(c.SOURCE / 'tests')) + ');'
                'import native_smoke;import test_background_integration as background;'
                'native_smoke.REPO=background.REPO=Path(' + repr(str(c.PACKAGE)) + ');'
                'import os;assert os.environ["CODEX_PLUS_TEST_RUNTIME"]==str(native_smoke.REPO/"bin/codex-plus-runtime");'
                'original=tempfile.TemporaryDirectory;tempfile.TemporaryDirectory=lambda *a,**kw: '
                'original(prefix="",dir=' + repr(c.SHORT.name) + ') if not a and "dir" not in kw else original(*a,**kw);')
            if label == 'native-smoke':
                code += ('native_smoke.run(Path(' + repr(str(c.PACKAGE / 'bin/codex-plus')) + '),Path(' +
                    repr(str(c.BASE / 'evidence' / label)) + '))')
            else:
                module = program.removesuffix('.py')
                code += 'import ' + module + ' as test;test.REPO=native_smoke.REPO;'
                code += ('test.run(Path(' + repr(str(c.PACKAGE / 'bin/codex')) + '),Path(' +
                    repr(str(c.BASE / 'evidence' / label)) + ')')
                if label == 'shell-policy':
                    code += (',stock_daemon=Path(' + repr(str(c.OFFICIAL)) + '),stock_launcher=Path(' +
                        repr(str(c.PACKAGE / 'bin/codex-plus')) + ')')
                code += ')'
            command = [c.PYTHON, '-c', code]
        else:
            command = c.fixture(program, *args, '--evidence', c.BASE / 'evidence' / label)
        c.run('packaged-' + label, command, cwd=c.SOURCE, timeout=900)
    for level in ('3', '2'):
        label = 'command-colors-' + level
        c.run('packaged-' + label, c.fixture('test_morandi_command_colors_tui.py',
            '--binary', c.PACKAGE / 'bin/codex', '--launcher', c.PACKAGE / 'bin/codex-plus',
            '--color-level', level, '--evidence', c.BASE / 'evidence' / label), cwd=c.SOURCE, timeout=180)
        assert json.loads((c.BASE / 'evidence' / label / 'report.json').read_text())['passed']
    for label, args in [('truecolor', ['--colors', 'on', '--color-level', '3']),
                        ('256color', ['--colors', 'on', '--color-level', '2']),
                        ('16color', ['--colors', 'on', '--color-level', '1']),
                        ('colors-off', ['--colors', 'off', '--color-level', '3']),
                        ('force-color-zero', ['--colors', 'on', '--color-level', '0']),
                        ('no-color', ['--colors', 'on', '--no-color'])]:
        for theme, extra in [('default', []), ('morandi', ['--theme', 'morandi'])]:
            c.run('packaged-status-' + theme + '-' + label,
                c.fixture('test_status_lines_tui.py', '--binary', c.PACKAGE / 'bin/codex',
                    '--source', c.SOURCE, '--evidence', c.BASE / 'evidence' / ('status-' + theme + '-' + label),
                    '--case', 'branch', '--compact', *args, *extra), cwd=c.SOURCE, timeout=120)
    markers = json.loads((c.BASE / 'evidence/command-display-morandi/completion-colors.json').read_text())
    status = json.loads((c.BASE / 'evidence/status-morandi-truecolor/branch/colors.json').read_text())
    context = status['items']['Context 75% left']['foreground']
    assert context == 'a8c9ad'
    assert all(row['foreground'] == context for row in markers['samples'] if row['title'] == 'Ran')
    c.REPORT['packaged_success_context_green_equal'] = dict(passed=True, foreground=context)
    c.REPORT['packaged_frontend_matrix_passed'] = True
