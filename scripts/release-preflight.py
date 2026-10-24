#!/usr/bin/env python3
"""Check reviewed release inputs before expensive work; never builds or qualifies a release.

Invoke with the exact fixture Python interpreter/environment. JSON schema_version=1
selects imports [{module, origin, symbols}] and optional python_path, staged_inputs
{root, files: {relative_path: sha256}}, socket_paths [{name, path}], and archive
{path, sha256}. Paths are absolute except staged file names. Imports execute trusted
reviewed code, including its import side effects; resolving dotted names can also
import their trusted parents. This is not a sandbox.

Socket evidence is a raw Linux path budget plus a scratch bind at equivalent byte
length (or the minimum scratch length for shorter paths), not a bind at the supplied
destination. Existing destination ancestors are inspected without mutation; missing
parents remain planned. Archives must be trusted by the supplied digest and match
the current package-release.py release; older donor packages are not supported.
"""
import argparse
from contextlib import redirect_stdout
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sys
import tarfile
import tempfile
import time

sys.dont_write_bytecode = True


def absolute(value):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f'Expected absolute path: {value}')
    return path


def relative(value):
    path = PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or path.as_posix() != value or value == '.':
        raise ValueError(f'Unsafe relative input path: {value}')
    return path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def check_imports(spec):
    paths = [absolute(path) for path in spec.get('python_path', [])]
    if not all(path.is_dir() for path in paths):
        raise ValueError('Python search path is not a directory')
    sys.path[:0] = list(map(str, paths))
    modules = []
    for row in spec['imports']:
        expected_origin = absolute(row['origin']).resolve()
        with redirect_stdout(sys.stderr):
            found = importlib.util.find_spec(row['module'])
        origin = getattr(found, 'origin', None)
        if origin is None or Path(origin).resolve() != expected_origin:
            raise ValueError(f"Wrong module origin for {row['module']}: {origin}; expected {row['origin']}")
        with redirect_stdout(sys.stderr):
            module = importlib.import_module(row['module'])
        origin = getattr(module, '__file__', None)
        if origin is None or Path(origin).resolve() != expected_origin:
            raise ValueError(f"Wrong module origin for {row['module']}: {origin}; expected {row['origin']}")
        if not isinstance(row['symbols'], list):
            raise ValueError('Import symbols must be a list')
        for symbol in row['symbols']:
            if not hasattr(module, symbol):
                raise ValueError(f"Missing symbol {row['module']}.{symbol} from {origin}")
        modules.append(dict(module=row['module'], origin=str(Path(origin).resolve()), symbols=row['symbols']))
    return dict(modules=modules)


def check_staged(spec):
    root = absolute(spec['root'])
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Staged root must be a regular directory')
    expected = spec['files']
    for name in expected:
        relative(name)
    actual = {}
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f'Unsupported staged input: {path}')
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = digest(path)
    if actual != expected:
        raise ValueError('Staged input closure/hash mismatch: missing=' + str(sorted(set(expected) - actual.keys())) +
                         ', extra=' + str(sorted(actual.keys() - set(expected))) +
                         ', changed=' + str(sorted(name for name in actual.keys() & expected.keys()
                                                  if actual[name] != expected[name])))
    return dict(root=str(root), files=len(actual))


def check_sockets(rows, scratch):
    sockets = []
    for row in rows:
        raw = row['path']
        absolute(raw)
        size = len(os.fsencode(raw))
        if '\0' in raw or size > 107:
            raise ValueError(f"{row['name']}: raw Unix socket path is {size} bytes; maximum 107")
        parent = Path(raw).parent
        ancestor = parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise ValueError(f"{row['name']}: destination ancestor is not a writable searchable directory: {ancestor}")
        target = scratch / ('s' * max(1, size - len(os.fsencode(scratch)) - 1))
        bind_size = len(os.fsencode(target))
        if bind_size > 107:
            raise ValueError(f'Owned scratch bind path is {bind_size} bytes; choose a shorter --scratch-parent')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(target))
        target.unlink()
        sockets.append(dict(name=row['name'], path=raw, raw_bytes=size, bind_path=str(target),
                            bind_bytes=bind_size, bound=True, destination_parent_exists=parent.is_dir(),
                            nearest_existing_parent=str(ancestor)))
    return dict(evidence='raw-path budget and owned scratch bind; supplied destination not bound', sockets=sockets)


