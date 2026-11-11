import importlib.util
import json
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('package_latest', Path(__file__).resolve().parents[1] / 'scripts/package.py')
pkg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pkg)


class LatestTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            'version': '1.2.3-r5', 'image_prefix': 'ghcr.io/tanakrit-d/project',
            'packaging_repository': 'tanakrit-d/project',
            'upstream_tag': 'v1.2.3', 'upstream_sha': 'b' * 40,
            'packaging_fingerprint': 'd' * 64,
        }
        self.release = {'tag_name': self.plan['version'], 'draft': False, 'prerelease': False}
        self.releases = [self.release]
        self.manifest = dict(self.plan, images={})
        self.sources = {}
        for index, service in enumerate(pkg.SERVICES, 1):
            digest = f'sha256:{index:064x}'
            ref = f"{self.plan['image_prefix']}-{service}@{digest}"
            self.manifest['images'][service] = ref
            self.sources[digest] = {
                'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                'config': {'digest': f'sha256:{index + 100:064x}'},
            }
        self.current = {}
        self.inspect_override = {}
        self.creates = []
        self.commands = []

    def alias(self, service):
        return f"{self.plan['image_prefix']}-{service}:latest"

    def mark_current(self, service, version=None, digest=None):
        self.current[self.alias(service)] = {
            'name': digest or self.manifest['images'][service].split('@')[1],
            'metadata': {'container': {'tags': ['latest', version or self.plan['version']]}},
        }

    def command(self, *args, **kwargs):
        self.commands.append(args)
        if args[:3] == ('gh', 'release', 'download'):
            return json.dumps(self.manifest)
        if args[:4] == ('docker', 'buildx', 'imagetools', 'create'):
            self.assertIn('--prefer-index=false', args)
            self.assertNotIn('--append', args)
            alias, source = args[-2:]
            self.creates.append((alias, source))
            self.current[alias] = {
                'name': source.split('@')[1],
                'metadata': {'container': {'tags': ['latest', self.plan['version']]}},
            }
            return ''
        if args[:4] == ('docker', 'buildx', 'imagetools', 'inspect'):
            ref = args[4]
            digest = ref.split('@')[1] if '@' in ref else self.current[ref]['name']
            if '--raw' in args:
                return json.dumps(self.sources[digest])
            return self.inspect_override.get(ref, digest)
        raise AssertionError(f'Unexpected command: {args}')

    @contextmanager
    def environment(self):
        with patch.object(pkg, 'load_plan', return_value=self.plan), \
             patch.object(pkg, 'list_releases', side_effect=lambda _: self.releases), \
             patch.object(pkg, 'registry_tag', side_effect=lambda ref: self.current.get(ref)), \
             patch.object(pkg, 'run', side_effect=self.command):
            yield

    def test_latest_cli_copies_the_existing_service_digests(self):
        with self.environment(), patch('sys.argv', ['package.py', 'latest']):
            pkg.main()
        self.assertEqual(len(self.creates), len(pkg.SERVICES))
        for service in pkg.SERVICES:
            ref = self.manifest['images'][service]
            self.assertIn((self.alias(service), ref), self.creates)
            self.assertEqual(self.current[self.alias(service)]['name'], ref.split('@')[1])
        self.assertFalse(any(command[:2] == ('docker', 'build') for command in self.commands))

    def test_rerun_repairs_a_partial_alias_update_without_recreating_matching_tags(self):
        self.mark_current('nakama')
        self.mark_current('api')
        with self.environment():
            pkg.latest()
        self.assertEqual({alias for alias, _ in self.creates}, {self.alias('web'), self.alias('moderation')})
        self.creates.clear()
        with self.environment():
            pkg.latest()
        self.assertEqual(self.creates, [])

    def test_older_manual_release_cannot_move_latest_back(self):
        for newer in ('1.3.0-r1', '1.2.3-r10'):
            with self.subTest(newer=newer):
                self.releases = [self.release, {'tag_name': newer, 'draft': False, 'prerelease': False}]
                with self.environment():
                    pkg.latest()
                self.assertEqual(self.creates, [])

    def test_larger_packaging_revision_is_ordered_numerically(self):
        self.plan['version'] = self.manifest['version'] = self.release['tag_name'] = '1.2.3-r10'
        self.releases.append({'tag_name': '1.2.3-r9', 'draft': False, 'prerelease': False})
        with self.environment():
            pkg.latest()
        self.assertEqual(len(self.creates), len(pkg.SERVICES))

    def test_existing_newer_registry_alias_blocks_all_updates(self):
        self.mark_current('web', version='1.2.4-r1', digest='sha256:' + 'e' * 64)
        with self.environment():
            pkg.latest()
        self.assertEqual(self.creates, [])

    def test_drafts_and_prereleases_are_excluded_from_latest(self):
        self.releases.extend([
            {'tag_name': '2.0.0-r1', 'draft': True, 'prerelease': False},
            {'tag_name': '3.0.0-r1', 'draft': False, 'prerelease': True},
        ])
        with self.environment():
            pkg.latest()
        self.assertEqual(len(self.creates), len(pkg.SERVICES))

    def test_missing_or_incomplete_releases_cannot_change_aliases(self):
        with self.environment():
            self.releases = []
            with self.assertRaisesRegex(ValueError, 'completed release'):
                pkg.latest()
            self.releases = [dict(self.release, draft=True)]
            with self.assertRaisesRegex(ValueError, 'incomplete release'):
                pkg.latest()
            self.releases = [self.release]
            del self.manifest['images']['moderation']
            with self.assertRaisesRegex(ValueError, 'incomplete image set'):
                pkg.latest()
        self.assertEqual(self.creates, [])

    def test_all_source_refs_are_preflighted_before_the_first_write(self):
        self.manifest['images']['moderation'] = 'ghcr.io/someone-else/image@sha256:' + 'a' * 64
        with self.environment(), self.assertRaisesRegex(ValueError, 'Unexpected published image'):
            pkg.latest()
        self.assertEqual(self.creates, [])

    def test_attestation_indices_cannot_be_copied_to_latest(self):
        digest = self.manifest['images']['moderation'].split('@')[1]
        self.sources[digest] = {'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': []}
        with self.environment(), self.assertRaisesRegex(ValueError, 'one image manifest'):
            pkg.latest()
        self.assertEqual(self.creates, [])

    def test_unknown_or_conflicting_existing_latest_is_preserved(self):
        for tags in (['latest'], ['latest', self.plan['version']]):
            with self.subTest(tags=tags):
                self.current[self.alias('web')] = {
                    'name': 'sha256:' + 'e' * 64, 'metadata': {'container': {'tags': tags}},
                }
                with self.environment(), self.assertRaises(ValueError):
                    pkg.latest()
                self.assertEqual(self.creates, [])

    def test_registry_lookup_errors_abort_before_writes(self):
        with self.environment(), patch.object(pkg, 'registry_tag', side_effect=RuntimeError('HTTP 403')):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                pkg.latest()
        self.assertEqual(self.creates, [])

    def test_digest_mismatch_fails_and_a_rerun_repairs_remaining_aliases(self):
        self.inspect_override[self.alias('nakama')] = 'sha256:' + 'f' * 64
        with self.environment(), self.assertRaisesRegex(ValueError, 'retain the versioned image digest'):
            pkg.latest()
        self.assertEqual(len(self.creates), 1)
        self.inspect_override.clear()
        with self.environment():
            pkg.latest()
        self.assertEqual(len(self.creates), len(pkg.SERVICES))


if __name__ == '__main__':
    unittest.main()
