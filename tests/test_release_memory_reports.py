"""Memory export refuses unsupported claims and tampered real SQLite/artifact captures."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_compat as compat
from fixtures.stock_memory_transition import snapshot


def memory_evidence(evidence, config):
    """Small real databases plus raw reports; no daemon or model is mocked as running."""
    lanes = []
    for lane in ('existing-v1', 'fresh-v2'):
        version = 'v1' if lane == 'existing-v1' else 'v2'
        marker = 'memory-transition-' + version
        directory = evidence / 'memory' / lane
        directory.mkdir(parents=True)
        memory_root = 'memories' if version == 'v1' else 'memories_v2'
        artifacts = directory / 'memory-artifacts' / memory_root
        (artifacts / 'rollout_summaries').mkdir(parents=True)
        (artifacts / 'memory_summary.md').write_text(marker + ' consolidated summary')
        (artifacts / 'rollout_summaries/source.md').write_text(marker + ' extracted summary')
        store_name = 'memories_1.sqlite' if version == 'v1' else 'memories_v2_1.sqlite'
        with sqlite3.connect(directory / store_name) as db:
            db.executescript('''CREATE TABLE stage1_outputs (thread_id TEXT, rollout_summary TEXT, raw_memory TEXT, selected_for_phase2 INTEGER);
                CREATE TABLE jobs (kind TEXT, job_key TEXT, status TEXT, worker_id TEXT);
                CREATE TABLE consolidation_progress (max_thread_count INTEGER);
                CREATE TABLE _sqlx_migrations (version INTEGER, checksum BLOB);
                INSERT INTO jobs VALUES ('memory_stage1','source','done','writer');
                INSERT INTO jobs VALUES ('memory_consolidate_global','global','done','writer');
                INSERT INTO consolidation_progress VALUES (1);
                INSERT INTO _sqlx_migrations VALUES (1, X'ABCD');''')
            db.execute('INSERT INTO stage1_outputs VALUES (?, ?, ?, 1)',
                ('source', marker + ' rollout summary', marker + ' raw memory' if version == 'v1' else ''))
        state = dict(stores=snapshot(directory)['stores'], artifacts=snapshot(directory / 'memory-artifacts')['artifacts'])
        settings = directory / 'config.toml'
        settings.write_text('''model_provider="isolated"
features.memories=true
memories.use_memories=true
memories.generate_memories=true
[model_providers.isolated]
name="OpenAI"
base_url="http://127.0.0.1:1234/v1"
''')
        (directory / 'models.json').write_text('{}')
        source = dict(id='source', source='cli', memory_mode='enabled')
        extraction = dict(role='extraction', thread='extract', body=dict(model='gpt-6.1-sol',
            input=[dict(text='SEED-SOURCE')], text=dict(format=dict(schema=dict(properties={
                'rollout_summary': {}, **({'raw_memory': {}} if version == 'v1' else {})})))))
        home = Path(config['short_root']) / 'aa' / lane / 'home'
        source_file = 'raw_memories.md' if version == 'v1' else 'phase2_workspace_diff.md'
        write = dict(role='consolidation-write', thread='writer', body=dict(model='gpt-6.1-sol',
            input=[dict(text=f'Read {home / memory_root / source_file}; write memory_summary.md')], tools=[dict(name='exec_command')]))
        final = dict(role='consolidation-final', thread='writer', body=dict(model='gpt-6.1-sol', input=[]))
        def probe(name, contains):
            return dict(thread=name, request=dict(role='probe', thread=name,
                body=dict(model='gpt-6.1-sol', input=[dict(text=marker if contains else 'ordinary input')])))
        candidate, default = probe('candidate', True), probe('default', version == 'v1')
        report = dict(schema=1, lane=lane, passed=True, remote_model_requests=0,
            fixture_sha256=config['fixture_files']['tests/fixtures/stock_memory_transition.py'],
            writer='stock165 startup pipeline', real_extraction_and_consolidation=True,
            original_memory_store_preserved=True, private_home_removed=True, owned_survivors_after_cleanup=[],
            private_home=str(home),
            stocks={role: dict(path=config[prefix + 'stock_binary'],
                sha256=config[prefix + 'stock_binary_sha256'][config['architecture']],
                version='codex-cli ' + config['previous_upstream' if prefix else 'upstream']['version'])
                for role, prefix in (('previous', 'previous_'), ('candidate', ''))},
            config_sha256=compat.digest(settings), catalog_sha256=compat.digest(directory / 'models.json'),
            source_thread='source', writer_thread='writer', prepared_source_metadata=source, final_source_metadata=[source],
            before_rollback=state, after_rollback=copy.deepcopy(state), final_memory_snapshot=copy.deepcopy(state),
            provider_requests=[extraction, write, final, candidate['request'], default['request']], provider_errors=[],
            candidate_probe=candidate, old_default_probe=default, old_default_version='v1',
            old_default_reads_written_summary=version == 'v1', transparent_default_rollback=version == 'v1',
            memory_status=dict(v2ConsolidatedThreads=1 if version == 'v2' else 0, v2Ready=version == 'v2'))
        if version == 'v2':
            explicit = probe('explicit', True)
            report['explicit_v2_probe'] = explicit
            report['provider_requests'].append(explicit['request'])
        path = directory / 'report.json'
        path.write_text(json.dumps(report))
        lanes.append(dict(lane=lane, report=path.relative_to(evidence).as_posix(), sha256=compat.digest(path)))
    bindings = {path.relative_to(evidence).as_posix(): compat.digest(path)
        for path in sorted((evidence / 'memory').rglob('*')) if path.is_file()}
    return lanes, bindings


class MemoryReports(unittest.TestCase):
    def setUp(self):
        import test_release_compat
        self.fixture = test_release_compat.Compatibility()
        self.compat = test_release_compat.compat
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.scope, self.raw = self.fixture.native_evidence(paired=True)
        self.config = json.loads((self.scope / 'native.config.json').read_text())
        self.evidence = self.scope / 'evidence/paired-stock-transition'
        self.report_path = self.evidence / 'report.json'
        self.report = json.loads(self.report_path.read_text())
        self.validator = compat._module(compat.RECIPE / 'memory-reports.py')

    def check(self):
        return self.validator.validate(self.evidence, self.config, self.report['memory_lanes'])

    def change(self, lane, mutate):
        row = next(row for row in self.report['memory_lanes'] if row['lane'] == lane)
        path = self.evidence / row['report']
        report = json.loads(path.read_text())
        mutate(report)
        path.write_text(json.dumps(report))
        row['sha256'] = compat.digest(path)

    def test_real_backups_and_artifact_hashes_are_exported(self):
        bindings = self.check()
        self.assertEqual(bindings, self.report['memory_evidence_sha256'])
        self.assertTrue(any(name.endswith('.sqlite') for name in bindings))
        self.assertTrue(any('/memory-artifacts/' in name for name in bindings))
        result = self.compat.export_reports(self.scope)
        exported = json.loads(Path(result['compat']).read_text())['checks']['paired-stock-transition']['evidence']
        self.assertTrue(any(name.endswith('.sqlite') for name in exported))
        self.assertTrue(any('/memory-artifacts/' in name for name in exported))

    def test_missing_writer_default_read_and_roles_refuse_after_report_rehash(self):
        for lane, mutate in (
                ('existing-v1', lambda r: r.update(provider_requests=[p for p in r['provider_requests'] if p['role'] != 'extraction'])),
                ('existing-v1', lambda r: r.update(old_default_reads_written_summary=False)),
                ('existing-v1', lambda r: r['old_default_probe']['request']['body'].update(input=[])),
                ('fresh-v2', lambda r: r.update(transparent_default_rollback=True)),
                ('fresh-v2', lambda r: r.update(explicit_v2_probe=r['candidate_probe'])),
                ('fresh-v2', lambda r: r['stocks']['previous'].update(sha256='0' * 64)),
                ('fresh-v2', lambda r: r['before_rollback']['stores']['memories_v2_1.sqlite']['stage1_outputs'].clear())):
            with self.subTest(lane=lane, mutation=mutate):
                original = (self.evidence / 'memory' / lane / 'report.json').read_bytes()
                self.change(lane, mutate)
                with self.assertRaises(ValueError):
                    self.check()
                (self.evidence / 'memory' / lane / 'report.json').write_bytes(original)
                next(row for row in self.report['memory_lanes'] if row['lane'] == lane)['sha256'] = compat.digest(
                    self.evidence / 'memory' / lane / 'report.json')

    def test_disabled_memories_or_explicit_selectors_refuse_even_with_rehashed_config(self):
        path = self.evidence / 'memory/existing-v1/config.toml'
        original = path.read_text()
        for content in (original.replace('features.memories=true', 'features.memories=false'),
                original.replace('memories.use_memories=true', 'memories.use_memories=false'),
                original.replace('memories.generate_memories=true', 'memories.generate_memories=false'),
                'memories.version="v1"\n' + original, 'memories.dual_write=false\n' + original):
            with self.subTest(config=content):
                path.write_text(content)
                self.change('existing-v1', lambda r: r.update(config_sha256=compat.digest(path)))
                with self.assertRaisesRegex(ValueError, 'disabled memories'):
                    self.check()

    def test_tampered_database_and_artifact_bytes_refuse(self):
        path = self.evidence / 'memory/fresh-v2/memories_v2_1.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM stage1_outputs')
        with self.assertRaisesRegex(ValueError, 'logical rows or artifact bytes'):
            self.check()
        path = self.evidence / 'memory/existing-v1/memory-artifacts/memories/memory_summary.md'
        path.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'logical rows or artifact bytes'):
            self.check()


if __name__ == '__main__':
    unittest.main()
