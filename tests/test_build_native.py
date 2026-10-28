"""The release build accepts only pinned upstream or its exact frontend patch."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PIN = '360319d8dee62d87e2d59fc0c21805d79519cfed'


class BuildNativeSourceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / '.build/source'
        self.source.mkdir(parents=True)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Build guard test')
        self.git('config', 'user.email', 'build-guard@example.invalid')
        for name in ('tui/hud.rs', 'core/backend.rs'):
            path = self.source / 'codex-rs' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('// upstream\n')
        (self.source / '.gitignore').write_text('target/\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'upstream fixture')
        commit = self.git('rev-parse', 'HEAD').stdout.strip()
        scripts = self.root / 'scripts'
        scripts.mkdir()
        script = (ROOT / 'scripts/build-native.sh').read_text().replace(PIN, commit)
        (scripts / 'build-native.sh').write_text(script)
        helper = ROOT / 'scripts/build-artifacts.py'
        shutil.copyfile(helper, scripts / helper.name)
        runtime = scripts / 'build-runtime.sh'
        runtime.write_text('#!/bin/sh\nexit 0\n')
        runtime.chmod(0o755)
        package = self.source / 'scripts/codex_package'
        package.mkdir(parents=True)
        (package / '__init__.py').touch()
        (package / 'targets.py').write_text('TARGET_SPECS = {"x86_64-unknown-linux-gnu": "native"}\n')
        (package / 'v8.py').write_text(
            'def resolve_codex_v8_cargo_env(spec):\n'
            '    assert spec == "native"\n'
            '    return {"RUSTY_V8_ARCHIVE": "archive with spaces", '
            '"RUSTY_V8_SRC_BINDING_PATH": "matching bindings"}\n')
        self.git('add', 'scripts')
        self.git('commit', '-qm', 'pinned packaging fixture')
        commit = self.git('rev-parse', 'HEAD').stdout.strip()
        (scripts / 'build-native.sh').write_text(script.replace(
            self.git('rev-parse', 'HEAD~').stdout.strip(), commit))
        (self.root / 'patches').mkdir()
        self.patch = self.root / 'patches/codex-0.165.0-worker-hud.patch'
        (self.source / 'codex-rs/tui/hud.rs').write_text('// frontend HUD\n')
        (self.source / 'codex-rs/tui/new_hud.rs').write_text('// new HUD module\n')
        self.git('add', '--intent-to-add', 'codex-rs/tui/new_hud.rs')
        self.patch.write_text(self.git('diff', '--binary', 'HEAD').stdout)
        self.git('reset', '--hard', 'HEAD')
        tools = self.root / 'tools'
        tools.mkdir()
        self.cargo_called = self.root / 'cargo-called'
        cargo = tools / 'cargo'
        cargo.write_text('#!/bin/sh\nprintf called > "$CARGO_CALLED"\nexit 73\n')
        cargo.chmod(0o755)
        rustc = tools / 'rustc'
        rustc.write_text('#!/bin/sh\nprintf "host: x86_64-unknown-linux-gnu\\n"\n')
        rustc.chmod(0o755)
        self.env = os.environ | {
            'PATH': str(tools) + os.pathsep + os.environ['PATH'],
            'CARGO_CALLED': str(self.cargo_called),
        }
        self.env.pop('CARGO_TARGET_DIR', None)
        self.env.pop('CARGO_BUILD_TARGET', None)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.source), *args], check=True,
                              text=True, capture_output=True)

    def build(self):
        return subprocess.run(['bash', str(self.root / 'scripts/build-native.sh')],
                              env=self.env, text=True, capture_output=True)

    def apply_frontend(self):
        self.git('apply', str(self.patch))

    def assert_rejected(self):
        result = self.build()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Source differs', result.stderr)
        self.assertFalse(self.cargo_called.exists())
        self.assertFalse((self.root / '.build/codex').exists())

    def test_pristine_and_exact_repeat_reach_cargo(self):
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.build()
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertTrue(self.cargo_called.exists())
                self.assertEqual((self.source / 'codex-rs/tui/new_hud.rs').read_text(),
                                 '// new HUD module\n')
                self.assertEqual((self.source / 'codex-rs/core/backend.rs').read_text(),
                                 '// upstream\n')

    def test_combined_backend_rejected(self):
        self.apply_frontend()
        (self.source / 'codex-rs/core/backend.rs').write_text('// daemon receipts\n')
        self.assert_rejected()

    def test_wrong_upstream_revision_rejected_before_patch_or_build(self):
        self.git('commit', '--allow-empty', '-qm', 'different upstream')
        result = self.build()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Unexpected source revision', result.stderr)
        self.assertFalse(self.cargo_called.exists())
        self.assertEqual((self.source / 'codex-rs/tui/hud.rs').read_text(), '// upstream\n')

    def test_unrelated_tracked_edit_rejected(self):
        (self.source / 'codex-rs/core/backend.rs').write_text('// unrelated edit\n')
        self.assert_rejected()

    def test_unrelated_staged_edit_rejected(self):
        self.apply_frontend()
        (self.source / 'codex-rs/core/backend.rs').write_text('// staged edit\n')
        self.git('add', 'codex-rs/core/backend.rs')
        self.assert_rejected()

    def test_untracked_source_rejected(self):
        self.apply_frontend()
        injected = self.source / 'codex-rs/core/injected.rs'
        injected.write_text('// unexpected source\n')
        self.assert_rejected()
        self.assertTrue(injected.exists())

    def test_changed_frontend_file_rejected(self):
        self.apply_frontend()
        (self.source / 'codex-rs/tui/new_hud.rs').write_text('// stale HUD\n')
        self.assert_rejected()

    def test_ignored_build_artifacts_allowed(self):
        self.apply_frontend()
        target = self.source / 'codex-rs/target'
        target.mkdir()
        (target / 'cache').write_text('build artifact\n')
        result = self.build()
        self.assertEqual(result.returncode, 73, result.stderr)

    def artifact_cargo(self):
        cargo = self.root / 'tools/cargo'
        cargo.write_text('#!' + sys.executable + '\n' + '''import json, os, pathlib, shutil, sys
args = sys.argv[1:]
pathlib.Path(os.environ["CARGO_CALLED"]).write_text(json.dumps(args))
assert os.environ["RUSTY_V8_ARCHIVE"] == "archive with spaces"
assert os.environ["RUSTY_V8_SRC_BINDING_PATH"] == "matching bindings"
target = pathlib.Path(os.environ["CARGO_TARGET_DIR"])
target.mkdir(parents=True, exist_ok=True)
for name in ("codex", "codex-code-mode-host"):
    executable = target / name
    shutil.copyfile("/bin/false", executable)
    executable.chmod(0o755)
    if os.environ.get("OMIT_HOST") and name.endswith("host"): continue
    message = {"reason": "compiler-artifact", "target": {"name": name, "kind": ["bin"]},
               "executable": str(executable), "fresh": target.exists()}
    print(json.dumps(message))
    if os.environ.get("AMBIGUOUS"):
        other = target / (name + "-other")
        shutil.copyfile(executable, other)
        other.chmod(0o755)
        message["executable"] = str(other)
        print(json.dumps(message))
sys.exit(73 if os.environ.get("CARGO_FAIL") else 0)
''')
        cargo.chmod(0o755)

    def test_installs_reported_cli_and_matching_host_with_custom_target_paths(self):
        self.artifact_cargo()
        stale = self.source / 'codex-rs/target/release/codex'
        stale.parent.mkdir(parents=True)
        shutil.copyfile('/bin/true', stale)
        for path in (str(self.root / 'absolute target with spaces'), '../../relative target with spaces'):
            self.env['CARGO_TARGET_DIR'] = path
            self.env['CARGO_BUILD_TARGET'] = 'configured-cross-target'
            for attempt in range(2):
                with self.subTest(path=path, attempt=attempt):
                    result = self.build()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    for name in ('codex', 'codex-code-mode-host'):
                        self.assertEqual((self.root / '.build' / name).read_bytes(), Path('/bin/false').read_bytes())
                    args = json.loads(self.cargo_called.read_text())
                    self.assertIn('--message-format=json-render-diagnostics', args)
                    self.assertEqual(args[args.index('--target') + 1], 'x86_64-unknown-linux-gnu')

    def test_failed_or_incomplete_build_preserves_previous_pair(self):
        self.artifact_cargo()
        self.env['CARGO_TARGET_DIR'] = str(self.root / 'artifacts')
        for name in ('codex', 'codex-code-mode-host'):
            (self.root / '.build' / name).write_bytes(b'old installed artifact')
        for failure in ('CARGO_FAIL', 'OMIT_HOST', 'AMBIGUOUS'):
            with self.subTest(failure=failure):
                self.env[failure] = '1'
                self.assertNotEqual(self.build().returncode, 0)
                for name in ('codex', 'codex-code-mode-host'):
                    self.assertEqual((self.root / '.build' / name).read_bytes(), b'old installed artifact')
                self.env.pop(failure)


class BuildRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'checkout with spaces'
        scripts = self.root / 'scripts'
        scripts.mkdir(parents=True)
        shutil.copyfile(ROOT / 'scripts/build-runtime.sh', scripts / 'build-runtime.sh')
        helper = ROOT / 'scripts/build-artifacts.py'
        shutil.copyfile(helper, scripts / helper.name)
        tools = self.root / 'tools'
        tools.mkdir()
        cargo = tools / 'cargo'
        cargo.write_text('#!' + sys.executable + '\nimport json,os,pathlib,sys\n'
            'pathlib.Path(os.environ["CARGO_ARGS"]).write_text(json.dumps(sys.argv[1:]))\n'
            'if os.environ.get("CARGO_FAIL"): sys.exit(73)\n'
            'target=pathlib.Path(sys.argv[sys.argv.index("--target-dir")+1])/"release"\n'
            'target=target/os.environ.get("CARGO_BUILD_TARGET", "")\n'
            'target.mkdir(parents=True,exist_ok=True)\n'
            'artifact=target/"codex-plus-runtime"\n'
            'artifact.write_bytes(b"runtime artifact")\n'
            'artifact.chmod(0o755)\n'
            'print(json.dumps({"reason":"compiler-artifact", "target":'
            '{"name":"codex-plus-runtime","kind":["bin"]},"executable":str(artifact)}))\n')
        cargo.chmod(0o755)
        self.arguments = self.root / 'cargo-args.json'
        self.env = os.environ | {'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                                 'CARGO_ARGS': str(self.arguments)}
        self.env.pop('CARGO_TARGET_DIR', None)
        self.env.pop('CARGO_BUILD_TARGET', None)

    def build(self):
        return subprocess.run(['bash', str(self.root / 'scripts/build-runtime.sh')],
                              env=self.env, text=True, capture_output=True, cwd='/tmp')

    def test_helper_only_build_uses_locked_pinned_toolchain_and_atomic_output(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.arguments.read_text()), [
            '+1.99.1', 'build', '--locked', '--release', '--manifest-path',
            str(self.root / 'rust-runtime/Cargo.toml'), '--target-dir',
            str(self.root / '.build/runtime-target'), '--message-format=json-render-diagnostics'])
        runtime = self.root / '.build/codex-plus-runtime'
        self.assertEqual(runtime.read_bytes(), b'runtime artifact')
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.root / '.build/codex').exists())
        self.env['CARGO_FAIL'] = '1'
        self.assertEqual(self.build().returncode, 73)
        self.assertEqual(runtime.read_bytes(), b'runtime artifact')
        self.assertFalse(runtime.with_suffix('.new').exists())

    def test_selects_reported_target_specific_artifact_on_warm_builds(self):
        stale = self.root / '.build/runtime-target/release/codex-plus-runtime'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'stale default artifact')
        self.env['CARGO_BUILD_TARGET'] = 'configured-target with spaces'
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.build()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.root / '.build/codex-plus-runtime').read_bytes(), b'runtime artifact')


if __name__ == '__main__':
    unittest.main()
