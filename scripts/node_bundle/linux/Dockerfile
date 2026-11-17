# Builds the Linux node pack builder image on Debian bookworm pinned by digest, with apt fixed to a dated snapshot so the native tools are reproducible.
# build_linux.py runs it to assemble a pack and reads the slim image digest below to check the finished pack in a clean system.
ARG BOOKWORM=debian:bookworm@sha256:268fb24c39bca92d326dd83ea3abebe4a3f7751c804ffef23bb6d857dd343ff8
ARG BOOKWORM_SLIM=debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
ARG SNAPSHOT=20261115T000000Z

FROM ${BOOKWORM} AS builder
ARG SNAPSHOT
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT}/ bookworm main" \
        "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${SNAPSHOT}/ bookworm-security main" \
        > /etc/apt/sources.list \
    && apt-get update -o Acquire::Check-Valid-Until=false \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates git patchelf python3 \
        libmagic1=1:5.44-3 libmagic-mgc=1:5.44-3 poppler-utils=22.12.0-2+deb12u3 \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
WORKDIR /work
