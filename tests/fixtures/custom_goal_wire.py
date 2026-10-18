"""Deliberately lost queue acknowledgement; a wire fault fixture, not a stock daemon."""

import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.native_server import AppServer


class LostAcknowledgementServer(AppServer):
    def __init__(self, home, cwd, root):
        self.trace_path = home / "wire.json"
        self.trace_lock = threading.Lock()
        self.trace = {"enqueues": [], "starts": [], "methods": []}
        socket = home / "app-server-control/app-server-control.sock"
        socket.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(root, cwd, 0, socket_path=socket)

    def reply(self, method, params):
        with self.trace_lock:
            self.trace["methods"].append(method)
            if method == "thread/queue/add":
                self.trace["enqueues"].append(params)
            if method == "thread/queue/start":
                self.trace["starts"].append(params)
            temporary = self.trace_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.trace))
            temporary.replace(self.trace_path)
        if method == "thread/queue/add":
            if os.environ.get("CODEX_PLUS_WIRE_HOLD_ADD") == "1":
                (self.trace_path.parent / "queue-add-entered").touch()
                release = self.trace_path.parent / "queue-add-release"
                deadline = time.monotonic() + 10
                while not release.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert release.exists(), "bounded queue admission fixture expired"
            # There is deliberately no positive queued/consumed evidence afterward.
            self.disconnect()
            self.available.set()
            return {"result": {"queuedSubmission": {"id": "lost-ack"}}}
        if method == "config/read":
            return {"result": {"config": {"features": {"goals": False}}, "origins": {}, "layers": []}}
        if method == "thread/queue/list" and (self.trace_path.parent / "pending-queue.json").exists():
            return {"result": {"data": json.loads((self.trace_path.parent / "pending-queue.json").read_text()),
                               "nextCursor": None}}
        if method in ("thread/queue/list", "thread/items/list", "thread/turns/list",
                      "thread/backgroundTerminals/list"):
            return {"result": {"data": [], "nextCursor": None}}
        return super().reply(method, params)


if __name__ == "__main__":
    home, cwd, root = map(Path, sys.argv[1:4])
    server = LostAcknowledgementServer(home, cwd, str(root))
    try:
        threading.Event().wait(90)
    finally:
        server.close()
