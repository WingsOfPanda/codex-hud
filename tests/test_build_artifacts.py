"""Real Cargo output selection with inherited target directories/configuration."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CargoArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'checkout with spaces'
        scripts = self.root / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('build-runtime.sh', 'build-artifacts.py'):
            shutil.copyfile(ROOT / 'scripts' / name, scripts / name)
        runtime = self.root / 'rust-runtime'
        (runtime / 'src').mkdir(parents=True)
        (runtime / 'Cargo.toml').write_text(
            '[package]\nname = "codex-plus-runtime"\nversion = "0.0.0"\nedition = "2024"\n')
        (runtime / 'Cargo.lock').write_text(
            'version = 4\n[[package]]\nname = "codex-plus-runtime"\nversion = "0.0.0"\n')
        self.source = runtime / 'src/main.rs'
        self.source.write_text('fn main() { println!("fresh runtime artifact"); }\n')
        self.env = os.environ.copy()
        for name in ('CARGO_TARGET_DIR', 'CARGO_BUILD_TARGET'):
            self.env.pop(name, None)
        self.env['CARGO_HOME'] = str(self.root / 'cargo home')
        version = subprocess.check_output(['rustc', '+1.99.1', '-vV'], text=True)
        self.host = next(line[6:] for line in version.splitlines() if line.startswith('host: '))
        config = self.root / '.cargo'
        config.mkdir()
        self.config = config / 'config.toml'

    def build(self):
        return subprocess.run(['bash', str(self.root / 'scripts/build-runtime.sh')],
                              cwd=self.root, env=self.env, text=True, capture_output=True)

    def test_real_config_and_environment_target_artifacts_warm_and_failed(self):
        stale = self.root / '.build/runtime-target/release/codex-plus-runtime'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'stale default output')
        cases = (
            ({'CARGO_TARGET_DIR': str(self.root / 'absolute target dir')}, ''),
            ({'CARGO_TARGET_DIR': 'relative target dir', 'CARGO_BUILD_TARGET': self.host}, ''),
            ({}, f'[build]\ntarget = "{self.host}"\n'),
        )
        installed = self.root / '.build/codex-plus-runtime'
        for additions, config in cases:
            with self.subTest(environment=additions, config=config):
                self.env.pop('CARGO_TARGET_DIR', None)
                self.env.pop('CARGO_BUILD_TARGET', None)
                self.env.update(additions)
                self.config.write_text(config)
                for attempt in range(2):
                    result = self.build()
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(subprocess.check_output([str(installed)], text=True),
                                     'fresh runtime artifact\n')
                    target = Path(additions.get('CARGO_TARGET_DIR', self.root / '.build/runtime-target'))
                    if not target.is_absolute():
                        target = self.root / target
                    if config or 'CARGO_BUILD_TARGET' in additions:
                        target /= self.host
                    self.assertEqual(installed.read_bytes(), (target / 'release/codex-plus-runtime').read_bytes())
                previous = installed.read_bytes()
                self.source.write_text('invalid Rust source\n')
                self.assertNotEqual(self.build().returncode, 0)
                self.assertEqual(installed.read_bytes(), previous)
                self.source.write_text('fn main() { println!("fresh runtime artifact"); }\n')

    def test_native_cli_and_host_use_explicit_native_target_with_real_cargo(self):
        upstream = self.root / 'upstream source'
        package = upstream / 'scripts/codex_package'
        package.mkdir(parents=True)
        (package / '__init__.py').touch()
        (package / 'targets.py').write_text(f'TARGET_SPECS = {{"{self.host}": "native"}}\n')
        (package / 'v8.py').write_text('def resolve_codex_v8_cargo_env(spec):\n'
                                      '    assert spec == "native"\n    return {}\n')
        source = upstream / 'codex-rs'
        source.mkdir()
        (source / 'Cargo.toml').write_text(
            '[workspace]\nmembers = ["cli", "code-mode-host"]\nresolver = "3"\n')
        (source / 'Cargo.lock').write_text(
            'version = 4\n[[package]]\nname = "codex-cli"\nversion = "0.0.0"\n'
            '[[package]]\nname = "codex-code-mode-host"\nversion = "0.0.0"\n')
        for directory, package, name in (('cli', 'codex-cli', 'codex'),
                                         ('code-mode-host', 'codex-code-mode-host', 'codex-code-mode-host')):
            member = source / directory
            (member / 'src').mkdir(parents=True)
            (member / 'Cargo.toml').write_text(
                f'[package]\nname = "{package}"\nversion = "0.0.0"\nedition = "2024"\n'
                f'[[bin]]\nname = "{name}"\npath = "src/main.rs"\n')
            (member / 'src/main.rs').write_text(
                f'fn main() {{ println!("fresh {name}"); }}\n')
        self.config.write_text('[build]\ntarget = "invalid-configured-target"\n')
        self.env['CARGO_BUILD_TARGET'] = 'invalid-environment-target'
        target = self.root / 'native target with spaces'
        self.env['CARGO_TARGET_DIR'] = str(target)
        for attempt in range(2):
            result = subprocess.run([sys.executable, str(self.root / 'scripts/build-artifacts.py'),
                                     'native', str(self.root), str(upstream)],
                                    cwd=self.root, env=self.env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('codex', 'codex-code-mode-host'):
                installed = self.root / '.build' / name
                self.assertEqual(subprocess.check_output([str(installed)], text=True), f'fresh {name}\n')
                self.assertTrue((target / self.host / 'release' / name).is_file())
        previous = {name: (self.root / '.build' / name).read_bytes()
                    for name in ('codex', 'codex-code-mode-host')}
        (source / 'code-mode-host/src/main.rs').write_text('invalid Rust source\n')
        result = subprocess.run([sys.executable, str(self.root / 'scripts/build-artifacts.py'),
                                 'native', str(self.root), str(upstream)],
                                cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        for name, contents in previous.items():
            self.assertEqual((self.root / '.build' / name).read_bytes(), contents)


if __name__ == '__main__':
    unittest.main()
