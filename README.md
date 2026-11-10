# Balatro Multiplayer Server Docker

Prebuilt Community server images for Docker (`linux/amd64`), built from upstream stable tags.

## Run

Download `compose.yml` and `.env.example` from a release. Copy `.env.example` to
`.env`, fill each secret with a different random hex value, and set `WEB_BASE_URL`.

```sh
docker compose pull
docker compose up -d --wait
```

Players connect on port `7350` with key `defaultkey`. Data lives in
`/mnt/user/appdata/balatro-mp-server`. Set `COMPOSE_PROFILES=web` for the website/API.

## Update

Back up `.env` and databases. Replace `compose.yml` with the next release's file,
retain `.env` and appdata, then rerun the commands above.

## Publish

Actions publishes tested images to GHCR daily, on `main` pushes, or manually.
Make packages public after the first publication. Versions use
`<upstream-version>-r<packaging_revision>`. Bump the revision in `config.json`
for packaging changes.
