"""Release-controller adapter for the pinned native advisor child supervisor."""
from recipe_context import CONFIG, DATA, RECIPE
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys


def run(args, *, cwd, env, stdout, timeout):
    binding = json.loads((DATA/'helper-binding.json').read_text())
    helper = Path(binding['helper'])
    with helper.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != binding['sha256']:
        raise RuntimeError(f'Owned supervisor hash mismatch: {helper}')
    args = list(map(str, args))
    reader, writer = socket.socketpair()
    guard = None
    try:
        with open('/dev/null', 'rb') as input_stream:
            # Popen preserves the source FD; the trampoline maps and closes it before exec.
            trampoline = ('import os,sys; fd=int(sys.argv[1]); '
                          'os.dup2(fd,3,inheritable=True); '
                          'os.close(fd) if fd!=3 else None; '
                          'os.execv(sys.argv[2],sys.argv[2:])')
            guard = subprocess.Popen(
                [sys.executable, '-c', trampoline, str(input_stream.fileno()), str(helper),
                 'advisor', '--owned-child', '--', *args],
                cwd=cwd, env=env, stdin=reader, stdout=stdout, stderr=subprocess.STDOUT,
                pass_fds=(input_stream.fileno(),), start_new_session=True)
        reader.close()
        writer.settimeout(5)
        if writer.recv(1) != b'\x01':
            raise RuntimeError('Owned supervisor did not acknowledge readiness')
        from permission_cleanup import process_identity
        record = dict(pid=guard.pid,boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            **process_identity(Path('/proc')/str(guard.pid)))
        with (DATA/'owned-process-identities.jsonl').open('a') as stream:
            stream.write(json.dumps(record)+'\n')
        try:
            code = guard.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired(args, timeout) from None
    except BaseException as error:
        writer.close()
        if guard is not None:
            try:
                guard.wait(timeout=8)
            except BaseException as cleanup_error:
                error.add_note(f'Owned supervisor cleanup unresolved: {cleanup_error!r}; guard PID={guard.pid}')
        raise
    finally:
        reader.close()
        writer.close()
    return subprocess.CompletedProcess(args, code)
