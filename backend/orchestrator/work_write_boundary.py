"""Freezes transport state and bounds/validates raw request bytes shared by every Work
write route, before any route authenticates or a cookie can change. Used by
work_api.py, work_admission_api.py, and work_control_authority.py.
"""

from __future__ import annotations

import asyncio
import json
import math
import re

from fastapi import Request
from persistent_agents.models import AssignmentError

MAX_BODY_BYTES = 16384
BODY_SECONDS = 15


def freeze_work_request(request: Request) -> Request:
    try:
        if not isinstance(request, Request):
            raise ValueError
        scope = dict(request.scope)
        headers, size = [], 0
        for key, value in scope["headers"]:
            if type(key) is not bytes or type(value) is not bytes:
                raise ValueError
            size += len(key) + len(value)
            if len(headers) >= 256 or size > 65536:
                raise ValueError
            headers.append((key.lower(), value))
        query = scope.get("query_string", b"")
        if type(query) is not bytes:
            raise ValueError
        scope.update(headers=headers, query_string=query, state={})
        for key in ("server", "client"):
            if scope.get(key) is not None:
                scope[key] = tuple(scope[key])
        return Request(scope, receive=request.receive)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise AssignmentError("work_authentication_required", 401) from None


def work_content_length(request: Request) -> int | None:
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1 or (
        lengths and re.fullmatch(r"[0-9]{1,10}", lengths[0]) is None
    ):
        raise AssignmentError("work_body_invalid", 400)
    length = int(lengths[0]) if lengths else None
    if length is not None and length > MAX_BODY_BYTES:
        raise AssignmentError("work_body_too_large", 413)
    if request.headers.getlist("content-encoding") not in ([], ["identity"]):
        raise AssignmentError("work_body_invalid", 415)
    return length


async def read_work_body(request: Request, expected: int | None, *, seconds=BODY_SECONDS) -> bytes:
    data = bytearray()
    try:
        async with asyncio.timeout(seconds):
            while True:
                message = await request.receive()
                # Yield even when empty, or an eager peer starves the timeout
                await asyncio.sleep(0)
                if type(message) is not dict:
                    raise AssignmentError("work_body_invalid", 400)
                if message.get("type") == "http.disconnect":
                    raise AssignmentError("work_disconnected", 400)
                chunk = message.get("body", b"")
                more = message.get("more_body", False)
                if (message.get("type") != "http.request" or type(chunk) is not bytes
                        or type(more) is not bool):
                    raise AssignmentError("work_body_invalid", 400)
                if len(data) + len(chunk) > MAX_BODY_BYTES:
                    raise AssignmentError("work_body_too_large", 413)
                data.extend(chunk)
                if not more:
                    break
    except TimeoutError:
        raise AssignmentError("work_body_timeout", 408) from None
    if expected is not None and len(data) != expected:
        raise AssignmentError("work_body_invalid", 400)
    return bytes(data)


async def cache_work_write_body(request: Request) -> None:
    raw = await read_work_body(request, work_content_length(request), seconds=BODY_SECONDS)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed

    def constant(_value):
        raise ValueError

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_float=number, parse_constant=constant)
        json.dumps(parsed, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        raise AssignmentError("work_body_invalid", 400) from None
    request._body = raw
