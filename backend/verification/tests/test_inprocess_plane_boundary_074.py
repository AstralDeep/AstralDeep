"""Static and wiring checks for the qualification driver's Plane boundary."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
import verification.drivers.in_process as in_process_module
from verification.drivers.in_process import InProcessDriver
from verification.isolation import Principal


def _driver_with_plane(
    runtime: object,
    repositories: object,
    blobs: object | None = None,
    *,
    materializer: object | None = None,
    purges: object | None = None,
    close_events: list[str] | None = None,
) -> InProcessDriver:
    driver = object.__new__(InProcessDriver)
    driver.config = SimpleNamespace(run_id="__verif__plane-boundary")
    driver._uploaded_blob_owners = set()
    driver._execution_nonce = "0123456789abcdef"
    driver._execution_principals = {}

    class VoiceServices:
        async def close(self) -> None:
            if close_events is not None:
                close_events.append("voice")

    class RuntimeComposition:
        def __init__(self) -> None:
            self.plane = SimpleNamespace(
                runtime=runtime,
                repositories=repositories,
                blobs=blobs or object(),
            )

        async def close(self) -> None:
            if close_events is not None:
                close_events.extend(
                    ["materializer", "materializations", "purge", "lets", "plane"]
                )

    driver.orch = SimpleNamespace(
        runtime_composition=RuntimeComposition(),
        voice_services=VoiceServices(),
        attachment_materialization_service=materializer,
        attachment_purge_coordinator=purges or _Purges(),
    )
    return driver


class _Transaction:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return None


class _Runtime:
    def transaction(self):
        return _Transaction()


class _Purges:
    def __init__(self) -> None:
        self.owners: list[str] = []
        self.ready_assertions = 0

    async def aschedule_owner(self, *, owner_id: str) -> None:
        self.owners.append(owner_id)

    async def areconcile_once(self, *, fail_on_incomplete: bool):
        assert fail_on_incomplete is True
        return ()

    def assert_globally_ready(self, _transaction: object) -> None:
        self.ready_assertions += 1


def test_driver_initializes_empty_blob_owner_inventory(tmp_path) -> None:
    driver = InProcessDriver(
        SimpleNamespace(run_dir=str(tmp_path), run_id="__verif__inventory")
    )

    assert driver._uploaded_blob_owners == set()
    assert driver._execution_principals == {}


def test_two_driver_executions_map_same_logical_principal_to_unique_owners(
    tmp_path,
) -> None:
    config = SimpleNamespace(
        run_dir=str(tmp_path),
        run_id="__verif__local",
    )
    logical = Principal("__verif__local_everyday_primary")
    first = InProcessDriver(config)
    second = InProcessDriver(config)

    first_owner = first._execution_principal(logical)
    second_owner = second._execution_principal(logical)

    assert first._execution_principal(first_owner) is first_owner
    assert second._execution_principal(second_owner) is second_owner
    assert first_owner.user_id != second_owner.user_id
    assert first_owner.user_id.startswith("__verif__local_everyday_primary_exec_")
    assert second_owner.user_id.startswith("__verif__local_everyday_primary_exec_")
    assert len(first_owner.user_id) <= 255
    assert len(second_owner.user_id) <= 255


def test_run_scenario_uses_one_execution_identity_at_every_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    from verification.personas import Fixture
    from verification.scenarios import Scenario

    driver = InProcessDriver(
        SimpleNamespace(
            run_dir=str(tmp_path),
            run_id="__verif__plane-boundary",
        )
    )
    logical = Principal("__verif__plane-boundary_everyday_primary")
    observed: list[str] = []

    class ToolPermissions:
        def set_agent_scopes(self, owner_id, _agent_id, _scopes):
            observed.append(owner_id)

    class History:
        def create_chat(self, *, user_id):
            observed.append(user_id)
            return "chat-identity"

    class Workspace:
        def live_components(self, _chat_id, owner_id):
            observed.append(owner_id)
            return []

    async def handle_chat_message(
        websocket,
        _query,
        _chat_id,
        *,
        user_id,
        attachments,
    ):
        observed.extend([user_id, websocket.label, websocket.client[1]])
        observed.append(driver.orch.ui_sessions[websocket]["sub"])
        assert attachments[0]["attachment_id"] == "attachment-identity"

    driver.orch = SimpleNamespace(
        tool_permissions=ToolPermissions(),
        history=History(),
        workspace=Workspace(),
        ui_sessions={},
        ui_clients=[],
        _ws_active_chat={},
        _llm_store=None,
        handle_chat_message=handle_chat_message,
    )

    async def upload_as(principal, _fixture):
        observed.append(principal.user_id)
        return {
            "attachment_id": "attachment-identity",
            "filename": "cohort.csv",
            "category": "spreadsheet",
            "path": str(tmp_path / "cohort.csv"),
        }

    driver.upload_as = upload_as  # type: ignore[method-assign]
    driver._read_audit = lambda owner_id: (observed.append(owner_id) or [], True)
    monkeypatch.setattr(in_process_module, "scripted_llm_for", lambda *_args: object())
    scenario = Scenario(
        scenario_id="everyday:primary",
        persona=SimpleNamespace(
            query="summarize",
            fixture=Fixture(
                category="spreadsheet",
                extension="csv",
                filename="cohort.csv",
                writer=lambda path: Path(path).write_bytes(b"id,value\n1,2\n"),
            ),
        ),
        principal=logical,
        auth_mode="mock_inprocess",
    )

    asyncio.run(driver.run_scenario(scenario))

    execution_id = driver._execution_principal(logical).user_id
    assert observed
    assert set(observed) == {execution_id}


def test_driver_teardown_injects_the_application_plane(monkeypatch) -> None:
    runtime = _Runtime()
    repositories = SimpleNamespace(harness_cleanup=object())
    close_events: list[str] = []
    purges = _Purges()
    driver = _driver_with_plane(
        runtime,
        repositories,
        purges=purges,
        close_events=close_events,
    )
    calls = []

    def cleanup(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(in_process_module, "teardown", cleanup)

    asyncio.run(driver.teardown())

    assert calls == [
        {
            "plane_runtime": runtime,
            "plane_repositories": repositories,
            "run_id": "__verif__plane-boundary",
        }
    ]
    assert purges.ready_assertions == 1
    assert close_events == [
        "voice",
        "materializer",
        "materializations",
        "purge",
        "lets",
        "plane",
    ]
    assert driver.orch is None
    asyncio.run(driver.teardown())
    assert len(close_events) == 6


@pytest.mark.parametrize(
    "plane",
    [
        None,
        SimpleNamespace(runtime=object(), repositories=SimpleNamespace()),
        SimpleNamespace(runtime=_Runtime(), repositories=None),
    ],
)
def test_driver_fails_closed_without_composed_application_plane(plane) -> None:
    driver = object.__new__(InProcessDriver)
    driver.orch = SimpleNamespace(
        runtime_composition=SimpleNamespace(plane=plane)
    )

    with pytest.raises(RuntimeError, match="application Plane"):
        driver._plane_dependencies()


def test_driver_source_has_no_deep_database_or_raw_sql_boundary() -> None:
    source = inspect.getsource(in_process_module)
    assert "history.db" not in source
    assert "shared.database" not in source
    assert "DELETE FROM" not in source
    assert ".execute(" not in source
    assert ".delete_owner(" not in source
    assert "materialize_stream(" in source
    assert "draft_store.create_draft_agent" in source
    assert "draft_store.delete_draft_agent" in source


def test_fixture_upload_streams_off_loop_through_application_plane(
    tmp_path,
    monkeypatch,
) -> None:
    from verification.isolation import Principal
    from verification.personas import Fixture

    runtime = _Runtime()
    repositories = SimpleNamespace(artifacts=object())
    calls: list[dict[str, object]] = []

    class Materializer:
        async def materialize_stream(self, **values):
            payload = b"".join([chunk async for chunk in values.pop("chunks")])
            calls.append({**values, "payload": payload})
            return SimpleNamespace(attachment_id=values["attachment_id"])

    purges = _Purges()
    driver = _driver_with_plane(
        runtime,
        repositories,
        materializer=Materializer(),
        purges=purges,
    )
    driver._tmp = str(tmp_path / "fixtures")
    fixture = Fixture(
        category="spreadsheet",
        extension="csv",
        filename="cohort.csv",
        writer=lambda path: Path(path).write_bytes(b"id,value\n1,2\n"),
    )

    result = asyncio.run(
        driver.upload_as(
            Principal("__verif__plane-boundary_blob_owner"),
            fixture,
        )
    )

    assert result["filename"] == "cohort.csv"
    owner_id = str(calls[0]["owner_id"])
    assert owner_id.startswith("__verif__plane-boundary_blob_owner_exec_")
    assert calls[0]["payload"] == b"id,value\n1,2\n"
    assert driver._uploaded_blob_owners == {owner_id}

    cleanup_calls = []
    monkeypatch.setattr(
        in_process_module,
        "teardown",
        lambda **kwargs: cleanup_calls.append(kwargs) or 0,
    )
    asyncio.run(driver.teardown())

    assert cleanup_calls == [
        {
            "plane_runtime": runtime,
            "plane_repositories": repositories,
            "run_id": "__verif__plane-boundary",
        }
    ]
    assert purges.owners == [owner_id]


def test_failed_fixture_materialization_still_tracks_owner_for_teardown(
    tmp_path,
    monkeypatch,
) -> None:
    from verification.isolation import Principal
    from verification.personas import Fixture

    runtime = _Runtime()
    repositories = SimpleNamespace(artifacts=object())
    owners: list[str] = []

    class FailingMaterializer:
        async def materialize_stream(self, **values):
            owners.append(str(values["owner_id"]))
            raise RuntimeError("materialization failed")

    purges = _Purges()
    driver = _driver_with_plane(
        runtime,
        repositories,
        materializer=FailingMaterializer(),
        purges=purges,
    )
    driver._tmp = str(tmp_path / "fixtures")
    fixture = Fixture(
        category="spreadsheet",
        extension="csv",
        filename="cohort.csv",
        writer=lambda path: Path(path).write_bytes(b"id,value\n1,2\n"),
    )

    with pytest.raises(RuntimeError, match="materialization failed"):
        asyncio.run(
            driver.upload_as(
                Principal("__verif__plane-boundary_blob_failure"),
                fixture,
            )
        )

    assert driver._uploaded_blob_owners == set(owners)
    monkeypatch.setattr(in_process_module, "teardown", lambda **_kwargs: 0)
    asyncio.run(driver.teardown())
    assert purges.owners == owners


def test_driver_teardown_closes_graph_after_cleanup_failure(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    repositories = SimpleNamespace(harness_cleanup=object())

    close_events: list[str] = []
    driver = _driver_with_plane(
        runtime,
        repositories,
        close_events=close_events,
    )

    def cleanup(**_kwargs):
        raise RuntimeError("typed cleanup failed")

    monkeypatch.setattr(in_process_module, "teardown", cleanup)

    with pytest.raises(RuntimeError, match="typed cleanup failed"):
        asyncio.run(driver.teardown())

    assert close_events == [
        "voice",
        "materializer",
        "materializations",
        "purge",
        "lets",
        "plane",
    ]
    assert driver.orch is None


def test_driver_graph_close_prefers_orchestrator_unified_shutdown() -> None:
    events: list[str] = []

    async def _unified_close() -> None:
        events.append("unified")

    async def _forbidden_close() -> None:
        events.append("bypassed")

    orchestrator = SimpleNamespace(
        _close_started_services=_unified_close,
        voice_services=SimpleNamespace(close=_forbidden_close),
        runtime_composition=SimpleNamespace(close=_forbidden_close),
    )

    asyncio.run(in_process_module._close_owned_orchestrator_graph(orchestrator))

    assert events == ["unified"]


def test_teardown_schedules_every_owner_through_repeated_cancel_and_failure(
    monkeypatch,
) -> None:
    runtime = _Runtime()
    repositories = SimpleNamespace(harness_cleanup=object())
    close_events: list[str] = []

    class BlockingPurges(_Purges):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def aschedule_owner(self, *, owner_id: str) -> None:
            self.owners.append(owner_id)
            if len(self.owners) == 1:
                self.entered.set()
                await self.release.wait()
                raise RuntimeError("first owner schedule failed")

    purges = BlockingPurges()
    driver = _driver_with_plane(
        runtime,
        repositories,
        purges=purges,
        close_events=close_events,
    )
    driver._uploaded_blob_owners = {"__verif__owner_b", "__verif__owner_a"}
    cleanup_calls: list[dict] = []
    monkeypatch.setattr(
        in_process_module,
        "teardown",
        lambda **values: cleanup_calls.append(values),
    )

    async def _exercise() -> None:
        first = asyncio.create_task(driver.teardown())
        await asyncio.wait_for(purges.entered.wait(), timeout=2)
        shared = driver._teardown_task
        assert shared is not None
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(driver.teardown())
        assert driver._teardown_task is shared
        purges.release.set()
        with pytest.raises(RuntimeError, match="first owner schedule failed"):
            await first
        with pytest.raises(RuntimeError, match="first owner schedule failed"):
            await second

    asyncio.run(_exercise())

    assert purges.owners == ["__verif__owner_a", "__verif__owner_b"]
    assert cleanup_calls == []
    assert close_events == [
        "voice",
        "materializer",
        "materializations",
        "purge",
        "lets",
        "plane",
    ]
    assert driver.orch is None
