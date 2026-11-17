# One image for the executor and the sandbox: the same code, run as `astralvessel executor` or `astralvessel sandbox`.
# Compose applies sandbox hardening and mounts agent packages at runtime. Build with
# --build-context kernel-artifacts=<folder holding the pinned kernel wheel and mailbox>; the payloads are verified, installed, and retained for matching Deep node builds.
FROM scratch AS kernel-artifacts

FROM python:3.15-slim
LABEL org.opencontainers.image.source="https://github.com/AstralDeep/AstralDeep"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ripgrep ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/astralvessel
COPY pyproject.toml README.md ./
COPY astralvessel ./astralvessel
COPY examples/field-guide ./examples/field-guide
COPY examples/sample-data ./examples/sample-data
RUN pip install --no-cache-dir .

COPY config/kernel-binding.json scripts/install_kernel.py /opt/vessel-build/
RUN --mount=type=bind,from=kernel-artifacts,target=/kernel-artifacts \
    python /opt/vessel-build/install_kernel.py /kernel-artifacts
COPY --from=kernel-artifacts / /opt/vessel-build/kernel-artifacts/

RUN useradd --create-home --uid 1001 --shell /usr/sbin/nologin vessel \
    && mkdir -p /data /executor && chown vessel:vessel /data /executor
USER vessel
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VESSEL_HOST=0.0.0.0
EXPOSE 8700
CMD ["astralvessel", "executor"]
