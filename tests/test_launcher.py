"""Installed and symlinked launchers preserve the native process contract."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstalledLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'release with spaces'
        self.bin = self.root / 'bin'
        self.bin.mkdir(parents=True)
        self.launcher = self.bin / 'codex-plus'
        shutil.copyfile(ROOT / 'bin/codex-plus', self.launcher)
        self.launcher.chmod(0o755)
        self.link = Path(self.temp.name) / 'links' / 'codex-plus'
        self.link.parent.mkdir()
        self.link.symlink_to(self.launcher)
        self.env = os.environ | {'CODEX_HOME': str(Path(self.temp.name) / 'home'),
                                 'LAUNCHER_CHECK': 'retained'}
        self.env.pop('PYTHONDONTWRITEBYTECODE', None)
        self.env.pop('PYTHONPYCACHEPREFIX', None)
        native = self.bin / 'codex'
        native.write_text('#!' + sys.executable + '\n'
                          'import json,os,signal,sys\n'
                          'if sys.argv[1:]==["--version"]: print("codex-cli 0.162.1"); sys.exit()\n'
                          'if sys.argv[1:]==["signal"]: os.kill(os.getpid(),signal.SIGTERM)\n'
                          'print(json.dumps({"argv":sys.argv[1:],"stdin":sys.stdin.read(),'
                          '"env":os.environ["LAUNCHER_CHECK"],"path":os.environ["PATH"]}))\n'
                          'sys.exit(23)\n')
        native.chmod(0o755)

    def launch(self, *args, input='input', launcher=None):
        return subprocess.run([str(launcher or self.link), *args], input=input,
                              capture_output=True, text=True, env=self.env, cwd='/tmp')

    def stage_runtime(self):
        runtime = Path(os.environ.get('CODEX_PLUS_TEST_RUNTIME',
                                      ROOT / '.build/codex-plus-runtime'))
        self.assertTrue(runtime.is_absolute(), 'CODEX_PLUS_TEST_RUNTIME must be absolute')
        if not runtime.is_file():
            self.skipTest('Build the native runtime with scripts/build-runtime.sh')
        shutil.copyfile(runtime, self.bin / 'codex-plus-runtime')
        (self.bin / 'codex-plus-runtime').chmod(0o755)

    def test_installed_symlink_forwards_arguments_stdin_environment_and_exit(self):
        result = self.launch('resume', 'a b', '--config', 'value="literal $HOME"')
        self.assertEqual(result.returncode, 23, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['argv'], ['resume', 'a b', '--config', 'value="literal $HOME"'])
        self.assertEqual(actual['stdin'], 'input')
        self.assertEqual(actual['env'], 'retained')
        self.assertEqual(actual['path'], self.env['PATH'])

    def test_packaged_plus_version_and_native_stock_version(self):
        self.stage_runtime()
        (self.root / 'codex-plus.json').write_text(json.dumps({
            'release': '0.162.1-plus.1', 'plus_commit': 'a' * 40,
            'upstream': {'version': '0.162.1'}}))
        result = self.launch('--version')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'codex-plus 0.162.1-plus.1 (Plus aaaaaaaaaaaa; upstream 0.162.1)\n')
        self.assertEqual(self.launch('--version', launcher=self.bin / 'codex').stdout,
                         'codex-cli 0.162.1\n')

    def test_development_fallback(self):
        build = self.root / '.build'
        build.mkdir()
        (self.bin / 'codex').rename(build / 'codex')
        self.assertEqual(self.launch('--version').stdout, 'codex-cli 0.162.1\n')

    def test_version_with_additional_arguments_retains_native_routing(self):
        (self.root / 'codex-plus.json').write_text('{}')
        result = self.launch('--version', '--literal')
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout)['argv'], ['--version', '--literal'])

    def test_native_signal_exit_is_preserved(self):
        self.assertEqual(self.launch('signal').returncode, -signal.SIGTERM)

    def test_runner_preserves_fleet_codex_wrapper(self):
        self.stage_runtime()
        self.env['HOME'] = str(Path(self.temp.name) / 'user')
        wrapper = Path(self.env['HOME']) / '.local/bin/codex'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('#!/bin/sh\nprintf "fleet wrapper\\n"\nexit 19\n')
        wrapper.chmod(0o755)
        self.env['PATH'] = str(wrapper.parent) + os.pathsep + self.env['PATH']
        resolved = self.launch('run', '--', sys.executable, '-c',
                               'import shutil; print(shutil.which("codex"))')
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertIn(str(wrapper) + '\n', resolved.stdout)
        result = self.launch('run', '--', 'codex', 'app-server', 'daemon',
                             'update', '--from-cli', '--yes')
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertIn('fleet wrapper\n', result.stdout)
        self.assertNotIn('codex-cli', result.stdout)

    def test_installed_helpers_through_symlink(self):
        self.stage_runtime()
        inventory = sorted(path.relative_to(self.root) for path in self.root.rglob('*'))
        result = self.launch('run', '--', sys.executable, '-c', 'print("helper works")')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('helper works\n', result.stdout)
        self.assertIn('codex-plus result: exit=0', result.stdout)
        result = self.launch('park', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--status', result.stdout)
        result = self.launch('advisor', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--brief', result.stdout)
        result = self.launch('goal', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('checkpoint', result.stdout)
        result = self.launch('ultracode', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('status', result.stdout)
        self.env.pop('CODEX_THREAD_ID', None)
        result = self.launch('advisor', '--brief', 'Installed advisor routing check.')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('root_required', result.stderr)
        parent = '00000000-0000-0000-0000-000000000001'
        result = self.launch('park', '--status', '--parent', parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.launch('supervise', '--parent', parent)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(path.relative_to(self.root) for path in self.root.rglob('*')),
                         inventory, 'Helpers and detached observers changed the installed package')

    def test_helpers_ignore_cwd_package_without_changing_child_imports(self):
        self.stage_runtime()
        cwd = Path(self.temp.name) / 'working directory'
        decoy = cwd / 'codex_plus'
        decoy.mkdir(parents=True)
        (decoy / '__init__.py').write_text('raise RuntimeError("decoy package imported")\n')
        (cwd / 'cwd_module.py').write_text('VALUE = "working directory import retained"\n')
        self.env.pop('PYTHONSAFEPATH', None)
        for helper, code in (('advisor', 0), ('park', 0), ('goal', 0), ('ultracode', 0), ('run', 2)):
            with self.subTest(helper=helper):
                result = subprocess.run([str(self.link), helper, '--help'], cwd=cwd,
                                        env=self.env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotIn('decoy package imported', result.stderr)
                self.assertIn('usage:', (result.stdout + result.stderr).lower())
        result = subprocess.run([str(self.link), 'run', '--', sys.executable, '-c',
                                 'import cwd_module; print(cwd_module.VALUE)'], cwd=cwd,
                                env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('working directory import retained', result.stdout)

    def test_missing_runtime_prints_explicit_build_instruction(self):
        result = self.launch('run', '--', '/bin/true')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(str(self.root / 'scripts/build-runtime.sh'), result.stderr)
        self.assertFalse((self.root / '.build').exists())

    def test_native_runtime_development_fallback(self):
        self.stage_runtime()
        build = self.root / '.build'
        build.mkdir()
        (self.bin / 'codex-plus-runtime').rename(build / 'codex-plus-runtime')
        result = self.launch('run', '--', '/bin/echo', 'runtime fallback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('runtime fallback\n', result.stdout)

    def test_helpers_preserve_python_environment_for_child(self):
        self.stage_runtime()
        expected = {'PYTHONPATH': '/caller/import path', 'PYTHONDONTWRITEBYTECODE': '0',
                    'PYTHONSAFEPATH': 'caller setting'}
        self.env.update(expected)
        result = self.launch('run', '--', sys.executable, '-c',
                             'import json,os; print(json.dumps({key: os.environ[key] '
                             'for key in ("PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONSAFEPATH")}))')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(json.dumps(expected) + '\n', result.stdout)


if __name__ == '__main__':
    unittest.main()
