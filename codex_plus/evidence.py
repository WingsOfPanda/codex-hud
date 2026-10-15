"""Read only bounded, exact terminal provenance from a local rollout."""
from contextlib import ExitStack
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat


HEADER_BYTES = 64 * 1024
TAIL_BYTES = 2 * 1024 * 1024
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})')


def _record(line):
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def _extent(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def terminal_event(home, path, thread_id, turn_id):
    """Return sanitized positive evidence, or None when the outcome is unknown."""
    if not isinstance(thread_id, str) or not thread_id or not isinstance(turn_id, str) or not turn_id:
        return None
    try:
        root = Path(home).resolve(strict=True)
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if stat.S_ISLNK(candidate.lstat().st_mode):
            return None
        parts = candidate.resolve(strict=True).relative_to(root).parts
        if len(parts) < 2 or parts[0] not in ('sessions', 'archived_sessions'):
            return None
        with ExitStack() as stack:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            directory = os.open(root, flags | os.O_DIRECTORY)
            stack.callback(os.close, directory)
            for part in parts[:-1]:
                directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
                stack.callback(os.close, directory)
            fd = os.open(parts[-1], flags, dir_fd=directory)
            stack.callback(os.close, fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return None
            header = os.pread(fd, HEADER_BYTES + 1, 0).partition(b'\n')
            if not header[1] or len(header[0]) > HEADER_BYTES:
                return None
            meta = _record(header[0])
            if (not meta or meta.get('type') != 'session_meta'
                    or not isinstance(meta.get('payload'), dict)
                    or meta['payload'].get('id') != thread_id):
                return None
            # shortcut: older events outside this tail stay unknown; widen only with measured need.
            start = max(0, before.st_size - TAIL_BYTES)
            offset = max(0, start - 1)
            tail = os.pread(fd, before.st_size - offset, offset)
            if len(tail) != before.st_size - offset:
                return None
            if start:
                tail = tail.partition(b'\n')[2]
            if (_extent(os.fstat(fd)) != _extent(before)
                    or _extent(candidate.stat(follow_symlinks=False)) != _extent(before)):
                return None
        result = None
        conflicted = False
        for raw in tail.split(b'\n')[:-1]:
            record = _record(raw)
            if not record:
                continue
            payload = record.get('payload')
            if (record.get('type') == 'compacted'
                    or (record.get('type') == 'event_msg' and isinstance(payload, dict)
                        and payload.get('type') == 'thread_rolled_back')):
                result, conflicted = None, False
                continue
            if record.get('type') != 'event_msg' or conflicted:
                continue
            if not isinstance(payload, dict) or payload.get('turn_id') != turn_id:
                continue
            event = payload.get('type')
            if event not in ('task_complete', 'turn_aborted'):
                continue
            error = payload.get('error')
            if error is not None and (not isinstance(error, dict) or not isinstance(error.get('message'), str)):
                return None
            if event == 'turn_aborted':
                if payload.get('reason') not in ('interrupted', 'replaced', 'review_ended', 'budget_limited'):
                    return None
                status = 'interrupted'
            else:
                status = 'failed' if error is not None else 'completed'
            if result and (result['status'], result['event']) != (status, event):
                result, conflicted = None, True
                continue
            result = {'status': status, 'event': event}
            timestamp = record.get('timestamp')
            if isinstance(timestamp, str) and _TIMESTAMP.fullmatch(timestamp):
                try:
                    datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    result['timestamp'] = timestamp
                except ValueError:
                    pass
        return result
    except (OSError, ValueError, TypeError, RuntimeError):
        return None
