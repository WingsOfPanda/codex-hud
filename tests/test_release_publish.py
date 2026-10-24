import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('release_publish', Path(__file__).resolve().parents[1]/'scripts/release_publish.py')
publish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publish)


class PublishChecks(unittest.TestCase):
    def setUp(self):
        self.handoff = dict(release='1.0.0-plus.0.1.0', targets={
            arch: dict(asset=arch+'.tar.gz', sha256=hashlib.sha256(arch.encode()).hexdigest())
            for arch in ('x86_64', 'aarch64')})
        self.raw = json.dumps(self.handoff).encode()
        self.assets = []
        for arch, row in self.handoff['targets'].items():
            self.assets.extend([dict(name=row['asset'], sha256=row['sha256'], size=10),
                                dict(name=row['asset']+'.sha256', sha256='a'*64, size=80)])
        self.assets.append(dict(name='handoff.json',sha256=hashlib.sha256(self.raw).hexdigest(),size=len(self.raw)))
        self.release = dict(id=7, tag_name=self.handoff['release'], draft=True, assets=[
            dict(name=row['name'],size=row['size'],digest='sha256:'+row['sha256'],state='uploaded') for row in self.assets])

    def verify(self, **overrides):
        args = dict(release_id=7, tag_commit='b'*40, expected_commit='b'*40,
                    downloaded_handoff=self.raw, draft=True)
        args.update(overrides)
        return publish.verify_release(self.raw,self.assets,self.release,**args)

    def test_missing_from_release_list_never_means_create_again(self):
        release_list = []
        self.assertEqual(release_list, [])
        view = dict(id='node7',tagName=self.handoff['release'],isDraft=True)
        graphql = dict(data=dict(node=dict(view,databaseId=7)))
        self.assertEqual(publish.resolve_identity(view,graphql,self.handoff['release'])['release_id'],7)
        graphql['data']['node']['id'] = 'another'
        with self.assertRaisesRegex(ValueError,'node mismatch'):
            publish.resolve_identity(view,graphql,self.handoff['release'])

    def test_exact_five_assets_and_downloaded_handoff(self):
        self.assertTrue(self.verify()['verified'])
        self.release.update(draft=False,published_at='2026-01-01T00:00:00Z')
        self.assertTrue(self.verify(draft=False)['verified'])

    def test_wrong_digest_or_duplicate_asset_is_rejected(self):
        original = copy.deepcopy(self.release)
        self.release['assets'][0]['digest'] = 'sha256:'+'c'*64
        with self.assertRaisesRegex(ValueError,'asset mismatch'):
            self.verify()
        self.release = original
        self.release['assets'].append(self.release['assets'][0])
        with self.assertRaisesRegex(ValueError,'inventory mismatch'):
            self.verify()

    def test_tag_state_and_download_must_match(self):
        for change in (dict(release_id=8), dict(tag_commit='d'*40),
                       dict(downloaded_handoff=self.raw+b'\n'), dict(draft=False)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(**change)

    def test_asset_bytes_are_bound_to_qualified_archives(self):
        self.assets[0]['sha256'] = 'd'*64
        with self.assertRaisesRegex(ValueError,'qualified handoff'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
