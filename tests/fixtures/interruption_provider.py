"""A single controlled native followup_task for the exact-worker identity check."""
import json
import time

from .recovery_provider import WorkerProvider


class InterruptionProvider(WorkerProvider):
    def __init__(self):
        super().__init__()
        self.reuse_next = False
        fixture = self
        original = self.server.RequestHandlerClass

        class Handler(original):
            def do_POST(self):
                if not fixture.reuse_next:
                    return super().do_POST()
                fixture.reuse_next = False
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.requests.append({'time': time.time(), 'path': self.path, 'body': body,
                                             'child': False, 'reuse': True})
                    fixture.root_requests += 1
                    number = len(fixture.requests)
                item = {'type': 'function_call', 'call_id': 'reuse-held-child',
                        'name': 'followup_task', 'namespace': 'collaboration',
                        'arguments': json.dumps({'target': 'held', 'message': 'Distinct later child turn.'})}
                events = [{'type': 'response.created', 'response': {'id': f'response-{number}'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': f'response-{number}',
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server.RequestHandlerClass = Handler
