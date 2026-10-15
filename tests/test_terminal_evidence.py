"""Bounded local terminal evidence; fixtures never read real session files."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_plus.evidence import HEADER_BYTES, TAIL_BYTES, terminal_event


THREAD = '22222222-2222-4222-8222-222222222222'
ROOT = '11111111-1111-4111-8111-111111111111'
STAMP = '2026-10-15T03:04:05.006Z'


def line(kind, payload, timestamp=STAMP):
    return json.dumps({'timestamp': timestamp, 'type': kind, 'payload': payload}).encode() + b'\n'


def terminal(event='task_complete', turn='work', **fields):
    payload = {'type': event, 'turn_id': turn, **fields}
    if event == 'turn_aborted':
        payload.setdefault('reason', 'interrupted')
    return line('event_msg', payload)


class TerminalEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.path = self.home / 'sessions/2026/10/15/rollout.jsonl'
        self.path.parent.mkdir(parents=True)
        self.header = line('session_meta', {'id': THREAD, 'session_id': ROOT})

    def write(self, *records):
        self.path.write_bytes(self.header + b''.join(records))

    def read(self, path=None, thread=THREAD, turn='work'):
        return terminal_event(self.home, path or self.path, thread, turn)

    def test_exact_completion_returns_only_sanitized_provenance(self):
        self.write(terminal(last_agent_message='private response', completed_at=None))
        self.assertEqual(self.read(), {'status': 'completed', 'event': 'task_complete', 'timestamp': STAMP})

    def test_abort_with_null_end_metadata_is_positive_evidence(self):
        self.write(terminal('turn_aborted', completed_at=None))
        self.assertEqual(self.read()['status'], 'interrupted')

    def test_terminal_error_marks_failure_without_returning_error_text(self):
        self.write(terminal(error={'message': 'private failure'}))
        self.assertEqual(self.read()['status'], 'failed')
        self.assertNotIn('private', json.dumps(self.read()))
        self.write(terminal(error=None))
        self.assertEqual(self.read()['status'], 'completed')

    def test_invalid_or_null_outer_timestamp_is_omitted(self):
        for timestamp in (None, 17, {}, 'private response', '2026-99-99T00:00:00Z', 'x' * 1000):
            with self.subTest(timestamp=type(timestamp)):
                self.write(line('event_msg', {'type': 'task_complete', 'turn_id': 'work'}, timestamp))
                self.assertEqual(self.read(), {'status': 'completed', 'event': 'task_complete'})

    def test_wrong_thread_header_and_neighbor_turn_do_not_count(self):
        self.write(terminal(turn='neighbor'))
        self.assertIsNone(self.read())
        self.write(terminal())
        self.assertIsNone(self.read(thread=ROOT))
        self.path.write_bytes(line('session_meta', {'session_id': THREAD}) + terminal())
        self.assertIsNone(self.read())

    def test_requires_first_complete_session_header(self):
        for header in (b'', b'{bad}\n', b'[]\n', line('event_msg', {'id': THREAD}),
                       line('session_meta', []), self.header[:-1]):
            with self.subTest(header=header[:30]):
                self.path.write_bytes(header + terminal())
                self.assertIsNone(self.read())

    def test_malformed_unrelated_lines_and_incomplete_final_line_are_ignored(self):
        self.write(b'{bad}\n', b'[]\n', b'{"type":"event_msg","payload":[]}\n',
                   b'\xff\n', terminal(), terminal('turn_aborted')[:-1])
        self.assertEqual(self.read()['status'], 'completed')
        self.write(terminal()[:-1])
        self.assertIsNone(self.read())

    def test_wrong_envelope_synthetic_status_and_missing_turn_do_not_count(self):
        for record in (line('response_item', {'type': 'task_complete', 'turn_id': 'work'}),
                       line('event_msg', {'status': 'completed', 'turn_id': 'work'}),
                       terminal(turn=None), terminal(event='turn_complete')):
            with self.subTest(record=record[:70]):
                self.write(record)
                self.assertIsNone(self.read())

    def test_invalid_matching_terminal_payload_and_conflicting_events_are_unresolved(self):
        for record in (terminal(error=[]), terminal(error={}), terminal(error='failure'),
                       terminal('turn_aborted', reason=[]), terminal('turn_aborted', reason='unknown')):
            with self.subTest(record=record[:70]):
                self.write(record)
                self.assertIsNone(self.read())
        self.write(terminal(), terminal('turn_aborted'))
        self.assertIsNone(self.read())
        self.write(terminal(), terminal())
        self.assertEqual(self.read()['status'], 'completed')

    def test_history_rewrite_invalidates_old_evidence_until_fresh_exact_terminal(self):
        rewrites = (line('compacted', {'message': 'summary', 'replacement_history': []}),
                    line('event_msg', {'type': 'thread_rolled_back', 'num_turns': 1}))
        for rewrite in rewrites:
            with self.subTest(rewrite=rewrite[:65]):
                self.write(terminal(), rewrite)
                self.assertIsNone(self.read())
                self.write(terminal(), rewrite, terminal(turn='neighbor'))
                self.assertIsNone(self.read())
                self.write(terminal(), rewrite, terminal('turn_aborted'))
                self.assertEqual(self.read()['status'], 'interrupted')
                self.write(terminal(), terminal('turn_aborted'), rewrite, terminal())
                self.assertEqual(self.read()['status'], 'completed')

    def test_context_compacted_annotation_does_not_rewrite_terminal_history(self):
        self.write(terminal(), line('event_msg', {'type': 'context_compacted'}))
        self.assertEqual(self.read()['status'], 'completed')

    def test_archive_and_normalized_in_root_path_are_allowed(self):
        self.write(terminal())
        archive = self.home / 'archived_sessions/rollout.jsonl'
        archive.parent.mkdir()
        archive.write_bytes(self.path.read_bytes())
        self.assertEqual(self.read(archive)['status'], 'completed')
        self.assertEqual(self.read(self.path.parent / '../15/rollout.jsonl')['status'], 'completed')

    def test_escape_final_and_parent_symlinks_are_rejected(self):
        self.write(terminal())
        outside = self.home / 'outside.jsonl'
        outside.write_bytes(self.path.read_bytes())
        self.assertIsNone(self.read(outside))
        link = self.path.parent / 'link.jsonl'
        link.symlink_to(self.path)
        self.assertIsNone(self.read(link))
        escaped = self.home / 'sessions/escape'
        escaped.symlink_to(self.home, target_is_directory=True)
        self.assertIsNone(self.read(escaped / 'outside.jsonl'))

    def test_special_file_and_directory_are_rejected_without_blocking(self):
        self.assertIsNone(self.read(self.path.parent))
        os.mkfifo(self.path)
        self.assertIsNone(self.read())

    def test_missing_or_unreadable_file_is_unknown(self):
        self.assertIsNone(self.read())
        self.write(terminal())
        with patch('codex_plus.evidence.os.open', side_effect=PermissionError):
            self.assertIsNone(self.read())

    def test_large_header_and_overlarge_terminal_line_are_unknown(self):
        self.path.write_bytes(line('session_meta', {'id': THREAD, 'extra': 'x' * HEADER_BYTES}) + terminal())
        self.assertIsNone(self.read())
        self.write(terminal(last_agent_message='x' * TAIL_BYTES))
        self.assertIsNone(self.read())

    def test_tail_finds_latest_exact_evidence_without_scanning_old_extent(self):
        self.write(terminal('turn_aborted', turn='old'), b'x' * (TAIL_BYTES * 2) + b'\n', terminal())
        reads = []
        real_pread = os.pread

        def pread(fd, count, offset):
            reads.append((count, offset))
            return real_pread(fd, count, offset)

        with patch('codex_plus.evidence.os.pread', side_effect=pread):
            self.assertEqual(self.read()['status'], 'completed')
        self.assertLessEqual(sum(count for count, _ in reads), HEADER_BYTES + TAIL_BYTES + 2)
        self.assertEqual(len(reads), 2)
        self.assertIsNone(self.read(turn='old'))

    def test_replacement_truncation_and_rewrite_during_read_are_unknown(self):
        real_pread = os.pread
        for mutation in ('replace', 'truncate', 'rewrite'):
            with self.subTest(mutation=mutation):
                self.write(terminal())
                mutated = False

                def pread(fd, count, offset):
                    nonlocal mutated
                    data = real_pread(fd, count, offset)
                    if not mutated:
                        mutated = True
                        if mutation == 'replace':
                            replacement = self.path.with_suffix('.new')
                            replacement.write_bytes(self.path.read_bytes())
                            replacement.replace(self.path)
                        elif mutation == 'truncate':
                            self.path.write_bytes(self.header)
                        else:
                            self.path.write_bytes(self.path.read_bytes().replace(b'task_complete', b'task_completX'))
                    return data

                with patch('codex_plus.evidence.os.pread', side_effect=pread):
                    self.assertIsNone(self.read())


if __name__ == '__main__':
    unittest.main()
