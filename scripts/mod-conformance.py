#!/usr/bin/env python3
"""Check one installed mod version without installing or advancing it.

Usage: mod-conformance.py --package-dir /mods/example/1.0.0 --node /absolute/node
Trusted packages execute as the current user; a private context is not a sandbox.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
import uuid


class ConformanceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ConformanceError(message)


def integer(value):
    return type(value) is int


def parse_json(data):
    try:
        return json.loads(data, parse_constant=lambda value: require(False, 'nonfinite JSON number'))
    except (ValueError, UnicodeError) as error:
        raise ConformanceError(f'invalid JSON: {error}') from error


def package_manifest(package):
    require(package.is_absolute(), 'package-dir must be absolute')
    require(not package.is_symlink() and package.is_dir(), 'package-dir must be a regular directory')
    payload = {}
    for directory, directories, files in os.walk(package, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), f'nonregular package entry: {path}')
            if stat.S_ISREG(mode) and path != package / 'SOURCE.json':
                payload[path.relative_to(package).as_posix()] = path
    source_path = package / 'SOURCE.json'
    require(source_path.is_file() and not source_path.is_symlink(), 'missing regular SOURCE.json')
    source = parse_json(source_path.read_bytes())
    require(isinstance(source, dict) and integer(source.get('schema')) and source['schema'] == 1,
            'unsupported SOURCE schema')
    require(isinstance(source.get('commit'), str) and re.fullmatch(r'[0-9a-fA-F]{40}', source['commit']),
            'SOURCE commit must be 40 hex digits')
    files = source.get('files')
    require(isinstance(files, dict), 'SOURCE files must be a map')
    for name, digest in files.items():
        require(isinstance(name, str) and name and not PurePosixPath(name).is_absolute()
                and all(part not in ('', '.', '..') for part in name.split('/')),
                f'unsafe receipt path: {name}')
        require(isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest),
                f'invalid SHA256: {name}')
    require(files.keys() == payload.keys(), 'SOURCE payload closure differs (missing or extra files)')
    for name, path in payload.items():
        require(hashlib.sha256(path.read_bytes()).hexdigest() == files[name].lower(), f'digest mismatch: {name}')
    require('mod.json' in files, 'missing receipt-bound mod.json')
    manifest = parse_json((package / 'mod.json').read_bytes())
    require(isinstance(manifest, dict), 'manifest must be an object')
    identifier = manifest.get('id')
    require(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier), 'invalid mod id')
    require(identifier == package.parent.name, 'manifest ID differs from selected directory')
    require(manifest.get('version') == package.name, 'manifest version differs from selected directory')
    require(integer(manifest.get('host_contract')) and manifest['host_contract'] == 1, 'unsupported host contract')
    require(manifest.get('provides') == ['view.rows'], 'unsupported provides capabilities')
    inputs = manifest.get('inputs')
    require(isinstance(inputs, list) and inputs in ([], ['session.identity']), 'unsupported input capabilities')
    runtime = manifest.get('runtime')
    require(isinstance(runtime, dict) and isinstance(runtime.get('node'), str), 'missing Node requirement')
    minimum = re.fullmatch(r'>=(\d+)\.(\d+)\.(\d+)', runtime['node'])
    require(minimum is not None, 'unsupported Node requirement (expected >=major.minor.patch)')
    entry = manifest.get('entry')
    require(isinstance(entry, str) and entry in files, 'entry must be a receipt-bound relative file')
    refresh, ui = manifest.get('refresh'), manifest.get('ui')
    require(isinstance(refresh, dict) and integer(refresh.get('min_interval_s'))
            and refresh['min_interval_s'] >= 0 and integer(refresh.get('timeout_ms'))
            and refresh['timeout_ms'] >= 0,
            'invalid refresh settings')
    require(isinstance(ui, dict) and integer(ui.get('rows')) and ui['rows'] >= 0, 'invalid row budget')
    require(manifest.get('self_test') in (None, '--self-test'), 'unsupported self-test argument')
    return manifest, tuple(map(int, minimum.groups()))


def run_bounded(arguments, request, cwd, env, timeout_ms, limits=(65536, 16384)):
    process = subprocess.Popen(arguments, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    buffers = [bytearray(), bytearray()]
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        process.stdin.write(request)
        process.stdin.close()
        with selectors.DefaultSelector() as selector:
            for index, pipe in enumerate((process.stdout, process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'process timed out')
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        index = key.data
                        buffers[index].extend(chunk)
                        require(len(buffers[index]) <= limits[index],
                                f'{("stdout", "stderr")[index]} overflow')
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                raise ConformanceError('process timed out') from error
            require(code == 0, f'process exited {code}: {buffers[1].decode("utf-8", errors="replace")}')
        return bytes(buffers[0]), bytes(buffers[1])
    finally:
        # Descendants can outlive an exit-zero parent and keep inherited pipes open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def validate_reply(data, request_id):
    reply = parse_json(data)
    require(isinstance(reply, dict), 'response must be an object')
    require(integer(reply.get('host_contract')) and reply['host_contract'] == 1
            and reply.get('request_id') == request_id,
            'response contract/request identity differs')
    require(reply.get('status') in ('ok', 'empty', 'unavailable', 'error'), 'invalid response status')
    rows = reply.get('rows')
    require(isinstance(rows, list) and len(rows) <= 64, 'response rows must contain at most 64 rows')
    require(reply['status'] != 'empty' or not rows, 'empty status returned rows')
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get('text'), str)
                and len(row['text'].encode('utf-8')) <= 2048, 'invalid row text')
        require(row.get('severity') in ('normal', 'ok', 'warn', 'error'), 'invalid row severity')
        if 'age_ms' in row:
            require(integer(row['age_ms']) and row['age_ms'] >= 0, 'invalid row age_ms')
        if 'share' in row:
            share = row['share']
            require(type(share) in (int, float) and math.isfinite(share) and 0 <= share <= 1,
                    'invalid row share')
    overflow = reply.get('overflow', 0)
    require(integer(overflow) and overflow >= 0, 'invalid overflow')
    require(isinstance(reply.get('diagnostics', ''), str), 'invalid diagnostics')
    return reply


def check(package, node):
    manifest, minimum = package_manifest(package)
    require(node.is_absolute() and node.is_file() and os.access(node, os.X_OK), 'node must be an absolute executable')
    with tempfile.TemporaryDirectory(prefix='mod-conformance-') as temporary:
        context = Path(temporary)
        home, work = context / 'home', context / 'work'
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        env = dict(HOME=str(home), CODEX_HOME=str(home), PATH='/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8')
        version, _ = run_bounded([str(node), '--version'], b'', work, env, 5000, (256, 16384))
        match = re.fullmatch(rb'v(\d+)\.(\d+)\.(\d+)\s*', version)
        require(match is not None, 'unrecognized Node version')
        actual = tuple(map(int, match.groups()))
        require(actual >= max((22, 18, 0), minimum), f'Node {actual} does not meet {manifest["runtime"]["node"]}')
        entry = str(package / manifest['entry'])
        if manifest.get('self_test'):
            run_bounded([str(node), entry, '--self-test'], b'', work, env, 5000)
        request_id = str(uuid.uuid4())
        request = dict(host_contract=1, op='view', request_id=request_id,
                       session=dict(thread_id=str(uuid.uuid4()), codex_home=str(home)),
                       width=100, rows=max(1, min(6, manifest['ui']['rows'])), now=int(time.time() * 1000))
        stdout, stderr = run_bounded([str(node), entry], json.dumps(request).encode(), work, env,
                                     max(100, min(5000, manifest['refresh']['timeout_ms'])))
        reply = validate_reply(stdout, request_id)
    return dict(passed=True, id=manifest['id'], version=manifest['version'], node=version.decode().strip(),
                self_test=bool(manifest.get('self_test')), request=request, response=reply,
                stderr=stderr.decode('utf-8', errors='replace'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package-dir', required=True, type=Path)
    parser.add_argument('--node', required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.package_dir, args.node), ensure_ascii=False))
    except (ConformanceError, OSError) as error:
        print(json.dumps(dict(passed=False, error=str(error))))
        raise SystemExit(1)
