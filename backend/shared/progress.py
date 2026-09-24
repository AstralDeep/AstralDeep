#!/usr/bin/env python3
"""Structured progress events (phase, step, percentage) for agent creation and testing,
built by ProgressEmitter and serialized to dicts or SSE frames for streaming to the
client.
"""

import json
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Callable
from enum import Enum

logger = logging.getLogger("ProgressSystem")


class ProgressPhase(str, Enum):
    GENERATION = "generation"
    TESTING = "testing"
    INSTALLATION = "installation"


class ProgressStep(str, Enum):
    PROMPT_CONSTRUCTION = "prompt_construction"
    LLM_API_CALL = "llm_api_call"
    RESPONSE_RECEIVED = "response_received"
    JSON_PARSING = "json_parsing"
    STRUCTURE_VALIDATION = "structure_validation"
    CODE_CLEANING = "code_cleaning"
    GENERATION_COMPLETE = "generation_complete"
    
    SAVING_FILES = "saving_files"
    STARTING_PROCESS = "starting_process"
    WAITING_FOR_BOOT = "waiting_for_boot"
    WEBSOCKET_CONNECTION = "websocket_connection"
    AGENT_REGISTRATION = "agent_registration"
    TOOLS_LIST_TEST = "tools_list_test"
    TOOLS_CALL_TEST = "tools_call_test"
    VALIDATION_COMPLETE = "validation_complete"
    INTEGRATION_READY = "integration_ready"
    TESTING_COMPLETE = "testing_complete"
    
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ProgressEvent:
    phase: ProgressPhase
    step: ProgressStep
    percentage: int
    message: str
    data: Optional[Dict[str, Any]] = None
    timestamp: float = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()
        
        if not 0 <= self.percentage <= 100:
            logger.warning(f"Progress percentage out of range: {self.percentage}")
            self.percentage = max(0, min(100, self.percentage))
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "progress",
            "phase": self.phase.value,
            "step": self.step.value,
            "percentage": self.percentage,
            "message": self.message,
            "data": self.data or {},
            "timestamp": self.timestamp
        }
    
    def to_sse(self) -> str:
        return f"data: {json.dumps(self.to_dict())}\n\n"
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProgressEvent":
        return cls(
            phase=ProgressPhase(data["phase"]),
            step=ProgressStep(data["step"]),
            percentage=data["percentage"],
            message=data["message"],
            data=data.get("data"),
            timestamp=data.get("timestamp", time.time())
        )


class ProgressEmitter:
    def __init__(self, 
                 phase: ProgressPhase,
                 callback: Optional[Callable[[ProgressEvent], None]] = None):
        self.phase = phase
        self.callback = callback
        self.current_step: Optional[ProgressStep] = None
        self.start_time = time.time()
        self.last_emit_time = 0.0
        self.emit_count = 0
    
    def emit(self, 
             step: ProgressStep,
             percentage: int,
             message: str,
             data: Optional[Dict[str, Any]] = None,
             force: bool = False) -> ProgressEvent:
        current_time = time.time()
        if not force and current_time - self.last_emit_time < 0.1:
            return None
        
        event = ProgressEvent(
            phase=self.phase,
            step=step,
            percentage=percentage,
            message=message,
            data=data
        )
        
        self.current_step = step
        self.last_emit_time = current_time
        self.emit_count += 1
        
        logger.debug(f"Progress: {self.phase.value}.{step.value} ({percentage}%): {message}")
        
        if self.callback:
            try:
                self.callback(event)
            except Exception as e:
                logger.error(f"Progress callback failed: {e}")
        
        return event
    
    def emit_sse(self, 
                 step: ProgressStep,
                 percentage: int,
                 message: str,
                 data: Optional[Dict[str, Any]] = None) -> str:
        event = self.emit(step, percentage, message, data)
        if event:
            return event.to_sse()
        return ""
    
    def emit_error(self, 
                   message: str,
                   error: Optional[Exception] = None,
                   data: Optional[Dict[str, Any]] = None) -> ProgressEvent:
        error_data = {
            "error": True,
            "error_message": message,
            "error_type": error.__class__.__name__ if error else "Unknown",
            "error_details": str(error) if error else None
        }
        if data:
            error_data.update(data)
        
        return self.emit(
            step=ProgressStep.ERROR,
            percentage=100,
            message=message,
            data=error_data,
            force=True
        )
    
    def emit_warning(self, 
                     message: str,
                     data: Optional[Dict[str, Any]] = None) -> ProgressEvent:
        warning_data = {"warning": True, "warning_message": message}
        if data:
            warning_data.update(data)
        
        return self.emit(
            step=ProgressStep.WARNING,
            percentage=self._get_current_percentage(),
            message=message,
            data=warning_data
        )
    
    def get_elapsed_time(self) -> float:
        return time.time() - self.start_time
    
    def _get_current_percentage(self) -> int:
        phase_steps = {
            ProgressPhase.GENERATION: [
                (ProgressStep.PROMPT_CONSTRUCTION, 10),
                (ProgressStep.LLM_API_CALL, 30),
                (ProgressStep.RESPONSE_RECEIVED, 40),
                (ProgressStep.JSON_PARSING, 50),
                (ProgressStep.STRUCTURE_VALIDATION, 60),
                (ProgressStep.CODE_CLEANING, 70),
                (ProgressStep.GENERATION_COMPLETE, 100)
            ],
            ProgressPhase.TESTING: [
                (ProgressStep.SAVING_FILES, 10),
                (ProgressStep.STARTING_PROCESS, 20),
                (ProgressStep.WAITING_FOR_BOOT, 30),
                (ProgressStep.WEBSOCKET_CONNECTION, 40),
                (ProgressStep.AGENT_REGISTRATION, 50),
                (ProgressStep.TOOLS_LIST_TEST, 60),
                (ProgressStep.TOOLS_CALL_TEST, 70),
                (ProgressStep.VALIDATION_COMPLETE, 80),
                (ProgressStep.INTEGRATION_READY, 90),
                (ProgressStep.TESTING_COMPLETE, 100)
            ]
        }
        
        steps = phase_steps.get(self.phase, [])
        for step_def, percentage in steps:
            if self.current_step == step_def:
                return percentage
        
        return 0


def create_log_event(message: str, status: str = "log") -> str:
    event = {
        "status": status,
        "message": message,
        "timestamp": time.time()
    }
    return f"data: {json.dumps(event)}\n\n"


def create_progress_from_log(message: str, 
                             phase: ProgressPhase,
                             step: ProgressStep) -> ProgressEvent:
    return ProgressEvent(
        phase=phase,
        step=step,
        percentage=0,
        message=message
    )
