"""Runs selected standard execution through the existing adapter lifecycle around one guarded invocation.
Lifecycle metadata copies no prompt or credentials; current owner, capture, provider and tool authority remain with the guarded runner.
"""

from __future__ import annotations

import asyncio

from orchestrator.harness.contract import HarnessOutcome, HarnessToolOption, HarnessTurnRequest
from orchestrator.harness.production_profiles import HarnessSelectionError
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness


def tool_metadata(descriptions):
    if descriptions is None:
        return ()
    if type(descriptions) not in (list, tuple) or len(descriptions) > 1024:
        raise HarnessSelectionError("harness_request_invalid", 422)
    names = []
    for entry in descriptions:
        name = (entry.get("function", {}).get("name") if type(entry) is dict
                and type(entry.get("function")) is dict else None)
        if (type(name) is not str or not 1 <= len(name) <= 256
                or any(ord(char) < 33 or ord(char) == 127 for char in name)):
            raise HarnessSelectionError("harness_request_invalid", 422)
        names.append(HarnessToolOption(name, ""))
    return tuple(names)


async def run_standard_invocation(invoke, *, owner_id, adapter_identity, tools=()):
    failures = []
    async def guarded():
        try:
            return await invoke()
        except Exception as error:
            failures.append(error)
            raise
    harness = StandardLLMHarness(invoke=guarded)
    if harness.identity != adapter_identity:
        raise HarnessSelectionError("harness_adapter_changed")
    execution = await harness.start(HarnessTurnRequest(owner_id, "", tools=tools))
    try:
        result = await harness.await_result(execution)
    except asyncio.CancelledError:
        settling = asyncio.create_task(harness.cancel(execution))
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError:
                continue
        settling.result()
        raise
    if failures:
        raise failures[0]
    if result.outcome is HarnessOutcome.CANCELLED:
        raise asyncio.CancelledError
    return result.decision if result.outcome is HarnessOutcome.SUCCEEDED else (None, None)
