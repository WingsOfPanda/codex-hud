"""Exercise terminal-emulator behavior used by the real native smoke fixtures."""
import unittest

import pyte

from native_smoke import FixtureScreen


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


if __name__ == '__main__':
    unittest.main()
