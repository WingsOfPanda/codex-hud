"""Qualify packaged recovery using the shipping private-stock scenarios unchanged."""
from recipe_context import CONFIG, DATA
import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace
from fixture_scope import validate_short

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from release_compat import RECOVERY_CASES, digest, validate_recovery_reports, write


def run(evidence):
    os.umask(0o077)
    short = validate_short()
    assert os.environ['TMPDIR'] == str(short)
    assert os.environ['CODEX_PLUS_RELEASE_QA_SCOPE'] == str(DATA)
    source = Path(CONFIG['fixture_source'])
    os.environ['CODEX_PLUS_TEST_RUNTIME'] = str(Path(CONFIG['new_package']) / 'bin/codex-plus-runtime')
    sys.path[:0] = [str(source / 'tests'), str(source)]
    from fixtures import custom_goal_server as fixture
    from fixtures.detached_jobs import DetachedJobServer
    fixture.tempfile = SimpleNamespace(TemporaryDirectory=lambda **unused:
        tempfile.TemporaryDirectory(prefix='s-', dir=short))
    suites = {'detached': (importlib.import_module('test_detached_jobs_stock'), DetachedJobServer),
              'goals': (importlib.import_module('test_custom_goals_stock'), fixture.CustomGoalServer)}
    runtime = Path(CONFIG['new_package']) / 'bin/codex-plus-runtime'
    old_runtime = Path(CONFIG['old_package']) / 'bin/codex-plus-runtime'
    binary = Path(CONFIG['stock_binary'])
    evidence.mkdir(mode=0o700)
    report = dict(schema=1, passed=False, source_commit=CONFIG['source_commit'],
        runtime_sha256=digest(runtime), old_runtime_sha256=digest(old_runtime),
        stock_sha256=digest(binary), cases=[])
    try:
        for suite, scenarios in RECOVERY_CASES.items():
            module, constructor = suites[suite]
            for scenario in scenarios:
                path = evidence / suite / scenario
                current = constructor(runtime, binary, path)
                current.old_runtime = old_runtime
                current.report.update(scenario=scenario, old_runtime_sha256=digest(old_runtime),
                                      private_home=str(current.home))
                passed = False
                try:
                    assert current.home.is_relative_to(short)
                    assert current.report['stock_version'] == 'codex-cli 0.163.0'
                    module.SCENARIOS[scenario](current)
                    passed = True
                except BaseException:
                    current.report['failure'] = traceback.format_exc()
                    raise
                finally:
                    current.close(passed)
                case_path = path / 'report.json'
                case = json.loads(case_path.read_text())
                case['private_home_removed'] = not current.home.exists()
                case_path.write_text(json.dumps(case, indent=2) + '\n')
                assert case['private_home_removed'], 'private fixture cleanup incomplete'
                report['cases'].append(dict(suite=suite, scenario=scenario,
                    report=str(case_path.relative_to(evidence)), sha256=digest(case_path)))
        report['passed'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        write(evidence / 'report.json', report)
    validate_recovery_reports(evidence, CONFIG)
    print(json.dumps(dict(passed=True, cases=len(report['cases']), remote_model_requests=0)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args().evidence.resolve())
