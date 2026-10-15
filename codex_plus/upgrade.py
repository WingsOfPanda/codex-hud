"""Select an adjacent, compatible immutable coordinator package."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

COORDINATOR_PROTOCOL = 1
UPGRADE_EXIT = 75


def _manifest(root):
    path = root / 'codex-plus.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('manifest must be a regular file')
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('manifest exceeds local read bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('manifest must be an object')
    return value


def select_package(root):
    root = Path(root).resolve()
    current = root.parent / 'current'
    if not (root / 'codex-plus.json').exists() or not current.is_symlink():
        if current.exists() and (root / 'codex-plus.json').exists():
            raise ValueError('current must be a symlink')
        return None
    try:
        installed = _manifest(root)
        link = Path(os.readlink(current))
        candidate = link if link.is_absolute() else root.parent / link
        if candidate.parent != root.parent or candidate.is_symlink() or not candidate.is_dir():
            raise ValueError('current must point directly to a sibling package')
        manifest = _manifest(candidate)
        if (manifest.get('schema_version') != 1 or
                type(manifest.get('coordinator_protocol')) is not int or
                manifest['coordinator_protocol'] != COORDINATOR_PROTOCOL or
                type(installed.get('coordinator_protocol')) is not int or
                installed.get('coordinator_protocol') != COORDINATOR_PROTOCOL or
                not isinstance(installed.get('upstream'), dict) or not installed['upstream'] or
                manifest.get('upstream') != installed['upstream'] or
                not isinstance(installed.get('target'), str) or not installed['target'] or
                manifest.get('target') != installed['target']):
            raise ValueError('incompatible coordinator package')
        files = manifest.get('files')
        if not isinstance(files, dict) or not files or any(not isinstance(name, str) for name in files):
            raise ValueError('missing package inventory')
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':'),
                                          ensure_ascii=True).encode('ascii')).hexdigest()
        if digest != manifest.get('tree_sha256'):
            raise ValueError('invalid package tree identity')
        helpers = {name: entry for name, entry in files.items() if name.startswith('codex_plus/')}
        actual = set()
        directory = candidate / 'codex_plus'
        if directory.is_symlink():
            raise ValueError('helper directory must not be a symlink')
        for index, path in enumerate(directory.rglob('*')):
            if index >= 256:
                raise ValueError('helper inventory exceeds local bound')
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('unsupported helper entry')
            if path.is_file():
                actual.add(path.relative_to(candidate).as_posix())
        required = {f'codex_plus/{name}.py' for name in
                    ('__init__', 'background', 'upgrade', 'ledger', 'rpc', 'evidence')}
        if set(helpers) != actual or not required <= actual:
            raise ValueError('helper inventory mismatch')
        total_size = 0
        for name, entry in helpers.items():
            path = candidate / name
            metadata = path.stat()
            total_size += metadata.st_size
            mode = entry.get('mode') if isinstance(entry, dict) else None
            if not isinstance(mode, str) or len(mode) != 4 or any(c not in '01234567' for c in mode):
                raise ValueError('invalid helper mode')
            installed_mode = stat.S_IMODE(metadata.st_mode)
            if (total_size > 16 * 1024 * 1024 or metadata.st_size > 2 * 1024 * 1024 or
                    not isinstance(entry, dict) or entry.get('size') != metadata.st_size or
                    installed_mode not in (int(mode, 8), int(mode, 8) & ~0o222) or
                    entry.get('sha256') != hashlib.sha256(path.read_bytes()).hexdigest()):
                raise ValueError('helper integrity mismatch')
        return candidate if candidate != root else None
    except (OSError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid advertised coordinator package') from exc


def package_env(root, home):
    return dict(os.environ, CODEX_HOME=str(home), PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1')


def package_command(root, action, parent, argv=None):
    arguments = []
    source = iter(argv if argv is not None else [action, '--parent', parent])
    for value in source:
        if value == '--package-root':
            next(source, None)
        elif not value.startswith('--package-root='):
            arguments.append(value)
    if action != 'park':
        arguments.extend(['--package-root', str(root) + '/'])
    return [sys.executable, '-m', 'codex_plus.background', *arguments]


def exec_package(root, action, parent, home, argv=None):
    root = Path(root)
    previous = Path.cwd()
    try:
        os.chdir(root)
        os.execve(sys.executable, package_command(root, action, parent, argv), package_env(root, home))
    except OSError:
        os.chdir(previous)
        raise
