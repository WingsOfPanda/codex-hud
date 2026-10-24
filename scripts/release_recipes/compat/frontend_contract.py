"""Exact final source/patch identity: no retained frontend receipt qualifies candidate."""
from recipe_context import CONFIG, DATA, OLD_VERSION, NEW_VERSION, UPSTREAM, source_identity
import re

SOURCE = CONFIG['source_commit']
PATCH = CONFIG['frontend_patch_sha256']


def validate_frontend(binary_commit, fixture_commit, binary_patch, fixture_patch,
                      binary_sha, host_sha, **optional):
    assert re.fullmatch('[0-9a-f]{40}', SOURCE), 'seal the final source first'
    assert binary_commit == fixture_commit == SOURCE
    assert binary_patch == fixture_patch == PATCH
    assert re.fullmatch('[0-9a-f]{64}', binary_sha)
    assert re.fullmatch('[0-9a-f]{64}', host_sha)
    assert optional.get('receipt_bytes') is None, 'retained frontend reuse is forbidden for changed candidate patch'
