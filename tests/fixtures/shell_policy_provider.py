"""Finite shell-policy Responses fixture; all requests stay on loopback."""

import http.server
import json
import re
import threading
import time


class ShellPolicyProvider:
    def __init__(self, command, yield_time_ms=10000, polls=0):
        self.requests = []
        self.final_gate = threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append({'time': time.time(), 'path': self.path, 'body': body})
                number = len(fixture.requests)
                if number == 1:
                    item = {'type': 'function_call', 'call_id': 'policy-command', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': command, 'yield_time_ms': yield_time_ms,
                                                     'max_output_tokens': 10000, 'login': False})}
                elif number <= polls + 1:
                    session = re.search(r'Process running with session ID (\d+)', json.dumps(body))
                    if session is None:
                        self.send_error(500, 'shell-policy poll missing native session ID')
                        return
                    item = {'type': 'function_call', 'call_id': f'policy-poll-{number}', 'name': 'write_stdin',
                            'arguments': json.dumps({'session_id': int(session[1]), 'chars': '',
                                                     'yield_time_ms': 1000, 'max_output_tokens': 10000})}
                else:
                    if number == polls + 2 and not fixture.final_gate.wait(30):
                        self.send_error(500, 'shell-policy final gate timed out')
                        return
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': 'fixture-finished'}]}
                events = [
                    {'type': 'response.created', 'response': {'id': f'response-{number}'}},
                    {'type': 'response.output_item.done', 'item': item},
                    {'type': 'response.completed', 'response': {'id': f'response-{number}',
                     'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}},
                ]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                                  for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def count(self):
        return len(self.requests)

    def close(self):
        self.final_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
