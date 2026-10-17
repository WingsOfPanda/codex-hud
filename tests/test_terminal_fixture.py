"""Exercise terminal-emulator behavior used by the real native smoke fixtures."""
import unittest
from unittest.mock import patch

import pyte

from native_smoke import (FixtureScreen, advisor_rows, advisor_transcript_rows,
                          assert_compact_hud, shell_rows, worker_rows)
from test_shell_policy_integration import process_running


class TerminalFixtureTests(unittest.TestCase):
    def render(self, data):
        screen = FixtureScreen(4, 1)
        pyte.Stream(screen).feed(data)
        return screen.display[0]

    def test_partial_wide_character_overwrite_clears_orphan_cell(self):
        self.assertEqual(self.render('界\r '), '    ')
        self.assertEqual(self.render('界\rX'), 'X   ')

    def test_valid_wide_character_and_following_cell_are_preserved(self):
        self.assertEqual(self.render('界X'), '界X ')

    def test_combining_character_is_preserved(self):
        self.assertEqual(self.render('e\u0301X'), 'éX  ')

    def test_erase_and_redraw_wide_character(self):
        self.assertEqual(self.render('界\r\x1b[2K好'), '好  ')


class ProcessObservationTests(unittest.TestCase):
    def test_process_disappearing_during_stat_read_is_not_running(self):
        for error in (FileNotFoundError(), ProcessLookupError()):
            with self.subTest(error=type(error).__name__), patch('pathlib.Path.read_text', side_effect=error):
                self.assertFalse(process_running(123))

    def test_stat_read_permission_failure_is_not_treated_as_completion(self):
        with patch('pathlib.Path.read_text', side_effect=PermissionError()), self.assertRaises(PermissionError):
            process_running(123)


