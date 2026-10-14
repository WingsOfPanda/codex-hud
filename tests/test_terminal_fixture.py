"""Exercise terminal-emulator behavior used by the real native smoke fixtures."""
import unittest

import pyte

from native_smoke import FixtureScreen, assert_compact_hud, shell_rows, worker_rows


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


class CompactHudAssertionTests(unittest.TestCase):
    def screen(self, rows):
        return '\n'.join(['› draft', '? for shortcuts', *rows, 'root-status'])

    def test_healthy_mixed_rows_and_overflow_count_toward_total(self):
        text = self.screen(['Sol worker (high) · task · 0:12 · ? tok',
                            'Background · python train.py · seen 0:12', '+4 more'])
        self.assertEqual(len(assert_compact_hud(text, rows=3)), 3)
        self.assertEqual(len(worker_rows(text)), 1)
        self.assertEqual(len(shell_rows(text)), 1)

    def test_combined_alert_and_overflow_keep_composer_in_last_six(self):
        text = self.screen(['Watcher unavailable · Workers stale · counts unavailable',
                            'mixed workers (mixed) · topic (3 agents) · 2:00 · ? tok', '+3 more'])
        assert_compact_hud(text, rows=3)

    def test_typed_draft_does_not_require_shortcuts_footer(self):
        assert_compact_hud('› draft\nroot-status\nBackground · command · seen 0:12', rows=1)
        assert_compact_hud('› draft\ntab to queue message\nBackground · command · seen 0:12',
                           rows=1, idle=False)

    def test_old_summary_and_four_total_rows_are_rejected(self):
        for rows in (['Workers 6 active'], ['Workers stale · counts unavailable',
                'Sol worker (high) · first · 0:12 · ? tok',
                'Astra worker (medium) · second · 0:12 · ? tok', '+4 more']):
            with self.subTest(rows=rows), self.assertRaises(AssertionError):
                assert_compact_hud(self.screen(rows))

    def test_idle_composer_outside_last_six_is_rejected(self):
        with self.assertRaises(AssertionError):
            assert_compact_hud('› draft\n' + '\n'.join(f'line {n}' for n in range(6)))


if __name__ == '__main__':
    unittest.main()
