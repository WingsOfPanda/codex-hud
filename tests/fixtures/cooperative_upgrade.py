"""Private package instrumentation and a loopback two-command upgrade provider."""

import hashlib
import http.server
import json
import os
from pathlib import Path
import sys
import time

from .background_provider import ResponsesProvider


def install(background, label):
    """Trace the real entry point; gates never replace coordinator decisions."""
    directory = Path(os.environ["UPGRADE_TRACE"])

    def event(kind, **values):
        record = dict(kind=kind, package=label, pid=os.getpid(), time=time.time(), **values)
        fd = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)

    event("boot", action=sys.argv[1], source_sha256=hashlib.sha256(Path(background.__file__).read_bytes()).hexdigest())
    original_store = background.Store.__init__

    def store_init(store, *args, **kwargs):
        event("store_open", action=sys.argv[1])
        return original_store(store, *args, **kwargs)

    background.Store.__init__ = store_init
    original_observe = background.Store.observe_job

    def observe(store, job, *args, **kwargs):
        result = original_observe(store, job, *args, **kwargs)
        if kwargs.get("running"):
            event("running", job=job["id"], observer=job["observer_run"])
        return result

    background.Store.observe_job = observe
    used = False

    def gate(point):
        nonlocal used
        if used or label != "A" or point != os.environ.get("UPGRADE_POINT"):
            return
        used = True
        event("gate", point=point)
        deadline = time.monotonic() + 40
        while not (directory / "release-gate").exists():
            if time.monotonic() >= deadline:
                raise AssertionError("private upgrade gate timed out")
            time.sleep(.02)
        event("gate_released", point=point)
        if os.environ.get("UPGRADE_CRASH") == "1":
            event("crash", point=point)
            os._exit(79)

    original_init = background.Observer.__init__

    def observer_init(observer, *args, **kwargs):
        original_init(observer, *args, **kwargs)

        def fault(point):
            if point == "after_start":
                event("explicit_start_ack")
            gate(point)

        observer.fault = fault
        event("observer", run=observer.run_id)

    background.Observer.__init__ = observer_init
    original_run = background.Observer.run

    def run(observer, *args, **kwargs):
        result = original_run(observer, *args, **kwargs)
        event("observer_exit", result=result)
        return result

    background.Observer.run = run
    original_step = background.Observer._step

    def step(observer):
        result = original_step(observer)
        # Stock can consume add automatically; recheck history every settled cycle.
        if label == "A" and not used and os.environ.get("UPGRADE_POINT") == "after_start":
            entries = observer.call("thread/items/list", {"threadId": observer.parent})["data"]
            clients = {entry.get("item", {}).get("clientId") for entry in entries}
            for callback in observer.store.status(observer.parent)["callbacks"]:
                if callback["client"] in clients:
                    event("native_consumed", client=callback["client"])
                    gate("after_start")
                    break
        gate("cycle")
        if not result:
            gate("completed_cycle")
        return result

    background.Observer._step = step


class UpgradeProvider(ResponsesProvider):
    """Two independent native executions leave work after the first callback."""

    def __init__(self, commands):
        super().__init__(None)
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    fixture.requests.append({"time": time.time(), "path": self.path, "body": body})
                    number = len(fixture.requests)
                if number == 1:
                    output = [{"type": "function_call", "call_id": f"held-{index}",
                               "name": "exec_command", "arguments": json.dumps({
                                   "cmd": command, "yield_time_ms": 1000,
                                   "max_output_tokens": 100, "login": False})}
                              for index, command in enumerate(commands)]
                else:
                    if number == 2 and not fixture.final_gate.wait(timeout=30):
                        self.send_error(500, "private upgrade initial turn gate timed out")
                        return
                    output = [{"type": "message", "role": "assistant", "id": f"message-{number}",
                               "content": [{"type": "output_text", "text": "parked" if number == 2 else "resumed"}]}]
                events = [{"type": "response.created", "response": {"id": f"response-{number}"}}]
                events.extend({"type": "response.output_item.done", "item": item} for item in output)
                events.append({"type": "response.completed", "response": {"id": f"response-{number}",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})
                payload = "".join(f"event: {entry['type']}\ndata: {json.dumps(entry)}\n\n" for entry in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server.RequestHandlerClass = Handler
