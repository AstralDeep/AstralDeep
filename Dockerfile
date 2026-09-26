# Builds the single backend image that serves the orchestrator's WS/REST API and server-driven web
# UI on port 8001 with no separate frontend build; installs pinned local component wheels plus
# poppler/libmagic for the file-upload tools.

FROM python:3.11-slim AS component-builder
WORKDIR /build

RUN python -m pip install --no-cache-dir \
        setuptools==83.0.0 \
        wheel==0.45.1 \
        hatchling==1.27.0 \
        uv_build==0.12.15

COPY pyproject.toml .gitmodules ./
COPY config/astral-composition.json ./config/astral-composition.json
COPY scripts/install_local_components.py ./scripts/install_local_components.py

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

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils \
        libmagic1 \
        build-essential \
        cmake \
        libssl-dev \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

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

RUN python -m spacy download en_core_web_lg

COPY backend/ ./backend/

COPY backend/start-docker.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/start-docker.sh

EXPOSE 8001

CMD ["/usr/local/bin/start-docker.sh"]
