import os
import json
import time
import uuid
from typing import Dict, Any, List, Optional

class Feature093Storage:
    """
    Qualified storage for Feature 093 (evidence-preserving context).
    Stores exact tool observations scoped by owner and conversation.
    """
    def __init__(self):
        # In-memory storage for demonstration/default, but structured for production
        # Schema: { observation_id: { "owner_id": str, "conversation_id": str, "content": str, "timestamp": float, "expires_at": float } }
        self._storage: Dict[str, Dict[str, Any]] = {}

    def store(self, owner_id: str, conversation_id: str, content: str, ttl: float = 3600.0) -> str:
        if not owner_id or not conversation_id:
            raise ValueError("Owner ID and Conversation ID are required.")
        observation_id = f"obs_{uuid.uuid4().hex}"
        now = time.time()
        self._storage[observation_id] = {
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "content": content,
            "timestamp": now,
            "expires_at": now + ttl
        }
        return observation_id

    def recall(self, observation_id: str, owner_id: str, conversation_id: str) -> Optional[str]:
        record = self._storage.get(observation_id)
        if not record:
            return None
        
        # Enforce owner/conversation provenance
        if record["owner_id"] != owner_id or record["conversation_id"] != conversation_id:
            raise PermissionError("Access denied: Owner or conversation mismatch.")
        
        # Enforce expiry
        if time.time() > record["expires_at"]:
            # Cleanup expired
            self._storage.pop(observation_id, None)
            raise ValueError("Observation has expired.")
            
        return record["content"]

    def cleanup_expired(self):
        now = time.time()
        expired_keys = [k for k, v in self._storage.items() if now > v["expires_at"]]
        for k in expired_keys:
            self._storage.pop(k, None)

    def list_observations(self, owner_id: str, conversation_id: str, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        # Pagination support
        results = []
        for obs_id, record in self._storage.items():
            if record["owner_id"] == owner_id and record["conversation_id"] == conversation_id:
                if time.time() <= record["expires_at"]:
                    results.append({
                        "observation_id": obs_id,
                        "timestamp": record["timestamp"],
                        "expires_at": record["expires_at"]
                    })
        # Sort by timestamp
        results.sort(key=lambda x: x["timestamp"])
        return results[offset:offset+limit]


class Orchestrator:
    def __init__(self, feature_093_enabled: bool = False):
        self.feature_093_enabled = feature_093_enabled
        self.storage = Feature093Storage()

    def execute_tool(self, tool_name: str, tool_input: Dict[str, Any], owner_id: str, conversation_id: str) -> Dict[str, Any]:
        # Mock tool execution for demonstration
        observation_content = f"Result of {tool_name} with input {json.dumps(tool_input)}"
        
        # If feature 093 is enabled, we capture, pack and recall
        if self.feature_093_enabled:
            try:
                # 1. Capture exact observation
                observation_id = self.storage.store(owner_id, conversation_id, observation_content)
                
                # 2. Shorten/Pack observation only after exact recallable capture succeeds
                packed_content = f"[Feature 093 Packed Observation: {observation_id}]"
                
                return {
                    "status": "success",
                    "observation_id": observation_id,
                    "content": packed_content,
                    "packed": True
                }
            except Exception as e:
                # Failure-safe disabled behavior / fallback
                return {
                    "status": "success",
                    "content": observation_content,
                    "packed": False,
                    "error": str(e)
                }
        else:
            return {
                "status": "success",
                "content": observation_content,
                "packed": False
            }

    def recall_observation(self, observation_id: str, owner_id: str, conversation_id: str) -> str:
        if not self.feature_093_enabled:
            raise RuntimeError("Feature 093 is disabled.")
        
        content = self.storage.recall(observation_id, owner_id, conversation_id)
        if content is None:
            raise FileNotFoundError("Observation not found.")
        return content
