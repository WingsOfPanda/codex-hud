"""Qualify the exact quiet stock163/Plus163 -> stock165/Plus165 -> old pair."""
from recipe_context import CONFIG, DATA, RECIPE
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(RECIPE.parents[1]))
from release_compat import digest, validate_paired_report, write


def run(evidence):
    from fixture_scope import validate_short
    short = validate_short()
    os.umask(0o077)
    source = Path(CONFIG['fixture_source'])
    sys.path[:0] = [str(source / 'tests'), str(source)]
    from fixtures import custom_goal_server
    from fixtures.detached_jobs import DetachedJobServer
    from fixtures.paired_stock_transition import exercise
    spec = importlib.util.spec_from_file_location('paired_oracle', RECIPE / 'verify-mixes.py')
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    custom_goal_server.tempfile = SimpleNamespace(TemporaryDirectory=lambda **unused:
        tempfile.TemporaryDirectory(prefix='s-', dir=short))
    old_runtime = Path(CONFIG['old_package']) / 'bin/codex-plus-runtime'
    new_runtime = Path(CONFIG['new_package']) / 'bin/codex-plus-runtime'
    old_binary, new_binary = Path(CONFIG['previous_stock_binary']), Path(CONFIG['stock_binary'])
    evidence.mkdir(mode=0o700)
    report = dict(schema=1, passed=False, source_commit=CONFIG['source_commit'],
        release=CONFIG['release'], previous_release=CONFIG['previous_release'],
        upstream=CONFIG['upstream'], previous_upstream=CONFIG['previous_upstream'],
        architecture=CONFIG['architecture'], configuration_sha256=digest(DATA / 'native.config.json'),
        old_manifest_sha256=digest(Path(CONFIG['old_package']) / 'codex-plus.json'),
        new_manifest_sha256=CONFIG['new_manifest_sha256'], remote_model_requests=0)
    fixture = None
    try:
        for binary, upstream, pins in ((old_binary, CONFIG['previous_upstream'], CONFIG['previous_stock_binary_sha256']),
                (new_binary, CONFIG['upstream'], CONFIG['stock_binary_sha256'])):
            oracle.native_elf(binary)
            assert digest(binary) == pins[CONFIG['architecture']]
            assert subprocess.check_output([str(binary), '--version'], text=True).strip() == 'codex-cli ' + upstream['version']
        fixture = DetachedJobServer(old_runtime, old_binary, evidence / 'fixture',
            config='features.apps=false\nfeatures.memories=false\n')
        report['private_home'] = str(fixture.home)
        report.update(exercise(fixture, new_runtime, old_binary, new_binary, oracle))
    except BaseException:
        report.update(passed=False, error=traceback.format_exc())
        raise
    finally:
        if fixture:
            fixture.close(report['passed'])
            report['fixture_report_sha256'] = digest(evidence / 'fixture/report.json')
            report['private_home_removed'] = not fixture.home.exists()
            report['owned_survivors_after_cleanup'] = oracle.owned_processes(fixture.home)
            if not report['private_home_removed'] or report['owned_survivors_after_cleanup']:
                report['passed'] = False
        write(evidence / 'report.json', report)
    validate_paired_report(evidence / 'report.json', CONFIG)
    print(json.dumps(dict(passed=True, report=str(evidence / 'report.json'))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    run(parser.parse_args().evidence.resolve())
