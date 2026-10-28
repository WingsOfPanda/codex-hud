"""Product inputs for the qualified 0.163 recipe, independent of current pins."""
from functools import cache
import io
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parents[1]
LEGACY_COMMIT = '236d0438547e3c104c25118070321aa32384ddac'
LEGACY_FILES = ('scripts/package-release.py', 'scripts/runtime-notices.py',
    'scripts/native-results.py', 'scripts/release-preflight.py',
    'rust-runtime/Cargo.toml', 'rust-runtime/Cargo.lock',
    'rust-runtime/src/main.rs', 'bin/codex-plus', 'LICENSE',
    'patches/codex-0.163.0-worker-hud.patch', 'tests/test_detached_jobs_stock.py',
    'tests/test_custom_goals_stock.py', 'tests/fixtures/detached_jobs.py',
    'tests/fixtures/custom_goal_server.py')


@cache
def legacy_archive():
    try:
        return subprocess.check_output(['git', '-C', str(ROOT), 'archive', LEGACY_COMMIT,
                                        *LEGACY_FILES], stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'Pinned 0.163 fixture {LEGACY_COMMIT} unavailable: '
                           + error.stderr.decode().strip()) from error


def legacy_source(path):
    with tarfile.open(fileobj=io.BytesIO(legacy_archive())) as archive:
        archive.extractall(path, filter='data')
    return path
