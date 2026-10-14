"""Stream a noninteractive command while retaining its complete byte output.

Native process-group cancellation owns descendant cleanup. When launched in an
unrelated caller's group, direct helper signals are forwarded to its child only.
Forced group cancellation preserves the partial log but cannot emit a final line.
"""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import uuid


def open_log(home):
    directory = Path(home).absolute() / 'codex-plus' / 'logs'
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, component in enumerate(directory.parts[1:]):
            try:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if index >= len(directory.parts) - 3:
                os.fchmod(descriptor, 0o700)
        name = f'{uuid.uuid4()}.log'
        log = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                      0o600, dir_fd=descriptor)
        try:
            os.fchmod(log, 0o600)
        except OSError:
            os.close(log)
            raise
        return directory / name, log
    finally:
        os.close(descriptor)


def write_all(descriptor, data):
    pending = memoryview(data)
    while pending:
        count = os.write(descriptor, pending)
        if count == 0:
            raise OSError('zero-byte log write')
        pending = pending[count:]


def run_command(command):
    try:
        path, log = open_log(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    except OSError as exc:
        print(f'codex-plus error: cannot open log: {exc}', file=sys.stderr)
        return 74

    child = None
    interrupted = None
    requested_signal = None
    log_error = None
    last_byte = b'\n'
    output = sys.stdout.fileno()
    blocking = os.get_blocking(output)
    os.set_blocking(output, False)

    def forward(signum, _frame):
        nonlocal interrupted, requested_signal
        if interrupted is not None:
            return
        interrupted = time.monotonic()
        requested_signal = signum
        if child is not None:
            try:
                if os.getpgrp() == os.getpid():
                    os.killpg(os.getpgrp(), signum)
                else:
                    child.send_signal(signum)
            except ProcessLookupError:
                pass

    def tick():
        if interrupted is not None and time.monotonic() - interrupted >= 2:
            if child is not None and os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            if child is not None and child.poll() is None:
                child.kill()
            return time.monotonic() - interrupted >= 3
        return False

    def emit(data):
        pending = memoryview(data)
        while pending:
            tick()
            try:
                count = os.write(output, pending)
                if count == 0:
                    raise BrokenPipeError('zero-byte output write')
                pending = pending[count:]
            except BlockingIOError:
                if interrupted is not None:
                    return
                select.select([], [output], [], 0.1)
            except BrokenPipeError:
                forward(signal.SIGTERM, None)
                return

    previous = {signum: signal.signal(signum, forward)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        emit(f'codex-plus log: {path}\n'.encode())
        if requested_signal is not None:
            code = 128 + requested_signal
            emit(f'codex-plus result: exit={code} log={path}\n'.encode())
            return code
        try:
            # Keep the native executor's process group so its cancellation reaches descendants.
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            print(f'codex-plus error: cannot launch command: {exc}', file=sys.stderr)
        else:
            if requested_signal is not None:
                child.send_signal(requested_signal)
            with child.stdout:
                os.set_blocking(child.stdout.fileno(), False)
                while True:
                    if tick():
                        break
                    if not select.select([child.stdout], [], [], 0.1)[0]:
                        continue
                    chunk = os.read(child.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    last_byte = chunk[-1:]
                    if log_error is None:
                        try:
                            write_all(log, chunk)
                        except OSError as exc:
                            log_error = exc
                    emit(chunk)
            while child.poll() is None:
                tick()
                time.sleep(0.1)
            status = child.wait()
            code = status if status >= 0 else 128 - status
        try:
            os.close(log)
        except OSError as exc:
            log_error = log_error or exc
        log = None
        if log_error is not None:
            print(f'codex-plus error: log incomplete: {log_error}; command exit={code}',
                  file=sys.stderr)
        prefix = b'' if last_byte == b'\n' else b'\n'
        suffix = ' log_incomplete=true' if log_error is not None else ''
        emit(prefix + f'codex-plus result: exit={code} log={path}{suffix}\n'.encode())
        return 74 if log_error is not None else code
    finally:
        if log is not None:
            os.close(log)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        os.set_blocking(output, blocking)


def main():
    arguments = sys.argv[1:]
    if len(arguments) < 3 or arguments[:2] != ['run', '--']:
        print('usage: codex-plus run -- <program> [args...]', file=sys.stderr)
        return 2
    return run_command(arguments[2:])


if __name__ == '__main__':
    sys.exit(main())
