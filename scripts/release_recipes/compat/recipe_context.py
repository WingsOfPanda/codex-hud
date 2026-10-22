"""Read immutable attempt data; executable recipes always stay in tooling."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

RECIPE = Path(__file__).resolve().parent
DATA = Path(os.environ['CODEX_PLUS_COMPAT_SCOPE']).resolve(strict=True)
CONFIG = json.loads((DATA / 'native.config.json').read_text())
assert DATA == Path(CONFIG['scope'])
for name, expected in CONFIG['recipe_hashes'].items():
    assert hashlib.sha256((RECIPE / name).read_bytes()).hexdigest() == expected, name
OLD_VERSION = CONFIG['previous_release'].split('-plus.', 1)[1]
NEW_VERSION = CONFIG['release'].split('-plus.', 1)[1]
UPSTREAM = CONFIG['upstream']


def source_identity(source, commit):
    """Git status can refresh stat data, including an alternate index."""
    source = Path(source).resolve(strict=True)
    assert subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip() == commit
    index = Path(subprocess.check_output(['git', '-C', str(source), 'rev-parse', '--path-format=absolute', '--git-path', 'index'], text=True).strip())
    with tempfile.TemporaryDirectory() as temporary:
        scratch = Path(temporary) / 'index'
        if index.exists():
            shutil.copyfile(index, scratch)
        env = dict(os.environ, GIT_INDEX_FILE=str(scratch))
        assert not subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=normal'], env=env, text=True), 'source is dirty'
    return commit
