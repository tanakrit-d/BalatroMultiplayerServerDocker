ARG NODE_IMAGE
ARG NAKAMA_IMAGE
FROM ${NODE_IMAGE} AS build
ARG COREPACK_VERSION
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN npm install --global corepack@${COREPACK_VERSION} && corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY nakama/package.json ./nakama/
RUN pnpm install --frozen-lockfile --filter bmp-nakama-modules
COPY nakama/ ./nakama/
COPY packages/types/ ./packages/types/
RUN pnpm --filter bmp-nakama-modules build

FROM build AS test
COPY . /app/
RUN pnpm --filter bmp-nakama-modules typecheck && pnpm --filter bmp-nakama-modules test

FROM ${NAKAMA_IMAGE} AS runtime
COPY --from=build /app/nakama/modules/index.js /nakama/data/modules/index.js
COPY nakama/local.yml /nakama/data/local.yml
COPY packaging/entrypoint.sh /nakama/entrypoint.sh
COPY packaging/build-manifest.json /nakama/build-manifest.json
ENTRYPOINT ["/bin/sh", "/nakama/entrypoint.sh"]
