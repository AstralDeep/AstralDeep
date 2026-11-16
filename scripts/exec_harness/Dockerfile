FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap patchelf \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==9.1.1 coverage==7.15.2 \
    && useradd --uid 1000 --create-home harness
USER 1000:1000
WORKDIR /src
