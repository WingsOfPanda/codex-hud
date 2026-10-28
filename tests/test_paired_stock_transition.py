"""Focused transition proof checks and a private development-only rehearsal CLI."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'scripts'))
import release_compat as compat


class PairedContract(unittest.TestCase):
    def test_only_exact_upstream_pair_and_distinct_complete_stock_pins(self):
        config = dict(recipe_revision=2, upstream=compat.UPSTREAM_165, previous_upstream=compat.UPSTREAM,
            stock_binary_sha256={'x86_64': '1' * 64, 'aarch64': '2' * 64},
            previous_stock_binary_sha256={'x86_64': '3' * 64, 'aarch64': '4' * 64})
        self.assertEqual(compat.upstreams(config), (compat.UPSTREAM, compat.UPSTREAM_165))
        self.assertEqual(compat.gate_names(2), compat.GATES + ('paired-stock-transition',))
        for key, value in (('previous_upstream', compat.UPSTREAM_165),
                ('upstream', dict(compat.UPSTREAM_165, commit='5' * 40)),
                ('previous_stock_binary_sha256', {'x86_64': '3' * 64}),
                ('previous_stock_binary_sha256', config['stock_binary_sha256']), ('recipe_revision', True)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                compat.upstreams(dict(config, **{key: value}))


def diagnostic(args):
    """Uses actual binaries; synthetic context supplies the byte oracle, never acceptance."""
    import shutil
    import subprocess
    args.evidence.mkdir(parents=True, exist_ok=False)
    old_package = args.old_runtime.resolve().parent.parent
    manifest = json.loads((old_package / 'codex-plus.json').read_text())
    assert manifest['release'] == '0.163.0-plus.0.4.9' and manifest['upstream'] == compat.UPSTREAM
    candidate = args.evidence / 'development-fixture'
    (candidate / 'bin').mkdir(parents=True)
    shutil.copy2(args.new_runtime, candidate / 'bin/codex-plus-runtime')
    (candidate / 'codex-plus.json').write_text(json.dumps(dict(
        diagnostic_only=True, release='0.165.0-plus.0.4.9', upstream=compat.UPSTREAM_165)))
    scope = args.evidence / 'scope'
    scope.mkdir()
    with tempfile.TemporaryDirectory(prefix='pt-', dir='/tmp') as temporary:
        short_root = Path(temporary)
        short = short_root / 'aa'
        short.mkdir(mode=0o700)
        (scope / 'unreadable-baseline.json').write_text('{}')
        info = short.stat()
        (scope / 'short-binding.json').write_text(json.dumps(dict(path=str(short),
            device=info.st_dev, inode=info.st_ino, baseline_sha256=compat.digest(scope / 'unreadable-baseline.json'))))
        config = dict(scope=str(scope), short_root=str(short_root),
            recipe_hashes={path.name: compat.digest(path) for path in compat.RECIPE.iterdir() if path.is_file()},
            source_commit=subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
            fixture_source=str(REPO), architecture=os.uname().machine,
            previous_release='0.163.0-plus.0.4.9', release='0.165.0-plus.0.4.9',
            upstream=compat.UPSTREAM_165, previous_upstream=compat.UPSTREAM,
            old_package=str(old_package), new_package=str(candidate),
            previous_identity={'manifests': {os.uname().machine: compat.digest(old_package / 'codex-plus.json')}},
            new_manifest_sha256=compat.digest(candidate / 'codex-plus.json'),
            stock_binary=str(args.new_stock), previous_stock_binary=str(args.old_stock),
            stock_binary_sha256={os.uname().machine: compat.digest(args.new_stock)},
            previous_stock_binary_sha256={os.uname().machine: compat.digest(args.old_stock)}, recipe_revision=2)
        (scope / 'native.config.json').write_text(json.dumps(config))
        env = dict(os.environ, CODEX_PLUS_COMPAT_SCOPE=str(scope), TMPDIR=str(short),
            PYTHONDONTWRITEBYTECODE='1')
        subprocess.run([sys.executable, str(compat.RECIPE / 'paired-stock-transition.py'),
            '--evidence', str(scope / 'evidence')], env=env, check=True, timeout=150)
        paired = json.loads((scope / 'evidence/report.json').read_text())
        report = dict(passed=paired['passed'], diagnostic_only=True, shipping_acceptance=False,
            paired_report=str(scope / 'evidence/report.json'),
            paired_report_sha256=compat.digest(scope / 'evidence/report.json'))
        (args.evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    if '--diagnostic' in sys.argv:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--diagnostic', action='store_true')
        for name in ('old-runtime', 'new-runtime', 'old-stock', 'new-stock', 'evidence'):
            parser.add_argument('--' + name, type=Path, required=True)
        diagnostic(parser.parse_args())
    else:
        unittest.main()
