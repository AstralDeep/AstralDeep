import unittest
import time
from backend.orchestrator.orchestrator import Orchestrator, Feature093Storage

class TestFeature093(unittest.TestCase):
    def setUp(self):
        self.owner_id = "user_123"
        self.conversation_id = "conv_456"
        self.tool_name = "calculator"
        self.tool_input = {"expression": "2 + 2"}

    def test_feature_disabled_by_default(self):
        # Packing remains independently default off
        orchestrator = Orchestrator()
        self.assertFalse(orchestrator.feature_093_enabled)
        
        result = orchestrator.execute_tool(self.tool_name, self.tool_input, self.owner_id, self.conversation_id)
        self.assertFalse(result["packed"])
        self.assertIn("Result of calculator", result["content"])
        
        # Recall should fail when feature is disabled
        with self.assertRaises(RuntimeError):
            orchestrator.recall_observation("some_id", self.owner_id, self.conversation_id)

    def test_deterministic_success(self):
        orchestrator = Orchestrator(feature_093_enabled=True)
        result = orchestrator.execute_tool(self.tool_name, self.tool_input, self.owner_id, self.conversation_id)
        
        self.assertTrue(result["packed"])
        self.assertIn("observation_id", result)
        observation_id = result["observation_id"]
        self.assertIn(observation_id, result["content"])
        
        # Recall exact observation
        recalled_content = orchestrator.recall_observation(observation_id, self.owner_id, self.conversation_id)
        self.assertEqual(recalled_content, f"Result of calculator with input {{\"expression\": \"2 + 2\"}}")

    def test_denial_provenance_mismatch(self):
        orchestrator = Orchestrator(feature_093_enabled=True)
        result = orchestrator.execute_tool(self.tool_name, self.tool_input, self.owner_id, self.conversation_id)
        observation_id = result["observation_id"]
        
        # Wrong owner
        with self.assertRaises(PermissionError):
            orchestrator.recall_observation(observation_id, "wrong_owner", self.conversation_id)
            
        # Wrong conversation
        with self.assertRaises(PermissionError):
            orchestrator.recall_observation(observation_id, self.owner_id, "wrong_conv")

    def test_expired_sources(self):
        orchestrator = Orchestrator(feature_093_enabled=True)
        # Store with very short TTL
        observation_id = orchestrator.storage.store(self.owner_id, self.conversation_id, "expired data", ttl=0.01)
        
        # Wait for expiration
        time.sleep(0.02)
        
        with self.assertRaises(ValueError):
            orchestrator.recall_observation(observation_id, self.owner_id, self.conversation_id)

    def test_pagination_and_cleanup(self):
        orchestrator = Orchestrator(feature_093_enabled=True)
        
        # Store multiple observations
        obs_ids = []
        for i in range(5):
            obs_id = orchestrator.storage.store(self.owner_id, self.conversation_id, f"data {i}")
            obs_ids.append(obs_id)
            
        # Test pagination
        page1 = orchestrator.storage.list_observations(self.owner_id, self.conversation_id, limit=3, offset=0)
        self.assertEqual(len(page1), 3)
        
        page2 = orchestrator.storage.list_observations(self.owner_id, self.conversation_id, limit=3, offset=3)
        self.assertEqual(len(page2), 2)

    def test_failure_safe_disabled_behavior(self):
        orchestrator = Orchestrator(feature_093_enabled=True)
        
        # Force store to fail by passing invalid inputs
        result = orchestrator.execute_tool(self.tool_name, self.tool_input, "", "")
        
        # Should fallback to unpacked behavior safely
        self.assertFalse(result["packed"])
        self.assertIn("Result of calculator", result["content"])
        self.assertIn("error", result)

if __name__ == "__main__":
    unittest.main()
