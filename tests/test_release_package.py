"""Native GNU release qualification, private homes and finite loopback providers.

Run: python tests/test_release_package.py PACKAGE --evidence DIRECTORY
     [--official-cli OFFICIAL_BINARY] [--skip-terminal]
The terminal gates require pyte==0.8.2; all other checks use the standard library.
"""

import argparse
import hashlib
import json
import mmap
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback

from fixtures.release_package_provider import PackageProvider


sys.dont_write_bytecode = True


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def command(args, cwd, env, timeout=45, expected=0):
    result = subprocess.run([str(arg) for arg in args], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == expected, f'{args}: exit={result.returncode}\n{result.stdout}\n{result.stderr}'
    return result.stdout


def private_env(root, provider=None, code_mode=False):
    home, workspace = root / 'home', root / 'workspace'
    home.mkdir(mode=0o700, parents=True)
    workspace.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
    for key in ('BASH_ENV', 'ENV', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'CODEX_HOME', 'CODEX_CONFIG_OVERRIDES'):
        env.pop(key, None)
    env.update(CODEX_HOME=str(home), ZDOTDIR=str(root), NO_PROXY='127.0.0.1,localhost',
               no_proxy='127.0.0.1,localhost', RUST_LOG='warn', PYTHONDONTWRITEBYTECODE='1')
    config = '''check_for_update_on_startup=false
suppress_unstable_features_warning=true
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
features.memories=false
features.apps=false
'''
    if code_mode:
        config += 'features.code_mode_only=true\nfeatures.code_mode_host=true\n'
    if provider:
        config += f'''model="gpt-6.1-sol"
model_provider="package_fixture"
[model_providers.package_fixture]
name="loopback release qualification"
base_url="{provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
'''
    (home / 'config.toml').write_text(config)
    return env, workspace


def manifest_check(package):
    manifest = json.loads((package / 'codex-plus.json').read_text())
    upstream = json.loads((package / 'codex-package.json').read_text())
    assert manifest['schema_version'] == 1 and manifest['voice'] is False, manifest
    assert manifest['upstream'] == {'version': '0.162.1', 'tag': 'rust-v0.162.1',
        'commit': '092d3acd6bec3e3a14bdc7e7a2810ab628ab759d'}, manifest['upstream']
    assert manifest['build']['rust'] == '1.99.0' and manifest['build']['cargo_profile'] == 'release'
    assert manifest['resources'], 'resource provenance missing'
    assert manifest['target'] == f'{platform.machine()}-unknown-linux-gnu', manifest['target']
    assert upstream == {'layoutVersion': 1, 'version': manifest['upstream']['version'],
        'target': manifest['target'], 'variant': 'codex', 'entrypoint': 'bin/codex',
        'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}, upstream
    required = ['bin/codex', 'bin/codex-plus', 'bin/codex-code-mode-host', 'codex-path/rg',
        'codex-resources/bwrap', 'codex-resources/zsh/bin/zsh', 'codex-package.json',
        'codex_plus/runner.py', 'codex_plus/background.py', 'codex_plus/rpc.py',
        'docs/shell-workflow.md', 'docs/background-work.md', 'LICENSE', 'NOTICE',
        'licenses/bubblewrap-COPYING', 'licenses/ripgrep-LICENSE-MIT',
        'licenses/ripgrep-LICENSE-UNLICENSE', 'licenses/zsh-LICENCE',
        'licenses/rusty-v8-LICENSE', 'licenses/v8-LICENSE', 'licenses/v8-LICENSE.v8',
        'licenses/codex-plus-LICENSE', 'licenses/provenance.json',
        'licenses/rusty-v8-release-manifests.sha256', 'README.md', 'GLOSSARY.md',
        'docs/verification.md', 'docs/release-notes.md']
    assert all((package / path).is_file() for path in required), 'missing canonical package file'
    paths = list(package.rglob('*'))
    assert not any(path.is_symlink() for path in paths), 'package has unmanifested symlinks'
    assert not any(any(part.startswith('voice') for part in path.relative_to(package).parts)
                   for path in paths), 'voice subtree shipped'
    actual = {str(path.relative_to(package)): {'sha256': digest(path),
        'mode': f'{stat.S_IMODE(path.stat().st_mode):04o}', 'size': path.stat().st_size}
        for path in paths if path.is_file() and path.relative_to(package).as_posix() != 'codex-plus.json'}
    assert actual == manifest['files'], 'file tree/hash/size/mode differs from manifest'
    tree = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('ascii')).hexdigest()
    assert tree == manifest['tree_sha256'], 'tree digest mismatch'
    for path in required[:6]:
        assert os.access(package / path, os.X_OK), f'{path} is not executable'
    with (package / 'bin/codex').open('rb') as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            assert binary.find(digest(package / 'codex-resources/bwrap').encode('ascii')) >= 0, 'bwrap digest absent from native CLI'
    return {'files': len(actual), 'tree_sha256': tree, 'target': manifest['target'], 'voice': False}


def public_commands(package, root):
    env, cwd = private_env(root)
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    manifest = json.loads((package / 'codex-plus.json').read_text())
    native = command([package / 'bin/codex', '--version'], cwd, env).strip()
    plus = command([package / 'bin/codex-plus', '--version'], cwd, env).strip()
    assert manifest['upstream']['version'] in native, native
    assert manifest['release'] in plus and manifest['plus_commit'][:12] in plus, plus
    assert 'Usage:' in command([package / 'bin/codex-plus', '--help'], cwd, env)
    assert 'codex' in command([package / 'bin/codex', 'completion', 'bash'], cwd, env)
    link = cwd / 'symlinked-codex-plus'
    link.symlink_to(package / 'bin/codex-plus')
    assert command([link, '--version'], cwd, env).strip() == plus
    assert 'Usage:' in command([link, '--help'], cwd, env)
    runner = command([link, 'run', '--', sys.executable, '-c', 'print("PACKAGE_RUN")'], cwd, env)
    assert 'PACKAGE_RUN' in runner and 'exit=0' in runner, runner
    logs = list((Path(env['CODEX_HOME']) / 'codex-plus/logs').glob('*'))
    assert len(logs) == 1 and 'PACKAGE_RUN' in logs[0].read_text(), logs
    command([link, 'park', '--status', '--parent', '00000000-0000-4000-8000-000000000001'], cwd, env)
    return {'native_version': native, 'plus_version': plus, 'outside_cwd_symlink': True,
            'helper_log': logs[0].read_text()}


def wrapper_check(package, root):
    copy = root / 'package'
    shutil.copytree(package, copy, copy_function=os.link)
    native = copy / 'bin/codex'
    native.unlink()
    native.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
        'print(json.dumps({"argv":sys.argv,"cwd":os.getcwd()}))\nsys.exit(17)\n')
    native.chmod(0o755)
    env, cwd = private_env(root / 'isolated')
    link = cwd / 'launcher'
    link.symlink_to(copy / 'bin/codex-plus')
    args = ['exec', 'argument with spaces', '', '--literal=$HOME']
    output = command([link, *args], cwd, env, expected=17)
    result = json.loads(output)
    assert result == {'argv': [str(native), *args], 'cwd': str(cwd)}, result
    env['HOME'] = str(root / 'user')
    wrapper = Path(env['HOME']) / '.local/bin/codex'
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                       'print(json.dumps(sys.argv))\nsys.exit(19)\n')
    wrapper.chmod(0o755)
    env['PATH'] = str(wrapper.parent) + os.pathsep + env['PATH']
    output = command([link, 'run', '--', sys.executable, '-c',
                      'import shutil; print(shutil.which("codex"))'], cwd, env)
    assert str(wrapper) + '\n' in output, output
    args = ['app-server', 'daemon', 'update', '--from-cli', '--yes']
    output = command([link, 'run', '--', 'codex', *args], cwd, env, expected=19)
    assert json.dumps([str(wrapper), *args]) + '\n' in output, output
    return {'native_path_args_stdout_exit_preserved': True,
            'runner_uses_home_local_bin_codex_wrapper': True,
            'daemon_update_test_used_only_stubs': True}


