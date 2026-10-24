"""Run fresh packaged cursor capture/integrity under the scoped child supervisor."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import owned_run

BASE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, BASE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args):
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    driver = load('run-native-compat-v5')
    driver.validate_config(config)
    gate = load('verify-mixes')
    gate.source_identity(Path(config['source']), config['source_commit'])
    assert os.uname().machine == config['architecture']
    accepted = Path(config['qualification_evidence'])
    assert gate.digest(accepted) == config['qualification_evidence_sha256']
    receipt = json.loads(accepted.read_text())
    assert receipt['accepted'] is True and receipt['cleanup_passed'] is True and receipt['native'] is True
    assert receipt['source'] == config['source_commit'] and receipt['architecture'] == config['architecture']
    assert receipt['archive_sha256'] == config['new_archive_sha256'] and receipt['manifest_sha256'] == config['new_manifest_sha256']
    package = Path(config['new_package'])
    identity = gate.package_identity(package, NEW_VERSION, config['source_commit'], config['new_manifest_sha256'])
    archive = load('archive-identity').archive_identity(config['new_archive'], config['architecture'],
        NEW_VERSION, config['source_commit'], config['new_manifest_sha256'])
    assert archive['archive_sha256'] == config['new_archive_sha256'] and archive['tree_sha256'] == identity['tree_sha256']
    assert gate.digest(args.footer_contract) == args.footer_contract_sha256
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    argv = [sys.executable, BASE / 'cursor-capture.py', '--source', config['source'],
            '--binary', package / 'bin/codex', '--binary-source-commit', config['source_commit'],
            '--footer-contract', args.footer_contract, '--evidence', args.evidence]
    if args.setup_only:
        argv.append('--setup-only')
    with args.evidence.with_suffix('.log').open('x') as output:
        result = owned_run.run(argv, cwd=BASE, env=env, stdout=output, timeout=240)
    assert result.returncode == 0, 'preserve cursor capture failure evidence'
    if not args.setup_only:
        with args.evidence.with_suffix('.integrity.log').open('x') as output:
            result = owned_run.run([sys.executable, BASE / 'cursor-verify.py', '--evidence', args.evidence],
                                   cwd=BASE, env=env, stdout=output, timeout=30)
        assert result.returncode == 0
        manifest = json.loads((args.evidence / 'manifest.json').read_text())
        assert manifest['candidate_acceptance'] and manifest['binary_sha256'] == identity['manifest']['files']['bin/codex']['sha256']
        assert manifest['code_mode_host']['sha256'] == identity['manifest']['files']['bin/codex-code-mode-host']['sha256']
        binding = dict(accepted=True, native=True, archive_sha256=config['new_archive_sha256'],
            manifest_sha256=config['new_manifest_sha256'], source=config['source_commit'],
            qualification_evidence_sha256=config['qualification_evidence_sha256'],
            capture_manifest_sha256=gate.digest(args.evidence / 'manifest.json'),
            integrity_sha256=gate.digest(args.evidence / 'integrity.json'))
        (args.evidence / 'package-binding.json').write_text(json.dumps(binding, indent=2) + '\n')
    assert gate.package_identity(package, NEW_VERSION, config['source_commit'], config['new_manifest_sha256']) == identity
    print(json.dumps(dict(passed=True, setup_only=args.setup_only, evidence=str(args.evidence))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--footer-contract', type=Path, required=True)
    parser.add_argument('--footer-contract-sha256', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--setup-only', action='store_true')
    run(parser.parse_args())
