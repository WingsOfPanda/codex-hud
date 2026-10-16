"""The release build accepts only pinned upstream or its exact frontend patch."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PIN = '83086188ae0614bd882818ff1aa23f3194962012'


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
        (self.root / 'patches').mkdir()
        self.patch = self.root / 'patches/codex-0.163.0-worker-hud.patch'
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
        self.env = os.environ | {
            'PATH': str(tools) + os.pathsep + os.environ['PATH'],
            'CARGO_CALLED': str(self.cargo_called),
        }

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


if __name__ == '__main__':
    unittest.main()
