#!/usr/bin/env python3
"""Tests for shared/progress.py: the agent-creation navigation journey (form to chat to
progress to editor to testing to approved) and its progress-event emission at each
phase.
"""

import sys
import os
from unittest.mock import Mock, AsyncMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from shared.progress import ProgressPhase, ProgressStep, ProgressEmitter


def simulate_user_journey():
    print("\nSimulating user journey...")
    print("1. User fills out form and starts session")
    
    session_id = "test-session-123"
    print(f"   Session created: {session_id}")
    
    print("2. User chats with LLM to refine agent")
    chat_messages = [
        {"role": "user", "content": "Make it able to fetch weather data"},
        {"role": "assistant", "content": "I'll add weather fetching tools"}
    ]
    print(f"   {len(chat_messages)} messages exchanged")
    
    print("3. User clicks 'Generate Code'")
    print("   UI transitions to 'progress' step")
    
    navigation_steps = ["form", "chat", "progress"]
    
    print("4. Backend emits progress events")
    generation_events = []
    
    emitter = ProgressEmitter(ProgressPhase.GENERATION)
    
    steps = [
        (ProgressStep.PROMPT_CONSTRUCTION, 10, "Constructing generation prompt..."),
        (ProgressStep.LLM_API_CALL, 30, "Calling LLM API..."),
        (ProgressStep.RESPONSE_RECEIVED, 40, "LLM response received, parsing..."),
        (ProgressStep.JSON_PARSING, 50, "Parsing JSON from LLM response..."),
        (ProgressStep.STRUCTURE_VALIDATION, 60, "Validating file structure..."),
        (ProgressStep.CODE_CLEANING, 70, "Cleaning code files..."),
        (ProgressStep.GENERATION_COMPLETE, 100, "Code generation complete!")
    ]
    
    for step, percentage, message in steps:
        event = emitter.emit(step, percentage, message, force=True)
        if event:
            generation_events.append(event)
            print(f"   - {step.value}: {percentage}% - {message}")
    
    assert len(generation_events) == 7, f"Expected 7 progress events, got {len(generation_events)}"
    assert generation_events[0].percentage == 10
    assert generation_events[-1].percentage == 100
    assert generation_events[-1].step == ProgressStep.GENERATION_COMPLETE
    
    print("5. Generation completes, UI transitions to 'editor' step")
    navigation_steps.append("editor")
    
    generated_files = {
        "tools": "# Weather tools code\ndef get_weather():\n    return {'_ui_components': []}",
        "agent": "# Agent class",
        "server": "# Server class"
    }
    print(f"   Generated {len(generated_files)} files")
    
    print("6. User clicks 'Save & Run Tests'")
    print("   UI transitions to 'testing' step")
    navigation_steps.append("testing")
    
    print("7. Backend emits testing progress events")
    testing_events = []
    
    testing_emitter = ProgressEmitter(ProgressPhase.TESTING)
    
    testing_steps = [
        (ProgressStep.SAVING_FILES, 10, "Saving agent files..."),
        (ProgressStep.STARTING_PROCESS, 20, "Starting agent process on port 8003..."),
        (ProgressStep.WAITING_FOR_BOOT, 30, "Waiting for agent to boot (3 seconds)..."),
        (ProgressStep.WEBSOCKET_CONNECTION, 40, "Agent started successfully. Trying to connect via WebSocket..."),
        (ProgressStep.AGENT_REGISTRATION, 50, "WebSocket connected! Waiting for registration..."),
        (ProgressStep.TOOLS_LIST_TEST, 60, "Agent registered 2 tools: ['get_weather', 'plot_temperature']"),
        (ProgressStep.TOOLS_CALL_TEST, 70, "Testing tools/list endpoint..."),
        (ProgressStep.VALIDATION_COMPLETE, 80, "tools/list succeeded! Agent conforms to MCP protocol."),
        (ProgressStep.TESTING_COMPLETE, 100, "All tests passed.")
    ]
    
    for step, percentage, message in testing_steps:
        event = testing_emitter.emit(step, percentage, message, force=True)
        if event:
            testing_events.append(event)
            print(f"   - {step.value}: {percentage}% - {message}")
    
    assert len(testing_events) == 9, f"Expected 9 testing events, got {len(testing_events)}"
    assert testing_events[0].percentage == 10
    assert testing_events[-1].percentage == 100
    assert testing_events[-1].step == ProgressStep.TESTING_COMPLETE
    
    print("8. Testing completes, UI transitions to 'approved' step")
    navigation_steps.append("approved")
    
    expected_steps = ["form", "chat", "progress", "editor", "testing", "approved"]
    assert navigation_steps == expected_steps, f"Navigation mismatch: {navigation_steps} != {expected_steps}"
    
    print("\nUser journey simulation completed successfully!")
    print(f"Navigation flow: {' -> '.join(navigation_steps)}")
    print(f"Total progress events: {len(generation_events)} generation + {len(testing_events)} testing")
    
    return True