def check_archive(spec, scratch):
    source = absolute(spec['path'])
    if digest(source) != spec['sha256']:
        raise ValueError('Trusted archive SHA-256 mismatch')
    with tarfile.open(source) as archive:
        members = archive.getmembers()
        names = set()
        roots = set()
        for member in members:
            path = relative(member.name.rstrip('/') if member.isdir() else member.name)
            name = path.as_posix()
            if name in names or not (member.isfile() or member.isdir()) or member.mode & ~0o777:
                raise ValueError(f'Unsafe, duplicate or unsupported archive member: {member.name}')
            names.add(name)
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise ValueError('Archive must contain one package root')
        extracted = scratch / 'archive'
        extracted.mkdir(mode=0o700)
        for member in members:
            target = extracted / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source_file, target.open('xb') as output:
                    shutil.copyfileobj(source_file, output)
                target.chmod(member.mode)
        for member in sorted((item for item in members if item.isdir()),
                             key=lambda item: len(PurePosixPath(item.name).parts), reverse=True):
            (extracted / member.name).chmod(member.mode)
    verifier_path = Path(__file__).with_name('package-release.py')
    module_spec = importlib.util.spec_from_file_location('preflight_package_release', verifier_path)
    verifier = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(verifier)
    manifest = verifier.verify_package(extracted / roots.pop())
    return dict(archive=str(source), sha256=spec['sha256'], release=manifest['release'],
                tree_sha256=manifest['tree_sha256'], files=len(manifest['files']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Reviewed explicit JSON selections')
    parser.add_argument('--scratch-parent', type=Path, default=Path('/tmp'))
    parser.add_argument('--output', type=Path, help='Optional JSON report path')
    args = parser.parse_args()
    report = dict(passed=False, python=dict(executable=sys.executable, version=sys.version), checks={})
    started = time.monotonic()
    try:
        spec = json.loads(args.input.read_text())
        if type(spec.get('schema_version')) is not int or spec['schema_version'] != 1 or set(spec) - {
                'schema_version', 'python_path', 'imports', 'staged_inputs', 'socket_paths', 'archive'}:
            raise ValueError('Unsupported preflight schema or unknown selection')
        if not any(name in spec for name in ('imports', 'staged_inputs', 'socket_paths', 'archive')):
            raise ValueError('Select at least one preflight check')
        for name in ('imports', 'socket_paths'):
            if name in spec and (not isinstance(spec[name], list) or not spec[name]):
                raise ValueError(f'{name} must be a nonempty list')
        with tempfile.TemporaryDirectory(prefix='cp-preflight-', dir=args.scratch_parent) as temporary:
            scratch = Path(temporary)
            checks = [('staged_inputs', lambda: check_staged(spec['staged_inputs'])),
                      ('imports', lambda: check_imports(spec)),
                      ('socket_paths', lambda: check_sockets(spec['socket_paths'], scratch)),
                      ('archive', lambda: check_archive(spec['archive'], scratch))]
            for name, check in checks:
                if name not in spec:
                    continue
                begin = time.monotonic()
                try:
                    if name == 'imports' and report['checks'].get('staged_inputs', {}).get('passed') is False:
                        raise ValueError('Imports not executed because staged inputs failed verification')
                    result = dict(passed=True, **check())
                except (Exception, SystemExit) as error:
                    result = dict(passed=False, error=f'{type(error).__name__}: {error}')
                result['seconds'] = round(time.monotonic() - begin, 6)
                report['checks'][name] = result
        report['passed'] = all(row['passed'] for row in report['checks'].values())
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    report['seconds'] = round(time.monotonic() - started, 6)
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        with args.output.open('x') as output:
            output.write(encoded)
    print(encoded, end='')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
