"""Trust the real writer only after its extraction and consolidation boundaries."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from fixtures.stock_memory_transition import response_item, snapshot, summary


class StockMemoryTransitionTests(unittest.TestCase):
    def test_extraction_contract_separates_raw_v1_from_summary_only_v2(self):
        for version, raw in (('v1', True), ('v2', False)):
            properties = {'rollout_summary': {}}
            if raw:
                properties['raw_memory'] = {}
            role, item = response_item({'text': {'format': {'schema': {'properties': properties}}}},
                version, Path('/private/memories'), 'marker', 0)
            output = json.loads(item['content'][0]['text'])
            self.assertEqual(role, 'extraction')
            self.assertEqual('raw_memory' in output, raw)
            self.assertIn('marker', output['rollout_summary'])

    def test_probe_cannot_trigger_writer_and_writer_requires_real_tool(self):
        role, _ = response_item({'input': [{'text': 'MEMORY-PROBE'}]}, 'v2', Path('/private'), 'marker', 0)
        self.assertEqual(role, 'probe')
        with self.assertRaisesRegex(AssertionError, 'exec_command absent'):
            response_item({'tools': []}, 'v2', Path('/private'), 'marker', 0)
        role, item = response_item({'tools': [{'name': 'exec_command'}]}, 'v2', Path('/private'), 'marker', 0)
        self.assertEqual(role, 'consolidation-write')
        command = json.loads(item['arguments'])['cmd']
        self.assertIn('memory_summary.md', command)
        self.assertNotIn('MEMORY.md', command)
        self.assertIn('## User Profile', summary('marker'))

    def test_artifact_inventory_detects_changes_and_excludes_git(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / 'memories_v2'
            (root / '.git').mkdir(parents=True)
            (root / '.git/config').write_text('git metadata')
            (root / 'memory_summary.md').write_text(summary('before'))
            before = snapshot(home)
            self.assertEqual(list(before['artifacts']), ['memories_v2/memory_summary.md'])
            (root / 'memory_summary.md').write_text(summary('after'))
            self.assertNotEqual(before, snapshot(home))

    def test_store_inventory_preserves_checksums_and_detects_row_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with sqlite3.connect(home / 'memories_v2_1.sqlite') as db:
                db.executescript('''CREATE TABLE stage1_outputs (thread_id TEXT, rollout_summary TEXT);
                    CREATE TABLE jobs (kind TEXT, status TEXT);
                    CREATE TABLE consolidation_progress (max_thread_count INTEGER);
                    CREATE TABLE _sqlx_migrations (version INTEGER, checksum BLOB);
                    INSERT INTO stage1_outputs VALUES ('source', 'real-writer-marker');
                    INSERT INTO consolidation_progress VALUES (1);
                    INSERT INTO _sqlx_migrations VALUES (1, X'ABCD');''')
            before = snapshot(home)
            self.assertEqual(before['stores']['memories_v2_1.sqlite']['_sqlx_migrations'][0]['checksum'], 'abcd')
            with sqlite3.connect(home / 'memories_v2_1.sqlite') as db:
                db.execute('DELETE FROM stage1_outputs')
            self.assertNotEqual(before, snapshot(home))


if __name__ == '__main__':
    unittest.main()
