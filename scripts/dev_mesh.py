#!/usr/bin/env python3
"""Creates a seeded development mesh owner, pairs a personal node to it and mints caller tickets for named node tools, all on the real kernel through backend/devtools/dev_mesh.py.
It refuses to run unless ASTRAL_ENV is exactly development, and the printed ticket plus the printed holder key sign each request with the Astral-Caller-Proof header."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from devtools import dev_mesh  # noqa: E402

CATALOG_WAIT_S = 60.0
PAIR_WAIT_S = 60.0


def _owner(args: argparse.Namespace) -> dev_mesh.DevOwner:
    return dev_mesh.DevOwner(args.owner, Path(args.dir))


async def _until(condition, what: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.2)
    raise SystemExit(f"timed out waiting for {what}")


async def create(args: argparse.Namespace) -> int:
    owner = _owner(args)
    owner.create_mesh()
    owner.confirm_recovery()
    print(f"mesh {owner.mesh_id}")
    print(f"owner-device {owner.device_id}")
    return 0


async def pair(args: argparse.Namespace) -> int:
    owner = _owner(args)
    await owner.start()
    try:
        await owner.approve_offer(args.offer)
        print(f"paired a node to mesh {owner.mesh_id}")
    finally:
        await owner.stop()
    return 0


async def ticket(args: argparse.Namespace) -> int:
    owner = _owner(args)
    await owner.start()
    try:
        async def listed() -> bool:
            names = {entry.tool.name for entry in owner.kernel.peer_tool_catalog() if entry.device == args.node}
            return set(args.tool) <= names

        await _until(listed, "the node's published tool catalog", CATALOG_WAIT_S)
        holder = dev_mesh.CallerHolder.generate()
        minted = owner.mint_caller_ticket(
            args.node, holder, args.tool, label=args.label, a2a=args.a2a, lifetime_s=args.lifetime_s,
        )
    finally:
        await owner.stop()
    print(f"ticket {minted.text}")
    print(f"holder-key {holder.key_text}")
    print(f"expires-ms {minted.expires_ms}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    root.add_argument("--dir", required=True, help="the owner device's data directory")
    root.add_argument("--owner", default="owner", choices=dev_mesh.OWNERS)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("create", help="create the development mesh on this owner device")
    pairing = commands.add_parser("pair", help="approve a node's pairing offer")
    pairing.add_argument("--offer", required=True)
    minting = commands.add_parser("ticket", help="mint a caller ticket for named node tools")
    minting.add_argument("--node", required=True, help="the node's device id")
    minting.add_argument("--tool", action="append", default=[], help="a tool name, repeatable")
    minting.add_argument("--a2a", action="store_true", help="also cover a2a.message")
    minting.add_argument("--label", default="Development caller")
    minting.add_argument("--lifetime-s", type=int, default=dev_mesh.DEFAULT_LIFETIME_S)
    return root


COMMANDS = {"create": create, "pair": pair, "ticket": ticket}


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        dev_mesh.require_development()
    except dev_mesh.DevMeshRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return asyncio.run(COMMANDS[args.command](args))


if __name__ == "__main__":
    raise SystemExit(main())
