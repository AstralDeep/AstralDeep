# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# Build the dedicated offline scanner root filesystem from the reviewed source archive and frozen SDK dependency graph.
FROM node:24-bookworm@sha256:e2007d1f4e652444f88d9bb196256d2d035269015e3d27a0d47c1d0cb5192a98

ADD --checksum=sha256:33dd0748f27e7916c4f1c8b6943461983e3453b06bbda6312a6280130b4881e5 https://registry.npmjs.org/pnpm/-/pnpm-11.25.0.tgz /tmp/pnpm.tgz
RUN npm install --global --ignore-scripts --no-audit --no-fund /tmp/pnpm.tgz

COPY runtime-lock.json /runtime-lock.json
COPY upstream.tar /tmp/upstream.tar
RUN echo '4b31e070605bec72aa82b661f21bea245ad0bee84fee7544c884960516a324bf  /tmp/upstream.tar' | sha256sum --check --strict \
    && mkdir /scanner \
    && tar -xf /tmp/upstream.tar -C /scanner \
    && rm /tmp/upstream.tar
WORKDIR /scanner/sdk/typescript
RUN test "$(node --version)" = v24.22.1 \
    && test "$(pnpm --version)" = 11.25.0 \
    && pnpm install --frozen-lockfile --ignore-scripts \
    && pnpm run build \
    && node --input-type=module -e 'import{readFileSync}from"node:fs";import{createHash}from"node:crypto";const lock=JSON.parse(readFileSync("/runtime-lock.json"));for(const[path,hash]of Object.entries(lock.files)){if(createHash("sha256").update(readFileSync("/"+path)).digest("hex")!==hash)throw Error("reviewed runtime file mismatch: "+path)}' \
    && node bin/codex-security.mjs --version \
    && command -v python3 \
    && command -v git \
    && rm -rf /root /tmp/* /usr/local/share/.cache /usr/local/lib/node_modules/pnpm/.tools \
    && mkdir -p /root /source /output

WORKDIR /
CMD ["/usr/bin/true"]
