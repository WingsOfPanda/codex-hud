"""Import and actually initialize selected existing fixtures; providers remain private loopback."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'short-root', 'runtime', 'stock', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source / 'tests'))
    os.environ['CODEX_PLUS_TEST_RUNTIME'] = str(args.runtime)
    original = tempfile.TemporaryDirectory
    tempfile.TemporaryDirectory = lambda *a, **kw: original(prefix='', dir=args.short_root) if not a else original(*a, **kw)
    from test_claude_advisor import ClaudeAdvisor
    from test_claude_advisor_stdio import WorkerPipes
    from test_executor_reconcile_startup import case
    from fixtures.detached_jobs import DetachedJobServer
    import test_release_package as package_tests
    from fixtures.release_package_provider import PackageProvider
    from fixtures.native_server import AppServer
    assert callable(WorkerPipes.exercise_disconnect) and callable(case)
    assert callable(PackageProvider) and callable(package_tests.main)
    records = []
    advisor = ClaudeAdvisor('test_root_required')
    try:
        advisor.setUp()
        path = advisor.home / 'app-server-control/app-server-control.sock'
        assert path.is_socket() and len(os.fsencode(path)) <= 107
        records.append(dict(fixture='advisor', socket=str(path), bytes=len(os.fsencode(path))))
    finally:
        if hasattr(advisor, 'server'):
            advisor.server.close()
        if hasattr(advisor, 'temp'):
            advisor.temp.cleanup()
    native = DetachedJobServer(args.runtime, args.stock, args.evidence)
    try:
        path = native.home / 'app-server-control/app-server-control.sock'
        assert path.is_socket() and len(os.fsencode(path)) <= 107
        records.append(dict(fixture='startup', socket=str(path), bytes=len(os.fsencode(path))))
        assert not native.provider.requests
    finally:
        native.close(True)
    packaged = args.short_root / 'stdio_code_mode'
    assert not packaged.exists(), 'Preserve unexpected existing package fixture path'
    server = None
    try:
        env, workspace = package_tests.private_env(packaged / 'isolated', code_mode=True)
        path = Path(env['CODEX_HOME']) / 'app-server-control/app-server-control.sock'
        assert len(os.fsencode(path)) <= 107, 'Actual packaged stdio fixture socket is too long'
        path.parent.mkdir()
        server = AppServer('11111111-1111-4111-8111-111111111111', workspace, 0, models=[], socket_path=path)
        assert path.is_socket()
        records.append(dict(fixture='packaged-stdio-constructor', socket=str(path), bytes=len(os.fsencode(path))))
    finally:
        if server is not None:
            server.close()
        shutil.rmtree(packaged)
    # Exercise the selected PTY constructor at each actual home shape before compilation.
    from test_morandi_tui import ThemeServer
    from test_morandi_command_colors_tui import run as command_colors
    assert callable(command_colors)
    for category, home_name in [('morandi', 'h'), ('command', 'h'), ('status', 'branch/home'),
                                ('command-color-success', 'unmarked_success/h'),
                                ('command-color-failure', 'unmarked_failure/h'),
                                ('footer-detached-topics', 'home'),
                                ('native-smoke-health', 'health-home'),
                                ('native-smoke-history', 'history-home'),
                                ('shell-policy-stock', 'stock-home')]:
        with original(prefix='', dir=args.short_root) as temporary:
            root = Path(temporary)
            home, work = root / home_name, root / 'work'
            home.mkdir(mode=0o700, parents=True)
            work.mkdir(mode=0o700)
            path = home / 'app-server-control/app-server-control.sock'
            current = None
            try:
                if category == 'morandi':
                    current = ThemeServer(home, work)
                else:
                    path.parent.mkdir(mode=0o700)
                    current = AppServer('11111111-1111-4111-8111-111111111111', work, 0, socket_path=path)
                assert path.is_socket() and len(os.fsencode(path)) <= 107
                records.append(dict(fixture=category, socket=str(path), bytes=len(os.fsencode(path))))
            finally:
                if current is not None:
                    current.close()
    print(json.dumps(dict(passed=True, actual_fixture_sockets=records,
        stdio='Imported actual WorkerPipes; no custom setUp; execution remains a separate gate', paid_provider_calls=0), indent=2))


if __name__ == '__main__':
    main()
