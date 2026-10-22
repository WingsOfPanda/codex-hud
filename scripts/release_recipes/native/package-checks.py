"""Run the existing seven packaged checks; only fresh identical uid-map limits can qualify resources."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def qualify(report, comparison, exit_code):
    expected = {'manifest', 'public_commands', 'wrapper', 'resources', 'exec', 'stdio_code_mode', 'manifest_after_runtime'}
    assert set(report['checks']) == expected
    assert report['official_cli_unchanged'] and report['remote_model_requests'] == 0
    assert report['skipped'] == ['terminal suites', 'official daemon helper park']
    failed = [name for name, row in report['checks'].items() if not row['passed']]
    assert failed in ([], ['resources']), failed
    assert exit_code == (1 if failed else 0)
    assert comparison['passed'] and comparison['official']['passed'] and comparison['plus']['passed']
    if failed:
        assert comparison['raw_bwrap_same_uid_map_failure']
        assert 'bwrap: setting up uid map: Permission denied' in report['checks']['resources']['error']
        for key in ('returncode', 'stdout', 'stderr'):
            assert comparison['raw_bwrap']['official'][key] == comparison['raw_bwrap']['plus'][key]
    return dict(passed=True, raw_package_exit_code=exit_code, raw_failed_checks=failed,
        qualified_checks=7, host_limitation=('Fresh identical official/Plus uid-map denial; both CLI read-only sandboxes passed' if failed else None),
        raw_report_unchanged=True, terminal_suites='Fresh packaged frontend matrix is a separate required controller gate; compatibility and AP replay remain root-owned')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'package', 'official', 'short-root', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source / 'tests'))
    import test_release_package as package_tests
    original = tempfile.TemporaryDirectory
    # Borrow the controller-owned short root; only the controller verifies and deletes it.
    tempfile.TemporaryDirectory = lambda *a, **kw: contextlib.nullcontext(str(args.short_root)) if (
        not a and kw.get('prefix') == 'codex-plus-package-') else original(*a, **kw)
    sys.argv = [str(args.source / 'tests/test_release_package.py'), str(args.package),
        '--evidence', str(args.evidence), '--official-cli', str(args.official), '--skip-terminal']
    try:
        code = package_tests.main()
    finally:
        tempfile.TemporaryDirectory = original
    report = json.loads((args.evidence / 'report.json').read_text())
    area = args.evidence / 'sandbox'
    area.mkdir(mode=0o700)
    subprocess.run([sys.executable, str(Path(__file__).with_name('check-sandbox.py')),
        '--area', str(area), '--official', str(args.official), '--package', str(args.package)],
        stdin=subprocess.DEVNULL, check=True, timeout=120)
    comparison = json.loads((area / 'sandbox-comparison.json').read_text())
    result = qualify(report, comparison, code)
    if result['raw_failed_checks']:
        assert (args.evidence / 'resource-dependencies.json').is_file()
    (args.evidence / 'qualification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
