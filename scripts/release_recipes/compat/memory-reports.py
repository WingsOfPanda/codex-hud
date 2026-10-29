"""Read the memory lane's real writer, logical SQLite backups and artifact bytes."""
import json
from pathlib import Path
import sqlite3
import tomllib
from urllib.parse import urlparse

from release_compat import digest, require

LANES = ('existing-v1', 'fresh-v2')
TABLES = ('stage1_outputs', 'jobs', 'consolidation_progress', '_sqlx_migrations')
STORES = ('memories_1.sqlite', 'memories_v2_1.sqlite')


def validate(evidence, config, lanes):
    evidence = Path(evidence)
    require([row.get('lane') for row in lanes] == list(LANES), 'Memory lane inventory incomplete')
    short = Path(json.loads((Path(config['scope']) / 'short-binding.json').read_text())['path']).resolve()
    bindings = {}
    for row, lane in zip(lanes, LANES):
        relative = 'memory/' + lane + '/report.json'
        directory = evidence / 'memory' / lane
        report_path = directory / 'report.json'
        require(row.get('report') == relative and report_path.is_file() and not report_path.is_symlink() and
            row.get('sha256') == digest(report_path), 'Memory lane report missing or changed')
        report = json.loads(report_path.read_text())
        require(report.get('schema') == 1 and report.get('lane') == lane and report.get('passed') is True and
            report.get('fixture_sha256') == config['fixture_files']['tests/fixtures/stock_memory_transition.py'] and
            report.get('remote_model_requests') == 0 and report.get('provider_errors') == [] and
            report.get('writer') == 'stock165 startup pipeline' and
            report.get('real_extraction_and_consolidation') is True and
            report.get('original_memory_store_preserved') is True and report.get('private_home_removed') is True and
            report.get('owned_survivors_after_cleanup') == [], 'Memory writer identity, result or cleanup mismatch')
        home = Path(report.get('private_home', ''))
        require(home.is_absolute() and '..' not in home.parts and home.resolve() != short and
            home.resolve().is_relative_to(short) and not home.exists(), 'Memory home escaped owned short scope or survived cleanup')
        for role, prefix in (('previous', 'previous_'), ('candidate', '')):
            stock = report.get('stocks', {}).get(role, {})
            require(stock.get('path') == config[prefix + 'stock_binary'] and
                stock.get('sha256') == config[prefix + 'stock_binary_sha256'][config['architecture']] and
                stock.get('version') == 'codex-cli ' + config['previous_upstream' if prefix else 'upstream']['version'],
                'Memory daemon role/pin mismatch')
        for name, key in (('config.toml', 'config_sha256'), ('models.json', 'catalog_sha256')):
            path = directory / name
            require(path.is_file() and not path.is_symlink() and digest(path) == report.get(key), 'Memory configuration bytes changed')
        settings = tomllib.loads((directory / 'config.toml').read_text())
        memories = settings.get('memories', {})
        provider = settings.get('model_providers', {}).get('isolated', {})
        url = urlparse(provider.get('base_url', ''))
        require(settings.get('features', {}).get('memories') is True and
            memories.get('use_memories') is True and memories.get('generate_memories') is True and
            'version' not in memories and 'dual_write' not in memories and
            settings.get('model_provider') == 'isolated' and provider.get('name') == 'OpenAI' and
            url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost') and url.port,
            'Memory lane disabled memories, selected a version/dual-write, or used a non-loopback provider')
        version = 'v1' if lane == 'existing-v1' else 'v2'
        marker = 'memory-transition-' + version
        store_name = 'memories_1.sqlite' if version == 'v1' else 'memories_v2_1.sqlite'
        memory_root = 'memories' if version == 'v1' else 'memories_v2'
        before, after = report.get('before_rollback', {}), report.get('after_rollback', {})
        require(before.get('stores') and before.get('artifacts') and report.get('final_memory_snapshot') == after and
            all(after.get('stores', {}).get(name) == value for name, value in before['stores'].items()) and
            all(after.get('artifacts', {}).get(name) == value for name, value in before['artifacts'].items()),
            'Memory rollback changed original logical stores or artifact inventory')
        store = before['stores'].get(store_name, {})
        stage1 = store.get('stage1_outputs', [])
        source, writer = report.get('source_thread'), report.get('writer_thread')
        require(source and writer and source != writer and len(stage1) == 1 and
            stage1[0].get('thread_id') == source and marker in stage1[0].get('rollout_summary', '') and
            stage1[0].get('raw_memory') == (marker + ' raw memory' if version == 'v1' else '') and
            stage1[0].get('selected_for_phase2') == 1 and
            any(row.get('max_thread_count', 0) >= 1 for row in store.get('consolidation_progress', [])) and
            all(any(job.get('kind') == kind and job.get('status') == 'done' and job.get('worker_id') == writer
                for job in store.get('jobs', [])) for kind in ('memory_stage1', 'memory_consolidate_global')),
            'Memory extraction/consolidation durable writer rows missing')
        prepared = report.get('prepared_source_metadata', {})
        require(prepared.get('id') == source and prepared.get('source') == 'cli' and
            prepared.get('memory_mode') == 'enabled' and prepared in report.get('final_source_metadata', []),
            'Memory real source metadata missing or changed')
        requests = report.get('provider_requests', [])
        roles = [request.get('role') for request in requests]
        require(len(requests) <= 10 and all(request.get('body', {}).get('model') == 'gpt-6.1-sol' for request in requests) and
            all(roles.count(role) == 1 for role in ('extraction', 'consolidation-write', 'consolidation-final')),
            'Memory actual extraction/consolidation requests missing or duplicated')
        extraction = requests[roles.index('extraction')]['body']
        properties = extraction.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
        require('rollout_summary' in properties and ('raw_memory' in properties) == (version == 'v1') and
            'SEED-SOURCE' in json.dumps(extraction.get('input', [])), 'Memory extraction input/schema mismatch')
        consolidation = requests[roles.index('consolidation-write')]['body']
        consolidation_input = json.dumps(consolidation.get('input', []))
        require('exec_command' in {tool.get('name') for tool in consolidation.get('tools', [])} and
            str(home / memory_root) in consolidation_input and 'memory_summary.md' in consolidation_input and
            ('raw_memories.md' if version == 'v1' else 'phase2_workspace_diff.md') in consolidation_input,
            'Memory actual consolidation tool/input missing')
        probes = []
        for name, expect_marker in (('candidate_probe', True), ('old_default_probe', version == 'v1'),
                *((('explicit_v2_probe', True),) if version == 'v2' else ())):
            probe = report.get(name, {})
            request = probe.get('request', {})
            require(request in requests and request.get('role') == 'probe' and
                request.get('thread') == probe.get('thread') and
                (marker in json.dumps(request.get('body', {}).get('input', []))) == expect_marker,
                'Memory candidate/old-reader request marker mismatch')
            probes.append(probe)
        require(len({probe['thread'] for probe in probes}) == len(probes) and
            requests[-len(probes):] == [probe['request'] for probe in probes],
            'Memory candidate/old-reader probes reused or out of order')
        require(report.get('old_default_reads_written_summary') is (version == 'v1') and
            report.get('transparent_default_rollback') is (version == 'v1') and report.get('old_default_version') == 'v1',
            'Memory old default V1 read/transparent rollback distinction missing')
        if version == 'v2':
            require(report.get('memory_status', {}).get('v2ConsolidatedThreads') == 1 and
                report.get('memory_status', {}).get('v2Ready') is True and
                not any(name.startswith('memories/') for name in before['artifacts']), 'Memory fresh V2 lane/default mismatch')
        captured_stores = {}
        for name in STORES:
            path = directory / name
            if path.exists():
                require(path.is_file() and not path.is_symlink(), 'Memory SQLite capture is not a regular file')
                with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
                    require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Memory SQLite capture integrity failed')
                    db.row_factory = sqlite3.Row
                    captured_stores[name] = {table: sorted([dict(value) for value in db.execute('SELECT * FROM ' + table)],
                        key=lambda value: json.dumps(value, sort_keys=True, default=str)) for table in TABLES}
                    for migration in captured_stores[name]['_sqlx_migrations']:
                        migration['checksum'] = migration['checksum'].hex()
        artifacts = directory / 'memory-artifacts'
        require(artifacts.is_dir() and not artifacts.is_symlink(), 'Memory artifact directory missing or symlinked')
        captured_artifacts = {}
        for path in sorted(artifacts.rglob('*')):
            require(not path.is_symlink(), 'Memory artifact symlink refused')
            if path.is_file():
                captured_artifacts[path.relative_to(artifacts).as_posix()] = digest(path)
        require(captured_stores == after.get('stores') and captured_artifacts == after.get('artifacts') and
            memory_root + '/memory_summary.md' in captured_artifacts and
            any(name.startswith(memory_root + '/rollout_summaries/') for name in captured_artifacts),
            'Memory SQLite logical rows or artifact bytes changed/missing')
        require(marker in (artifacts / memory_root / 'memory_summary.md').read_text(), 'Memory written summary marker missing')
        for path in sorted(directory.rglob('*')):
            require(not path.is_symlink(), 'Memory evidence symlink refused')
            if path.is_file():
                bindings[path.relative_to(evidence).as_posix()] = digest(path)
    return bindings
