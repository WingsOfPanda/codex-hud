"""Reuse the accepted archive validator; binding and execution belong to root."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile

AREA = Path(__file__).resolve().parent
from recipe_context import UPSTREAM
spec = importlib.util.spec_from_file_location("compat_gate", AREA / "verify-mixes.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def archive_identity(path, architecture, version, commit, expected_manifest=None):
    path = Path(path).resolve(strict=True)
    root = f"codex-plus-{UPSTREAM['version']}-plus.{version}-{architecture}-unknown-linux-gnu"
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        assert len(names) == len(set(names)), "duplicate archive member"
        member = archive.getmember(root + "/codex-plus.json")
        assert member.isfile()
        raw = archive.extractfile(member).read()
        manifest = json.loads(raw)
        assert manifest["schema_version"] == 1
        assert manifest["release"] == f"{UPSTREAM['version']}-plus.{version}"
        assert manifest["plus_commit"] == commit
        assert manifest["target"] == architecture + "-unknown-linux-gnu"
        assert type(manifest["coordinator_protocol"]) is int and manifest["coordinator_protocol"] == 5
        assert type(manifest["execution_record_format"]) is int and manifest["execution_record_format"] == 1
        assert manifest["upstream"] == UPSTREAM
        manifest_sha = hashlib.sha256(raw).hexdigest()
        if expected_manifest is not None:
            assert manifest_sha == expected_manifest, "old release manifest pin mismatch"
        tree_sha = hashlib.sha256(gate.canonical(manifest["files"]).encode("ascii")).hexdigest()
        assert tree_sha == manifest["tree_sha256"], "manifest tree hash mismatch"
        helper_member = archive.getmember(root + "/bin/codex-plus-runtime")
        assert helper_member.isfile()
        helper = archive.extractfile(helper_member)
        helper_sha = hashlib.file_digest(helper, "sha256").hexdigest()
        assert helper_sha == manifest["files"]["bin/codex-plus-runtime"]["sha256"]
    return {"archive": str(path), "archive_sha256": gate.digest(path),
            "manifest_sha256": manifest_sha, "tree_sha256": tree_sha,
            "helper_sha256": helper_sha, "release": manifest["release"],
            "plus_commit": commit, "target": manifest["target"]}