def resources_check(package, root, evidence):
    env, cwd = private_env(root)
    results = {}
    for relative in ('bin/codex', 'bin/codex-code-mode-host', 'codex-path/rg',
                     'codex-resources/zsh/bin/zsh', 'codex-resources/bwrap'):
        path = package / relative
        with path.open('rb') as stream:
            assert stream.read(4) == b'\x7fELF', relative
        loaded = subprocess.run(['ldd', str(path)], capture_output=True, text=True, timeout=15)
        output = loaded.stdout + loaded.stderr
        assert 'not found' not in output, f'{relative}: {output}'
        assert loaded.returncode == 0 or any(text in output for text in
            ('not a dynamic executable', 'statically linked')), output
        results[relative] = {'sha256': digest(path), 'ldd': output}
    assert 'ripgrep' in command([package / 'codex-path/rg', '--version'], cwd, env)
    assert command([package / 'codex-resources/zsh/bin/zsh', '-f', '-c',
                    'print -r -- PACKAGE_ZSH'], cwd, env).strip() == 'PACKAGE_ZSH'
    (evidence / 'resource-dependencies.json').write_text(json.dumps(results, indent=2) + '\n')
    command([package / 'codex-resources/bwrap', '--ro-bind', '/', '/', '--unshare-pid', '--as-pid-1',
             '--proc', '/proc', '--dev', '/dev', '--', '/bin/sh', '-c',
             'test $$ = 1 && printf PACKAGE_BWRAP'], cwd, env)
    return results


