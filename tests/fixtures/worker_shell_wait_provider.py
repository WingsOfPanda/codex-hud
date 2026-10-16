"""Loopback responses for a real native child waiting inside one code cell."""

import http.server
import json
import time

from .background_provider import ResponsesProvider


def wait_script(command, budget_ms=110000, outer_ms=120000):
    return f'''// @exec: {{"yield_time_ms": {outer_ms}, "max_output_tokens": 1000}}
const deadline = Date.now() + {budget_ms};
let result = await tools.exec_command({{cmd: {json.dumps(command)}, yield_time_ms: 250, max_output_tokens: 1000, login: false}});
const log = result.output.match(/codex-plus log: (.+)/)?.[1];
let output = result.output;
while (result.session_id !== undefined && Date.now() < deadline) {{
  result = await tools.write_stdin({{session_id: result.session_id, chars: "", yield_time_ms: Math.min(300000, Math.max(1, deadline - Date.now())), max_output_tokens: 1000}});
  output = (output + result.output).slice(-4000);
}}
text({{state: result.session_id === undefined ? "finished" : "budget_expired", exit_code: result.exit_code, session_id: result.session_id, log, output}});
'''


def cell_result(body):
    for entry in body.get('input', []):
        if entry.get('type') != 'custom_tool_call_output' or entry.get('call_id') != 'worker-wait':
            continue
        output = entry['output']
        for item in output if isinstance(output, list) else [{'text': output}]:
            try:
                result = json.loads(item.get('text', ''))
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and 'state' in result:
                return result
    return None


class WorkerShellWaitProvider(ResponsesProvider):
    def __init__(self, command, budget_ms=110000, outer_ms=120000):
        super().__init__(command)
        self.child_requests = 0
        self.root_requests = 0
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                child = any(entry.get('type') == 'agent_message' and entry.get('recipient') == '/root/held'
                            for entry in body.get('input', []))
                with fixture.lock:
                    fixture.requests.append({'time': time.time(), 'body': body, 'child': child})
                    if child:
                        fixture.child_requests += 1
                        number = fixture.child_requests
                    else:
                        fixture.root_requests += 1
                        number = fixture.root_requests
                if child and number == 1:
                    item = {'type': 'custom_tool_call', 'call_id': 'worker-wait', 'name': 'exec',
                            'input': wait_script(command, budget_ms, outer_ms)}
                elif not child and number == 1:
                    item = {'type': 'function_call', 'call_id': 'spawn-held', 'name': 'spawn_agent',
                            'namespace': 'collaboration', 'arguments': json.dumps({
                                'message': 'Run the finite command, wait locally, and return its actual result.',
                                'task_name': 'held', 'fork_turns': 'none'})}
                elif not child and number == 3:
                    item = {'type': 'function_call', 'call_id': 'receive-result', 'name': 'wait_agent',
                            'namespace': 'collaboration', 'arguments': json.dumps({'timeout_ms': 10000})}
                else:
                    text = json.dumps(cell_result(body)) if child else 'parent waiting for child result'
                    item = {'type': 'message', 'role': 'assistant', 'id': f'{child}-{number}',
                            'content': [{'type': 'output_text', 'text': text}]}
                events = [{'type': 'response.created', 'response': {'id': f'{child}-{number}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'{child}-{number}',
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server.RequestHandlerClass = Handler
