"""Bounded JSON RPC over the local daemon's Unix WebSocket."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time


def peer_generation(sock):
    """Identify the connected Linux daemon across both PID reuse and host reboots."""
    pid, _, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    return f'{boot}:{pid}:{start}'


class RpcError(ValueError):
    def __init__(self, method, error):
        self.method, self.error = method, error
        super().__init__(f"{method}: {error.get('message', 'RPC error')}")


class Client:
    """Minimal RFC 6455 transport for the existing local daemon control socket."""
    def __init__(self, path, timeout=5):
        self.sock = socket.socket(socket.AF_UNIX)
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.sock.settimeout(timeout)
        self.buffer = b''
        self.sequence = 0
        try:
            self.sock.connect(str(path))
            self.generation = peer_generation(self.sock)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f'GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            while b'\r\n\r\n' not in self.buffer:
                self.buffer += self.read_socket()
                if len(self.buffer) > 8192:
                    raise ValueError('oversized handshake')
            header, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            headers = dict(line.split(b':', 1) for line in header.split(b'\r\n')[1:] if b':' in line)
            if not header.startswith(b'HTTP/1.1 101 ') or not any(k.lower() == b'sec-websocket-accept' and v.strip() == accept for k, v in headers.items()):
                raise ValueError('invalid websocket handshake')
            self.initialize_result = self.call('initialize', {'clientInfo': {'name': 'codex_hud_background', 'version': '0.1'}, 'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def read_socket(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("daemon response timed out")
        self.sock.settimeout(remaining)
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError('daemon disconnected')
        return data

    def take(self, size):
        while len(self.buffer) < size:
            self.buffer += self.read_socket()
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def frame(self, payload, opcode=1):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode, 0x80 | (size if size < 126 else 126 if size < 65536 else 127)])
        if size >= 126:
            header += struct.pack('!H' if size < 65536 else '!Q', size)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, value):
        self.frame(json.dumps(value).encode())

    def receive(self):
        parts = bytearray()
        fragmented = False
        while True:
            first, second = self.take(2)
            if first & 0x70:
                raise ValueError('unsupported daemon frame extension')
            opcode, size = first & 15, second & 127
            if size in (126, 127):
                size = struct.unpack('!H' if size == 126 else '!Q', self.take(2 if size == 126 else 8))[0]
            if second & 128 or size > 16 * 1024 * 1024 or len(parts) + size > 16 * 1024 * 1024:
                raise ValueError('invalid or oversized daemon frame')
            if opcode >= 8 and (not first & 128 or size > 125):
                raise ValueError('invalid daemon control frame')
            payload = self.take(size)
            if opcode == 8:
                raise ConnectionError('daemon closed connection')
            if opcode == 9:
                self.frame(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != fragmented:
                raise ValueError('unexpected daemon frame')
            fragmented = not bool(first & 128)
            parts.extend(payload)
            if first & 128:
                return json.loads(parts)

    def call(self, method, params):
        self.deadline = time.monotonic() + self.timeout
        self.sock.settimeout(self.timeout)
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = self.deadline
        while time.monotonic() < deadline:
            response = self.receive()
            if not isinstance(response, dict):
                raise ValueError('malformed daemon response')
            if response.get('id') != request_id:
                continue
            if 'error' in response:
                raise RpcError(method, response['error'])
            result = response.get('result')
            if not isinstance(result, dict):
                raise ValueError('missing daemon result')
            return result
        raise TimeoutError('daemon response timed out')