class CompactHudAssertionTests(unittest.TestCase):
    def screen(self, rows):
        return '\n'.join(['› draft', 'root-status', *rows])

    def test_healthy_mixed_rows_and_overflow_count_toward_total(self):
        text = self.screen(['GPT-6.1-Sol (high) · task · 0:12 · ? tok',
                            'Background · python train.py · seen 0:12', '+4 more'])
        self.assertEqual(len(assert_compact_hud(text, rows=3)), 3)
        self.assertEqual(len(worker_rows(text)), 1)
        self.assertEqual(len(shell_rows(text)), 1)

    def test_combined_alert_and_overflow_keep_composer_in_last_six(self):
        text = self.screen(['Watcher unavailable · Workers stale · counts unavailable',
                            'mixed (mixed) · topic (3 agents) · 2:00 · ? tok', '+3 more'])
        assert_compact_hud(text, rows=3)

    def test_advisor_shares_total_budget_with_warning_and_worker(self):
        advisor = 'Advisor · GPT-6-Astra (xhigh) · reviewing · 1:23'
        text = self.screen(['Wake delivery uncertain', advisor,
                            'GPT-6.1-Sol (high) · task · 0:12 · ? tok'])
        self.assertEqual(advisor_rows(text), [advisor])
        assert_compact_hud(text, rows=3)
        with self.assertRaises(AssertionError):
            assert_compact_hud(text + '\n+2 more')

    def test_advisor_transcript_is_separate_from_three_row_footer(self):
        transcript = ['● Advising using GPT-6-Astra · 0m59s',
                      '● Advised by GPT-6-Astra · 1m05s',
                      '● Advisor failed · 1m05s']
        footer = ['Advisor · GPT-6-Astra (xhigh) · reviewing · 1:23',
                  'GPT-6.1-Sol (high) · task · 0:12 · ? tok',
                  'Background · python train.py · seen 0:12']
        for row in transcript:
            text = row + '\n' + self.screen(footer)
            with self.subTest(row=row):
                self.assertEqual(advisor_transcript_rows(text), [row])
                self.assertEqual(len(assert_compact_hud(text, rows=3)), 3)

    def test_advisor_transcript_recognition_excludes_draft_and_footer(self):
        self.assertEqual(advisor_transcript_rows('\n'.join([
            'Advisor · GPT-6-Astra (xhigh) · reviewing · 1:23',
            '› ● Advising using GPT-6-Astra · 0m59s',
            '> ● Advised by GPT-6-Astra · 1m05s',
            '● Advising using GPT-6-Astra · ?m??s',
            '● Advising using GPT-6-Astra · 0m59s extra',
        ])), [])

    def test_typed_draft_does_not_require_shortcuts_footer(self):
        assert_compact_hud('› draft\nroot-status\nBackground · command · seen 0:12', rows=1)
        assert_compact_hud('› draft\ntab to queue message\nBackground · command · seen 0:12',
                           rows=1, idle=False)

    def test_removed_navigation_hints_are_rejected(self):
        for hint in ('← for agents · ? for shortcuts', '? for shortcuts'):
            with self.subTest(hint=hint), self.assertRaises(AssertionError):
                assert_compact_hud('› draft\n' + hint)

    def test_old_summary_and_four_total_rows_are_rejected(self):
        for rows in (['Workers 6 active'], ['Workers stale · counts unavailable',
                'GPT-6.1-Sol (high) · first · 0:12 · ? tok',
                'GPT-6-Astra (medium) · second · 0:12 · ? tok', '+4 more']):
            with self.subTest(rows=rows), self.assertRaises(AssertionError):
                assert_compact_hud(self.screen(rows))

    def test_idle_composer_outside_last_six_is_rejected(self):
        with self.assertRaises(AssertionError):
            assert_compact_hud('› draft\n' + '\n'.join(f'line {n}' for n in range(6)))

    def test_worker_recognition_preserves_full_custom_and_unknown_models(self):
        rows = ['GPT-6.1-Sol (high) · first · 0:12 · ? tok',
                'GPT-6-Astra (medium) · second · 2:03 · 1.2K tok',
                'provider/gpt-7-sol-preview (xhigh) · third · 0:00 · 0 tok',
                'custom:model name (none) · fourth · 12:34 · 1M tok',
                'other-model (minimal) · large usage · 123:45 · 1.23T tok',
                'GPT-6.1-Sol (max) · maximum · 0:12 · ? tok',
                'GPT-6.1-Sol (ultra) · ultra · 0:12 · ? tok',
                'GPT-6.1-Sol (persistent) · persistent · 0:12 · ? tok',
                'custom-model (future effort) · custom · 0:12 · ? tok',
                'mixed (mixed) · topic (3 agents) · 2:00 · ? tok',
                '? (?) · unknown · stale · ? · ? tok',
                'vendor/model (low) · approval · wait approval · 1:02 · 234 tok']
        self.assertEqual(worker_rows('\n'.join('  ' + row for row in rows)), rows)

    def test_worker_recognition_accepts_clipped_metric_suffix(self):
        row = 'GPT-6.1-Sol (high) · task · 0:12 · ? tok'
        for suffix in (' · ? tok', ' · ? to', ' · ? t', ' · ?', ''):
            clipped = row.removesuffix(' · ? tok') + suffix
            with self.subTest(row=clipped):
                self.assertEqual(worker_rows(clipped), [clipped])

    def test_worker_recognition_excludes_shells_alerts_and_conversation(self):
        lines = ['Background · command (high) · seen 0:12',
                 'Workers stale · counts unavailable', '+4 more',
                 'Watcher unavailable · Workers stale · counts unavailable',
                 'GPT-6.1-Sol (high) · ordinary text without metrics',
                 'GPT-6.1-Sol · missing effort · 0:12 · ? tok',
                 'GPT-6.1-Sol (high) · task · 0:12 · ? tok extra text',
                 '› GPT-6.1-Sol (high) · draft · 0:12 · ? tok',
                 '> GPT-6.1-Sol (high) · quoted row · 0:12 · ? tok']
        self.assertEqual(worker_rows('\n'.join(lines)), [])


if __name__ == '__main__':
    unittest.main()
