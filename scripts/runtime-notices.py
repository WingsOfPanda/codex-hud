#!/usr/bin/env python3
"""Generate shipped native dependency notices from locked, offline Cargo graphs."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.request import urlopen


RUST = '1.99.1'
NOTICES = 'codex-plus-runtime-NOTICES.txt'
DEPENDENCIES = 'codex-plus-runtime-dependencies.json'
RUST_NOTICES = 'codex-plus-runtime-rust-COPYRIGHT.html'
NATIVE_NOTICES = 'codex-native-NOTICES.txt'
NATIVE_DEPENDENCIES = 'codex-native-dependencies.json'
NATIVE_ROOTS = ['codex-cli', 'codex-code-mode-host']
LICENSE_SOURCES = Path(__file__).with_name('native-license-sources.json')
SPDX_REVISION = '31ba1a50e5397e00a304dbadc76531740e89ee48'
SPDX_LICENSES = {
    'MIT': 'b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5',
    'Apache-2.0': '074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff',
    'CC-BY-3.0-US': 'df36d89de58066f49fa9b81f4290ea409adbbdd9d69c620359efbdc8bccaac3c',
}


BZIP2_TERMS = (
    'redistribution and use in source and binary forms, with or without modification, are '
    'permitted provided that the following conditions are met: 1. redistributions of source '
    'code must retain the above copyright notice, this list of conditions and the following '
    'disclaimer. 2. the origin of this software must not be misrepresented; you must not '
    'claim that you wrote the original software. if you use this software in a product, an '
    'acknowledgment in the product documentation would be appreciated but is not required. 3. '
    'altered source versions must be plainly marked as such, and must not be misrepresented '
    'as being the original software. 4. the name of the author may not be used to endorse or '
    'promote products derived from this software without specific prior written permission. '
    "this software is provided by the author ``as is'' and any express or implied warranties, "
    'including, but not limited to, the implied warranties of merchantability and fitness for '
    'a particular purpose are disclaimed. in no event shall the author be liable for any '
    'direct, indirect, incidental, special, exemplary, or consequential damages (including, '
    'but not limited to, procurement of substitute goods or services; loss of use, data, or '
    'profits; or business interruption) however caused and on any theory of liability, '
    'whether in contract, strict liability, or tort (including negligence or otherwise) '
    'arising in any way out of the use of this software, even if advised of the possibility '
    'of such damage.'
)


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
        'BSD-3-Clause': 'redistribution and use in source and binary forms' in text,
        'ISC': 'permission to use, copy, modify, and' in text,
        'MPL-2.0': 'mozilla public license' in text and '2.0' in text,
        'CC0-1.0': 'creative commons' in text and 'universal' in text,
        'CDLA-Permissive-2.0': 'community data license agreement' in text and '2.0' in text,
        'Unicode-3.0': 'unicode license v3' in text,
        'Unlicense': 'free and unencumbered software released into the public domain' in text,
        'Zlib': 'origin of this software' in text and 'freely' in text,
        'bzip2-1.0.6': BZIP2_TERMS in text,
        'LLVM-exception': 'llvm exception' in text,
    }
    return expression_covered(expression, present)


def expression_covered(expression, present):
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


def pinned_bytes(entry, cache):
    encoding = entry.get('encoding', 'raw')
    if encoding not in ('raw', 'base64'):
        raise ValueError(f'Unsupported supplemental encoding: {encoding}')
    path = cache / entry['sha256']
    data = path.read_bytes() if path.is_file() else None
    if data is None or digest(data) != entry['sha256']:
        with urlopen(entry['url'], timeout=60) as response:
            data = response.read()
        if encoding == 'base64':
            data = base64.b64decode(b''.join(data.split()), validate=True)
        if digest(data) != entry['sha256']:
            raise ValueError(f'Supplemental license checksum mismatch: {entry["url"]}')
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return data


def declaration_evidence(package, supplement, cache):
    """Accept only curated declarations with independent full-body/component gates."""
    if supplement['basis'] != 'published-license-declaration':
        raise ValueError('Unsupported supplemental license basis')
    declared, selected = supplement['declared_license'], supplement['selected_license']
    if declared != package['license'] or not expression_covered(declared, {selected: True}):
        raise ValueError(f'Invalid declared/selected license for {package["name"]}')
    entries = {entry['path']: entry for entry in supplement['files']}
    if len(entries) != len(supplement['files']):
        raise ValueError('Duplicate curated supplemental path')
    for entry in entries.values():
        if entry['kind'] == 'canonical-license':
            identifier = entry['license']
            url = (f'https://raw.githubusercontent.com/spdx/license-list-data/{SPDX_REVISION}'
                   f'/text/{identifier}.txt')
            if (identifier not in SPDX_LICENSES or entry['revision'] != SPDX_REVISION or
                    entry['url'] != url or entry['sha256'] != SPDX_LICENSES[identifier]):
                raise ValueError(f'Invalid canonical license identity: {entry["path"]}')
    body = entries.get(supplement['selected_license_file'])
    if not body or body.get('kind') != 'canonical-license' or body.get('license') != selected:
        raise ValueError(f'Missing canonical selected license body for {package["name"]}')
    required = supplement['required_files']
    if not required or {ref['path'] for ref in required} != entries.keys():
        raise ValueError('Missing required supplemental file references')
    for ref in required:
        entry = entries.get(ref['path'], {})
        if any(entry.get(key) != value for key, value in ref.items()):
            raise ValueError(f'Missing or invalid required supplemental file: {ref["path"]}')
        if entry['kind'] not in ('canonical-license', 'source-evidence', 'retained-source',
                                 'component-grant', 'component-notices',
                                 'component-attribution', 'embedded-notice'):
            raise ValueError(f'Invalid required supplemental kind: {entry["kind"]}')
    archive = supplement['archive']
    archive_url = (f'https://static.crates.io/crates/{package["name"]}/'
                   f'{package["name"]}-{package["version"]}.crate')
    if archive['sha256'] != supplement['checksum'] or archive['url'] != archive_url:
        raise ValueError(f'Invalid curated source archive for {package["name"]}')
    root = Path(package['manifest_path']).parent
    evidence = supplement['source_evidence']
    if not {'Cargo.toml', 'Cargo.toml.orig', 'README.md'} <= evidence.keys():
        raise ValueError('Missing curated local declaration evidence')
    local = {}
    for name, origin in evidence.items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f'Curated local source mismatch: {package["name"]}/{name}')
        data = path.read_bytes()
        if digest(data) != origin['sha256']:
            raise ValueError(f'Curated local source mismatch: {package["name"]}/{name}')
        local[name] = data
    for name in ('Cargo.toml', 'Cargo.toml.orig'):
        manifest = tomllib.loads(local[name].decode('utf-8'))['package']
        if (manifest.get('license') != declared or manifest['name'] != package['name'] or
                manifest['version'] != package['version'] or
                manifest.get('authors', []) != supplement['authors_metadata']):
            raise ValueError(f'Curated source declaration mismatch: {package["name"]}/{name}')
    if package.get('authors', []) != supplement['authors_metadata']:
        raise ValueError(f'Curated authors metadata mismatch: {package["name"]}')
    for entry in entries.values():
        for binding in entry.get('source_bindings', []):
            if binding not in local:
                raise ValueError(f'Missing required supplemental source binding: {binding}')
        if entry.get('local_path') and (entry['local_path'] not in local or
                digest(local[entry['local_path']]) != entry['sha256']):
            raise ValueError(f'Invalid required supplemental local file: {entry["path"]}')
        if entry['kind'] == 'embedded-notice' and (
                not entry.get('source_bindings') or
                not all(entry['text'].encode('utf-8') in local[binding]
                        for binding in entry['source_bindings'])):
            raise ValueError('Embedded supplemental notice does not match its source')
    pinned_bytes(archive, cache)
    return local


def selected_packages(metadata, roots=None):
    nodes = {node['id']: node for node in metadata['resolve']['nodes']}
    selected, pending = set(), list(roots or [metadata['resolve']['root']])
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        selected.add(identity)
        pending.extend(dep['pkg'] for dep in nodes[identity]['deps']
                       if any(kind['kind'] != 'dev' for kind in dep['dep_kinds']))
    return sorted((package for package in metadata['packages'] if package['id'] in selected),
                  key=lambda package: (package['name'], package['version'], package['source'] or ''))


def license_files(package, project_root):
    root = Path(package['manifest_path']).parent
    if not (root / 'Cargo.toml').is_file():
        raise ValueError(f'Missing cached source for {package["name"]} {package["version"]}')
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
    # Workspace crates inherit their repository's license and notice files.
    boundary = root
    if package['source'] is None and root.is_relative_to(project_root):
        boundary = project_root
    elif (package['source'] or '').startswith('git+'):
        boundary = next((parent for parent in (root, *root.parents)
                         if (parent / '.git').exists()), root)
    if boundary != root:
        for parent in root.parents:
            if not parent.is_relative_to(boundary):
                break
            for path in parent.iterdir():
                if path.is_file() and path.name.upper().startswith(
                        ('LICENSE', 'LICENCE', 'COPYING', 'COPYRIGHT', 'NOTICE', 'UNLICENSE')):
                    files[Path(os.path.relpath(path, root)).as_posix()] = path
                elif path.is_dir() and path.name.upper() in ('LICENSES', 'LICENCES'):
                    for inherited in path.rglob('*'):
                        if inherited.is_file():
                            files[Path(os.path.relpath(inherited, root)).as_posix()] = inherited
    for name, path in files.items():
        if not path.resolve().is_relative_to(boundary.resolve()) or not path.is_file():
            raise ValueError(f'Invalid license file for {package["name"]}: {name}')
    return sorted(files.items())


def collect(root, target, metadata, rust_version, rust_copyright, *, manifest=None,
            roots=None, selected=None, features=None, supplemental=None, license_cache=None):
    manifest = manifest or root / 'rust-runtime/Cargo.toml'
    lock_path = manifest.parent / 'Cargo.lock'
    lock_bytes = lock_path.read_bytes()
    locked = {(package['name'], package['version'], package.get('source')): package.get('checksum')
              for package in tomllib.loads(lock_bytes.decode())['package']}
    records, bundle, missing = [], ['Codex native third-party notices\n'], []
    packages = (selected_packages(metadata) if selected is None else
                sorted((p for p in metadata['packages'] if p['id'] in selected),
                       key=lambda p: (p['name'], p['version'], p['source'] or '')))
    for package in packages:
        identity = (package['name'], package['version'], package.get('source'))
        if identity not in locked:
            raise ValueError(f'Package absent from Cargo.lock: {identity[0]} {identity[1]}')
        source = package['source'] or 'path+' + Path(package['manifest_path']).parent.relative_to(root).as_posix()
        files = license_files(package, root)
        original = [(name, path.read_bytes()) for name, path in files]
        texts, redirects = [], {}
        supplement = (supplemental or {}).get(identity[0] + '@' + identity[1])
        curated = bool(supplement and 'basis' in supplement)
        if supplement:
            if supplement['checksum'] != locked[identity] or supplement['source'] != package['source']:
                raise ValueError(f'Supplemental license source mismatch: {identity[0]} {identity[1]}')
            local = declaration_evidence(package, supplement, license_cache) if curated else {}
            for entry in supplement['files']:
                if curated and 'local_path' in entry:
                    data = local[entry['local_path']]
                elif curated and 'text' in entry:
                    data = entry['text'].encode('utf-8')
                else:
                    data = pinned_bytes(entry, license_cache)
                if digest(data) != entry['sha256']:
                    raise ValueError(f'Supplemental license checksum mismatch: {entry["path"]}')
                name = 'supplemental/' + entry['path']
                original.append((name, data))
                redirects[name] = entry
        for name, data in original:
            pointer = data.decode('utf-8').strip()
            if re.fullmatch(r'(\.\./)+(?:LICENSE|LICENCE|COPYING|NOTICE)[A-Za-z0-9._-]*', pointer):
                supplemental_texts = [(other_name, value) for other_name, value in original
                                      if other_name.startswith('supplemental/') and
                                      Path(other_name).name == Path(pointer).name and value != data]
                if supplemental_texts and len({digest(value) for _, value in supplemental_texts}) == 1:
                    other_name, data = supplemental_texts[0]
                    redirects[name] = redirects[other_name]
                    texts.append((name, data))
                    continue
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
        if not texts or (not curated and expression and not license_covered(expression,
                                                           [data.decode('utf-8') for _, data in texts])):
            missing.append(f'{identity[0]} {identity[1]} ({expression})')
            continue
        record = {'name': identity[0], 'version': identity[1], 'source': source,
                  'license': expression, 'checksum': locked[identity],
                  'manifest_sha256': digest(Path(package['manifest_path']).read_bytes()),
                  'license_files': {name: digest(data) for name, data in original},
                  'license_text_sources': redirects}
        if curated:
            record['license_resolution'] = {key: value for key, value in supplement.items()
                                            if key != 'files'}
        if features is not None:
            record['features'] = sorted(features[package['id']])
        records.append(record)
        bundle.append(f'\n=== {identity[0]} {identity[1]} ({source}) ===\nLicense: {expression}\n')
        if curated:
            bundle.append(f'Selected license: {supplement["selected_license"]}\n'
                          'Basis: published Cargo license declaration.\n'
                          'Authors metadata (not a copyright notice): ' +
                          json.dumps(supplement['authors_metadata'], ensure_ascii=False) + '\n'
                          'Canonical standard terms are not publisher-authored license files; '
                          'template placeholders are preserved without inferred authors or years.\n')
        for name, data in texts:
            origin = redirects.get(name, {})
            label = ''
            if curated and origin.get('kind'):
                label = f'Kind: {origin["kind"]}\n'
                if origin['kind'] == 'canonical-license':
                    label += f'CANONICAL SPDX STANDARD LICENSE TERMS: {origin["license"]} (v3.29.0)\n'
                if origin.get('url'):
                    label += f'Source/provenance: {origin["url"]}\n'
            bundle.append(f'\n--- {name} ---\n' + label + data.decode('utf-8').rstrip('\n') + '\n')
    if missing:
        raise ValueError('Missing applicable license text for ' + '; '.join(missing))
    sources = [manifest, lock_path]
    if roots is None:
        sources += sorted((manifest.parent / 'src').rglob('*.rs'))
    else:
        sources += [Path(p['manifest_path']) for p in packages if p['source'] is None]
    source_files = {path.relative_to(root).as_posix(): digest(path.read_bytes()) for path in sources}
    provenance = {'schema_version': 1, 'target': target,
                  'toolchain': {'rust': RUST, 'rustc': rust_version,
                                'library_notices': RUST_NOTICES,
                                'library_notices_sha256': digest(rust_copyright)},
                  'cargo_lock_sha256': digest(lock_bytes), 'source_files': source_files,
                  'source_sha256': digest(canonical(source_files)), 'packages': records}
    if roots is not None:
        provenance['roots'] = roots
        provenance['build_features'] = {'default_features': True, 'features': []}
    return ''.join(bundle).encode('utf-8'), provenance


def command(*args):
    try:
        return subprocess.run(args, check=True, capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'{args[0]} failed: {exc.stderr.decode().strip()}') from exc


def cargo_graph(manifest, target, roots):
    common = ['--locked', '--offline', '--manifest-path', str(manifest)]
    metadata = json.loads(command('cargo', '+' + RUST, 'metadata', *common,
        '--format-version', '1', '--filter-platform', target))
    tree = command('cargo', '+' + RUST, 'tree', *common, '--target', target,
                   '--edges', 'normal,build', '--prefix', 'none', '--format', '{p}|{f}',
                   *[arg for name in roots for arg in ('-p', name)]).decode()
    by_identity = {}
    for package in metadata['packages']:
        by_identity.setdefault((package['name'], 'v' + package['version']), []).append(package)
    selected, features = set(), {}
    for line in tree.splitlines():
        if not line.strip():
            continue
        description, _, enabled = line.removesuffix(' (*)').partition('|')
        name, version, *_ = description.split()
        candidates = by_identity.get((name, version), [])
        if len(candidates) != 1:
            raise ValueError(f'Ambiguous or missing Cargo tree source: {description}')
        identity = candidates[0]['id']
        selected.add(identity)
        features.setdefault(identity, set()).update(filter(None, enabled.split(',')))
    if not all(any(p['name'] == name and p['id'] in selected for p in metadata['packages'])
               for name in roots):
        raise ValueError('Missing production graph root')
    return metadata, selected, features


def generate(root, target, output, upstream_source=None, license_cache=None):
    rust_version = command('rustc', '+' + RUST, '--version').decode().strip()
    if not rust_version.startswith('rustc ' + RUST + ' '):
        raise ValueError('Unexpected runtime toolchain: ' + rust_version)
    sysroot = Path(command('rustc', '+' + RUST, '--print', 'sysroot').decode().strip())
    rust_copyright = (sysroot / 'share/doc/rust/COPYRIGHT-library.html').read_bytes()
    metadata, selected, features = cargo_graph(root / 'rust-runtime/Cargo.toml', target,
                                              ['codex-plus-runtime'])
    text, provenance = collect(root, target, metadata, rust_version, rust_copyright,
                               selected=selected, features=features)
    outputs = [(NOTICES, text), (RUST_NOTICES, rust_copyright), (DEPENDENCIES, provenance)]
    if upstream_source is not None:
        manifest = upstream_source / 'codex-rs/Cargo.toml'
        metadata, selected, features = cargo_graph(manifest, target, NATIVE_ROOTS)
        native_text, native_provenance = collect(
            upstream_source, target, metadata, rust_version, rust_copyright,
            manifest=manifest, roots=NATIVE_ROOTS, selected=selected, features=features,
            supplemental=json.loads(LICENSE_SOURCES.read_text()),
            license_cache=license_cache or output.parent / 'license-cache')
        outputs += [(NATIVE_NOTICES, native_text), (NATIVE_DEPENDENCIES, native_provenance)]
    output.mkdir(parents=True, exist_ok=True)
    for name, data in outputs:
        if isinstance(data, dict):
            data = (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + '\n').encode('ascii')
        path = output / name
        path.write_bytes(data)
        path.chmod(0o644)
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--upstream-source', type=Path)
    parser.add_argument('--license-cache', type=Path)
    args = parser.parse_args()
    try:
        provenance = generate(Path(__file__).resolve().parents[1], args.target, args.output_dir,
                              args.upstream_source.resolve() if args.upstream_source else None,
                              args.license_cache)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Runtime notices failed: {exc}\n')
    print(f'Runtime notices: {len(provenance["packages"])} locked packages for {args.target}')


if __name__ == '__main__':
    main()
