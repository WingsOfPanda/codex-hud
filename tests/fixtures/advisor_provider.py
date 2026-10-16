"""Loopback-only Responses fixture for the stock advisor exec acceptance."""
import http.server
import json
import threading
import time


RESULT = {'verdict': 'revise', 'summary': 'The sandbox denied the attempted write.',
          'findings': [{'severity': 'low', 'message': 'Read-only inspection completed.'}],
          'next_steps': ['Keep the consultation read-only.']}


class AdvisorProvider:
    def __init__(self, command):
        self.command, self.mode = command, 'success'
        self.requests = []
        self.offset = 0
        self.entered, self.release = threading.Event(), threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append({'time': time.time(), 'path': self.path, 'body': body})
                number = len(fixture.requests) - fixture.offset
                if fixture.mode == 'failure':
                    self.send_error(500, 'intentional advisor provider failure')
                    return
                if fixture.mode == 'held':
                    fixture.entered.set()
                    fixture.release.wait(20)
                if number == 1 and fixture.mode == 'success':
                    item = {'type': 'function_call', 'call_id': 'read-only-probe', 'name': 'exec_command',
                            'arguments': json.dumps({'cmd': fixture.command, 'yield_time_ms': 1000,
                                                     'max_output_tokens': 300, 'login': False})}
                else:
                    text = '{invalid-json' if fixture.mode == 'invalid' else json.dumps(RESULT)
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': text}]}
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
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def reset(self, mode):
        self.mode, self.offset = mode, len(self.requests)
        self.entered.clear()
        self.release.clear()

    @property
    def count(self):
        return len(self.requests)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
