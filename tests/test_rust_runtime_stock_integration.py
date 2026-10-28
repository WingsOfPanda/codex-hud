"""Run existing stock-daemon acceptance against a staged Rust product runtime.

Only the old Python entry-point launches and package staging are adapted. Existing
daemon/provider assertions stay intact; the source oracle is never substituted
for a Rust coordinator invocation.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess

import test_advisor_integration as advisor
import test_advisor_foreground_integration as foreground
import test_background_integration as background

SCENARIOS = ('background', 'advisor', 'foreground')


class RuntimeSubprocess:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, args, *positional, **kwargs):
        if len(args) > 3 and args[1:3] == ['-m', 'codex_plus.background']:
            staged = Path(kwargs['env']['CODEX_HOME']) / 'runtime-package/bin/codex-plus-runtime'
            if not staged.exists():
                staged.parent.mkdir(parents=True)
                shutil.copy2(self.runtime, staged)
            args = [str(staged), *args[3:]]
        return subprocess.run(args, *positional, **kwargs)


class RuntimeStaging:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        return getattr(shutil, name)

    def copyfile(self, source, target, *args, **kwargs):
        result = shutil.copyfile(source, target, *args, **kwargs)
        if Path(target).name == 'codex-plus':
            shutil.copy2(self.runtime, Path(target).parent / 'codex-plus-runtime')
        return result

    def copytree(self, source, target, *args, **kwargs):
        if Path(source) == advisor.REPO / 'codex_plus':
            # This is a Rust-only staged product, including the reused advisor fixture.
            return str(target)
        return shutil.copytree(source, target, *args, **kwargs)


def stop_rust_observers(home):
    for role in (b'supervise', b'observe'):
        for entry in Path('/proc').glob('[0-9]*'):
            try:
                command = (entry / 'cmdline').read_bytes().split(b'\0')
                env = (entry / 'environ').read_bytes().split(b'\0')
                if (role in command and any(part.endswith(b'codex-plus-runtime') for part in command)
                        and f'CODEX_HOME={home}'.encode() in env):
                    descriptor = os.pidfd_open(int(entry.name))
                    try:
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                        if not select.select([descriptor], [], [], 3)[0]:
                            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            assert select.select([descriptor], [], [], 3)[0], 'fixture coordinator did not exit'
                    finally:
                        os.close(descriptor)
            except (OSError, ProcessLookupError):
                pass


def run(runtime, binary, evidence, scenario):
    assert runtime.is_file() and os.access(runtime, os.X_OK), runtime
    version, digest = advisor.official_stock_identity(binary)
    evidence.mkdir(parents=True, exist_ok=True)
    identity = {'runtime': str(runtime), 'runtime_sha256': hashlib.sha256(runtime.read_bytes()).hexdigest(),
                'stock_binary': str(binary), 'stock_version': version,
                'stock_sha256': digest,
                'scenario': scenario, 'remote_model_requests': 0}
    (evidence / 'runtime-identity.json').write_text(json.dumps(identity, indent=2))
    if scenario == 'background':
        old_process, old_stop = background.subprocess, background.stop_observers
        background.subprocess = RuntimeSubprocess(runtime)
        background.stop_observers = stop_rust_observers
        try:
            background.run(binary, evidence)
        finally:
            background.subprocess, background.stop_observers = old_process, old_stop
    elif scenario in ('advisor', 'foreground'):
        fixture = advisor if scenario == 'advisor' else foreground
        old_staging = fixture.shutil
        fixture.shutil = RuntimeStaging(runtime)
        try:
            fixture.run(binary, evidence)
        finally:
            fixture.shutil = old_staging
    else:
        raise ValueError(scenario)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--stock-binary', required=True, type=Path)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--scenario', choices=SCENARIOS + ('all',), default='all')
    args = parser.parse_args()
    for scenario in SCENARIOS if args.scenario == 'all' else (args.scenario,):
        run(args.runtime.resolve(strict=True), args.stock_binary.resolve(strict=True),
            args.evidence.resolve() / scenario, scenario)
