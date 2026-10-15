#!/usr/bin/env python3
"""Package stripped native outputs using the pinned upstream canonical builder.

Tree hash v1: enumerate every regular file except codex-plus.json by relative
POSIX path. Each value is {"sha256": lowercase hex, "mode": four octal digits,
"size": integer bytes}. Hash the ASCII bytes of json.dumps(files,
sort_keys=True, separators=(",", ":"), ensure_ascii=True) with SHA-256.
Symlinks and special files are rejected. File times and directory modes are not
part of the tree hash. Archive metadata is normalized; this does not claim that
independent native compilations are bit reproducible.
"""

import argparse
import gzip
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = '092d3acd6bec3e3a14bdc7e7a2810ab628ab759d'
UPSTREAM_VERSION = '0.162.1'
RELEASE = '0.162.1-plus.4'
PATCH = 'patches/codex-0.162.1-worker-hud.patch'
TARGETS = {'x86_64-unknown-linux-gnu': 62, 'aarch64-unknown-linux-gnu': 183}
HELPERS = tuple('codex_plus/' + name for name in
                ('__init__.py', 'background.py', 'evidence.py', 'ledger.py', 'rpc.py', 'runner.py'))
DOCS = ('README.md', 'GLOSSARY.md', 'docs/background-work.md',
        'docs/shell-workflow.md', 'docs/verification.md', 'docs/release-notes.md',
        'docs/releasing.md')
SOURCE_FILES = HELPERS + DOCS + ('bin/codex-plus', 'LICENSE', PATCH,
                               'scripts/package-release.py', 'scripts/build-release.sh')
EXECUTABLES = ('bin/codex', 'bin/codex-code-mode-host', 'codex-resources/bwrap',
               'codex-resources/zsh/bin/zsh', 'codex-path/rg')
# Licenses are pinned independently because the binary archives omit them.
LICENSE_DOWNLOADS = {
    'ripgrep-LICENSE-MIT': ('BurntSushi/ripgrep/15.2.0/LICENSE-MIT',
                          '0f96a83840e146e43c0ec96a22ec1f392e0680e6c1226e6f3ba87e0740af850f'),
    'ripgrep-LICENSE-UNLICENSE': ('BurntSushi/ripgrep/15.2.0/UNLICENSE',
                                '7e12e5df4bae12cb21581ba157ced20e1986a0508dd10d0e8a4ab9a4cf94e85c'),
    'zsh-LICENCE': ('zsh-users/zsh/77045ef899e53b9598bebc5a41db93a548a40ca6/LICENCE',
                    'd06fdf3ef9b1ec69d6b9e170b0a9516fbad3523261ff1668bde3bfea6e0ef5f5'),
    'rusty-v8-LICENSE': ('denoland/rusty_v8/v150.4.0/LICENSE',
                         '1c6356fb751d45f0c53093ebf8a7f5e580e802f51999178e19d60f3ec39e147d'),
    'v8-LICENSE': ('v8/v8/15.0.245.2/LICENSE',
                   '6ab33af8774a0f396ee3aeeb761e3229057682d6f9fa7f572e390c2cb3a6e509'),
    'v8-LICENSE.v8': ('v8/v8/15.0.245.2/LICENSE.v8',
                      '4af93c12062c58058378de2397dc1c92bbff9ddfb1d583a01c84127557ce97ca'),
}
LICENSE_FILES = tuple('licenses/' + name for name in LICENSE_DOWNLOADS) + (
    'LICENSE', 'NOTICE', 'licenses/bubblewrap-COPYING',
    'licenses/codex-plus-LICENSE', 'licenses/provenance.json',
    'licenses/rusty-v8-release-manifests.sha256')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    path.chmod(0o644)


