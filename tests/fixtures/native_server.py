"""Loopback-only app-server fixture; never forwards requests to a model."""

import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
import time


def frame(payload, opcode=1):
    size = len(payload)
    header = bytes([128 | opcode, size if size < 126 else 126 if size < 65536 else 127])
    if size >= 126:
        header += struct.pack('!H' if size < 65536 else '!Q', size)
    return header + payload


def receive(sock):
    def take(size):
        result = b''
        while len(result) < size:
            chunk = sock.recv(size - len(result))
            if not chunk:
                raise EOFError
            result += chunk
        return result

    first, second = take(2)
    size = second & 127
    if size in (126, 127):
        size = struct.unpack('!H' if size == 126 else '!Q', take(2 if size == 126 else 8))[0]
    if size > 1024 * 1024:
        raise ValueError('oversized fixture request')
    mask = take(4) if second & 128 else None
    payload = take(size)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return first & 15, payload


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        fixture = self.server.fixture
        sock = self.request
        with fixture.lock:
            fixture.connections.add(sock)
        try:
            header = b''
            while b'\r\n\r\n' not in header:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                header += chunk
            key = next(line.split(b':', 1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key:'))
            accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
            sock.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            while True:
                opcode, payload = receive(sock)
                if opcode == 8:
                    return
                if opcode == 9:
                    sock.sendall(frame(payload, 10))
                    continue
                if opcode != 1:
                    continue
                request = json.loads(payload)
                fixture.requests.append(request)
                if 'id' not in request:
                    continue
                if request['method'] == 'initialize':
                    fixture.available.wait(20)
                if request['method'] == 'thread/list' and request.get('params', {}).get('parentThreadId') == fixture.root and fixture.hold_metadata:
                    fixture.metadata_pending.set()
                    fixture.release_metadata.wait(20)
                result = fixture.reply(request['method'], request.get('params') or {})
                reply = {'id': request['id'], **result}
                sock.sendall(frame(json.dumps(reply).encode()))
        except (EOFError, OSError):
            pass
        except Exception as error:
            fixture.errors.append(repr(error))
        finally:
            with fixture.lock:
                fixture.connections.discard(sock)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AppServer:
    def __init__(self, root, cwd, workers):
        self.root, self.cwd = root, str(cwd)
        self.requests, self.errors = [], []
        self.lock, self.connections = threading.Lock(), set()
        self.available = threading.Event()
        self.available.set()
        self.metadata_pending, self.release_metadata = threading.Event(), threading.Event()
        self.hold_metadata = False
        self.reject_turn_metadata = False
        self.workers = [self.thread(f'{root[:-1]}{index + 2}', parent=root, status='active', name=f'worker_{index + 1}') for index in range(workers)]
        for index, worker in enumerate(self.workers):
            worker['model'] = 'gpt-6.1-sol' if index % 2 == 0 else 'gpt-6-astra'
            worker['reasoningEffort'] = 'high' if index % 2 == 0 else 'medium'
        now = int(time.time())
        for index, worker in enumerate(self.workers):
            worker['createdAt'] = now - max(30, 120 - index * 30)
        self.turns = {worker['id']: [{'id': 'latest', 'items': [], 'status': 'inProgress', 'error': None, 'startedAt': now - 12, 'completedAt': None}] for worker in self.workers}
        self.server = Server(('127.0.0.1', 0), Handler)
        self.server.fixture = self
        self.url = f'ws://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def thread(self, identifier, parent=None, status='idle', name=None):
        return {'id': identifier, 'sessionId': self.root, 'parentThreadId': parent,
                'preview': '', 'ephemeral': False, 'modelProvider': 'openai',
                'createdAt': 1, 'updatedAt': 2, 'status': {'type': status, **({'activeFlags': []} if status == 'active' else {})},
                'cwd': self.cwd, 'cliVersion': '0.162.1', 'source': 'cli' if parent is None else {'subAgent': {'thread_spawn': {'parent_thread_id': parent, 'depth': 1, 'agent_path': f'/root/{name}'}}}, 'name': name, 'turns': []}

    def reply(self, method, params):
        result = None
        if method == 'initialize':
            result = {'userAgent': 'native-hud-smoke'}
        elif method == 'experimentalFeature/list':
            result = {'data': [{'name': name, 'stage': 'stable', 'displayName': None, 'description': None, 'announcement': None, 'enabled': True, 'defaultEnabled': True} for name in ['api_key_model_discovery', 'code_mode_host', 'auth_elicitation']], 'nextCursor': None}
        elif method == 'account/read':
            result = {'account': {'type': 'apiKey'}, 'requiresOpenaiAuth': False}
        elif method in ('model/list', 'collaborationMode/list', 'skills/list', 'app/list', 'mcpServerStatus/list'):
            result = {'data': [], 'nextCursor': None}
        elif method == 'config/read':
            result = {'config': {'model': 'gpt-6-sol', 'model_provider': 'openai', 'analytics': {'enabled': False}, 'tui': {'status_line': ['thread-id']}, 'projects': {self.cwd: {'trust_level': 'trusted'}}}, 'origins': {}, 'layers': []}
        elif method == 'configRequirements/read':
            result = {'requirements': None}
        elif method in ('thread/start', 'thread/resume'):
            result = {'thread': self.thread(self.root), 'model': 'gpt-6-sol', 'modelProvider': 'openai', 'cwd': self.cwd, 'approvalPolicy': 'never', 'approvalsReviewer': 'user', 'sandbox': {'type': 'dangerFullAccess'}, 'reasoningEffort': None}
        elif method == 'thread/read':
            result = {'thread': self.thread(params['threadId'])}
        elif method == 'thread/goal/get':
            result = {'goal': None}
        elif method == 'thread/list':
            if params.get('parentThreadId') is not None:
                assert params.get('parentThreadId') == self.root, params
                assert params.get('useStateDbOnly') is True, params
                assert params.get('sourceKinds') == ['subAgentThreadSpawn'], params
            result = {'data': self.workers if params.get('parentThreadId') == self.root else [], 'nextCursor': None, 'backwardsCursor': None}
        elif method == 'thread/turns/list':
            assert params.get('limit') == 1 and params.get('itemsView') == 'notLoaded', params
            if self.reject_turn_metadata:
                return {'error': {'code': -32603, 'message': 'intentional metadata failure'}}
            result = {'data': self.turns[params['threadId']], 'nextCursor': None, 'backwardsCursor': None}
        if result is None:
            return {'error': {'code': -32601, 'message': f'fixture does not implement {method}'}}
        return {'result': result}

    def disconnect(self):
        self.available.clear()
        with self.lock:
            connections = list(self.connections)
        for sock in connections:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self.available.set()
        self.release_metadata.set()
        self.disconnect()
        self.available.set()
        self.server.shutdown()
        self.server.server_close()
