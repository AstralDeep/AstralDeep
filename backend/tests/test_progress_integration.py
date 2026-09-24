#!/usr/bin/env python3
"""Tests for backend/shared/progress.py's ProgressEvent/ProgressEmitter integration with
the SSE endpoint and the frontend's progress hook: event shape, SSE framing, and
legacy log compatibility.
"""

import sys
import os
import json
import asyncio
import time
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.progress import ProgressPhase, ProgressStep, ProgressEvent, ProgressEmitter


def test_progress_event_compatibility():
    backend_event = ProgressEvent(
        phase=ProgressPhase.GENERATION,
        step=ProgressStep.PROMPT_CONSTRUCTION,
        percentage=10,
        message="Constructing prompt...",
        data={"agent_name": "test"},
        timestamp=time.time()
    )
    
    event_dict = backend_event.to_dict()
    
    assert event_dict["type"] == "progress"
    assert event_dict["phase"] == "generation"
    assert event_dict["step"] == "prompt_construction"
    assert event_dict["percentage"] == 10
    assert event_dict["message"] == "Constructing prompt..."
    assert event_dict["data"]["agent_name"] == "test"
    assert "timestamp" in event_dict
    
    required_fields = ["type", "phase", "step", "percentage", "message", "data", "timestamp"]
    for field in required_fields:
        assert field in event_dict, f"Missing field: {field}"
    
    print("[OK] ProgressEvent compatibility test passed")


def test_sse_format():
    backend_event = ProgressEvent(
        phase=ProgressPhase.TESTING,
        step=ProgressStep.SAVING_FILES,
        percentage=10,
        message="Saving files...",
        timestamp=time.time()
    )
    
    sse = backend_event.to_sse()
    
    assert sse.startswith("data: ")
    assert sse.endswith("\n\n")
    
    json_str = sse[6:-2]
    parsed = json.loads(json_str)
    assert parsed["type"] == "progress"
    
    print("[OK] SSE format test passed")


def test_legacy_log_compatibility():
    from shared.progress import create_log_event
    
    sse_log = create_log_event("Test log message", "log")
    assert sse_log.startswith("data: ")
    assert sse_log.endswith("\n\n")
    
    json_str = sse_log[6:-2]
    log_data = json.loads(json_str)
    assert log_data["status"] == "log"
    assert log_data["message"] == "Test log message"
    assert "timestamp" in log_data
    
    sse_success = create_log_event("Success!", "success")
    json_str = sse_success[6:-2]
    success_data = json.loads(json_str)
    assert success_data["status"] == "success"
    
    sse_error = create_log_event("Error!", "error")
    json_str = sse_error[6:-2]
    error_data = json.loads(json_str)
    assert error_data["status"] == "error"
    
    print("[OK] Legacy log compatibility test passed")


def test_progress_emitter_integration():
    collected_events = []
    
    def collect_event(event):
        collected_events.append(event)
    
    emitter = ProgressEmitter(
        phase=ProgressPhase.GENERATION,
        callback=collect_event
    )
    
    steps = [
        (ProgressStep.PROMPT_CONSTRUCTION, 10, "Building prompt..."),
        (ProgressStep.LLM_API_CALL, 30, "Calling LLM..."),
        (ProgressStep.RESPONSE_RECEIVED, 40, "Response received"),
        (ProgressStep.GENERATION_COMPLETE, 100, "Done!")
    ]
    
    for step, percentage, message in steps:
        emitter.emit(step, percentage, message, force=True)
    
    assert len(collected_events) == 4
    
    for event in collected_events:
        event_dict = event.to_dict()
        assert event_dict["type"] == "progress"
        assert event_dict["phase"] == "generation"
        assert event_dict["percentage"] >= 0
        assert event_dict["percentage"] <= 100
        assert isinstance(event_dict["message"], str)
        
    print("[OK] ProgressEmitter integration test passed")


def test_endpoint_simulation():
    def mock_generate_code(session_id, progress_callback=None, user_id=None):
        ProgressEmitter(ProgressPhase.GENERATION, progress_callback)

        if progress_callback:
            event1 = ProgressEvent(
                phase=ProgressPhase.GENERATION,
                step=ProgressStep.PROMPT_CONSTRUCTION,
                percentage=10,
                message="Building prompt..."
            )
            progress_callback(event1)

            event2 = ProgressEvent(
                phase=ProgressPhase.GENERATION,
                step=ProgressStep.LLM_API_CALL,
                percentage=30,
                message="Calling LLM..."
            )
            progress_callback(event2)

            event3 = ProgressEvent(
                phase=ProgressPhase.GENERATION,
                step=ProgressStep.GENERATION_COMPLETE,
                percentage=100,
                message="Generation complete!"
            )
            progress_callback(event3)

        return {"files": {"tools": "# Test code", "agent": "", "server": ""}}

    mock_gen = AsyncMock(side_effect=mock_generate_code)

    async def simulate_endpoint():
        queue = asyncio.Queue()

        def progress_callback(event):
            queue.put_nowait(f"data: {json.dumps(event.to_dict())}\n\n")

        async def generate_task():
            result = await mock_gen(
                "test-session",
                progress_callback=progress_callback,
                user_id="test-user"
            )
            await queue.put(json.dumps({
                "type": "complete",
                "result": result
            }))

        asyncio.create_task(generate_task())

        events = []
        for _ in range(4):
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                events.append(event)
            except asyncio.TimeoutError:
                break

        return events

    events = asyncio.run(simulate_endpoint())

    assert len(events) >= 3

    for i in range(min(3, len(events))):
        if events[i].startswith('data: '):
            json_str = events[i][6:-2]
            data = json.loads(json_str)
            assert data["type"] == "progress"

    print("[OK] Endpoint simulation test passed")


def test_frontend_hook_compatibility():
    backend_event = ProgressEvent(
        phase=ProgressPhase.GENERATION,
        step=ProgressStep.PROMPT_CONSTRUCTION,
        percentage=10,
        message="Test message",
        data={"test": True},
        timestamp=time.time()
    )
    
    event_dict = backend_event.to_dict()
    
    assert "type" in event_dict
    assert event_dict["type"] == "progress"
    
    assert isinstance(event_dict["phase"], str)
    assert isinstance(event_dict["step"], str)
    
    assert isinstance(event_dict["percentage"], int)
    
    assert isinstance(event_dict["message"], str)
    
    assert isinstance(event_dict["data"], dict)
    
    assert isinstance(event_dict["timestamp"], (int, float))
    
    print("[OK] Frontend hook compatibility test passed")


def main():
    print("\n" + "="*60)
    print("Progress System Integration Tests")
    print("="*60 + "\n")
    
    tests = [
        test_progress_event_compatibility,
        test_sse_format,
        test_legacy_log_compatibility,
        test_progress_emitter_integration,
        test_frontend_hook_compatibility,
        test_endpoint_simulation,
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test_func.__name__} failed: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "="*60)
    print(f"Integration Test Results: {passed} passed, {failed} failed")
    print("="*60)
    
    if failed > 0:
        sys.exit(1)
    else:
        print("\nAll integration tests passed! Frontend-backend compatibility verified.")


if __name__ == "__main__":
    main()
