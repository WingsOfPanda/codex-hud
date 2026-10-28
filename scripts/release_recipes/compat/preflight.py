"""Setup-only proof of the three actual compatibility fixture categories."""
from recipe_context import CONFIG, DATA, RECIPE
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from fixture_scope import BASE, validate_short
from advisor_tmp import install

os.umask(0o077)
short = validate_short()
assert os.environ['TMPDIR'] == str(short)
assert os.environ['CODEX_PLUS_RELEASE_QA_SCOPE'] == str(BASE)
assert Path(tempfile.gettempdir()) == short
config = json.loads((BASE / 'native.config.json').read_text())
source = Path(config['source'])
fixture_source = Path(config['fixture_source']).resolve(strict=True)
sys.path[:0] = [str(fixture_source / 'tests'), str(fixture_source)]
os.environ['CODEX_PLUS_TEST_RUNTIME'] = str(Path(config['new_package']) / 'bin/codex-plus-runtime')
assert os.uname().machine == config['architecture']


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('frozen_rollback', RECIPE / 'verify-mixes.py')
assert gate.source_identity(source, config['source_commit']) == config['source_commit']
fixtures = gate.fixture_identity(fixture_source)
from fixtures import custom_goal_server as goal_fixture
from fixtures.detached_jobs import DetachedJobServer

# This is the exact constructor and module-local temporary-directory override
# used by the frozen rollback gate. No workload/goal/turn is launched.
goal_fixture.tempfile = SimpleNamespace(TemporaryDirectory=lambda **unused:
    tempfile.TemporaryDirectory(prefix='s-', dir=tempfile.gettempdir()))
fixture = DetachedJobServer(Path(config['new_package']) / 'bin/codex-plus-runtime',
    Path(config['stock_binary']), BASE / 'setup-smoke/rollback',
    config='features.apps=false\nfeatures.memories=false\n')
rows = []
home = fixture.home
try:
    path = home / 'app-server-control/app-server-control.sock'
    assert path.is_socket() and len(os.fsencode(path)) < 104
    assert fixture.provider.count == 0
    rows.append(dict(name='actual-rollback-constructor', path=str(path),
                     socket_bytes=len(os.fsencode(path)), passed=True, provider_requests=0))
finally:
    fixture.close(True)
assert not home.exists() and not gate.owned_processes(home)

if config['recipe_revision'] == 2:
    from fixtures.paired_stock_transition import PairedStockServer
    binary = Path(config['previous_stock_binary'])
    assert gate.digest(binary) == config['previous_stock_binary_sha256'][config['architecture']]
    assert subprocess.check_output([str(binary), '--version'], text=True).strip() == 'codex-cli ' + config['previous_upstream']['version']
    fixture = PairedStockServer(Path(config['old_package']) / 'bin/codex-plus-runtime',
        binary, BASE / 'setup-smoke/previous-stock',
        config='features.apps=false\nfeatures.memories=false\n')
    home = fixture.home
    try:
        path = home / 'app-server-control/app-server-control.sock'
        assert path.is_socket() and len(os.fsencode(path)) < 104
        assert fixture.provider.count == 0
        rows.append(dict(name='actual-previous-stock-constructor', path=str(path),
            socket_bytes=len(os.fsencode(path)), passed=True, provider_requests=0,
            stock_sha256=gate.digest(binary), stock_upstream=config['previous_upstream']))
    finally:
        fixture.close(True)
    assert not home.exists() and not gate.owned_processes(home)
    from fixtures import stock_memory_transition as memory
    with tempfile.TemporaryDirectory(prefix='mm-', dir=short) as temporary:
        root = Path(temporary)
        home, work = root / 'home', root / 'work'
        home.mkdir(mode=0o700)
        work.mkdir()
        provider = memory.Provider('v2', home / 'memories_v2', 'setup-only-memory')
        server = None
        try:
            memory.write_config(home, provider)
            server = memory.StockServer(Path(config['stock_binary']), home, work,
                BASE / 'setup-smoke/memory-constructor.log')
            path = home / 'app-server-control/app-server-control.sock'
            assert path.is_socket() and len(os.fsencode(path)) < 104 and not provider.requests
            rows.append(dict(name='actual-memory-StockServer-constructor', path=str(path),
                socket_bytes=len(os.fsencode(path)), passed=True, provider_requests=0))
        finally:
            if server:
                server.close()
            provider.close()
        assert not gate.owned_processes(home)
    assert not home.exists()

