import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('package', Path(__file__).resolve().parents[1] / 'scripts/package.py')
pkg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pkg)


class PackagingTests(unittest.TestCase):
    def test_stable_selection_uses_semantic_versions_and_ignores_prereleases(self):
        tags = [{'name': x} for x in ['v1.9.0', 'v1.10.0', 'v2.0.0-rc1', 'main', 'v01.2.3']]
        self.assertEqual(pkg.select_tag(tags), 'v1.10.0')
        with self.assertRaises(ValueError):
            pkg.select_tag([{'name': 'main'}])
        with self.assertRaises(ValueError):
            pkg.select_tag([{'name': '1.0.2'}, {'name': 'v1.0.2'}])

    def test_upstream_cannot_be_publication_destination(self):
        with self.assertRaisesRegex(ValueError, 'upstream is read-only'):
            pkg.plan('v1.0.2', 'Balatro-Multiplayer/BalatroMultiplayerServer')

    def test_published_source_changes_are_rejected_including_moved_tags(self):
        version = f"1.0.2-r{json.loads((pkg.ROOT / 'config.json').read_text())['packaging_revision']}"
        def api(endpoint, missing=False):
            if '/git/ref/' in endpoint:
                return {'object': {'type': 'tag', 'sha': 'b' * 40}}
            if '/git/tags/' in endpoint:
                return {'object': {'type': 'commit', 'sha': 'c' * 40}}
            return [{'tag_name': version, 'draft': False, 'prerelease': False}]
        previous = {'version': version, 'upstream_tag': 'v1.0.2', 'upstream_sha': 'a' * 40,
                    'packaging_fingerprint': pkg.fingerprint()}
        with patch.object(pkg, 'api', side_effect=api), patch.object(pkg, 'run', return_value=json.dumps(previous)):
            with self.assertRaisesRegex(ValueError, 'upstream_sha changed'):
                pkg.plan('v1.0.2', 'tanakrit-d/BalatroMultiplayerServerDocker')

    def test_publication_refuses_existing_or_unverifiable_registry_tags(self):
        ref = 'ghcr.io/tanakrit-d/project-api:1.0.2-r1'
        existing = {'metadata': {'container': {'tags': ['1.0.2-r1']}}}
        with patch.object(pkg, 'api', return_value=[existing]):
            with self.assertRaisesRegex(ValueError, 'never overwrite'):
                pkg.assert_absent(ref)
        with patch.object(pkg, 'api', side_effect=RuntimeError('HTTP 403')):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                pkg.assert_absent(ref)
        with patch.object(pkg, 'api', return_value=None):
            pkg.assert_absent(ref)  # A new package can be published for the first time.
        untagged = {'metadata': {'container': {'tags': []}}}
        with patch.object(pkg, 'api', side_effect=[[untagged] * 100, [existing]]) as api:
            with self.assertRaisesRegex(ValueError, 'never overwrite'):
                pkg.assert_absent(ref)
            self.assertEqual(api.call_count, 2)

    def test_release_assets_pin_the_whole_set_and_match_its_version(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            plan = {
                'version': '1.2.3-r5', 'image_prefix': 'ghcr.io/tanakrit-d/project',
                'packaging_repository': 'tanakrit-d/project', 'packaging_sha': 'a' * 40,
                'upstream_tag': 'v1.2.3', 'upstream_sha': 'b' * 40,
                'upstream': 'Balatro-Multiplayer/BalatroMultiplayerServer',
                'platform': 'linux/amd64', 'created': '2026-11-10T00:00:00Z',
                'packaging_fingerprint': 'd' * 64,
            }
            for filename in ('plan.json', 'smoke-passed.json', 'build-manifest.json'):
                pkg.save(build / filename, plan)
            calls = []
            def run(*args, **kwargs):
                calls.append(args)
                if args[:3] == ('docker', 'buildx', 'imagetools'):
                    if '--raw' in args:
                        return json.dumps({'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                                           'config': {'digest': 'sha256:' + 'e' * 64}})
                    return 'sha256:' + 'c' * 64
                return None
            with patch.object(pkg, 'BUILD', build), patch.object(pkg, 'api', return_value=None), \
                 patch.object(pkg, 'assert_absent'), \
                 patch.object(pkg, 'inspect_local_image', return_value='sha256:' + 'e' * 64), \
                 patch.object(pkg, 'fingerprint', return_value=plan['packaging_fingerprint']), \
                 patch.object(pkg, 'run', side_effect=run):
                pkg.publish()
            compose = (build / 'compose.yml').read_text()
            for service in pkg.SERVICES:
                self.assertIn(f"{plan['image_prefix']}-{service}@sha256:", compose)
            self.assertNotIn('${BMP_VERSION', compose)
            self.assertNotIn('${IMAGE_PREFIX', compose)
            self.assertIn('BMP_VERSION=1.2.3-r5', (build / '.env.example').read_text())
            self.assertIn('IMAGE_PREFIX=ghcr.io/tanakrit-d/project', (build / '.env.example').read_text())
            manifest = json.loads((build / 'build-manifest.json').read_text())
            self.assertEqual(set(manifest['images']), set(pkg.SERVICES))
            self.assertTrue(any(call[:3] == ('gh', 'release', 'create') and '--draft' in call for call in calls))
            self.assertEqual(calls[-1][:3], ('gh', 'release', 'edit'))
            pushes = [call for call in calls if call[:2] == ('docker', 'push')]
            self.assertEqual(len(pushes), len(pkg.SERVICES))
            for call in pushes:
                self.assertEqual(call[2:4], ('--platform', 'linux/amd64'))

    def metadata_plan(self):
        return {
            'version': '1.2.3-r5', 'image_prefix': 'ghcr.io/tanakrit-d/project',
            'packaging_repository': 'tanakrit-d/project', 'packaging_sha': 'a' * 40,
            'upstream': 'Balatro-Multiplayer/BalatroMultiplayerServer',
            'upstream_tag': 'v1.2.3', 'upstream_sha': 'b' * 40,
            'platform': 'linux/amd64', 'created': '2026-11-10T00:00:00Z',
            'packaging_fingerprint': 'd' * 64, 'corepack_version': '0.36.0',
            'bases': {'node': 'node:26@sha256:' + 'e' * 64,
                      'nakama': 'heroiclabs/nakama:3.41.0@sha256:' + 'f' * 64},
        }

    def test_build_disables_attestations_and_has_service_metadata(self):
        plan = self.metadata_plan()
        with patch.object(pkg, 'load_plan', return_value=plan), patch.object(pkg, 'run') as run:
            pkg.build()
        self.assertEqual(run.call_count, 7)
        for call in run.call_args_list:
            args = call.args
            self.assertIn('--provenance=false', args)
            self.assertIn('--sbom=false', args)
            self.assertIn('--load', args)
            self.assertNotIn('--push', args)
            self.assertIn('org.opencontainers.image.revision=' + plan['packaging_sha'], args)
            self.assertIn('io.balatromp.upstream.revision=' + plan['upstream_sha'], args)
        for service in pkg.SERVICES:
            labels = pkg.image_labels(plan, service)
            self.assertEqual(labels['io.balatromp.service'], service)
            self.assertIn(plan['version'], labels['org.opencontainers.image.url'])

    def test_publish_checks_local_metadata_and_platform(self):
        plan = self.metadata_plan()
        labels = pkg.image_labels(plan, 'api')
        info = {'Os': 'linux', 'Architecture': 'amd64', 'Id': 'sha256:' + 'c' * 64,
                'Config': {'Labels': labels}}
        with patch.object(pkg, 'run', return_value=json.dumps([info])):
            self.assertEqual(pkg.inspect_local_image(plan, 'api'), info['Id'])
        labels['org.opencontainers.image.revision'] = plan['upstream_sha']
        with patch.object(pkg, 'run', return_value=json.dumps([info])):
            with self.assertRaisesRegex(ValueError, 'mismatched metadata'):
                pkg.inspect_local_image(plan, 'api')
        info['Architecture'] = 'arm64'
        with patch.object(pkg, 'run', return_value=json.dumps([info])):
            with self.assertRaisesRegex(ValueError, 'unexpected platform'):
                pkg.inspect_local_image(plan, 'api')

    def test_registry_rejects_attestation_indices_and_wrong_image_content(self):
        config = 'sha256:' + 'c' * 64
        valid = {'mediaType': 'application/vnd.oci.image.manifest.v1+json', 'config': {'digest': config}}
        with patch.object(pkg, 'run', return_value=json.dumps(valid)):
            pkg.verify_registry_manifest('ghcr.io/tanakrit-d/image@sha256:' + 'a' * 64, config)
        for invalid in [
            {'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': []},
            dict(valid, artifactType='application/vnd.in-toto+json'),
            dict(valid, config={'digest': 'sha256:' + 'b' * 64}),
        ]:
            with self.subTest(manifest=invalid), patch.object(pkg, 'run', return_value=json.dumps(invalid)):
                with self.assertRaises(ValueError):
                    pkg.verify_registry_manifest('image', config)

    def test_release_detection_includes_drafts_and_rejects_duplicates(self):
        draft = {'tag_name': '1.2.3-r5', 'draft': True, 'prerelease': False}
        with patch.object(pkg, 'api', return_value=[draft]):
            self.assertEqual(pkg.find_release('tanakrit-d/project', draft['tag_name']), draft)
        with self.assertRaisesRegex(ValueError, 'incomplete release'):
            pkg.verify_release(draft, self.metadata_plan())
        with patch.object(pkg, 'api', return_value=[draft, draft]):
            with self.assertRaisesRegex(ValueError, 'Multiple releases'):
                pkg.find_release('tanakrit-d/project', draft['tag_name'])
        other = {'tag_name': 'other'}
        with patch.object(pkg, 'api', side_effect=[[other] * 100, [draft]]) as api:
            self.assertEqual(pkg.find_release('tanakrit-d/project', draft['tag_name']), draft)
            self.assertEqual(api.call_count, 2)

    def test_publish_rerun_skips_matching_completed_release(self):
        plan = self.metadata_plan()
        previous = dict(plan, images={service: 'sha256:' + 'c' * 64 for service in pkg.SERVICES})
        release = {'tag_name': plan['version'], 'draft': False, 'prerelease': False}
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            pkg.save(build / 'plan.json', plan)
            pkg.save(build / 'smoke-passed.json', plan)
            with patch.object(pkg, 'BUILD', build), patch.object(pkg, 'find_release', return_value=release), \
                 patch.object(pkg, 'run', return_value=json.dumps(previous)) as run:
                pkg.publish()
                pkg.publish()
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertEqual(call.args[:3], ('gh', 'release', 'download'))

    def test_publish_rejects_uncommitted_or_changed_packaging_before_writes(self):
        for changed_key, changed_value, message in [
            ('packaging_sha', 'local', 'committed packaging SHA'),
            ('packaging_fingerprint', 'stale', 'changed after planning'),
        ]:
            plan = self.metadata_plan()
            plan[changed_key] = changed_value
            with self.subTest(key=changed_key), tempfile.TemporaryDirectory() as directory:
                build = Path(directory)
                pkg.save(build / 'plan.json', plan)
                pkg.save(build / 'smoke-passed.json', plan)
                with patch.object(pkg, 'BUILD', build), patch.object(pkg, 'find_release', return_value=None), \
                     patch.object(pkg, 'fingerprint', return_value='d' * 64), patch.object(pkg, 'run') as run:
                    with self.assertRaisesRegex(ValueError, message):
                        pkg.publish()
                    run.assert_not_called()

    def test_prepare_preserves_multistage_and_pins_external_bases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / '.build'
            build.mkdir()
            archive = root / 'source.tar.gz'
            files = {
                'apps/server/Dockerfile': 'FROM node:22-alpine\nRUN corepack enable && corepack prepare pnpm@latest --activate\n',
                'apps/web/Dockerfile': 'FROM node:22-alpine AS base\nFROM base AS build\nFROM node:22-alpine AS runner\n',
                'apps/moderation/Dockerfile': 'FROM node:24-trixie-slim\n',
            }
            with tarfile.open(archive, 'w:gz') as tar:
                for name, text in files.items():
                    info = tarfile.TarInfo('upstream/' + name)
                    data = text.encode()
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            plan = {'corepack_version': '0.36.0', 'bases': {'node': 'node:22@sha256:' + 'a' * 64,
                              'moderation': 'node:24@sha256:' + 'b' * 64}}
            pkg.save(build / 'plan.json', plan)
            with patch.object(pkg, 'BUILD', build):
                pkg.prepare(str(archive))
            web = (build / 'source/packaging/web.Dockerfile').read_text()
            self.assertIn('FROM base AS build', web)
            self.assertEqual(web.count(plan['bases']['node']), 2)
            api = (build / 'source/packaging/api.Dockerfile').read_text()
            self.assertNotIn('pnpm@latest', api)
            self.assertIn('npm install --global corepack@0.36.0 && corepack enable', api)
            self.assertIn('FROM runtime AS test', api)
            self.assertTrue((build / 'build-manifest.json').is_file())
            self.assertTrue((build / 'upstream-source.tar.gz').is_file())


if __name__ == '__main__':
    unittest.main()