def tree_files(package):
    files = {}
    for path in sorted(package.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unsupported package entry: {path.relative_to(package)}')
        if path.is_file() and path != package / 'codex-plus.json':
            files[path.relative_to(package).as_posix()] = {
                'sha256': sha256(path), 'mode': f'{stat.S_IMODE(path.stat().st_mode):04o}',
                'size': path.stat().st_size,
            }
    return files


def tree_sha256(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('ascii')).hexdigest()


def validate_elf(path, target):
    with path.open('rb') as stream:
        header = stream.read(20)
    if (len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or
            int.from_bytes(header[18:20], 'little') != TARGETS[target]):
        raise ValueError(f'Wrong architecture or invalid ELF for {target}: {path.name}')


def validate_layout(package, target):
    metadata = json.loads((package / 'codex-package.json').read_text())
    expected = {'layoutVersion': 1, 'version': UPSTREAM_VERSION, 'target': target,
                'variant': 'codex', 'entrypoint': 'bin/codex',
                'resourcesDir': 'codex-resources', 'pathDir': 'codex-path'}
    if metadata != expected:
        raise ValueError('Invalid canonical codex-package.json')
    for name in EXECUTABLES + ('bin/codex-plus',) + HELPERS + DOCS + LICENSE_FILES:
        path = package / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing regular package file: {name}')
    for name in EXECUTABLES + ('bin/codex-plus',):
        if stat.S_IMODE((package / name).stat().st_mode) != 0o755:
            raise ValueError(f'Wrong executable mode: {name}')
    for name in EXECUTABLES:
        validate_elf(package / name, target)
    if any(any(part.startswith('voice') for part in path.relative_to(package).parts)
           for path in package.rglob('*')):
        raise ValueError('Voice must be omitted from this release')
    digest = sha256(package / 'codex-resources/bwrap').encode('ascii')
    with (package / 'bin/codex').open('rb') as stream:
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as binary:
            if binary.find(digest) < 0:
                raise ValueError('CLI does not contain the packaged bwrap SHA-256')


def verify_package(package):
    manifest_path = package / 'codex-plus.json'
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError('Missing regular codex-plus.json')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('voice') is not False:
        raise ValueError('Manifest must explicitly declare voice=false')
    if (manifest.get('schema_version') != 1 or manifest.get('release') != RELEASE or
            manifest.get('target') not in TARGETS or
            manifest.get('upstream') != {'version': UPSTREAM_VERSION, 'tag': 'rust-v0.162.1',
                                          'commit': UPSTREAM_COMMIT}):
        raise ValueError('Invalid release identity')
    if not re.fullmatch(r'[0-9a-f]{40}', manifest.get('plus_commit', '')):
        raise ValueError('Invalid Plus source commit')
    if not re.fullmatch(r'[0-9a-f]{64}', manifest.get('frontend_patch_sha256', '')):
        raise ValueError('Invalid frontend patch hash')
    build = manifest.get('build', {})
    if (build.get('rust') != '1.99.0' or build.get('cargo_profile') != 'release' or
            not re.fullmatch(r'\d+\.\d+', build.get('glibc', ''))):
        raise ValueError('Invalid build provenance')
    actual = tree_files(package)
    if actual != manifest.get('files'):
        raise ValueError('Package file inventory, hash, size, or mode mismatch')
    if tree_sha256(actual) != manifest.get('tree_sha256'):
        raise ValueError('Package tree SHA-256 mismatch')
    validate_layout(package, manifest['target'])
    return manifest


def git(root, *args, env=None):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, env=env).stdout


