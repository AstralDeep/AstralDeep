# Dockerfile for AstralDeep Multi-Agent System
#
# Feature 026: single backend image. The orchestrator serves the server-driven
# web UI (astralprims primitives rendered by webrender, adapted by ROTE) directly
# on port 8001 — there is no separate React/Vite frontend build or static server.

FROM python:3.11-slim AS component-builder
WORKDIR /build

# Component build backends are isolated from the runtime image and pinned here.
# The component installer disables index access, dependency resolution, and
# build isolation while producing the four wheels, so only these preinstalled
# backends and the exact local submodule sources can participate in the build.
RUN python -m pip install --no-cache-dir \
        setuptools==83.0.0 \
        wheel==0.45.1 \
        hatchling==1.27.0 \
        uv_build==0.12.3

COPY pyproject.toml .gitmodules ./
COPY config/astral-composition.json ./config/astral-composition.json
COPY scripts/install_local_components.py ./scripts/install_local_components.py

# Copy only declared Python build inputs. Native clients and other component
# product sources stay in the build context for their own qualification lanes,
# but they are not copied into the backend image or a component wheel.
COPY components/AstralProjection/pyproject.toml components/AstralProjection/README.md components/AstralProjection/LICENSE.md components/AstralProjection/NOTICE ./components/AstralProjection/
COPY components/AstralProjection/src/astralprojection/ ./components/AstralProjection/src/astralprojection/
COPY components/AstralProjection/backend/webrender/ ./components/AstralProjection/backend/webrender/
COPY components/AstralProjection/backend/rote/ ./components/AstralProjection/backend/rote/
COPY components/AstralProjection/contracts/ ./components/AstralProjection/contracts/

COPY components/AstralPlane/pyproject.toml components/AstralPlane/README.md components/AstralPlane/LICENSE ./components/AstralPlane/
COPY components/AstralPlane/src/astralplane/ ./components/AstralPlane/src/astralplane/

COPY components/AstralPrimitives/pyproject.toml components/AstralPrimitives/README.md components/AstralPrimitives/LICENSE ./components/AstralPrimitives/
COPY components/AstralPrimitives/src/astralprims/ ./components/AstralPrimitives/src/astralprims/

COPY components/LETS/pyproject.toml components/LETS/README.md components/LETS/LICENSE components/LETS/NOTICE ./components/LETS/
COPY components/LETS/src/lets/ ./components/LETS/src/lets/

RUN python scripts/install_local_components.py build \
        --root /build \
        --wheel-dir /component-wheels \
        --lock /component-wheels/astral-component-wheels.lock.json

FROM python:3.11-slim AS runtime
WORKDIR /app

# System packages.
#
# Runtime (file-upload parsing, feature 002-file-uploads):
#   poppler-utils  - PDF rendering used by pdf2image (image-only PDFs are
#                    handed to the vision model)
#   libmagic1      - libmagic bindings used by python-magic for content-type sniffing
#
# Build toolchain (build-essential + cmake + git):
#   Required to compile source-only wheels. On linux/arm64 — which Docker Desktop
#   builds by default on Apple Silicon Macs — some medical-imaging deps publish no
#   prebuilt wheel and build from source. aicspylibczi in particular uses a
#   CMake/pybind11 build that (a) fails with "No such file or directory: 'cmake'"
#   without cmake, and (b) fetches its vendored libCZI sources over git, so git is
#   needed too (verified: build-essential+cmake+git compiles aicspylibczi 3.3.1 on
#   arm64). On amd64 (typical Linux/Windows CI + prod) a prebuilt wheel is used and
#   this toolchain is never invoked. Installing it keeps `docker compose build`
#   green on every architecture — Mac, Windows, and Linux alike.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        libmagic1 \
        build-essential \
        cmake \
        git \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install wheel/setuptools first to ensure binary wheels are downloaded
# instead of compiling heavy packages like pandas from source, saving lots of time and memory.
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy backend requirements and install
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Install the exact local component wheels only after the runtime dependency
# closure exists. The lock is retained in the image for provenance/diagnostics;
# every install is checked against the wheel SHA-256, PEP 610 archive hash,
# installed RECORD hashes, exact name/version, and required package data.
COPY --from=component-builder /build/pyproject.toml /build/.gitmodules ./
COPY --from=component-builder /build/config/astral-composition.json ./config/astral-composition.json
COPY --from=component-builder /build/scripts/install_local_components.py ./scripts/install_local_components.py
COPY --from=component-builder /component-wheels/ /opt/astral-component-wheels/
RUN python scripts/install_local_components.py install \
        --root /app \
        --lock /opt/astral-component-wheels/astral-component-wheels.lock.json \
    && python scripts/install_local_components.py verify \
        --root /app \
        --lock /opt/astral-component-wheels/astral-component-wheels.lock.json \
    && python -m pip check

# Download the spaCy model used by Presidio for PHI detection at build time
# (feature 025-agentic-soul-integration) so no model is fetched over the network at runtime.
RUN python -m spacy download en_core_web_lg

# Copy backend source
COPY backend/ ./backend/

# NOTE: configuration is intentionally NOT baked into the image. Secrets in
# image layers survive `docker rmi` in registry caches and leak via `docker
# history`. Supply configuration at runtime instead:
#   docker compose:  env_file: .env   (already wired in docker-compose.yml)
#   docker run:      --env-file .env
# load_dotenv(override=False) in start.py tolerates the absent file.

# Setup entrypoint script
COPY backend/start-docker.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/start-docker.sh

# Expose ports
# 8001: Orchestrator Gateway — serves WS, REST API, and the server-driven web UI
EXPOSE 8001

CMD ["/usr/local/bin/start-docker.sh"]
