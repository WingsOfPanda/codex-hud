"""Qualify fixed developer bootstrap against an official stock daemon.

Run: python3 tests/test_ultracode_bootstrap.py --bootstrap-file POLICY --binary CODEX
"""

import argparse
import hashlib
import json
from pathlib import Path

from fixtures.ultracode_bootstrap import BootstrapServer


EXISTING = "Existing user developer instructions: preserve bootstrap fixture sentinel 81073."
REPLACEMENT = "LOADED_RESUME_OVERRIDE_MUST_NOT_REPLACE_STARTUP_BOOTSTRAP_81073"


def developer_text(body):
    return "\n".join(part.get("text", "") for item in body.get("input", [])
                     if item.get("role") == "developer" for part in item.get("content", []))


def run(binary, evidence, bootstrap, *, remote=False, pressure=False):
    assert bootstrap.strip(), "bootstrap must not be empty"
    server = BootstrapServer(binary, evidence, remote=remote, pressure=pressure)
    passed = False
    instructions = EXISTING + "\n\n" + bootstrap
    server.report.update(bootstrap_sha256=hashlib.sha256(bootstrap.encode()).hexdigest(),
                         bootstrap=bootstrap, tested_automatic_compaction=False,
                         tested_budget_eviction=False, tested_remote_compaction=remote)
    try:
        server.launch()
        server.parent = server.rpc.call("thread/start", {"cwd": str(server.work),
            "developerInstructions": instructions, "approvalPolicy": "never",
            "sandbox": "danger-full-access"})["thread"]["id"]
        server.provider.parent = server.parent

        def check(label):
            body = server.turn(label)
            text = developer_text(body)
            assert bootstrap in text, f"complete bootstrap missing: {label}"
            assert EXISTING in text, f"existing instructions missing: {label}"
            assert REPLACEMENT not in text, f"loaded resume changed instructions: {label}"
            server.report["checks"].append({"name": label, "request": server.provider.count,
                "bootstrap_complete": True, "existing_instructions_preserved": True})

        check("fresh root")
        for number in range(2):
            server.compact()
            check(f"after manual compaction {number + 1}")
        server.rpc.call("thread/resume", {"threadId": server.parent,
            "developerInstructions": REPLACEMENT})
        check("loaded resume ignores developer override")
        server.restart()
        server.rpc.call("thread/resume", {"threadId": server.parent,
            "developerInstructions": instructions})
        check("cold resume after daemon restart with startup instructions")
        server.compact()
        check("cold resume followed by manual compaction")

        if pressure:
            for number in range(2):
                # More than the pinned 64k retained-client-message budget, independent of bootstrap.
                noise = f"ULTRACODE_NOISE_{number}:" + "n" * (70000 * 4)
                server.rpc.call("thread/inject_items", {"threadId": server.parent,
                    "items": [{"type": "message", "role": "developer",
                               "content": [{"type": "input_text", "text": noise}]}]})
                requests = server.automatic_compact(f"exercise automatic mid-turn compaction {number + 1}")
                for offset, request in enumerate(requests):
                    text = developer_text(request["body"])
                    assert bootstrap in text and EXISTING in text, f"bootstrap lost at automatic request {offset}"
                assert noise in developer_text(requests[0]["body"]), "budget pressure missing"
                after = developer_text(requests[-1]["body"])
                noise_size = after.count("n")
                if remote:
                    assert noise not in after, "oversized retained client message did not encounter budget"
                    assert f"ULTRACODE_NOISE_{number}:" in after, "latest retained client noise missing"
                    if number:
                        assert f"ULTRACODE_NOISE_{number - 1}:" not in after, "older noise was not evicted by budget"
                server.report["checks"].append({"name": f"automatic mid-turn compaction with budget pressure {number + 1}",
                    "bootstrap_complete": True, "existing_instructions_preserved": True,
                    "injected_noise_characters": len(noise), "post_compact_n_characters": noise_size,
                    "older_noise_evicted": remote and number > 0,
                    "request_count": len(requests), "same_turn_compaction_receipt": True})
            server.report.update(tested_automatic_compaction=True, tested_budget_pressure=True,
                                 tested_budget_eviction=remote)
            server.restart()
            server.rpc.call("thread/resume", {"threadId": server.parent,
                "developerInstructions": instructions})
            for mode in ("on", "off"):
                body = server.queued_wake(f"LIVE COMPLETION fixture; requested Ultracode {mode}; inspect results")
                text = developer_text(body)
                assert bootstrap in text and EXISTING in text
                server.report["checks"].append({"name": f"cold-resumed queued wake with {mode} payload",
                    "bootstrap_complete": True, "existing_instructions_preserved": True})
            server.report["tested_runtime_mode_persistence"] = False

            server.restart()
            server.rpc.call("thread/resume", {"threadId": server.parent,
                "excludeTurns": True, "config": {"features.goals": False}})
            text = developer_text(server.queued_wake("LIVE COMPLETION cold observer without developer override"))
            assert bootstrap in text and EXISTING in text, "ordinary cold observer wake lost historical bootstrap"
            requests = server.automatic_compact("cold observer turn triggering automatic compaction")
            assert bootstrap in developer_text(requests[0]["body"])
            after = developer_text(requests[-1]["body"])
            assert bootstrap not in after and EXISTING not in after, "negative control changed: reassess cold observer route"
            server.report["cold_observer_without_developer_override_safe"] = False
            server.report["checks"].append({"name": "cold observer without developer override loses bootstrap at automatic compaction",
                "ordinary_wake_bootstrap_present": True, "post_compaction_bootstrap_absent": True,
                "post_compaction_existing_instructions_absent": True,
                "same_turn_compaction_receipt": True})

        server.parent = server.rpc.call("thread/start", {"cwd": str(server.work),
            "developerInstructions": EXISTING, "approvalPolicy": "never",
            "sandbox": "danger-full-access"})["thread"]["id"]
        server.provider.parent = server.parent
        text = developer_text(server.turn("legacy root before bootstrap upgrade"))
        assert EXISTING in text and bootstrap not in text
        server.rpc.call("thread/resume", {"threadId": server.parent,
            "developerInstructions": instructions})
        text = developer_text(server.turn("loaded legacy root ignores bootstrap upgrade"))
        assert EXISTING in text and bootstrap not in text
        server.report["checks"].append({"name": "loaded legacy root ignores bootstrap upgrade",
            "bootstrap_absent": True, "request": server.provider.count})
        server.restart()
        server.rpc.call("thread/resume", {"threadId": server.parent,
            "developerInstructions": instructions})
        text = developer_text(server.turn("cold legacy resume before compaction"))
        assert EXISTING in text and bootstrap not in text
        server.report["checks"].append({"name": "cold legacy resume does not retrofit ordinary history",
            "bootstrap_absent": True, "request": server.provider.count})
        server.compact()
        check("cold legacy bootstrap upgrade survives manual compaction")
        passed = True
    finally:
        server.close(passed)
    print(json.dumps({"passed": passed, "checks": server.report["checks"],
                      "evidence": str(evidence / "report.json")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path(".build/upstream-0163/official-x86/bin/codex"))
    parser.add_argument("--evidence", type=Path, default=Path(".build/ultracode/bootstrap"))
    parser.add_argument("--bootstrap-file", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs/ultracode-bootstrap.txt")
    parser.add_argument("--remote", action="store_true", help="qualify Responses compaction v2")
    parser.add_argument("--pressure", action="store_true", help="also qualify automatic compaction and queued wakes")
    args = parser.parse_args()
    run(args.binary.resolve(), args.evidence.resolve(),
        args.bootstrap_file.read_text(), remote=args.remote, pressure=args.pressure)
