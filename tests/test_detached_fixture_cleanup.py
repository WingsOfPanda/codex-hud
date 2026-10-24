"""Exercise detached teardown's actual copy boundary without a daemon/provider."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fixtures import detached_jobs
from fixtures.custom_goal_server import CustomGoalServer
from test_background_integration import wait
from test_rust_runtime_stock_integration import stop_rust_observers


class DetachedFixtureCleanup(unittest.TestCase):
    def fixture(self, root):
        fixture = detached_jobs.DetachedJobServer.__new__(detached_jobs.DetachedJobServer)
        fixture.home, fixture.work, fixture.evidence = [root / name for name in ('home', 'work', 'evidence')]
        for directory in (fixture.home, fixture.work, fixture.evidence):
            directory.mkdir()
        fixture.releases = []
        fixture.report = {}
        return fixture

    def test_copy_waits_for_owned_atomic_registry_writer(self):
        with tempfile.TemporaryDirectory(prefix='fixture-copy-') as temporary:
            root = Path(temporary)
            fixture = self.fixture(root)
            registry = fixture.home / 'codex-plus/executions/v1'
            job = registry / 'root/job'
            job.mkdir(parents=True)
            marker, ready = root / 'release', root / 'ready'
            writer = subprocess.Popen([sys.executable, '-c', '''
import pathlib,sys,time
root,release,ready=map(pathlib.Path,sys.argv[1:])
temporary=root/'.f9bfe584-52e2-4438-943b-14a9e6c43f51.tmp'
temporary.write_text('committed resource registry')
ready.touch()
while not release.exists(): time.sleep(.005)
temporary.replace(root/'resources.json')
''', str(registry), str(marker), str(ready)])
            try:
                wait(ready.exists, 'atomic writer did not reach the copy barrier')
                fields = Path(f'/proc/{writer.pid}/stat').read_text().rsplit(')', 1)[1].split()
                identity = dict(pid=writer.pid, start_ticks=int(fields[19]),
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())
                (job / 'state.json').write_text(json.dumps(dict(state='failed', executor=identity)))
                (job / 'result.json').write_text('{"termination":"deadline"}')
                original_copytree = shutil.copytree

                def quiesce(home):
                    self.assertEqual(home, fixture.home)
                    marker.touch()

                def copy_file(source, destination):
                    if Path(source).name.endswith('.tmp'):
                        # copytree already listed the entry; reproduce its captured rename race.
                        marker.touch()
                        writer.wait(timeout=3)
                    return shutil.copy2(source, destination)

                def copytree(source, destination, *args, **kwargs):
                    if len(args) < 3:
                        kwargs.setdefault('copy_function', copy_file)
                    return original_copytree(source, destination, *args, **kwargs)

                with patch.object(detached_jobs, 'stop_rust_observers', quiesce, create=True), \
                     patch.object(detached_jobs.shutil, 'copytree', copytree), \
                     patch.object(CustomGoalServer, 'close') as base_close:
                    fixture.close(True)
                base_close.assert_called_once_with(True)
                self.assertEqual((fixture.evidence / 'executions/v1/resources.json').read_text(),
                                 'committed resource registry')
                self.assertTrue((fixture.evidence / 'executions/v1/root/job/result.json').is_file())
                self.assertEqual(writer.wait(timeout=3), 0)
            finally:
                marker.touch()
                writer.wait(timeout=3)

    def test_copy_failure_still_runs_base_cleanup_and_remains_failure(self):
        with tempfile.TemporaryDirectory(prefix='fixture-copy-') as temporary:
            fixture = self.fixture(Path(temporary))
            (fixture.home / 'codex-plus/executions').mkdir(parents=True)
            with patch.object(detached_jobs, 'stop_rust_observers', create=True), \
                 patch.object(detached_jobs.shutil, 'copytree', side_effect=PermissionError('evidence denied')), \
                 patch.object(CustomGoalServer, 'close') as base_close:
                with self.assertRaisesRegex(PermissionError, 'evidence denied'):
                    fixture.close(True)
            base_close.assert_called_once_with(False)

    def test_native_coordinators_are_stopped_and_waited_in_owner_order(self):
        with tempfile.TemporaryDirectory(prefix='fixture-stop-') as temporary:
            root = Path(temporary)
            events = root / 'events'
            children = []
            script = '''import os,pathlib,signal,sys,time
events=pathlib.Path(sys.argv[3]);role=sys.argv[2]
def stop(*args):
 time.sleep(.05)
 with events.open('a') as f: f.write(role+'\\n')
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
pathlib.Path(sys.argv[4]).touch()
while True:time.sleep(.01)
'''
            try:
                for index, (role, home) in enumerate((('observe', root), ('supervise', root), ('observe', root / 'foreign'))):
                    ready = root / f'ready-{index}'
                    child = subprocess.Popen([sys.executable, '-c', script, 'codex-plus-runtime', role,
                        str(events), str(ready)], env=dict(os.environ, CODEX_HOME=str(home)))
                    children.append(child)
                    wait(ready.exists, 'private coordinator did not initialize')
                stop_rust_observers(root)
                for child in children[:2]:
                    descriptor = os.pidfd_open(child.pid)
                    try:
                        self.assertTrue(select.select([descriptor], [], [], 0)[0], 'stop returned before exit')
                    finally:
                        os.close(descriptor)
                    self.assertEqual(child.wait(timeout=3), 0)
                self.assertEqual(events.read_text().splitlines(), ['supervise', 'observe'])
                self.assertIsNone(children[2].poll(), 'foreign coordinator was signalled')
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
