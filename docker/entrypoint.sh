#!/bin/sh
set -eu
: "${DATABASE_ADDRESS:?Set DATABASE_ADDRESS}"
/nakama/nakama migrate up --database.address "$DATABASE_ADDRESS"
exec /nakama/nakama --config /nakama/data/local.yml \
  --database.address "$DATABASE_ADDRESS" \
  --socket.server_key "${NAKAMA_SERVER_KEY:-defaultkey}" \
  --session.encryption_key "${NAKAMA_SESSION_KEY:?}" \
  --session.refresh_encryption_key "${NAKAMA_REFRESH_KEY:?}" \
  --runtime.http_key "${NAKAMA_HTTP_KEY:?}" \
  --console.password "${NAKAMA_CONSOLE_PASSWORD:?}" \
  --console.signing_key "${NAKAMA_CONSOLE_SIGNING_KEY:?}" \
  --runtime.env "BMP_SERVER_KIND=community" \
  --runtime.env "ALLOW_DEVICE_AUTH=" \
  --runtime.env "STEAM_APP_ID=2379780" \
  --runtime.env "STEAM_WEB_API_KEY=${STEAM_WEB_API_KEY:-}" \
  --runtime.env "PLAYER_ID_SALT=${PLAYER_ID_SALT:?}" \
  --runtime.env "IP_HASH_SALT=${IP_HASH_SALT:-unused-community}" \
  --runtime.env "LOBBY_GRACE_SECONDS=${LOBBY_GRACE_SECONDS:-}" \
  --runtime.env "RESULT_TIMEOUT_SECONDS=${RESULT_TIMEOUT_SECONDS:-}" \
  --runtime.env "LOBBY_CATCH_UP_LIMIT_SECONDS=${LOBBY_CATCH_UP_LIMIT_SECONDS:-}" \
  --runtime.env "MODERATION_BEARER_TOKEN=${MODERATION_BEARER_TOKEN:-}" \
  --runtime.env "MODERATION_REMOTE=${MODERATION_REMOTE:-}" \
  --runtime.env "EXPRESS_INTERNAL_URL=http://api:8788" \
  --runtime.env "EXPRESS_INTERNAL_KEY=${INTERNAL_API_KEY:?}"
