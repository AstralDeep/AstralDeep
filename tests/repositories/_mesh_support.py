"""Builders for synthetic mesh events, bodies, roster entries and checkpoints whose ids are
real sha256 references of their bytes, shared by the mesh event store tests.
"""

from __future__ import annotations

import hashlib
from typing import Any

from astralplane import create_event_log_repository, create_event_roster_repository

OWNER = "mesh-owner"
OTHER = "mesh-other"
FEED = "feed-phone"
FEED_TWO = "feed-laptop"


def ref(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def genesis(tx: Any, owner: str = OWNER, tag: str = "genesis") -> str:
    entry = create_event_roster_repository().create_mesh(
        tx, owner_id=owner, kind="genesis", signed_bytes=f"{tag}-{owner}".encode(), recorded_at=1
    )
    return entry.mesh_id


def event_kwargs(
    mesh: str,
    seq: int,
    prev: str | None,
    *,
    owner: str = OWNER,
    feed: str = FEED,
    tag: str = "a",
    scope: str = "chat-1",
    clock: tuple[int, int] = (1000, 0),
    body: bytes | None = None,
    labels: object | None = None,
) -> dict[str, Any]:
    header = f"header|{feed}|{seq}|{tag}".encode()
    sealed_body = body if body is not None else f"body|{feed}|{seq}|{tag}".encode()
    return {
        "owner_id": owner,
        "mesh_id": mesh,
        "feed_id": feed,
        "seq": seq,
        "event_id": ref(header),
        "prev_hash": prev,
        "clock_ms": clock[0],
        "clock_n": clock[1],
        "scope": scope,
        "event_type": "chat.message",
        "labels": {"trust": "owner"} if labels is None else labels,
        "key_id": "epoch-1",
        "body_hash": ref(sealed_body),
        "header_bytes": header,
        "header_sealed": b"sealed|" + header,
        "recorded_at": 5000 + seq,
    }


def append_chain(tx: Any, mesh: str, count: int, **options: Any) -> list[Any]:
    repo = create_event_log_repository()
    results = []
    prev = None
    for seq in range(1, count + 1):
        kwargs = event_kwargs(mesh, seq, prev, **options)
        result = repo.append_event(tx, **kwargs)
        results.append(result)
        prev = kwargs["event_id"]
    return results
