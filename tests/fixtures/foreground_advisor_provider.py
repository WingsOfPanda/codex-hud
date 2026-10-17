"""Root Responses fixture: consult once in one cell, then review the result."""
import http.server
import json
import time

from .background_provider import ResponsesProvider


class ForegroundAdvisorProvider(ResponsesProvider):
    def __init__(self, script):
        super().__init__(None)
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.requests.append({'time': time.time(), 'body': body})
                    number = len(fixture.requests)
                if number == 1:
                    item = {'type': 'custom_tool_call', 'call_id': 'worker-wait', 'name': 'exec',
                            'input': script}
                else:
                    item = {'type': 'message', 'role': 'assistant', 'id': f'review-{number}',
                            'content': [{'type': 'output_text', 'text': 'Reviewed advisor result.'}]}
                events = [{'type': 'response.created', 'response': {'id': str(number)}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': str(number),
                           'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
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

        self.server.RequestHandlerClass = Handler
