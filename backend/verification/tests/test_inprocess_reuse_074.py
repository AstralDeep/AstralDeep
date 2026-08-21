"""Live proof that reusable verification run IDs never reuse retired blob owners."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

import pytest
from astralplane.repositories import RepositoryConflictError

from verification.config import RunConfig
from verification.drivers.in_process import InProcessDriver
from verification.isolation import Principal
from verification.personas import Fixture
from verification.tests.conftest import INTEGRATION, run_async

pytestmark = INTEGRATION

PAYLOAD = b"id,value\n1,2\n"
LOGICAL_OWNER = Principal("__verif__local_reuse_primary")


def test_two_sequential_default_run_ids_publish_with_fresh_retired_owners(
    tmp_path,
    monkeypatch,
) -> None:
    blob_root = (tmp_path / "blobs").resolve()
    monkeypatch.setenv("ATTACHMENT_UPLOAD_ROOT", str(blob_root))
    fixture = Fixture(
        category="spreadsheet",
        extension="csv",
        filename="cohort.csv",
        writer=lambda path: Path(path).write_bytes(PAYLOAD),
    )

    async def _exercise() -> None:
        owners: list[str] = []
        attachments: list[str] = []
        for _index in range(2):
            driver = InProcessDriver(
                RunConfig(
                    mode="in_process",
                    run_id="__verif__local",
                    out_dir=str(tmp_path),
                )
            )
            await driver.setup()
            try:
                _runtime, _repositories, blobs = driver._plane_dependencies()
                for retired_owner in owners:
                    assert blobs.is_owner_absent(owner_id=retired_owner)
                uploaded = await driver.upload_as(LOGICAL_OWNER, fixture)
                owner_id = next(iter(driver._uploaded_blob_owners))
                owners.append(owner_id)
                attachments.append(uploaded["attachment_id"])
                with blobs.open_reader(
                    owner_id=owner_id,
                    key=f"{uploaded['attachment_id']}/cohort.csv",
                    max_bytes=len(PAYLOAD),
                    expected_size_bytes=len(PAYLOAD),
                    expected_sha256=hashlib.sha256(PAYLOAD).hexdigest(),
                ) as reader:
                    assert b"".join(reader.iter_chunks()) == PAYLOAD
            finally:
                await driver.teardown()
            from orchestrator import offline_grant, orchestrator as orchestrator_module
            from orchestrator import web_auth

            assert web_auth._STORE is None
            assert web_auth._CREDENTIAL_MANAGER is None
            assert offline_grant._APPLICATION_STORE is None
            assert orchestrator_module._ORCH_INSTANCE is None

        assert owners[0] != owners[1]
        inspector = InProcessDriver(
            RunConfig(
                mode="in_process",
                run_id="__verif__local",
                out_dir=str(tmp_path),
            )
        )
        await inspector.setup()
        try:
            _runtime, _repositories, blobs = inspector._plane_dependencies()
            materializations = (
                inspector.orch.runtime_composition.plane.attachment_materializations
            )
            for owner_id, prior_attachment_id in zip(owners, attachments, strict=True):
                assert blobs.is_owner_absent(owner_id=owner_id)
                probe_id = str(uuid.uuid4())
                with pytest.raises(RepositoryConflictError, match="owner is retired"):
                    materializations.begin_pending_materialization(
                        attachment_id=probe_id,
                        owner_id=owner_id,
                        filename="retired-owner-probe.txt",
                        category="text",
                        extension="txt",
                        storage_locator=(
                            f"{owner_id}/{probe_id}/retired-owner-probe.txt"
                        ),
                        storage_key=f"{probe_id}/retired-owner-probe.txt",
                        max_bytes=128,
                        created_at=int(time.time() * 1000),
                        lease_id=f"probe-{prior_attachment_id}",
                        lease_seconds=30,
                    )
        finally:
            await inspector.teardown()

    run_async(_exercise())
