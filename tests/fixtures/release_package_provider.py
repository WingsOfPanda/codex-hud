"""Finite Responses fixture for the native release package; loopback only."""

import http.server
import json
import threading


class PackageProvider:
    def __init__(self, code=None):
        self.requests = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fixture.requests.append({'path': self.path, 'body': body})
                number = len(fixture.requests)
                if code and number == 1:
                    item = {'type': 'custom_tool_call', 'call_id': 'package-code-mode',
                            'name': 'exec', 'input': code}
                else:
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': 'PACKAGE_FINISHED'}]}
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

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
