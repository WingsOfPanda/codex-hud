"""The bounded wait retains its log even when startup outlasts the first yield."""

import json
import subprocess
import unittest

from fixtures.worker_shell_wait_provider import wait_script


class WorkerShellWaitFixtureTests(unittest.TestCase):
    def test_log_survives_empty_first_yield_and_output_truncation(self):
        for initial in ('', 'codex-plus log: /private/command.log\n'):
            with self.subTest(initial=bool(initial)):
                results = [
                    {'session_id': 7, 'output': initial},
                    {'session_id': 7, 'output': 'codex-plus log: /private/command.log\n' + 'x' * 5000},
                    {'exit_code': 0, 'output': '\ncompleted\n'},
                ]
                program = '''
const results = RESULTS;
const tools = {
  exec_command: async () => results.shift(),
  write_stdin: async () => results.shift()
};
const text = value => console.log(JSON.stringify(value));
''' .replace('RESULTS', json.dumps(results))
                program += wait_script('ignored')
                result = subprocess.run(['node', '-e', '(async () => {\n' + program + '\n})()'],
                                        check=True, text=True, capture_output=True)
                value = json.loads(result.stdout)
                self.assertEqual(value['log'], '/private/command.log')
                self.assertEqual(value['state'], 'finished')
                self.assertEqual(value['exit_code'], 0)
                self.assertLessEqual(len(value['output']), 4000)
                self.assertTrue(value['output'].endswith('completed\n'))


if __name__ == '__main__':
    unittest.main()
