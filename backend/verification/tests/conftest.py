"""Shared fixtures for the verification harness suite (backend/verification/config.py):
boots against the live container Postgres, skipping cleanly when unavailable, and
runs async tests via run_async().
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _db_ok() -> bool:
    try:
        import psycopg2

        from tests.helpers.voice_plane_runtime import build_test_database_url

        conn = psycopg2.connect(build_test_database_url())
        conn.close()
        return True
    except Exception:
        return False


INTEGRATION = [
    pytest.mark.integration,
    pytest.mark.skipif(not _db_ok(), reason="Postgres unavailable"),
]


@pytest.fixture
def run_config(tmp_path):
    from verification.config import RunConfig

    return RunConfig(
        mode="in_process",
        run_id=f"__verif__{uuid.uuid4().hex[:10]}",
        out_dir=str(tmp_path),
    )


def run_async(coro):
    async def _wrapper():
        result = await coro
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    return asyncio.run(_wrapper())
