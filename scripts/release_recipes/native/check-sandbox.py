#!/usr/bin/env python3
"""Qualify only the known raw-bwrap host limit against the identical official probe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--area', required=True, type=Path)
    parser.add_argument('--official', required=True, type=Path)
    parser.add_argument('--package', required=True, type=Path)
    args = parser.parse_args()
    results = {'raw_bwrap': {}}
    env = {key: value for key, value in os.environ.items()
           if not any(secret in key.upper() for secret in ('API_KEY', 'AUTH_TOKEN', 'BASE_URL'))}
    env.pop('CODEX_THREAD_ID', None)
    env.pop('CODEX_CONFIG_OVERRIDES', None)
    try:
        for name, binary in [('official', args.official), ('plus', args.package / 'bin/codex')]:
            with tempfile.TemporaryDirectory(prefix='sandbox-', dir=args.area) as temporary:
                root = Path(temporary)
                home, work = root / 'home', root / 'workspace'
                home.mkdir(mode=0o700)
                work.mkdir()
                env['CODEX_HOME'] = str(home)
                before = digest(binary)
                command = [str(binary), 'sandbox', '--', '/bin/sh', '-c',
                    'printf "PRIVATE_SANDBOX_OK\\n"; if touch denied-file; then exit 17; fi; printf "READ_ONLY_ENFORCED\\n"']
                outcome = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30)
                results[name] = dict(argv=command, returncode=outcome.returncode,
                    stdout=outcome.stdout, stderr=outcome.stderr, sha256=before)
                assert outcome.returncode == 0 and 'READ_ONLY_ENFORCED' in outcome.stdout
                assert not (work / 'denied-file').exists() and digest(binary) == before
                results[name].update(passed=True, binary_unchanged=True)
                helper = binary.parents[1] / 'codex-resources/bwrap'
                command = [str(helper), '--ro-bind', '/', '/', '--unshare-pid', '--as-pid-1',
                    '--proc', '/proc', '--dev', '/dev', '--', '/bin/sh', '-c',
                    'test $$ = 1 && printf PACKAGE_BWRAP']
                outcome = subprocess.run(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30)
                results['raw_bwrap'][name] = dict(argv=command, returncode=outcome.returncode,
                    stdout=outcome.stdout, stderr=outcome.stderr)
        official, plus = (results['raw_bwrap'][key] for key in ('official', 'plus'))
        results['raw_bwrap_same_uid_map_failure'] = (
            official['returncode'] == 1 and official['stdout'] == '' and
            official['stderr'] == 'bwrap: setting up uid map: Permission denied\n' and
            all(official[key] == plus[key] for key in ('returncode', 'stdout', 'stderr')))
        results['host_policy'] = {name: (path.read_text().strip() if path.exists() else None)
            for name in ('apparmor_restrict_unprivileged_userns', 'unprivileged_userns_clone')
            for path in [Path('/proc/sys/kernel') / name]}
        results['passed'] = True
    except BaseException as error:
        results.update(passed=False, error=repr(error))
        raise
    finally:
        (args.area / 'sandbox-comparison.json').write_text(json.dumps(results, indent=2) + '\n')
        print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