def exec_check(package, root, evidence):
    provider = PackageProvider()
    try:
        env, cwd = private_env(root, provider)
        output = command([package / 'bin/codex-plus', 'exec', '--skip-git-repo-check',
                          '--sandbox', 'danger-full-access', 'Finite package acceptance.'], cwd, env)
        assert 'PACKAGE_FINISHED' in output, output
        assert len(provider.requests) == 1, provider.requests
        return {'finite_exec': True, 'loopback_requests': len(provider.requests)}
    finally:
        (evidence / 'exec-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def stdio_check(package, root, evidence):
    copy = root / 'package'
    shutil.copytree(package, copy, copy_function=os.link)
    host = copy / 'bin/codex-code-mode-host'
    real = host.with_name('codex-code-mode-host.real')
    host.rename(real)
    marker = root / 'code-mode-host.json'
    host.write_text('#!/usr/bin/env python3\nimport json,os,sys\n'
        f'open({str(marker)!r},"w").write(json.dumps(sys.argv))\n'
        f'os.execv({str(real)!r},[{str(real)!r},*sys.argv[1:]])\n')
    host.chmod(0o755)
    tool_args = {'cmd': 'command -v rg; rg --version', 'login': False, 'yield_time_ms': 10000}
    provider = PackageProvider('text(JSON.stringify(await tools.exec_command('
                               + json.dumps(tool_args) + ')))')
    messages = []
    process = None
    try:
        env, cwd = private_env(root / 'isolated', provider, code_mode=True)
        launcher = cwd / 'symlinked-codex-plus'
        launcher.symlink_to(copy / 'bin/codex-plus')
        with (evidence / 'stdio.stderr').open('w') as log:
            process = subprocess.Popen([str(launcher), 'app-server'], cwd=cwd,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, start_new_session=True)
            buffer = b''

            def send(value):
                process.stdin.write(json.dumps(value).encode() + b'\n')
                process.stdin.flush()

            def receive_until(predicate, timeout=45):
                nonlocal buffer
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if b'\n' not in buffer:
                        assert select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))[0], 'stdio timeout'
                        data = os.read(process.stdout.fileno(), 65536)
                        assert data, 'app-server closed stdout'
                        buffer += data
                        continue
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    messages.append(message)
                    if 'method' in message and 'id' in message:
                        send({'id': message['id'], 'error': {'code': -32601,
                                                          'message': 'fixture denies server requests'}})
                    if predicate(message):
                        assert 'error' not in message, message
                        return message
                raise AssertionError('stdio response timeout')

            def call(number, method, params):
                send({'id': number, 'method': method, 'params': params})
                return receive_until(lambda message: message.get('id') == number)['result']

            initialized = call(1, 'initialize', {'clientInfo': {'name': 'release_package', 'version': '1'},
                                                'capabilities': {'experimentalApi': True}})
            assert not initialized.get('queueIdempotency'), initialized
            send({'method': 'initialized'})
            parent = call(2, 'thread/start', {'cwd': str(cwd), 'approvalPolicy': 'never',
                                             'sandbox': 'danger-full-access'})['thread']['id']
            call(3, 'turn/start', {'threadId': parent,
                'input': [{'type': 'text', 'text': 'Run package Code Mode.', 'text_elements': []}]})
            completed = receive_until(lambda message: message.get('method') == 'turn/completed')
            assert completed['params']['turn']['status'] == 'completed', completed
            assert marker.exists(), 'packaged code-mode-host was not launched'
            assert json.loads(marker.read_text())[0] == str(host), marker.read_text()
            assert len(provider.requests) == 2, provider.requests
            outputs = [item['output'] for request in provider.requests
                       for item in request['body'].get('input', [])
                       if item.get('type') == 'custom_tool_call_output']
            output = outputs[0]
            if not isinstance(output, str):
                output = next(item['text'] for item in output if item.get('text', '').startswith('{'))
            execution = json.loads(output)
            assert execution['exit_code'] == 0, execution
            assert str(copy / 'codex-path/rg') in execution['output'], execution
            return {'initialize': initialized, 'typed_thread_turn': True,
                    'code_mode_host': json.loads(marker.read_text()), 'execution': execution}
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        (evidence / 'stdio-rpc.json').write_text(json.dumps(messages, indent=2) + '\n')
        (evidence / 'stdio-provider.json').write_text(json.dumps(provider.requests, indent=2) + '\n')
        provider.close()


