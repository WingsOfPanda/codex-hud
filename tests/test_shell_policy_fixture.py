"""Exercise shell disclosure checks without launching a terminal or native process."""

from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_shell_policy_integration as shell


class ShellPolicyFixtureTests(unittest.TestCase):
    def compact(self, failed):
        return ('• Failed (exit 17) command\n  └ Output: FAIL_LINE_039\n+ Show details'
                if failed else '• Ran command\n+ 3 lines')

    def check_details(self, failed, expanded=None):
        label = 'Hide details' if failed else 'Show less'
        if expanded is None:
            output = ([f'FAIL_LINE_{index:03}' for index in range(40)]
                      if failed else ['POLICY_COMPLETE'])
            expanded = '\n'.join(['codex-plus log: /private/command.log', 'POLICY_STARTED',
                                  *output, f'− {label}'])
        log = Path(re.search(r'codex-plus log: (\S+)', expanded).group(1))
        terminal = SimpleNamespace(text=self.compact(failed), master=123,
                                   screen=SimpleNamespace(lines=32, columns=120),
                                   resize=Mock(return_value=0), resized_frame=Mock(return_value=True))

        def write(master, data):
            self.assertEqual(master, terminal.master)
            if data == b'\x1bOS\x1b[C':
                terminal.text = expanded
            else:
                self.assertEqual(data, b'\x1b[D\x1b[27u')

        def wait(terminals, predicate, reason):
            self.assertEqual(terminals, [terminal])
            if reason == 'shell details did not collapse':
                self.assertFalse(predicate(), 'collapse accepted an expanded shell')
                terminal.text = self.compact(failed)
            self.assertTrue(predicate(), reason)

        evidence = Mock()
        with patch.object(shell.os, 'write', side_effect=write), patch.object(shell, 'wait', side_effect=wait):
            shell.check_shell_details(terminal, log, failed, evidence)
        evidence.write_text.assert_called_once_with(expanded + '\n')
        self.assertEqual(terminal.resize.call_args_list[-1].args, (32, 120))

    def test_success_output_expands_and_waits_for_collapse(self):
        self.check_details(False)

    def test_failure_details_expand_and_wait_for_collapse(self):
        self.check_details(True)

    def test_compact_replay_rejects_both_expanded_disclosure_labels(self):
        for failed in (False, True):
            text = self.compact(failed)
            shell.assert_compact_completion(text, 17 if failed else 0)
            for label in ('Show less', 'Hide details'):
                with self.subTest(failed=failed, label=label), self.assertRaises(AssertionError):
                    shell.assert_compact_completion(text + '\n− ' + label, 17 if failed else 0)


if __name__ == '__main__':
    unittest.main()
