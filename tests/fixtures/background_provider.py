"""Finite, loopback-only Responses sequence for real app-server acceptance."""

import http.server
import json
import threading
import time


class ResponsesProvider:
    def __init__(self, command):
        self.command = command
        self.requests = []
        self.lock = threading.Lock()
        self.final_gate = threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.requests.append({'time': time.time(), 'path': self.path, 'body': body})
                    number = len(fixture.requests)
                if number == 1:
                    item = {'type': 'function_call', 'call_id': 'held-command', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': fixture.command, 'yield_time_ms': 1000,
                                                     'max_output_tokens': 100, 'login': False})}
                else:
                    if number == 2 and not fixture.final_gate.wait(timeout=20):
                        self.send_error(500, 'acceptance final-response gate timed out')
                        return
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': 'parked' if number == 2 else 'resumed'}]}
                events = [
                    {'type': 'response.created', 'response': {'id': f'response-{number}'}},
                    {'type': 'response.output_item.done', 'item': item},
                    {'type': 'response.completed', 'response': {'id': f'response-{number}',
                     'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}},
                ]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
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
        with self.lock:
            return len(self.requests)

    def close(self):
        self.final_gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