def native_terminal_check(package, evidence):
    sys.path.insert(0, str(package))
    __import__('codex_plus.rpc')
    import native_smoke
    native_smoke.REPO = package
    native_smoke.run(package / 'bin/codex-plus', evidence / 'native-smoke')
    return json.loads((evidence / 'native-smoke/report.json').read_text())


def shell_terminal_check(package, evidence, official):
    import test_shell_policy_integration as shell
    shell.REPO = package
    previous = {key: os.environ.pop(key, None)
                for key in ('PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX')}
    try:
        shell.run(package / 'bin/codex-plus', evidence / 'shell-policy', stock_daemon=official)
    finally:
        for key, value in previous.items():
            if value is not None:
                os.environ[key] = value
    return json.loads((evidence / 'shell-policy/report.json').read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--official-cli', type=Path)
    parser.add_argument('--skip-terminal', action='store_true')
    args = parser.parse_args()
    package, evidence = args.package.resolve(), args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    official = args.official_cli.resolve() if args.official_cli else None
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    original = digest(official) if official else None
    report = {'package': str(package), 'native_host': platform.machine(), 'checks': {},
              'official_cli': str(official) if official else None, 'remote_model_requests': 0}
    checks = [('manifest', lambda root: manifest_check(package)),
              ('public_commands', lambda root: public_commands(package, root)),
              ('wrapper', lambda root: wrapper_check(package, root)),
              ('resources', lambda root: resources_check(package, root, evidence)),
              ('exec', lambda root: exec_check(package, root, evidence)),
              ('stdio_code_mode', lambda root: stdio_check(package, root, evidence))]
    if not args.skip_terminal:
        checks.extend([('native_terminal', lambda root: native_terminal_check(package, evidence)),
                       ('shell_terminal', lambda root: shell_terminal_check(package, evidence, official))])
    checks.append(('manifest_after_runtime', lambda root: manifest_check(package)))
    with tempfile.TemporaryDirectory(prefix='codex-plus-package-', dir=evidence) as temporary:
        for name, check in checks:
            root = Path(temporary) / name
            root.mkdir()
            try:
                report['checks'][name] = {'passed': True, 'result': check(root)}
            except Exception:
                error = traceback.format_exc()
                (evidence / f'{name}.failure.txt').write_text(error)
                report['checks'][name] = {'passed': False, 'error': error}
            print(f'{name}: {"PASS" if report["checks"][name]["passed"] else "FAIL"}', flush=True)
            (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    report['official_cli_unchanged'] = digest(official) == original if official else None
    report['official_cli_sha256'] = original
    report['skipped'] = (['terminal suites'] if args.skip_terminal else []) + (
        ['official daemon helper park'] if not official or args.skip_terminal else [])
    report['passed'] = all(check['passed'] for check in report['checks'].values()) and report['official_cli_unchanged'] is not False
    (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'report': str(evidence / 'report.json'),
                      'skipped': report['skipped']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
