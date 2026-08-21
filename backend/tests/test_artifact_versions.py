"""Feature 055 (US4) — component version history store (research D10, FR-024).

Exercises backend/orchestrator/artifact_versions.py against a real Postgres:
monotonic version numbering, archive-time pruning to the newest RETAIN rows,
bounded metadata listing, full-dict retrieval, (chat_id, user_id) ownership
scoping, the async twins, and the deletion cascades wired into
WorkspaceManager.remove, HistoryManager.delete_component and
HistoryManager.delete_chat.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import MappingProxyType

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from orchestrator import artifact_versions as av  # noqa: E402
from orchestrator.workspace import WorkspaceManager  # noqa: E402
from tests.helpers.voice_plane_runtime import (  # noqa: E402
    history_manager,
    isolated_voice_plane_runtime,
)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_voice_plane_runtime("artifact_versions") as runtime:
        yield runtime


@pytest.fixture(scope="module")
def history(plane_runtime):
    return history_manager(plane_runtime)


@pytest.fixture(scope="module")
def ws(history, plane_runtime):
    return WorkspaceManager(
        history,
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )


@pytest.fixture
def chat(history):
    """A fresh chat with a unique user per test; delete_chat sweeps versions."""
    user_id = f"pytest-av-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    yield chat_id, user_id
    history.delete_chat(chat_id, user_id)


def _comp(n: int, **extra):
    c = {"type": "card", "title": f"Version {n}", "body": f"content v{n}"}
    c.update(extra)
    return c


# ----------------------------------------------------------------------
# archive()
# ----------------------------------------------------------------------


def test_archive_assigns_monotonic_version_numbers(history, chat):
    chat_id, user_id = chat
    cid = "wc_avtest000000001"
    assert av.archive(history, chat_id, user_id, cid, _comp(1)) == 1
    assert av.archive(history, chat_id, user_id, cid, _comp(2)) == 2
    assert av.archive(history, chat_id, user_id, cid, _comp(3), reason="restore") == 3


def test_archive_numbering_is_per_component(history, chat):
    chat_id, user_id = chat
    assert av.archive(history, chat_id, user_id, "wc_avtest_a", _comp(1)) == 1
    assert av.archive(history, chat_id, user_id, "wc_avtest_b", _comp(1)) == 1
    assert av.archive(history, chat_id, user_id, "wc_avtest_a", _comp(2)) == 2


def test_archive_rejects_invalid_args(history, chat):
    chat_id, user_id = chat
    with pytest.raises(ValueError):
        av.archive(history, "", user_id, "wc_x", _comp(1))
    with pytest.raises(ValueError):
        av.archive(history, chat_id, "", "wc_x", _comp(1))
    with pytest.raises(ValueError):
        av.archive(history, chat_id, user_id, "", _comp(1))
    with pytest.raises(ValueError):
        av.archive(history, chat_id, user_id, "wc_x", "not-a-dict")
    with pytest.raises(ValueError):
        av.archive(history, chat_id, user_id, "wc_x", _comp(1), reason="undo")


def test_retention_prunes_to_newest_five(history, chat):
    """FR-024: at most RETAIN (=5) versions survive per component."""
    chat_id, user_id = chat
    cid = "wc_avtest_prune01"
    for n in range(1, 8):
        av.archive(history, chat_id, user_id, cid, _comp(n))
    versions = av.list_versions(history, chat_id, user_id, cid)
    assert [v["version_no"] for v in versions] == [7, 6, 5, 4, 3]
    assert av.get_version(history, chat_id, user_id, cid, 1) is None
    assert av.get_version(history, chat_id, user_id, cid, 2) is None
    assert av.get_version(history, chat_id, user_id, cid, 3) is not None
    assert len(versions) == av.RETAIN


# ----------------------------------------------------------------------
# list_versions() / get_version()
# ----------------------------------------------------------------------


def test_list_versions_metadata_only_and_bounded(history, chat):
    chat_id, user_id = chat
    cid = "wc_avtest_list001"
    av.archive(history, chat_id, user_id, cid, _comp(1))
    av.archive(history, chat_id, user_id, cid, _comp(2), reason="restore")
    versions = av.list_versions(history, chat_id, user_id, cid)
    assert len(versions) == 2
    newest = versions[0]
    assert newest["version_no"] == 2
    assert newest["reason"] == "restore"
    assert newest["title"] == "Version 2"
    assert newest["component_type"] == "card"
    assert isinstance(newest["created_at"], str)  # wire-ready ISO string
    assert "component" not in newest  # metadata only, no payloads
    # explicit limit respected; oversized/garbage limits clamp to RETAIN
    assert len(av.list_versions(history, chat_id, user_id, cid, limit=1)) == 1
    assert len(av.list_versions(history, chat_id, user_id, cid, limit=999)) == 2
    assert len(av.list_versions(history, chat_id, user_id, cid, limit="junk")) == 2
    assert av.list_versions(history, chat_id, user_id, "") == []


def test_get_version_roundtrips_component_dict(history, chat):
    chat_id, user_id = chat
    cid = "wc_avtest_get0001"
    original = _comp(
        1,
        component_id=cid,
        _source_agent="agentX",
        _source_tool="toolY",
        rows=[["Alice"], ["Bob"]],
        metadata={"tags": ["clinical", "review"], "approved": False},
    )
    av.archive(history, chat_id, user_id, cid, original)
    got = av.get_version(history, chat_id, user_id, cid, 1)
    assert got is not None
    assert got["component"] == original
    assert type(got["component"]) is dict
    assert type(got["component"]["rows"]) is list
    assert type(got["component"]["rows"][0]) is list
    assert type(got["component"]["metadata"]) is dict
    assert type(got["component"]["metadata"]["tags"]) is list
    assert got["version_no"] == 1
    assert got["reason"] == "refine"
    assert got["chat_id"] == chat_id
    assert got["component_id"] == cid
    assert av.get_version(history, chat_id, user_id, cid, 2) is None
    assert av.get_version(history, chat_id, user_id, cid, "junk") is None


def test_plain_component_thaws_mapping_proxy_and_rejects_non_json_values():
    detached = MappingProxyType(
        {
            "type": "table",
            "rows": (("Alice",), ("Bob",)),
            "metadata": MappingProxyType(
                {"tags": ("clinical", "review"), "approved": False}
            ),
        }
    )

    assert av._plain_component(detached) == {
        "type": "table",
        "rows": [["Alice"], ["Bob"]],
        "metadata": {"tags": ["clinical", "review"], "approved": False},
    }
    with pytest.raises(ValueError, match="non-JSON value"):
        av._plain_component(MappingProxyType({"type": "card", "body": object()}))
    with pytest.raises(ValueError, match="non-finite number"):
        av._plain_component(MappingProxyType({"type": "card", "score": float("nan")}))


def test_reads_and_deletes_are_user_scoped(history, chat):
    """Ownership: another user sees nothing and can delete nothing."""
    chat_id, user_id = chat
    cid = "wc_avtest_scope01"
    av.archive(history, chat_id, user_id, cid, _comp(1))
    intruder = f"pytest-av-intruder-{uuid.uuid4().hex[:8]}"
    assert av.list_versions(history, chat_id, intruder, cid) == []
    assert av.get_version(history, chat_id, intruder, cid, 1) is None
    assert av.delete_for_component(history, chat_id, intruder, cid) == 0
    assert av.delete_for_chat(history, chat_id, intruder) == 0
    assert av.get_version(history, chat_id, user_id, cid, 1) is not None


def test_delete_helpers_return_row_counts(history, chat):
    chat_id, user_id = chat
    av.archive(history, chat_id, user_id, "wc_avtest_del_a", _comp(1))
    av.archive(history, chat_id, user_id, "wc_avtest_del_a", _comp(2))
    av.archive(history, chat_id, user_id, "wc_avtest_del_b", _comp(1))
    assert av.delete_for_component(history, chat_id, user_id, "wc_avtest_del_a") == 2
    assert av.delete_for_component(history, chat_id, user_id, "wc_avtest_del_a") == 0
    assert av.delete_for_chat(history, chat_id, user_id) == 1
    assert av.get_version(history, chat_id, user_id, "wc_avtest_del_b", 1) is None


# ----------------------------------------------------------------------
# Deletion cascades
# ----------------------------------------------------------------------


def test_workspace_remove_cascades_versions(history, ws, chat):
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [{
        "type": "card", "title": "Live",
        "_source_agent": "agentX", "_source_tool": "toolY",
        "_source_params": {"q": 1},
    }])
    cid = ops[0]["component_id"]
    av.archive(history, chat_id, user_id, cid, _comp(1))
    av.archive(history, chat_id, user_id, cid, _comp(2))
    assert ws.remove(chat_id, user_id, cid) is True
    assert av.list_versions(history, chat_id, user_id, cid) == []


def test_history_delete_component_cascades_versions(history, ws, chat):
    """The WS/REST delete verb path (row-uuid keyed) sweeps version rows."""
    chat_id, user_id = chat
    ops = ws.upsert(chat_id, user_id, [{
        "type": "card", "title": "Live",
        "_source_agent": "agentX", "_source_tool": "toolZ",
        "_source_params": {"q": 2},
    }])
    cid = ops[0]["component_id"]
    av.archive(history, chat_id, user_id, cid, _comp(1))
    row = ws.get_by_component_id(chat_id, user_id, cid)
    assert row is not None
    assert history.delete_component(row["id"], user_id=user_id) is True
    assert av.list_versions(history, chat_id, user_id, cid) == []


def test_delete_chat_cascades_versions(history):
    user_id = f"pytest-av-{uuid.uuid4().hex[:12]}"
    chat_id = history.create_chat(user_id=user_id)
    av.archive(history, chat_id, user_id, "wc_avtest_chatdel", _comp(1))
    av.archive(history, chat_id, user_id, "wc_avtest_chatde2", _comp(1))
    assert av.get_version(history, chat_id, user_id, "wc_avtest_chatdel", 1)
    assert av.get_version(history, chat_id, user_id, "wc_avtest_chatde2", 1)
    history.delete_chat(chat_id, user_id)
    assert av.get_version(history, chat_id, user_id, "wc_avtest_chatdel", 1) is None
    assert av.get_version(history, chat_id, user_id, "wc_avtest_chatde2", 1) is None


# ----------------------------------------------------------------------
# Async twins (loop-guard-safe: only a*-functions touch the DB here)
# ----------------------------------------------------------------------


async def test_async_twins_cover_full_cycle(history, chat):
    chat_id, user_id = chat
    cid = "wc_avtest_async01"
    assert await av.aarchive(history, chat_id, user_id, cid, _comp(1)) == 1
    assert await av.aarchive(history, chat_id, user_id, cid, _comp(2), "restore") == 2
    versions = await av.alist_versions(history, chat_id, user_id, cid)
    assert [v["version_no"] for v in versions] == [2, 1]
    got = await av.aget_version(history, chat_id, user_id, cid, 1)
    assert got is not None and got["component"]["title"] == "Version 1"
    assert await av.adelete_for_component(history, chat_id, user_id, cid) == 2
    assert await av.adelete_for_chat(history, chat_id, user_id) == 0
    assert await av.alist_versions(history, chat_id, user_id, cid) == []
