#!/usr/bin/env python3
"""Generate runtime dependency notices from the locked, offline Cargo graph."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib


RUST = '1.99.0'
NOTICES = 'codex-plus-runtime-NOTICES.txt'
DEPENDENCIES = 'codex-plus-runtime-dependencies.json'
RUST_NOTICES = 'codex-plus-runtime-rust-COPYRIGHT.html'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def license_covered(expression, contents):
    """Require one complete SPDX alternative, including AND/WITH obligations."""
    text = re.sub(r'\s+', ' ', '\n'.join(contents)).lower()
    present = {
        'MIT': 'permission is hereby granted, free of charge' in text,
        'Apache-2.0': 'apache license' in text and 'version 2.0' in text,
        'BSD-2-Clause': 'redistribution and use in source and binary forms' in text,
        'Unicode-3.0': 'unicode license v3' in text,
        'Unlicense': 'free and unencumbered software released into the public domain' in text,
        'Zlib': 'origin of this software' in text and 'freely' in text,
        'LLVM-exception': 'llvm exception' in text,
    }
    tokens = iter(re.findall(r'[A-Za-z0-9.+-]+|[()]', expression.replace('/', ' OR ')))
    token = next(tokens, None)

    def atom():
        nonlocal token
        if token == '(':
            token = next(tokens, None)
            value = alternative()
            if token != ')':
                raise ValueError('Invalid SPDX license expression')
        elif token not in (None, ')', 'AND', 'OR', 'WITH'):
            value = present.get(token, False)
        else:
            raise ValueError('Invalid SPDX license expression')
        token = next(tokens, None)
        return value

    def conjunction():
        nonlocal token
        value = atom()
        while token in ('AND', 'WITH'):
            token = next(tokens, None)
            other = atom()
            value = value and other
        return value

    def alternative():
        nonlocal token
        value = conjunction()
        while token == 'OR':
            token = next(tokens, None)
            other = conjunction()
            value = value or other
        return value

    covered = alternative()
    if token is not None:
        raise ValueError('Invalid SPDX license expression')
    return covered


def selected_packages(metadata):
    nodes = {node['id']: node for node in metadata['resolve']['nodes']}
    selected, pending = set(), [metadata['resolve']['root']]
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        selected.add(identity)
        pending.extend(dep['pkg'] for dep in nodes[identity]['deps']
                       if any(kind['kind'] != 'dev' for kind in dep['dep_kinds']))
    return sorted((package for package in metadata['packages'] if package['id'] in selected),
                  key=lambda package: (package['name'], package['version'], package['source'] or ''))


def license_files(package, project_root, root_id):
    root = Path(package['manifest_path']).parent
    if package['id'] == root_id:
        return [('LICENSE', project_root / 'LICENSE')]
    files = {}
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if (path.is_file() and (path.name.upper().startswith(
                ('LICENSE', 'LICENCE', 'COPYING', 'COPYRIGHT', 'NOTICE', 'UNLICENSE'))
                or any(part.upper() in ('LICENSES', 'LICENCES') for part in relative.parts[:-1]))):
            files[relative.as_posix()] = path
    if package.get('license_file'):
        path = root / package['license_file']
        files[path.relative_to(root).as_posix()] = path
    for name, path in files.items():
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f'Invalid license file for {package["name"]}: {name}')
    return sorted(files.items())


def collect(root, target, metadata, rust_version, rust_copyright):
    lock_bytes = (root / 'rust-runtime/Cargo.lock').read_bytes()
    locked = {(package['name'], package['version'], package.get('source')): package.get('checksum')
              for package in tomllib.loads(lock_bytes.decode())['package']}
    records, bundle = [], ['Codex Plus runtime third-party notices\n']
    packages = selected_packages(metadata)
    for package in packages:
        identity = (package['name'], package['version'], package.get('source'))
        if identity not in locked:
            raise ValueError(f'Package absent from Cargo.lock: {identity[0]} {identity[1]}')
        source = package['source'] or 'path+codex-plus/rust-runtime'
        files = license_files(package, root, metadata['resolve']['root'])
        original = [(name, path.read_bytes()) for name, path in files]
        texts, redirects = [], {}
        for name, data in original:
            pointer = data.decode('utf-8').strip()
            if re.fullmatch(r'(\.\./)+(?:LICENSE|LICENCE|COPYING|NOTICE)[A-Za-z0-9._-]*', pointer):
                candidates = []
                for other in packages:
                    if (other.get('repository') and other['repository'] == package.get('repository')
                            and other['version'] == package['version']):
                        candidate = Path(other['manifest_path']).parent / Path(pointer).name
                        if candidate.is_file() and candidate.read_bytes() != data:
                            candidates.append((other, candidate, candidate.read_bytes()))
                if not candidates or len({digest(value) for _, _, value in candidates}) != 1:
                    raise ValueError(f'Unresolved license pointer for {identity[0]} {identity[1]}: {name}')
                other, candidate, data = candidates[0]
                redirects[name] = {'package': other['name'], 'version': other['version'],
                                   'source': other['source'], 'path': candidate.name,
                                   'sha256': digest(data)}
            texts.append((name, data))
        expression = package.get('license')
        if not texts or (expression and not license_covered(expression,
                                                           [data.decode('utf-8') for _, data in texts])):
            raise ValueError(f'Missing applicable license text for {identity[0]} {identity[1]} ({expression})')
        record = {'name': identity[0], 'version': identity[1], 'source': source,
                  'license': expression, 'checksum': locked[identity],
                  'manifest_sha256': digest(Path(package['manifest_path']).read_bytes()),
                  'license_files': {name: digest(data) for name, data in original},
                  'license_text_sources': redirects}
        records.append(record)
        bundle.append(f'\n=== {identity[0]} {identity[1]} ({source}) ===\nLicense: {expression}\n')
        for name, data in texts:
            bundle.append(f'\n--- {name} ---\n' + data.decode('utf-8').rstrip('\n') + '\n')
    crate = root / 'rust-runtime'
    sources = [crate / 'Cargo.toml', crate / 'Cargo.lock', *sorted((crate / 'src').rglob('*.rs'))]
    source_files = {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in sources}
    provenance = {'schema_version': 1, 'target': target,
                  'toolchain': {'rust': RUST, 'rustc': rust_version,
                                'library_notices': RUST_NOTICES,
                                'library_notices_sha256': digest(rust_copyright)},
                  'cargo_lock_sha256': digest(lock_bytes), 'source_files': source_files,
                  'source_sha256': digest(canonical(source_files)), 'packages': records}
    return ''.join(bundle).encode('utf-8'), provenance


def generate(root, target, output):
    def command(*args):
        return subprocess.run(args, check=True, capture_output=True).stdout
    metadata = json.loads(command('cargo', '+' + RUST, 'metadata', '--locked', '--offline',
        '--format-version', '1', '--filter-platform', target, '--manifest-path',
        str(root / 'rust-runtime/Cargo.toml')))
    rust_version = command('rustc', '+' + RUST, '--version').decode().strip()
    if not rust_version.startswith('rustc ' + RUST + ' '):
        raise ValueError('Unexpected runtime toolchain: ' + rust_version)
    sysroot = Path(command('rustc', '+' + RUST, '--print', 'sysroot').decode().strip())
    rust_copyright = (sysroot / 'share/doc/rust/COPYRIGHT-library.html').read_bytes()
    text, provenance = collect(root, target, metadata, rust_version, rust_copyright)
    output.mkdir(parents=True, exist_ok=True)
    for name, data in ((NOTICES, text), (RUST_NOTICES, rust_copyright),
                       (DEPENDENCIES, (json.dumps(provenance, sort_keys=True, indent=2,
                                                ensure_ascii=True) + '\n').encode('ascii'))):
        path = output / name
        path.write_bytes(data)
        path.chmod(0o644)
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        provenance = generate(Path(__file__).resolve().parents[1], args.target, args.output_dir)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Runtime notices failed: {exc}\n')
    print(f'Runtime notices: {len(provenance["packages"])} locked packages for {args.target}')


if __name__ == '__main__':
    main()
