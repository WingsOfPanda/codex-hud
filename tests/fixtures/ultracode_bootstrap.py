"""Private stock-daemon bootstrap probe; every provider request stays loopback."""

import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from fixtures.background_provider import ResponsesProvider
from fixtures.upstream_0163 import message
from test_background_integration import RecordingClient, stop, wait
from codex_plus.rpc import RpcError


class BootstrapProvider(ResponsesProvider):
    """Script usage counts as well as output so compaction is deterministic."""

    def __init__(self):
        super().__init__(None)
        self.parent = None
        self.root_steps = []
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fixture.lock:
                    item, tokens = fixture.root_steps.pop(0) if fixture.root_steps else (message(), 0)
                    fixture.requests.append(dict(time=time.time(), path=self.path, body=body,
                        thread=self.headers.get("thread-id"), scripted_tokens=tokens or 0))
                    number = len(fixture.requests)
                events = [dict(type="response.created", response={"id": f"response-{number}"}),
                          dict(type="response.output_item.done", item=item),
                          dict(type="response.completed", response={"id": f"response-{number}",
                               "usage": {"input_tokens": tokens or 0, "output_tokens": 0,
                                         "total_tokens": tokens or 0}})]
                payload = "".join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n'
                                  for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server.RequestHandlerClass = Handler