contract = importlib.import_module('test_claude_advisor')
with install(BASE):
    case = contract.ClaudeAdvisor('test_root_required')
    case.setUp()
    home = case.home
    try:
        path = home / 'app-server-control/app-server-control.sock'
        assert path.is_socket() and len(os.fsencode(path)) < 104
        rows.append(dict(name='actual-advisor-setUp', path=str(path),
                         socket_bytes=len(os.fsencode(path)), passed=True))
    finally:
        case.tearDown()
assert not home.exists()

with tempfile.TemporaryDirectory(prefix='a45-') as temporary:
    path = Path(temporary) / 'h/app-server-control/app-server-control.sock'
    path.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
        rows.append(dict(name='unchanged-old-tui-real-bind', path=str(path),
                         socket_bytes=len(os.fsencode(path)), passed=True))
with (BASE/'setup-smoke/cursor-controller.log').open('x') as output:
    result = subprocess.run([sys.executable, RECIPE/'run-cursor.py', '--config', BASE/'native.config.json',
        '--footer-contract', BASE/'footer-contract.json',
        '--footer-contract-sha256', gate.digest(BASE/'footer-contract.json'),
        '--evidence', BASE/'setup-smoke/cursor', '--setup-only'],
        stdout=output, stderr=subprocess.STDOUT, timeout=60, check=True)
cursor = json.loads((BASE/'setup-smoke/cursor/setup.json').read_text())
assert cursor['passed'] and cursor['setup_only'] and cursor['socket_bytes'] < 104
rows.append(dict(name='actual-cursor-Server-constructor', path=cursor['socket'],
                 socket_bytes=cursor['socket_bytes'], passed=True, provider_requests=0))
imports = {
    'fixtures.foreground_advisor_provider': ['ForegroundAdvisorProvider'],
    'fixtures.worker_shell_wait_provider': ['cell_result', 'wait_script'],
    'native_smoke': ['Terminal', 'advisor_rows', 'advisor_transcript_rows', 'composer_ready', 'wait', 'write_model_catalog'],
    'test_background_integration': ['RecordingClient', 'stop', 'wait'],
    'test_detached_jobs_stock': ['SCENARIOS'],
    'test_custom_goals_stock': ['SCENARIOS'],
}
for name, symbols in imports.items():
    module = importlib.import_module(name)
    assert all(hasattr(module, symbol) for symbol in symbols)
recovery = load('recovery_contract', RECIPE.parents[1] / 'release_compat.py')
for suite, module in (('detached', 'test_detached_jobs_stock'), ('goals', 'test_custom_goals_stock')):
    assert set(recovery.RECOVERY_CASES[suite]) <= importlib.import_module(module).SCENARIOS.keys()
load('frozen_old_tui', RECIPE / 'old-tui-root-result-v5.py').self_check()
load('frozen_driver', RECIPE / 'run-native-compat-v5.py').validate_config(config)

report = dict(passed=True, setup_only=True, native_gates_run=False, source_commit=config['source_commit'],
    baseline_sha256=gate.digest(BASE/'unreadable-baseline.json'),
    fixture_source=fixtures,
    configuration_sha256=gate.digest(BASE / 'native.config.json'),
    interpreter=sys.executable, version=sys.version,
    environment={key: os.environ.get(key) for key in ('PATH', 'PYTHONPATH', 'TMPDIR',
        'PYTHONDONTWRITEBYTECODE', 'CODEX_PLUS_RELEASE_QA_SCOPE')}, rows=rows,
    actual_deferred_imports=imports, short_binding=json.loads((BASE / 'short-binding.json').read_text()),
    short_directory_preserved_for_root_launch=True)
(BASE / 'preflight.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(dict(passed=True, rows=rows, short=str(short), native_gates_run=False)))
