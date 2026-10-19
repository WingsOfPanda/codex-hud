"""Private PTY contract for a stable saved-setting badge and separate diagnostics."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--source', type=Path, required=True)
p.add_argument('--binary', type=Path, required=True)
p.add_argument('--evidence', type=Path, required=True)
a = p.parse_args()
a.evidence.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(a.source / 'tests'))
from native_smoke import Terminal, composer_ready, wait
from test_ultracode_tui import LocalServer, helper_script, footer

ROOT = '00000000-0000-0000-0009-000000000001'
reason = 'Private admission confirmation is unavailable; saved setting remains unchanged.'
report = {'passed': False, 'binary_sha256': hashlib.sha256(a.binary.read_bytes()).hexdigest(),
          'cases': [], 'violations': []}
with tempfile.TemporaryDirectory(prefix='plus-setting-') as temporary:
    base = Path(temporary)
    package = base / 'package'
    package.mkdir()
    native = package / 'codex'
    shutil.copy2(a.binary, native)
    host = a.binary.with_name('codex-code-mode-host')
    if host.exists():
        (package / host.name).symlink_to(host)
    helper = package / 'codex-plus-runtime'
    helper.write_text(helper_script((a.source / 'docs/ultracode-bootstrap.txt').read_text()))
    helper.chmod(0o700)
    for selection_name, selection in [('model', ['model-with-reasoning', 'thread-id']), ('empty', [])]:
        home, work = base / (selection_name + '-home'), base / (selection_name + '-work')
        home.mkdir()
        work.mkdir()
        (home / 'config.toml').write_text('model="gpt-6-sol"\nmodel_provider="openai"\n'
            'check_for_update_on_startup=false\nsuppress_unstable_features_warning=true\n'
            'analytics.enabled=false\nfeatures.daemon_auto_start=false\nfeatures.plugins=false\n'
            '[tui]\nstatus_line=' + json.dumps(selection) + '\n')
        server = LocalServer(ROOT, home, work, selection)
        terminal = None
        try:
            terminal = Terminal(native, home, work)
            wait([terminal], lambda: composer_ready(terminal.text), 'composer missing', 30)
            path = home / 'codex-plus/ultracode' / (ROOT + '.json')
            path.parent.mkdir(parents=True, exist_ok=True)
            effective = dict(enabled=True, revision=1, policy_version=1,
                             submission='bound', turn='current', generation='generation')
            states = [
                ('off-idle', False, None, 'available', []),
                ('on-unadmitted', True, None, 'available', []),
                ('on-effective', True, effective, 'available', []),
                ('on-old-pending', True, effective, 'available', [dict(submission='older', state='pending')]),
                ('on-unavailable', True, None, 'unavailable', []),
                ('off-effective-on', False, effective, 'available', [dict(submission='off-request', state='pending')]),
                ('off-effective-off', False, dict(effective, enabled=False), 'available', []),
                ('off-unavailable', False, None, 'unavailable', []),
            ]
            for name, enabled, bound, availability, pending in states:
                value = dict(version=1, root=ROOT, requested=dict(enabled=enabled,
                    revision=1 if enabled else 2, policy_version=1), effective=bound,
                    availability=availability, pending=pending, unavailable_reason=reason)
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(value))
                temp.replace(path)
                before = path.read_bytes()
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    terminal.read()
                labels = [m.group().strip().lower() for line in footer(terminal) if ROOT in line
                          for m in [re.search(r'\bultracode[^·]*', line, re.I)] if m]
                expected = [] if not selection else ['ultracode ' + ('on' if enabled else 'off')]
                case = dict(selection=selection_name, state=name, expected=expected, actual=labels)
                report['cases'].append(case)
                if labels != expected:
                    report['violations'].append(case)
                assert path.read_bytes() == before, 'footer rewrote admission evidence'
                (a.evidence / (selection_name + '-' + name + '.txt')).write_text(terminal.text)
            os.write(terminal.master, b'editable-setting-draft')
            wait([terminal], lambda: 'editable-setting-draft' in terminal.text, 'draft blocked')
            os.write(terminal.master, b'\x15/ultracode status')
            wait([terminal], lambda: '/ultracode status' in terminal.text, 'status command not editable')
            os.write(terminal.master, b'\r')
            wait([terminal], lambda: reason in terminal.text, 'detailed status lost admission reason')
            (a.evidence / (selection_name + '-diagnostic.txt')).write_text(terminal.text)
            if selection:
                for name, contents in [('corrupt', '{'), ('missing', None)]:
                    if contents is None:
                        path.unlink()
                    else:
                        path.write_text(contents)
                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        terminal.read()
                    if any('ultracode' in line.lower() for line in footer(terminal) if ROOT in line):
                        report['violations'].append({'state': name, 'error': 'unknown setting fabricated in footer'})
            assert not server.errors, server.errors
            assert not [r for r in server.requests if r['method'] in
                        ('turn/start', 'turn/steer', 'thread/queue/add', 'thread/queue/start')]
        finally:
            if terminal:
                (a.evidence / (selection_name + '.ansi')).write_bytes(terminal.output)
                terminal.close()
            server.close()
report.update(passed=not report['violations'], diagnostic_reason_preserved=True,
              display_model_requests=0, admission_evidence_unchanged=True)
(a.evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
assert report['passed'], report['violations']
