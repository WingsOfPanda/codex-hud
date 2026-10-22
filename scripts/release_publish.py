"""Read-only publication checks; this module never creates, uploads or publishes."""
import argparse
import hashlib
import json
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def resolve_identity(view, graphql, expected_tag):
    node = graphql['data']['node']
    require(view['tagName'] == node['tagName'] == expected_tag, 'Release tag mismatch')
    require(view['id'] == node['id'], 'Release node mismatch')
    require(type(node['databaseId']) is int and node['databaseId'] > 0, 'Missing exact release ID')
    require(type(node['isDraft']) is bool and view['isDraft'] == node['isDraft'], 'Release state mismatch')
    return dict(tag=expected_tag, release_id=node['databaseId'], node_id=node['id'], draft=node['isDraft'])


def verify_release(handoff_bytes, assets, release, *, release_id, tag_commit, expected_commit,
                   downloaded_handoff, draft):
    handoff = json.loads(handoff_bytes)
    require(type(draft) is bool and type(release['draft']) is bool and release['draft'] == draft,
            'Unexpected draft/public state')
    require(release['id'] == release_id and release['tag_name'] == handoff['release'], 'Wrong release identity')
    require(re.fullmatch(r'[0-9a-f]{40}', expected_commit) and tag_commit == expected_commit,
            'Tag does not point at the reviewed companion commit')
    require(set(handoff['targets']) == {'x86_64', 'aarch64'}, 'Both native targets are required')
    expected = {}
    for row in assets:
        require(row['name'] not in expected, 'Duplicate expected asset')
        require(re.fullmatch(r'[0-9a-f]{64}', row['sha256']) and type(row['size']) is int and row['size'] >= 0,
                'Invalid expected asset identity')
        expected[row['name']] = row
    names = {'handoff.json'}
    for target in handoff['targets'].values():
        names.update((target['asset'], target['asset']+'.sha256'))
        require(target['asset'] in expected and expected[target['asset']]['sha256'] == target['sha256'],
                'Archive does not match qualified handoff')
    require(set(expected) == names and len(names) == 5, 'Exactly five qualified assets are required')
    actual = {row['name']: row for row in release['assets']}
    require(len(actual) == len(release['assets']) == 5 and set(actual) == names, 'Uploaded asset inventory mismatch')
    for name, row in expected.items():
        found = actual[name]
        require(found['state'] == 'uploaded' and found['size'] == row['size'] and
                found['digest'] == 'sha256:'+row['sha256'], f'Uploaded asset mismatch: {name}')
    sha = hashlib.sha256(handoff_bytes).hexdigest()
    require(expected['handoff.json']['sha256'] == sha and expected['handoff.json']['size'] == len(handoff_bytes),
            'Expected handoff bytes differ')
    require(downloaded_handoff == handoff_bytes, 'Downloaded handoff bytes differ')
    if not draft:
        require(bool(release.get('published_at')), 'Published timestamp missing')
    return dict(verified=True, release_id=release_id, tag=handoff['release'], draft=draft,
                companion_commit=expected_commit, asset_count=5, handoff_sha256=sha,
                downloaded_handoff_byte_identical=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    identity = commands.add_parser('identity')
    identity.add_argument('--view', type=Path, required=True)
    identity.add_argument('--graphql', type=Path, required=True)
    identity.add_argument('--tag', required=True)
    verify = commands.add_parser('verify')
    for name in ('handoff', 'assets', 'release', 'downloaded-handoff'):
        verify.add_argument('--'+name, type=Path, required=True)
    verify.add_argument('--release-id', type=int, required=True)
    verify.add_argument('--tag-commit', required=True)
    verify.add_argument('--expected-commit', required=True)
    verify.add_argument('--draft', action='store_true')
    args = parser.parse_args()
    read = lambda p: json.loads(p.read_text())
    if args.command == 'identity':
        result = resolve_identity(read(args.view), read(args.graphql), args.tag)
    else:
        result = verify_release(args.handoff.read_bytes(), read(args.assets), read(args.release),
                                release_id=args.release_id, tag_commit=args.tag_commit,
                                expected_commit=args.expected_commit,
                                downloaded_handoff=args.downloaded_handoff.read_bytes(), draft=args.draft)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
