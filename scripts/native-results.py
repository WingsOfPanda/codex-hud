"""Accept only libtest's dedicated result file; terminal stdout supplies the summary."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


CSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def listed_names(listed):
    names = re.findall(r'^(\S+): test$', CSI.sub('', listed), re.MULTILINE)
    if not names or len(names) != len(set(names)):
        raise ValueError('Empty or duplicate listed test names')
    return names


def summary(output):
    text = CSI.sub('', output)
    rows = re.findall(r'^test result: [^\n]+', text, re.MULTILINE)
    if len(rows) != 1:
        raise ValueError('Missing or duplicate stdout summary')
    row = re.fullmatch(r'test result: ok\. (\d+) passed; 0 failed; (\d+) ignored; 0 measured; (\d+) filtered out; finished in [^\n]+', rows[0])
    if not row:
        raise ValueError('Failed or malformed stdout summary')
    return {'passed': int(row[1]), 'ignored': int(row[2]), 'filtered': int(row[3])}


def result_records(result_log):
    records = []
    for line in result_log.splitlines():
        reason = ''
        if line.startswith('ignored: '):
            reason, name = line[len('ignored: '):].rsplit(' ', 1)
            status = 'ignored'
            if not reason:
                raise ValueError('Empty ignored reason')
        else:
            match = re.fullmatch(r'(ok|failed|ignored) (\S+)', line)
            if not match:
                raise ValueError(f'Malformed dedicated result row: {line!r}')
            status, name = match.groups()
        if not re.fullmatch(r'\S+', name):
            raise ValueError('Malformed test name')
        records.append((name, status, reason))
    names = [name for name, _, _ in records]
    if not names or len(names) != len(set(names)):
        raise ValueError('Empty or duplicate dedicated results')
    return records


def validate_test_output(name, listed, output, result_log, *, exit_code, allowed_ignored=None):
    if exit_code != 0:
        raise ValueError(f'{name}: Native command did not exit successfully: {exit_code}')
    if allowed_ignored is not None and (not isinstance(allowed_ignored, dict) or
            any(not isinstance(test, str) or not re.fullmatch(r'\S+', test) or not isinstance(reason, str)
                for test, reason in allowed_ignored.items())):
        raise ValueError('Ignored reasons must map test names to reason strings')
    expected = listed_names(listed)
    records = result_records(result_log)
    actual = [test for test, _, _ in records]
    if Counter(actual) != Counter(expected):
        raise ValueError(f'{name}: Dedicated results differ from listed test names')
    if any(status == 'failed' for _, status, _ in records):
        raise ValueError(f'{name}: Failed native test')
    ignored = {test: reason for test, status, reason in records if status == 'ignored'}
    if ignored != (allowed_ignored or {}):
        raise ValueError(f'{name}: Unexpected ignored tests or reasons: {ignored!r}')
    counts = summary(output)
    if counts['passed'] != sum(status == 'ok' for _, status, _ in records):
        raise ValueError(f'{name}: Passed count differs from dedicated results')
    if counts['ignored'] != len(ignored):
        raise ValueError(f'{name}: Ignored count differs from dedicated results')
    return {'passed': counts['passed'], 'failed': 0, 'filtered': counts['filtered'],
            'platform_ignored': [{'test': test, 'reason': reason} for test, reason in sorted(ignored.items())],
            'listed_names': sorted(expected), 'executed_names': sorted(actual),
            'acceptance_source': 'dedicated libtest --logfile', 'stdout_test_records_ignored': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='native')
    parser.add_argument('--list', dest='listed', type=Path, required=True,
                        help='Independently captured libtest --list stdout')
    parser.add_argument('--logfile', type=Path, required=True, help='Dedicated libtest --logfile')
    parser.add_argument('--stdout', type=Path, required=True, help='Native command stdout with its summary')
    parser.add_argument('--exit-code', type=int, required=True, help='Captured native command exit status')
    parser.add_argument('--ignored-reasons', type=Path, help='Explicit JSON object mapping ignored names to reasons')
    args = parser.parse_args()
    try:
        ignored = json.loads(args.ignored_reasons.read_text()) if args.ignored_reasons else None
        if args.ignored_reasons and ignored is None:
            raise ValueError('Ignored reasons must be a JSON object')
        result = validate_test_output(args.name, args.listed.read_text(), args.stdout.read_text(),
                                      args.logfile.read_text(), exit_code=args.exit_code, allowed_ignored=ignored)
    except (OSError, ValueError) as error:
        print(json.dumps({'accepted': False, 'error': str(error)}))
        return 1
    print(json.dumps(dict(accepted=True, **result)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
