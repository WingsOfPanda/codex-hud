"""Hold one additional model response to leave a real native queue pending."""

import threading
import http.server
import json
import time

from .background_provider import ResponsesProvider


class RecoveryProvider(ResponsesProvider):
    def __init__(self, command):
        super().__init__(command)
        self.hold_next = False
        self.hold_started = threading.Event()
        self.hold_release = threading.Event()
        fixture = self
        original = self.server.RequestHandlerClass

        class Handler(original):
            def do_POST(self):
                if fixture.hold_next:
                    fixture.hold_next = False
                    fixture.hold_started.set()
                    if not fixture.hold_release.wait(timeout=30):
                        self.send_error(500, 'recovery hold timed out')
                        return
                try:
                    super().do_POST()
                except (BrokenPipeError, ConnectionResetError):
                    # A restarted daemon intentionally abandons the held request.
                    pass

        self.server.RequestHandlerClass = Handler

    def close(self):
        self.hold_release.set()
        super().close()


class WorkerProvider(ResponsesProvider):
    """Spawn a real child and hold its first response through daemon restart."""
    def __init__(self):
        super().__init__('')
        self.child_gate = threading.Event()
        self.child_started = threading.Event()
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
                    fixture.requests.append({'time': time.time(), 'path': self.path, 'body': body, 'child': child})
                    if not child:
                        fixture.root_requests += 1
                    number = len(fixture.requests)
                    root_number = fixture.root_requests
                if child:
                    fixture.child_started.set()
                    if not fixture.child_gate.wait(30):
                        self.send_error(500, 'child hold timed out')
                        return
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': 'child done'}]}
                elif root_number == 1:
                    item = {'type': 'function_call', 'call_id': 'spawn-held-child', 'name': 'spawn_agent',
                            'namespace': 'collaboration',
                            'arguments': json.dumps({'message': 'RECOVERY_CHILD_HELD', 'task_name': 'held',
                                                     'fork_turns': 'none'})}
                else:
                    if root_number == 2 and not fixture.final_gate.wait(20):
                        self.send_error(500, 'parent hold timed out')
                        return
                    item = {'type': 'message', 'role': 'assistant', 'id': f'message-{number}',
                            'content': [{'type': 'output_text', 'text': 'parked' if root_number == 2 else 'resumed'}]}
                events = [{'type': 'response.created', 'response': {'id': f'response-{number}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'response-{number}',
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server.RequestHandlerClass = Handler

    def close(self):
        self.child_gate.set()
        super().close()
