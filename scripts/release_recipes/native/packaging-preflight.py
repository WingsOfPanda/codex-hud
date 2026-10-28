"""Exercise product packaging's pinned compiler and offline notices before building."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached(path, expected):
    if not path.is_file() or sha(path) != expected:
        raise ValueError(f'Missing or invalid offline license input: {path}')


def supplemental_inputs(supplements, cache):
    for supplement in supplements.values():
        for entry in supplement['files']:
            if not ('text' in entry or 'local_path' in entry):
                yield cache / entry['sha256'], entry['sha256']
        if 'basis' in supplement:
            archive = supplement['archive']
            yield cache / archive['sha256'], archive['sha256']


def check(source, upstream, target, cache, output):
    notices = module(source / 'scripts/runtime-notices.py')
    packager = module(source / 'scripts/package-release.py')
    version = os.environ['CODEX_PLUS_RELEASE_RUST_VERSION']
    if notices.RUST != version:
        raise ValueError('Product notice compiler differs from pinned release compiler')
    versions = {tool: notices.command(tool, '+' + version, '--version').decode().strip()
                for tool in ('rustc', 'cargo')}
    for tool, observed in versions.items():
        if not observed.startswith(tool + ' ' + version + ' '):
            raise ValueError(f'Unexpected packaging toolchain: {observed}')
    sysroot = Path(notices.command('rustc', '+' + version, '--print', 'sysroot').decode().strip())
    copyright = sysroot / 'share/doc/rust/COPYRIGHT-library.html'
    copyright_hash = sha(copyright)
    inputs = {cache / name: expected for name, (_, expected) in packager.LICENSE_DOWNLOADS.items()}
    inputs.update(supplemental_inputs(json.loads(notices.LICENSE_SOURCES.read_text()), cache))
    for path, expected in inputs.items():
        cached(path, expected)
    source_licenses = [source / 'LICENSE', *[upstream / name for name in
        ('LICENSE', 'NOTICE', 'codex-rs/vendor/bubblewrap/COPYING',
         'third_party/v8/rusty_v8_152_2_0_release_manifests.sha256')]]
    source_hashes = {str(path): sha(path) for path in source_licenses}
    notices.generate(source, target, output, upstream_source=upstream, license_cache=cache)
    native = json.loads((output / notices.NATIVE_DEPENDENCIES).read_text())
    helper = json.loads((output / notices.DEPENDENCIES).read_text())
    report = {'schema_version': 1, 'outcome': 'passed', 'target': target,
              'compiler_versions': versions, 'sysroot': str(sysroot),
              'library_copyright_sha256': copyright_hash,
              'product_scripts': {str(path): sha(path) for path in
                                  (source / 'scripts/package-release.py', source / 'scripts/runtime-notices.py')},
              'offline_license_inputs': {str(path): expected for path, expected in inputs.items()},
              'source_license_inputs': source_hashes,
              'packages': {'helper': len(helper['packages']), 'upstream': len(native['packages'])},
              'outputs': {path.name: sha(path) for path in sorted(output.iterdir()) if path.is_file()}}
    (output / 'preflight.json').write_text(json.dumps(report, sort_keys=True, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--upstream-source', type=Path, required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--license-cache', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.source, args.upstream_source, args.target, args.license_cache, args.output_dir)))


if __name__ == '__main__':
    main()
