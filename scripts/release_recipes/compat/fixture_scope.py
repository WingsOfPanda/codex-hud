"""Exact ownership checks for the output scope and its short fixture directory."""
from recipe_context import CONFIG, DATA, RECIPE
import json
import os
import hashlib
from pathlib import Path

BASE = DATA


def validate_short():
    assert BASE == DATA
    binding = json.loads((BASE / 'short-binding.json').read_text())
    short = Path(binding['path'])
    assert short.parent == Path(CONFIG['short_root']) and len(short.name) == 2 and short.name.isalnum()
    info = short.lstat()
    assert short.is_dir() and not short.is_symlink()
    assert (info.st_dev, info.st_ino) == (binding['device'], binding['inode'])
    assert info.st_mode & 0o777 == 0o700
    baseline_path = BASE/'unreadable-baseline.json'
    assert hashlib.sha256(baseline_path.read_bytes()).hexdigest() == binding['baseline_sha256'], 'Frozen baseline changed'
    return short


def inspect_scope():
    from permission_cleanup import inspect_processes
    short = validate_short()
    baseline = json.loads((BASE/'unreadable-baseline.json').read_text())
    records = BASE/'owned-process-identities.jsonl'
    recorded = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    return dict(inspect_processes([BASE,short],baseline,scope=BASE,recorded=recorded),
        scope=str(BASE),short=str(short),action='Read only; exact frozen baseline, no new identities admitted')
