"""Process-local adapter for the one overlong advisor fixture prefix."""
from recipe_context import CONFIG, DATA, RECIPE
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import json


@contextmanager
def install(root):
    root = Path(root).resolve(strict=True)
    assert root == DATA
    assert os.environ['CODEX_PLUS_RELEASE_QA_SCOPE'] == str(root)
    assert root.stat().st_mode & 0o777 == 0o700
    binding = json.loads((root/'short-binding.json').read_text())
    short = Path(binding['path']).resolve(strict=True)
    assert short.parent == Path(CONFIG['short_root']) and len(short.name) == 2 and short.name.isalnum()
    assert (short.stat().st_dev, short.stat().st_ino) == (binding['device'], binding['inode'])
    assert os.environ['TMPDIR'] == str(short)
    original = tempfile.TemporaryDirectory

    def temporary(*args, **kwargs):
        if not args and kwargs.get('prefix') == 'claude-advisor-contract-':
            assert set(kwargs) == {'prefix'}, 'fixture contract changed; review the adapter'
            return original(prefix='', dir=short)
        return original(*args, **kwargs)

    tempfile.TemporaryDirectory = temporary
    try:
        yield
    finally:
        tempfile.TemporaryDirectory = original
