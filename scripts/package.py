#!/usr/bin/env python3
"""Resolve stable upstream tags, test exact sources, and publish immutable sets."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'
SERVICES = ('nakama', 'api', 'web', 'moderation')
TAG = re.compile(r'^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$')
PACKAGE_TAG = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-r([1-9]\d*)$')


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None, **kwargs).stdout


def api(endpoint, missing=False):
    result = subprocess.run(['gh', 'api', endpoint], text=True, capture_output=True)
    if result.returncode:
        if missing and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def version_key(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError(f'Not a stable version tag: {tag}')
    return tuple(map(int, match.groups()))


def package_version_key(version):
    match = PACKAGE_TAG.fullmatch(version)
    if not match:
        raise ValueError(f'Not a versioned image tag: {version}')
    return tuple(map(int, match.groups()))


def select_tag(tags):
    stable = [t for t in tags if TAG.fullmatch(t['name'])]
    if not stable:
        raise ValueError('Upstream has no stable version tags; refusing to use main')
    ordered = sorted(stable, key=lambda t: version_key(t['name']), reverse=True)
    if len(ordered) > 1 and version_key(ordered[0]['name']) == version_key(ordered[1]['name']):
        raise ValueError('Ambiguous upstream version tags')
    return ordered[0]['name']


def fingerprint():
    digest = hashlib.sha256()
    paths = [ROOT / 'config.json', ROOT / 'compose.yml', ROOT / '.env.example']
    for directory in ('docker', 'scripts', '.github/workflows'):
        paths.extend(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts)
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def list_releases(repository):
    # Authenticated results include drafts, which reserve their version.
    result = []
    page = 1
    while True:
        releases = api(f'repos/{repository}/releases?per_page=100&page={page}', missing=True)
        if releases is None:
            if page > 1:
                raise RuntimeError('Repository became inaccessible while checking releases')
            return []
        result.extend(releases)
        if len(releases) < 100:
            return result
        page += 1


def find_release(repository, version, releases=None):
    if releases is None:
        releases = list_releases(repository)
    matches = [release for release in releases if release['tag_name'] == version]
    if len(matches) > 1:
        raise ValueError(f'Multiple releases reserve {version}; resolve the duplicate releases first')
    return matches[0] if matches else None


def verify_release(release, plan):
    if release['draft'] or release['prerelease']:
        raise ValueError('Version is occupied by an incomplete release; bump packaging_revision')
    previous = json.loads(run('gh', 'release', 'download', plan['version'], '--repo',
                              plan['packaging_repository'], '--pattern', 'build-manifest.json',
                              '--output', '-', capture=True))
    for key in ('version', 'upstream_tag', 'upstream_sha', 'packaging_fingerprint'):
        if previous[key] != plan[key]:
            raise ValueError(f'Published {key} changed; bump packaging_revision')
    if set(previous.get('images', {})) != set(SERVICES):
        raise ValueError('Published release has an incomplete image set')
    return previous


def plan(tag, repository):
    config = json.loads((ROOT / 'config.json').read_text())
    if not re.fullmatch(r'tanakrit-d/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Packaging repository must be under tanakrit-d; upstream is read-only')
    if config['bases']['postgres'] not in (ROOT / 'compose.yml').read_text():
        raise ValueError('Postgres digest in compose.yml must match config.json')
    upstream = config['upstream']
    if repository.lower() == upstream.lower():
        raise ValueError('Refusing to publish to upstream')
    if not tag:
        pages = json.loads(run('gh', 'api', '--paginate', '--slurp',
                               f'repos/{upstream}/tags?per_page=100', capture=True))
        tag = select_tag([item for page in pages for item in page])
    version_key(tag)
    obj = api(f'repos/{upstream}/git/ref/tags/{tag}')['object']
    while obj['type'] == 'tag':
        obj = api(f"repos/{upstream}/git/tags/{obj['sha']}")['object']
    if obj['type'] != 'commit' or not re.fullmatch(r'[0-9a-f]{40}', obj['sha']):
        raise ValueError('Tag does not resolve to a commit')
    revision = config['packaging_revision']
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError('packaging_revision must be a positive integer')
    corepack_version = config['corepack_version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', corepack_version):
        raise ValueError('Corepack must be pinned to an exact version')
    for image in config['bases'].values():
        if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', image):
            raise ValueError(f'Base image must be pinned by digest: {image}')
    result = {
        'upstream': upstream, 'upstream_tag': tag, 'upstream_sha': obj['sha'],
        'version': f'{".".join(map(str, version_key(tag)))}-r{revision}',
        'packaging_revision': revision, 'packaging_fingerprint': fingerprint(),
        'packaging_repository': repository,
        'packaging_sha': os.environ.get('GITHUB_SHA', 'local'),
        'image_prefix': f'ghcr.io/{repository.lower()}',
        'platform': config['platform'], 'bases': config['bases'],
        'corepack_version': corepack_version,
        'created': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
    }
    if result['platform'] != 'linux/amd64':
        raise ValueError('Only linux/amd64 is currently validated for Unraid')
    release = find_release(repository, result['version'])
    if release:
        verify_release(release, result)
    published = release is not None
    save(BUILD / 'plan.json', result)
    print(f"{result['version']}: {tag} at {obj['sha']} ({'already published' if published else 'new build'})")
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a') as stream:
            stream.write(f"needed={str(not published).lower()}\nversion={result['version']}\n")


def load_plan():
    return json.loads((BUILD / 'plan.json').read_text())


def prepare(archive=None):
    plan = load_plan()
    source = BUILD / 'source'
    if source.exists():
        shutil.rmtree(source)
    source.mkdir(parents=True)
    archive = Path(archive) if archive else BUILD / 'upstream-source.tar.gz'
    if not archive.exists() or archive == BUILD / 'upstream-source.tar.gz':
        with archive.open('wb') as stream:
            subprocess.run(['gh', 'api', f"repos/{plan['upstream']}/tarball/{plan['upstream_sha']}"],
                           stdout=stream, check=True)
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        roots = {m.name.split('/')[0] for m in members}
        if len(roots) != 1:
            raise ValueError('Source archive must have a single root directory')
        extracted = BUILD / 'extracted'
        if extracted.exists():
            shutil.rmtree(extracted)
        extracted.mkdir()
        tar.extractall(extracted, filter='data')
        upstream_root = extracted / roots.pop()
        for child in upstream_root.iterdir():
            shutil.move(str(child), source / child.name)
        shutil.rmtree(extracted)
    expected_engine = plan['bases'].get('nakama')
    upstream_compose = source / 'compose.local.yml'
    if expected_engine and upstream_compose.exists():
        engine = re.search(r'image:\s*(heroiclabs/nakama:[^\s]+)', upstream_compose.read_text())
        if not engine or engine.group(1).split('@')[0] != expected_engine.split('@')[0]:
            raise ValueError('Upstream changed Nakama version; update its pinned base in config.json')
    manifest = dict(plan, source_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    # Capture the exact consumed source for release users and source-license compliance.
    if archive != BUILD / 'upstream-source.tar.gz':
        shutil.copyfile(archive, BUILD / 'upstream-source.tar.gz')
    shutil.copytree(ROOT / 'docker', source / 'packaging')
    save(source / 'packaging/build-manifest.json', manifest)
    save(BUILD / 'build-manifest.json', manifest)
    # Consume upstream Dockerfiles, changing only toolchain pinning and provenance.
    for service, path in [('api', 'apps/server/Dockerfile'), ('web', 'apps/web/Dockerfile'),
                          ('moderation', 'apps/moderation/Dockerfile')]:
        text = (source / path).read_text()
        base = plan['bases']['moderation' if service == 'moderation' else 'node']
        stages = set()
        def pin(match):
            image, suffix = match.groups()
            if image in stages:
                result = match.group(0)
            elif image.startswith('node:'):
                result = f'FROM {base}{suffix}'
            else:
                raise ValueError(f'Unexpected upstream base image: {image}')
            if stage := re.search(r' AS (\w+)', suffix, re.I):
                stages.add(stage.group(1))
            return result
        text, count = re.subn(r'^FROM (\S+)(.*)$', pin, text, flags=re.M)
        if not count:
            raise ValueError(f'No base found in {path}')
        text = text.replace('corepack enable && corepack prepare pnpm@latest --activate', 'corepack enable')
        text = text.replace('RUN corepack enable',
                            f"RUN npm install --global corepack@{plan['corepack_version']} && corepack enable")
        text = text.replace('COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./',
                            'COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./')
        text += '\nCOPY packaging/build-manifest.json /build-manifest.json\n'
        (source / f'packaging/{service}.Dockerfile').write_text(text)
    # These suites use mocks; the deployment smoke test covers real databases.
    for service, package in [('api', 'balatro-multiplayer-api-server'),
                             ('moderation', 'balatro-multiplayer-moderation')]:
        path = source / f'packaging/{service}.Dockerfile'
        text = path.read_text()
        base = plan['bases']['moderation' if service == 'moderation' else 'node']
        text = text.replace(f'FROM {base}\n', f'FROM {base} AS runtime\n', 1)
        text += f'\nFROM runtime AS test\nCOPY . /app/\nRUN pnpm --filter {package} test\n'
        path.write_text(text)


def image(plan, service):
    return f"{plan['image_prefix']}-{service}:{plan['version']}"


def image_labels(plan, service):
    source = f"https://github.com/{plan['packaging_repository']}"
    return {
        'org.opencontainers.image.title': f'Balatro Multiplayer Server ({service})',
        'org.opencontainers.image.description': f"Community {service} service from upstream {plan['upstream_tag']}",
        'org.opencontainers.image.source': source,
        'org.opencontainers.image.revision': plan['packaging_sha'],
        'org.opencontainers.image.version': plan['version'],
        'org.opencontainers.image.created': plan['created'],
        'org.opencontainers.image.url': f"{source}/releases/tag/{plan['version']}",
        'org.opencontainers.image.documentation': f"{source}/blob/{plan['packaging_sha']}/README.md",
        'org.opencontainers.image.vendor': plan['packaging_repository'].split('/')[0],
        'io.balatromp.service': service,
        'io.balatromp.upstream.source': f"https://github.com/{plan['upstream']}",
        'io.balatromp.upstream.tag': plan['upstream_tag'],
        'io.balatromp.upstream.revision': plan['upstream_sha'],
        'io.balatromp.packaging.fingerprint': plan['packaging_fingerprint'],
    }


def inspect_local_image(plan, service):
    ref = image(plan, service)
    info = json.loads(run('docker', 'image', 'inspect', ref, capture=True))[0]
    platform = f"{info['Os']}/{info['Architecture']}"
    if platform != plan['platform']:
        raise ValueError(f'{ref} has unexpected platform {platform}')
    actual = info['Config'].get('Labels') or {}
    for key, expected in image_labels(plan, service).items():
        if actual.get(key) != expected:
            raise ValueError(f'{ref} has mismatched metadata {key}')
    return info['Id']


def registry_manifest(ref):
    manifest = json.loads(run('docker', 'buildx', 'imagetools', 'inspect', ref, '--raw', capture=True))
    allowed = ('application/vnd.oci.image.manifest.v1+json',
               'application/vnd.docker.distribution.manifest.v2+json')
    if manifest.get('mediaType') not in allowed or 'manifests' in manifest or 'artifactType' in manifest:
        raise ValueError(f'{ref} must contain one image manifest without attestation attachments')
    return manifest


def verify_registry_manifest(ref, expected_config):
    manifest = registry_manifest(ref)
    if manifest.get('config', {}).get('digest') != expected_config:
        raise ValueError(f'{ref} does not match the validated local image')


def build():
    plan = load_plan()
    for service in SERVICES:
        args = ['docker', 'buildx', 'build', '--load', '--platform', plan['platform'],
                '--provenance=false', '--sbom=false',
                '--file', str(BUILD / f'source/packaging/{service}.Dockerfile')]
        for key, value in image_labels(plan, service).items():
            args += ['--label', f'{key}={value}']
        if service == 'nakama':
            args += ['--build-arg', f"COREPACK_VERSION={plan['corepack_version']}",
                     '--build-arg', f"NODE_IMAGE={plan['bases']['node']}",
                     '--build-arg', f"NAKAMA_IMAGE={plan['bases']['nakama']}"]
        if service in ('nakama', 'api', 'moderation'):
            run(*args, '--target', 'test', '--tag', f'bmp-test-{service}:local', str(BUILD / 'source'))
        args += ['--tag', image(plan, service)]
        if service in ('nakama', 'api', 'moderation'):
            args += ['--target', 'runtime']
        if service == 'moderation':
            args += ['--build-arg', f"GIT_SHA={plan['upstream_sha']}"]
        run(*args, str(BUILD / 'source'))


def smoke():
    plan = load_plan()
    env_path = BUILD / 'smoke.env'
    values = {}
    for line in (ROOT / '.env.example').read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value or hashlib.sha256(key.encode()).hexdigest()[:48]
    values.update(IMAGE_PREFIX=plan['image_prefix'], BMP_VERSION=plan['version'],
                  COMPOSE_PROFILES='web', NAKAMA_BIND_ADDRESS='127.0.0.1',
                  WEB_BIND_ADDRESS='127.0.0.1', NAKAMA_PORT='17350', CONSOLE_PORT='17351',
                  API_PORT='18788', WEB_PORT='13000', WEB_BASE_URL='http://127.0.0.1:13000',
                  MODERATION_REMOTE='')
    env_path.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
    base = ['docker', 'compose', '--env-file', str(env_path), '-f', str(ROOT / 'compose.yml'),
            '--profile', 'web', '-p', 'bmp-packaging-smoke']
    config = json.loads(run(*base, 'config', '--format', 'json', capture=True))
    # Isolated Docker-managed storage; never touch the deployment's appdata.
    config['services']['postgres']['volumes'] = [
        {'type': 'volume', 'source': 'smoke-postgres', 'target': '/var/lib/postgresql/data'}]
    config['volumes'] = {'smoke-postgres': {}}
    config['services'].pop('moderation', None)
    for service in config['services'].values():
        service.pop('profiles', None)
        service['pull_policy'] = 'never' if not service['image'].startswith('postgres:') else 'missing'
    smoke_file = BUILD / 'smoke.compose.json'
    save(smoke_file, config)
    command = ['docker', 'compose', '-f', str(smoke_file), '-p', 'bmp-packaging-smoke']
    marker = BUILD / 'smoke-passed.json'
    marker.unlink(missing_ok=True)
    try:
        run(*command, 'up', '-d', '--wait', '--wait-timeout', '240', 'postgres', 'nakama')
        run('node', str(ROOT / 'scripts/smoke.mjs'), 'create')
        run(*command, 'up', '-d', '--wait', '--wait-timeout', '240')
        run('node', str(ROOT / 'scripts/smoke.mjs'), 'reconnect')
        # Re-run both schema migrations over existing data, then verify identity survives.
        run(*command, 'stop', 'web', 'api', 'nakama')
        run(*command, 'run', '--rm', 'migrate')
        run(*command, 'up', '-d', '--wait', '--wait-timeout', '240')
        run('node', str(ROOT / 'scripts/smoke.mjs'), 'reconnect')
        # A file-based subprocess inherits Node flags, so avoid --input-type here.
        backend_check = """
        (async () => {
          const { getLlama } = await import('node-llama-cpp');
          const llama = await getLlama({ gpu: false, build: 'never' });
          await llama.dispose();
          console.log('Moderation CPU backend passed');
        })().catch(error => {
          console.error(error);
          process.exitCode = 1;
        });
        """
        run('docker', 'run', '--rm', '--platform', plan['platform'], '--entrypoint', 'node',
            image(plan, 'moderation'), '-e', backend_check)
        save(marker, plan)
    finally:
        run(*command, 'logs', '--no-color', '--tail', '100')
        run(*command, 'down', '--volumes', '--remove-orphans')


def registry_tag(ref):
    # GHCR returns "denied" for a package that has never existed. Use the
    # authenticated Packages API instead of treating registry errors as absence.
    match = re.fullmatch(r'ghcr.io/(tanakrit-d)/([a-z0-9_.-]+):([a-z0-9_.-]+)', ref)
    if not match:
        raise ValueError(f'Unexpected image destination: {ref}')
    owner, package, tag = match.groups()
    found = None
    page = 1
    while True:
        versions = api(f'users/{owner}/packages/container/{package}/versions?per_page=100&page={page}',
                       missing=True)
        if versions is None:
            if page != 1:
                raise RuntimeError('Package disappeared during preflight')
            return
        for version in versions:
            if tag in version['metadata']['container']['tags']:
                if found is not None:
                    raise ValueError(f'Multiple registry versions carry {ref}')
                found = version
        if len(versions) < 100:
            return found
        page += 1


def assert_absent(ref):
    if registry_tag(ref) is not None:
        raise ValueError(f'{ref} already exists; bump packaging_revision (never overwrite)')


def latest():
    plan = load_plan()
    repository = plan['packaging_repository']
    releases = list_releases(repository)
    release = find_release(repository, plan['version'], releases)
    if release is None:
        raise ValueError('A completed release is required before promoting latest')
    manifest = verify_release(release, plan)
    candidate = package_version_key(plan['version'])
    successful = [package_version_key(item['tag_name']) for item in releases
                  if not item['draft'] and not item['prerelease'] and PACKAGE_TAG.fullmatch(item['tag_name'])]
    if candidate < max(successful, default=candidate):
        print(f"Skipping latest for {plan['version']}: a newer release is published")
        return

    # Preflight the complete set before changing the first alias.
    sources = {}
    current = {}
    for service in SERVICES:
        ref = manifest['images'][service]
        expected = re.escape(f"{plan['image_prefix']}-{service}") + r'@sha256:[0-9a-f]{64}'
        if not re.fullmatch(expected, ref):
            raise ValueError(f'Unexpected published image reference for {service}')
        config = registry_manifest(ref).get('config', {}).get('digest', '')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', config):
            raise ValueError(f'Invalid image config for {ref}')
        alias = f"{plan['image_prefix']}-{service}:latest"
        tagged = registry_tag(alias)
        if tagged:
            versions = [package_version_key(tag) for tag in tagged['metadata']['container']['tags']
                        if PACKAGE_TAG.fullmatch(tag)]
            if not versions:
                raise ValueError(f'Cannot identify the version currently tagged {alias}')
            newest = max(versions)
            if newest > candidate:
                print(f'Skipping latest: {alias} already references a newer version')
                return
            if newest == candidate and tagged['name'] != ref.split('@')[1]:
                raise ValueError(f'{alias} conflicts with the immutable version digest')
        sources[service] = (ref, config)
        current[service] = tagged

    for service, (ref, config) in sources.items():
        alias = f"{plan['image_prefix']}-{service}:latest"
        digest = ref.split('@')[1]
        if not current[service] or current[service]['name'] != digest:
            # Copy the existing manifest verbatim, preserving its digest and shape.
            run('docker', 'buildx', 'imagetools', 'create', '--prefer-index=false', '--tag', alias, ref)
        actual = run('docker', 'buildx', 'imagetools', 'inspect', alias,
                     '--format', '{{.Manifest.Digest}}', capture=True).strip()
        if actual != digest:
            raise ValueError(f'{alias} did not retain the versioned image digest')
        verify_registry_manifest(alias, config)
    print(f"Latest aliases match {plan['version']}")


def publish():
    plan = load_plan()
    if json.loads((BUILD / 'smoke-passed.json').read_text()) != plan:
        raise ValueError('This exact build must pass smoke tests before publication')
    repository = plan['packaging_repository']
    version = plan['version']
    release = find_release(repository, version)
    if release:
        verify_release(release, plan)
        print(f'{version} already published with matching metadata')
        return
    if not re.fullmatch(r'[0-9a-f]{40}', plan['packaging_sha']):
        raise ValueError('Publication requires the committed packaging SHA supplied by GitHub Actions')
    if plan['packaging_fingerprint'] != fingerprint():
        raise ValueError('Packaging files changed after planning; rebuild before publication')
    local_configs = {service: inspect_local_image(plan, service) for service in SERVICES}
    if api(f'repos/{repository}/git/ref/tags/{version}', missing=True):
        raise ValueError('Release tag already exists without a complete release; bump packaging_revision')
    # Check every destination before making the first registry write.
    for service in SERVICES:
        assert_absent(image(plan, service))
    digests = {}
    for service in SERVICES:
        ref = image(plan, service)
        run('docker', 'push', '--platform', plan['platform'], ref)
        digest = run('docker', 'buildx', 'imagetools', 'inspect', ref,
                     '--format', '{{.Manifest.Digest}}', capture=True).strip()
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError(f'Invalid published digest for {ref}')
        digest_ref = f"{plan['image_prefix']}-{service}@{digest}"
        verify_registry_manifest(digest_ref, local_configs[service])
        digests[service] = digest_ref
    manifest = json.loads((BUILD / 'build-manifest.json').read_text())
    manifest['images'] = digests
    save(BUILD / 'build-manifest.json', manifest)
    # The downloadable Compose file uses digests for the entire application set.
    compose = (ROOT / 'compose.yml').read_text()
    for service, digest_ref in digests.items():
        compose = compose.replace('${IMAGE_PREFIX:?Set IMAGE_PREFIX}-' + service +
                                  ':${BMP_VERSION:?Set BMP_VERSION}', digest_ref)
    (BUILD / 'compose.yml').write_text(compose)
    env_example = (ROOT / '.env.example').read_text()
    env_example = re.sub(r'^BMP_VERSION=.*$', f"BMP_VERSION={version}", env_example, flags=re.M)
    env_example = re.sub(r'^IMAGE_PREFIX=.*$', f"IMAGE_PREFIX={plan['image_prefix']}", env_example, flags=re.M)
    (BUILD / '.env.example').write_text(env_example)
    notes = BUILD / 'release-notes.md'
    notes.write_text(f"Upstream `{plan['upstream_tag']}` (`{plan['upstream_sha']}`).\n\n"
                     'Download compose.yml and .env.example for an image-only deployment. '
                     'Back up the databases before upgrading.\n')
    # A draft cannot be mistaken for a complete downloadable image set.
    run('gh', 'release', 'create', version, '--repo', repository, '--draft',
        '--target', plan['packaging_sha'], '--title', version, '--notes-file', str(notes),
        str(BUILD / 'build-manifest.json'), str(BUILD / 'compose.yml'),
        str(BUILD / '.env.example'), str(BUILD / 'upstream-source.tar.gz'))
    run('gh', 'release', 'edit', version, '--repo', repository, '--draft=false', '--latest=false')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'prepare', 'build', 'smoke', 'publish', 'latest'))
    parser.add_argument('--tag', default='')
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY',
                                                               'tanakrit-d/BalatroMultiplayerServerDocker'))
    parser.add_argument('--archive')
    args = parser.parse_args()
    if args.command == 'plan':
        plan(args.tag, args.repository)
    elif args.command == 'prepare':
        prepare(args.archive)
    else:
        globals()[args.command]()


if __name__ == '__main__':
    main()
