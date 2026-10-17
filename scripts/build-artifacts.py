"""Build developer binaries and install Cargo's reported executables."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def build(command, names, output, *, cwd=None, env=None, strip=False):
    artifacts = {name: set() for name in names}
    with tempfile.TemporaryFile(mode='w+') as messages:
        result = subprocess.run(command + ['--message-format=json-render-diagnostics'],
                                cwd=cwd, env=env, stdout=messages)
        if result.returncode:
            raise SystemExit(result.returncode)
        messages.seek(0)
        for line in messages:
            message = json.loads(line)
            if message.get('reason') == 'compiler-artifact':
                target = message['target']
                name = target['name']
                if name in artifacts and 'bin' in target['kind'] and message.get('executable'):
                    path = Path(message['executable'])
                    if not path.is_absolute():
                        path = Path(cwd or Path.cwd()) / path
                    artifacts[name].add(path)
    for name, paths in artifacts.items():
        if len(paths) != 1 or not all(path.is_file() and os.access(path, os.X_OK) for path in paths):
            raise ValueError(f'Expected one executable Cargo artifact for {name}, found: {paths}')
    # Stage the whole pair before replacing either installed binary.
    for name, paths in artifacts.items():
        subprocess.run(['install', *(['-s'] if strip else []), '-m', '755',
                        str(next(iter(paths))), str(output / (name + '.new'))], check=True)
    for name in names:
        (output / (name + '.new')).replace(output / name)


def main():
    mode, root, *args = sys.argv[1:]
    root = Path(root)
    output = root / '.build'
    output.mkdir(parents=True, exist_ok=True)
    command = ['cargo', '+1.99.1', 'build', '--locked', '--release']
    if mode == 'runtime':
        build(command + ['--manifest-path', str(root / 'rust-runtime/Cargo.toml'),
                         '--target-dir', os.environ.get('CARGO_TARGET_DIR', str(output / 'runtime-target'))],
              ['codex-plus-runtime'], output)
    else:
        source = Path(args[0])
        # Developer builds are native; use the same explicit target for V8 and Cargo.
        version = subprocess.check_output(['rustc', '+1.99.1', '-vV'], text=True)
        target = next(line.removeprefix('host: ') for line in version.splitlines()
                      if line.startswith('host: '))
        os.environ['CODEX_REPO_ROOT'] = str(source)
        sys.path.insert(0, str(source / 'scripts'))
        sys.dont_write_bytecode = True
        from codex_package.targets import TARGET_SPECS
        from codex_package.v8 import resolve_codex_v8_cargo_env
        env = os.environ | resolve_codex_v8_cargo_env(TARGET_SPECS[target])
        names = ['codex', 'codex-code-mode-host']
        build(command + ['--target', target, '-p', 'codex-cli', '-p', 'codex-code-mode-host', '--bin', names[0],
                         '--bin', names[1], '-j', '12'], names, output,
              cwd=source / 'codex-rs', env=env, strip=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
