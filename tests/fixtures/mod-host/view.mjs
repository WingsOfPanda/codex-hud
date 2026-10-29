import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';

const settings = JSON.parse(fs.readFileSync(path.join(import.meta.dirname, 'fixture.json'), 'utf8'));
if (process.argv.includes('--self-test')) process.exit(settings.self_test_exit || 0);
const request = JSON.parse(fs.readFileSync(0, 'utf8'));
if (settings.check_env && ['NODE_OPTIONS', 'NODE_PATH', 'OPENAI_API_KEY'].some(key => key in process.env)) {
  throw new Error('credential or Node injection variable inherited');
}
if (settings.trace) {
  fs.appendFileSync(path.join(request.session.codex_home, 'mod-fixture-calls.jsonl'),
    JSON.stringify({label: settings.label, request, pid: process.pid,
      env: Object.keys(process.env), cwd: process.cwd()}) + '\n');
}
if (settings.mode === 'orphan') {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {stdio: 'ignore'});
  fs.writeFileSync(settings.pid_file, String(child.pid));
  child.unref();
}
if (settings.mode === 'hang' || settings.mode === 'grandchild') {
  if (settings.mode === 'grandchild') {
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {stdio: 'inherit'});
    fs.writeFileSync(settings.pid_file, String(child.pid));
  }
  setInterval(() => {}, 1000);
} else if (settings.mode === 'malformed') {
  process.stdout.write('{broken');
} else if (settings.mode === 'stdout-overflow') {
  process.stdout.write('x'.repeat(65537));
} else if (settings.mode === 'stderr-overflow') {
  process.stderr.write('x'.repeat(16385));
} else if (settings.mode === 'exit') {
  process.exitCode = 7;
} else {
  if (settings.mode === 'stderr') process.stderr.write('fixture diagnostic\n');
  const rows = Array.from({length: settings.rows || 1}, (_, index) => ({
    text: `${settings.label} ${index + 1}`, severity: 'normal', age_ms: 0, share: 0.5,
  }));
  process.stdout.write(JSON.stringify({host_contract: 1,
    request_id: settings.mode === 'wrong-id' ? 'previous-request' : request.request_id,
    status: 'ok', rows, overflow: 0}));
}