def verify_sources(source, plus_commit):
    if not re.fullmatch(r'[0-9a-f]{40}', plus_commit):
        raise ValueError('--plus-commit must be the full source snapshot SHA')
    commit = git(ROOT, 'rev-parse', '--verify', plus_commit + '^{commit}').decode().strip()
    if commit != plus_commit:
        raise ValueError('--plus-commit must be the full source snapshot SHA')
    for name in SOURCE_FILES:
        if (ROOT / name).read_bytes() != git(ROOT, 'show', f'{commit}:{name}'):
            raise ValueError(f'Source differs from Plus snapshot: {name}')
    try:
        git(ROOT, 'diff', '--quiet', commit, '--', *SOURCE_FILES)
    except subprocess.CalledProcessError as exc:
        raise ValueError('Source modes differ from Plus snapshot') from exc
    if git(source, 'rev-parse', 'HEAD').decode().strip() != UPSTREAM_COMMIT:
        raise ValueError('Unexpected upstream source revision')
    with tempfile.TemporaryDirectory(prefix='codex-plus-index-') as temp:
        env = os.environ | {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git(source, 'read-tree', UPSTREAM_COMMIT, env=env)
        git(source, 'apply', '--cached', str(ROOT / PATCH), env=env)
        try:
            git(source, 'diff', '--quiet', '--', env=env)
        except subprocess.CalledProcessError as exc:
            raise ValueError('Source differs from the exact frontend release patch') from exc
        if git(source, 'ls-files', '--others', '--exclude-standard', env=env).strip():
            raise ValueError('Unexpected untracked upstream source files')


def copy_file(source, dest, mode=0o644):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    dest.chmod(mode)


def add_licenses(source, package, cache):
    provenance = {}
    for name, (suffix, expected) in LICENSE_DOWNLOADS.items():
        url = 'https://raw.githubusercontent.com/' + suffix
        cached = cache / name
        if not cached.is_file() or sha256(cached) != expected:
            cache.mkdir(parents=True, exist_ok=True)
            with urlopen(url, timeout=60) as response:
                data = response.read()
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f'License checksum mismatch: {name}')
            cached.write_bytes(data)
        copy_file(cached, package / 'licenses' / name)
        provenance[name] = {'url': url, 'sha256': expected}
    for name, origin in {'LICENSE': 'LICENSE', 'NOTICE': 'NOTICE',
                         'licenses/bubblewrap-COPYING': 'codex-rs/vendor/bubblewrap/COPYING',
                         'licenses/rusty-v8-release-manifests.sha256':
                         'third_party/v8/rusty_v8_150_4_0_release_manifests.sha256'}.items():
        copy_file(source / origin, package / name)
        provenance[name] = {'upstream_path': origin, 'upstream_commit': UPSTREAM_COMMIT,
                            'sha256': sha256(source / origin)}
    copy_file(ROOT / 'LICENSE', package / 'licenses/codex-plus-LICENSE')
    write_json(package / 'licenses/provenance.json', provenance)


def resource_provenance(source, target):
    # Use the pinned builder's parser and target selection, as for downloads.
    os.environ['CODEX_REPO_ROOT'] = str(source)
    sys.path.insert(0, str(source / 'scripts'))
    from codex_package.dotslash import artifact_for_target
    from codex_package.targets import TARGET_SPECS
    result = {}
    for name in ('rg', 'codex-zsh'):
        relative = f'scripts/codex_package/{name}'
        artifact = artifact_for_target(TARGET_SPECS[target], source / relative,
                                       artifact_label=name)
        result[name] = {'manifest': relative, 'manifest_sha256': sha256(source / relative),
                        'archive_url': artifact.url, 'archive_sha256': artifact.digest,
                        'archive_size': artifact.size, 'archive_member': artifact.archive_member}
    result['rusty-v8'] = {'crate_version': '150.4.0', 'v8_version': '15.0.245.2',
                          'release': 'rusty-v8-v150.4.0',
                          'checksum_manifest': 'licenses/rusty-v8-release-manifests.sha256'}
    result['bwrap'] = {'upstream_path': 'codex-rs/vendor/bubblewrap',
                       'upstream_commit': UPSTREAM_COMMIT, 'license': 'licenses/bubblewrap-COPYING'}
    return result


def write_archive(package, archive):
    with archive.open('wb') as output:
        with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as tar:
                for path in [package, *sorted(package.rglob('*'))]:
                    name = package.name + ('/' + path.relative_to(package).as_posix()
                                           if path != package else '')
                    info = tar.gettarinfo(str(path), arcname=name)
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    info.pax_headers = {}
                    if path.is_file():
                        with path.open('rb') as stream:
                            tar.addfile(info, stream)
                    else:
                        info.mode = 0o755
                        tar.addfile(info)
    archive.with_suffix(archive.suffix + '.sha256').write_text(
        sha256(archive) + '  ' + archive.name + '\n')