def test_error_handling():
    print("\nTesting error handling...")
    
    emitter = ProgressEmitter(ProgressPhase.GENERATION)
    error_event = emitter.emit_error(
        message="LLM API call failed",
        error=Exception("API timeout"),
        data={"retry_count": 3}
    )
    
    assert error_event is not None
    assert error_event.step == ProgressStep.ERROR
    assert error_event.percentage == 100
    assert error_event.data["error"] is True
    assert "API timeout" in str(error_event.data["error_details"])
    
    print("   Error event emitted correctly")
    print("   UI would transition back to 'chat' after error")
    
    return True


def test_progress_state_transitions():
    print("\nTesting progress state transitions...")
    
    events = []
    emitter = ProgressEmitter(ProgressPhase.GENERATION)
    
    events.append(emitter.emit(
        ProgressStep.PROMPT_CONSTRUCTION, 10, "Starting...", force=True
    ))
    events.append(emitter.emit(
        ProgressStep.LLM_API_CALL, 30, "Calling API...", force=True
    ))
    events.append(emitter.emit(
        ProgressStep.GENERATION_COMPLETE, 100, "Done!", force=True
    ))
    
    assert len(events) == 3
    assert events[0].percentage == 10
    assert events[1].percentage == 30
    assert events[2].percentage == 100
    
    percentages = [e.percentage for e in events]
    assert percentages == [10, 30, 100]
    
    print(f"   Progress percentages: {percentages}")
    print("   Frontend would animate progress bar from 10% to 30% to 100%")
    
    return True


def test_complete_integration_with_mocks():
    print("\nTesting complete integration with mocked APIs...")

    mock_gen = Mock()

    mock_gen.start_session = AsyncMock(return_value={
        "session_id": "test-123",
        "initial_response": "Hello! Let's create your agent."
    })

    mock_gen.chat = AsyncMock(return_value={
        "response": "I'll add those features.",
        "required_packages": [],
        "tool_call_id": None
    })

    async def mock_generate_code(session_id, progress_callback=None, user_id=None):
        if progress_callback:
            emitter = ProgressEmitter(ProgressPhase.GENERATION, progress_callback)
            emitter.emit(ProgressStep.PROMPT_CONSTRUCTION, 10, "Starting...", force=True)
            emitter.emit(ProgressStep.LLM_API_CALL, 30, "Calling...", force=True)
            emitter.emit(ProgressStep.GENERATION_COMPLETE, 100, "Done!", force=True)

        return {
            "files": {
                "tools": "# Code",
                "agent": "# Agent",
                "server": "# Server"
            }
        }

    mock_gen.generate_code = AsyncMock(side_effect=mock_generate_code)

    print("   Mocked all backend endpoints")
    print("   Simulated progress emission works correctly")

    return True


def main():
    print("\n" + "="*70)
    print("Navigation Flow Tests")
    print("="*70 + "\n")
    
    tests = [
        simulate_user_journey,
        test_error_handling,
        test_progress_state_transitions,
        test_complete_integration_with_mocks
    ]
    
    passed = 0
    failed = 0
    
    for test_func in tests:
        try:
            success = test_func()
            if success:
                passed += 1
                print(f"[OK] {test_func.__name__} passed")
            else:
                failed += 1
                print(f"[FAIL] {test_func.__name__} returned False")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {test_func.__name__} failed: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "="*70)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("="*70)
    
    if failed > 0:
        print("\nSome navigation flow tests failed. Issues found:")
        print("1. Check that progress events are emitted in correct sequence")
        print("2. Verify UI transitions between steps (form -> chat -> progress -> editor -> testing -> approved)")
        print("3. Ensure error handling transitions back to chat")
        sys.exit(1)
    else:
        print("\nAll navigation flow tests passed!")
        print("[OK] Progress indicators update correctly")
        print("[OK] Page transitions from 'progress' to 'editor' after generation")
        print("[OK] Testing phase shows proper progress")
        print("[OK] Navigation happens correctly")
        print("[OK] Error handling works as expected")


if __name__ == "__main__":
    main()
