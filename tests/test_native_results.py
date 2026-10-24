"""Dedicated libtest records, independent list and command status are the verdict."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/native-results.py'
spec = importlib.util.spec_from_file_location('native_results', SCRIPT)
native_results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_results)


class NativeResults(unittest.TestCase):
    def invoke(self, *, listed='suite::first: test\n', records='ok suite::first\n',
               stdout='test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n',
               exit_code=0, ignored=None, optimized=False):
        with tempfile.TemporaryDirectory(prefix='native-results-') as directory:
            root = Path(directory)
            for name, text in [('list', listed), ('logfile', records), ('stdout', stdout)]:
                (root / name).write_text(text)
            command = [sys.executable] + (['-O'] if optimized else []) + [
                str(SCRIPT), '--list', str(root / 'list'), '--logfile', str(root / 'logfile'),
                '--stdout', str(root / 'stdout'), '--exit-code', str(exit_code)]
            if ignored is not None:
                (root / 'ignored.json').write_text(json.dumps(ignored))
                command += ['--ignored-reasons', str(root / 'ignored.json')]
            result = subprocess.run(command, capture_output=True, text=True)
            return result.returncode, json.loads(result.stdout)

    def test_cli_reports_acceptance_from_independent_files(self):
        code, report = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(report['accepted'])
        self.assertEqual(report['passed'], 1)

    def test_explicit_ignored_reasons_accept_only_the_matching_record(self):
        stdout = ('test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; '
                  '0 filtered out; finished in 0.01s\n')
        for record, reason in [('ignored: Windows only suite::first\n', 'Windows only'),
                               ('ignored suite::first\n', '')]:
            with self.subTest(reason=reason):
                code, report = self.invoke(records=record, stdout=stdout,
                    ignored={'suite::first': reason})
                self.assertEqual(code, 0)
                self.assertEqual(report['platform_ignored'], [{'test': 'suite::first', 'reason': reason}])

    def test_cli_refuses_invalid_evidence_even_with_python_optimization(self):
        ignored_stdout = ('test result: ok. 0 passed; 0 failed; 1 ignored; 0 measured; '
                          '0 filtered out; finished in 0.01s\n')
        cases = {
            'missing result': {'records': ''},
            'duplicate result': {'records': 'ok suite::first\nok suite::first\n'},
            'unexpected result': {'records': 'ok suite::other\n'},
            'failed result': {'records': 'failed suite::first\n'},
            'malformed result': {'records': 'test suite::first ... ok\n'},
            'nonzero exit': {'exit_code': 101},
            'missing list': {'listed': ''},
            'duplicate list': {'listed': 'suite::first: test\nsuite::first: test\n'},
            'missing summary': {'stdout': 'test suite::first ... ok\n'},
            'duplicate summary': {'stdout': 2 * ('test result: ok. 1 passed; 0 failed; '
                '0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n')},
            'inconsistent passed count': {'stdout': 'test result: ok. 2 passed; 0 failed; '
                '0 ignored; 0 measured; 0 filtered out; finished in 0.01s\n'},
            'inconsistent ignored count': {'stdout': ignored_stdout},
            'unknown ignore': {'records': 'ignored: Windows only suite::first\n',
                               'stdout': ignored_stdout},
            'changed ignore reason': {'records': 'ignored: changed reason suite::first\n',
                'stdout': ignored_stdout, 'ignored': {'suite::first': 'Windows only'}},
            'wrong ignore shape': {'ignored': []},
            'string ignore shape': {'ignored': 'null'},
        }
        for optimized in (False, True):
            for name, arguments in cases.items():
                with self.subTest(case=name, optimized=optimized):
                    code, report = self.invoke(optimized=optimized, **arguments)
                    self.assertEqual(code, 1)
                    self.assertFalse(report['accepted'])
                    self.assertTrue(report['error'])

    def test_dedicated_results_accept_noisy_concatenated_terminal_rows(self):
        listed = 'suite::first: test\nsuite::second: test\n\n2 tests, 0 benchmarks\n'
        stdout = ('running 2 tests\ntest suite::first ... ok\n'
                  '\x1b[?25hterminal frame texttest suite::second ... ok\n'
                  'test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; '
                  '7 filtered out; finished in 0.01s\n')
        result = native_results.validate_test_output(
            'suite', listed, stdout, 'ok suite::first\nok suite::second\n', exit_code=0)
        self.assertEqual(result['passed'], 2)
        self.assertEqual(result['filtered'], 7)
        self.assertEqual(result['listed_names'], ['suite::first', 'suite::second'])
        self.assertTrue(result['stdout_test_records_ignored'])


if __name__ == '__main__':
    unittest.main()