def build(args):
    source = args.source_dir.resolve()
    native = args.native_dir.resolve()
    verify_sources(source, args.plus_commit)
    if not re.fullmatch(r'\d+\.\d+', args.build_glibc):
        raise ValueError('--build-glibc must record the builder version, e.g. 2.39')
    for name in ('codex', 'codex-code-mode-host', 'bwrap'):
        validate_elf(native / name, args.target)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    package = output / f'codex-plus-{RELEASE}-{args.target}'
    archive = output / (package.name + '.tar.gz')
    if package.exists() or archive.exists() or archive.with_suffix('.gz.sha256').exists():
        raise ValueError('Release output already exists; use a new output directory')
    # Private staging prevents a failed download/verification from leaving a release.
    with tempfile.TemporaryDirectory(prefix='.package-', dir=output) as temp:
        stage = Path(temp) / package.name
        subprocess.run([sys.executable, str(source / 'scripts/build_codex_package.py'),
                        '--target', args.target, '--variant', 'codex',
                        '--package-version', UPSTREAM_VERSION, '--cargo-profile', 'release',
                        '--entrypoint-bin', str(native / 'codex'),
                        '--code-mode-host-bin', str(native / 'codex-code-mode-host'),
                        '--bwrap-bin', str(native / 'bwrap'), '--package-dir', str(stage)],
                       check=True, env=os.environ | {'CODEX_REPO_ROOT': str(source)})
        for name in HELPERS + DOCS:
            copy_file(ROOT / name, stage / name)
        copy_file(ROOT / 'bin/codex-plus', stage / 'bin/codex-plus', 0o755)
        add_licenses(source, stage, output / 'license-cache')
        (stage / 'codex-package.json').chmod(0o644)
        for name in EXECUTABLES:
            (stage / name).chmod(0o755)
        manifest = {
            'schema_version': 1, 'release': RELEASE, 'plus_commit': args.plus_commit,
            'upstream': {'version': UPSTREAM_VERSION, 'tag': 'rust-v0.162.1',
                         'commit': UPSTREAM_COMMIT},
            'frontend_patch_sha256': sha256(ROOT / PATCH), 'target': args.target,
            'build': {'glibc': args.build_glibc, 'rust': '1.99.0', 'cargo_profile': 'release'},
            'voice': False, 'resources': resource_provenance(source, args.target),
            'files': tree_files(stage),
        }
        manifest['tree_sha256'] = tree_sha256(manifest['files'])
        write_json(stage / 'codex-plus.json', manifest)
        verify_package(stage)
        staged_archive = Path(temp) / archive.name
        write_archive(stage, staged_archive)
        stage.rename(package)
        staged_archive.rename(archive)
        staged_archive.with_suffix('.gz.sha256').rename(archive.with_suffix('.gz.sha256'))
    print(f'Built {archive.name}; tree SHA-256 {manifest["tree_sha256"]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path, metavar='PACKAGE_DIR')
    parser.add_argument('--source-dir', type=Path)
    parser.add_argument('--native-dir', type=Path)
    parser.add_argument('--target', choices=TARGETS)
    parser.add_argument('--build-glibc')
    parser.add_argument('--plus-commit')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.build/dual-release')
    args = parser.parse_args()
    try:
        if args.verify:
            manifest = verify_package(args.verify)
            print(f'Verified {args.verify.name}; tree SHA-256 {manifest["tree_sha256"]}')
        else:
            for name in ('source_dir', 'native_dir', 'target', 'build_glibc', 'plus_commit'):
                if getattr(args, name) is None:
                    parser.error('--' + name.replace('_', '-') + ' is required')
            build(args)
    except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f'Release packaging failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
