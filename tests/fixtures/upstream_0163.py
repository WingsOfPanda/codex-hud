"""Scripted loopback Responses provider for upstream boundary acceptance."""

import http.server
import json
import threading
import time

from .background_provider import ResponsesProvider


def message(text='done'):
    return {'type': 'message', 'role': 'assistant', 'id': 'fixture-message',
            'content': [{'type': 'output_text', 'text': text}]}


def function(name, arguments, ident, namespace=None):
    return dict(type='function_call', name=name, arguments=json.dumps(arguments),
                call_id=ident, **({'namespace': namespace} if namespace else {}))


def execute(code, ident='fixture-exec'):
    return dict(type='custom_tool_call', name='exec', input=code, call_id=ident)


class BoundaryProvider(ResponsesProvider):
    def __init__(self):
        super().__init__(None)
        self.parent = None
        self.root_steps = []
        self.child_gates = []
        self.gates = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                thread = self.headers.get('thread-id')
                child = thread != fixture.parent
                with fixture.lock:
                    if child:
                        number = sum(r['child'] for r in fixture.requests)
                        gate = fixture.child_gates[number] if number < len(fixture.child_gates) else None
                        item = message(f'child result {number}')
                    else:
                        item, gate = fixture.root_steps.pop(0) if fixture.root_steps else (message(), None)
                    fixture.requests.append(dict(time=time.time(), body=body, thread=thread, child=child))
                    number = len(fixture.requests)
                if gate is not None and not gate.wait(45):
                    self.send_error(500, 'boundary fixture gate expired')
                    return
                events = [dict(type='response.created', response={'id': f'response-{number}'}),
                          dict(type='response.output_item.done', item=item),
                          dict(type='response.completed', response={'id': f'response-{number}',
                               'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}})]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Interrupting a turn intentionally abandons its response.

        self.server.RequestHandlerClass = Handler

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def close(self):
        for gate in self.gates:
            gate.set()
        super().close()