class BootstrapServer:
    def __init__(self, binary, evidence, *, remote=False, pressure=False):
        self.binary, self.evidence = binary, evidence
        self.remote = remote
        evidence.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="uc-")
        self.root = Path(self.temporary.name)
        self.home, self.work = self.root / "home", self.root / "work"
        self.home.mkdir(mode=0o700)
        self.work.mkdir()
        self.provider = BootstrapProvider()
        self.env = {key: value for key, value in os.environ.items()
                    if not any(secret in key.upper() for secret in
                               ("API_KEY", "AUTH_TOKEN", "BASE_URL"))}
        self.env.update(CODEX_HOME=str(self.home), RUST_LOG="warn")
        self.env.pop("CODEX_THREAD_ID", None)
        (self.home / "config.toml").write_text(f'''model="gpt-6.1-sol"
model_provider="isolated"
model_reasoning_effort="high"
analytics.enabled=false
features.daemon_auto_start=false
features.plugins=false
features.goals=false
features.retain_client_developer_messages={'true' if pressure else 'false'}
model_auto_compact_token_limit={200000 if pressure else 1000000}
[model_providers.isolated]
name="loopback bootstrap acceptance"
base_url="{self.provider.url}"
wire_api="responses"
requires_openai_auth=false
request_max_retries=0
stream_max_retries=0
capabilities.remote_compaction="{'v2' if remote else 'unsupported'}"
''')
        self.log = (evidence / "daemon.log").open("w")
        self.rpc = self.native = None
        self.messages = []
        self.report = {"binary": str(binary), "stock_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                       "stock_version": subprocess.check_output([str(binary), "--version"], text=True).strip(),
                       "remote_model_requests": 0, "checks": [],
                       "route": "remote v2 compaction" if remote else "local responses compaction"}

    def launch(self):
        self.native = subprocess.Popen([str(self.binary), "app-server", "--listen", "unix://"],
            env=self.env, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            start_new_session=True)

        def connect():
            assert self.native.poll() is None, "private daemon exited: inspect daemon.log"
            try:
                return RecordingClient(self.home / "app-server-control/app-server-control.sock")
            except (OSError, ValueError):
                return None

        self.rpc = wait(connect, "private daemon did not accept RPC")
        assert not self.rpc.initialize_result.get("queueIdempotency"), "official stock binary required"

    def restart(self):
        self.messages.extend(self.rpc.messages)
        self.rpc.close()
        self.rpc = None
        stop(self.native)
        self.launch()

    def terminal(self, ident):
        def check():
            self.rpc.call("thread/read", {"threadId": self.parent})
            current = next((message["params"]["turn"] for message in self.rpc.messages
                if message.get("method") == "turn/completed"
                and message["params"]["threadId"] == self.parent
                and message["params"]["turn"]["id"] == ident), None)
            if current:
                assert current["status"] == "completed", current
                return current
        return wait(check, "stock turn did not complete")

    def turn(self, text):
        before = self.provider.count
        ident = self.rpc.call("turn/start", {"threadId": self.parent,
            "input": [{"type": "text", "text": text}], "effort": "high"})["turn"]["id"]
        self.terminal(ident)
        assert self.provider.count == before + 1, "unexpected provider request count"
        return self.provider.requests[-1]["body"]

    def queued_wake(self, text):
        before = self.provider.count
        message_start = len(self.rpc.messages)
        submission = self.rpc.call("thread/queue/add", {"threadId": self.parent,
            "clientUserMessageId": f"ultracode-wake-{before}",
            "input": [{"type": "text", "text": text}]})["queuedSubmission"]
        try:
            turn = self.rpc.call("thread/queue/start", {"threadId": self.parent,
                "queuedSubmissionId": submission["id"]})["turn"]
            dispatch = "explicit queue/start"
        except RpcError as error:
            assert "queued submission not found" in str(error), error
            assert not self.rpc.call("thread/queue/list", {"threadId": self.parent})["data"]
            def started():
                self.rpc.call("thread/read", {"threadId": self.parent})
                return next((event["params"]["turn"] for event in self.rpc.messages[message_start:]
                    if event.get("method") == "turn/started"
                    and event["params"]["threadId"] == self.parent), None)
            turn = wait(started, "consumed queue item has no turn receipt")
            dispatch = "automatic queue/add idle dispatch"
        self.terminal(turn["id"])
        assert self.provider.count == before + 1, "queued wake caused extra provider requests"
        assert text in json.dumps(self.provider.requests[-1]["body"]), "queued input missing from admitted request"
        self.report.setdefault("queued_dispatch", []).append({"turn": turn["id"], "kind": dispatch})
        return self.provider.requests[-1]["body"]

    def automatic_compact(self, text):
        before = self.provider.count
        message_start = len(self.rpc.messages)
        tool = {"type": "function_call", "name": "unknown_ultracode_fixture_tool",
                "arguments": "{}", "call_id": f"ultracode-midturn-{before}"}
        summary = ({"type": "compaction", "encrypted_content": f"AUTO_SUMMARY_{before}"}
                   if self.remote else message("local automatic summary"))
        self.provider.root_steps.extend([(tool, 330000), (summary, 200), (message("after mid-turn compact"), 120)])
        ident = self.rpc.call("turn/start", {"threadId": self.parent,
            "input": [{"type": "text", "text": text}], "effort": "high"})["turn"]["id"]
        self.terminal(ident)
        assert self.provider.count == before + 3, "expected tool, automatic compaction, continuation"
        events = self.rpc.messages[message_start:]
        assert any(event.get("method") == "item/completed"
                   and event["params"].get("turnId") == ident
                   and event["params"]["item"]["type"] == "contextCompaction" for event in events), "no same-turn compaction receipt"
        compact = self.provider.requests[before + 1]["body"]
        assert any(item.get("type") == "compaction_trigger" for item in compact["input"]) == self.remote
        return self.provider.requests[before:]

    def compact(self):
        before = self.provider.count
        message_start = len(self.rpc.messages)
        if self.remote:
            self.provider.root_steps.append(({"type": "compaction",
                "encrypted_content": f"ULTRACODE_BOOTSTRAP_SUMMARY_{before}"}, 0))
        self.rpc.call("thread/compact/start", {"threadId": self.parent})
        wait(lambda: self.provider.count > before, "compaction provider request missing")
        def done():
            self.rpc.call("thread/read", {"threadId": self.parent})
            return next((message["params"]["turnId"] for message in self.rpc.messages[message_start:]
                if message.get("method") == "item/completed"
                and message["params"]["threadId"] == self.parent
                and message["params"]["item"]["type"] == "contextCompaction"), None)
        ident = wait(done, "compaction did not complete")
        self.terminal(ident)
        if self.remote:
            assert any(item.get("type") == "compaction_trigger"
                       for item in self.provider.requests[before]["body"]["input"]), "remote route not exercised"

    def close(self, passed):
        if self.rpc:
            self.messages.extend(self.rpc.messages)
            self.rpc.close()
        if self.native:
            stop(self.native)
        self.provider.close()
        self.log.close()
        self.report.update(passed=passed, provider_requests=self.provider.requests, rpc_messages=self.messages)
        (self.evidence / "report.json").write_text(json.dumps(self.report, indent=2) + "\n")
        self.temporary.cleanup()
