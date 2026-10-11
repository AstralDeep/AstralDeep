# replay.py

from typing import List, Dict, Any
import hashlib

class Turn:
    def __init__(self, identity: str, action: str, result: str):
        self.identity = identity
        self.action = action
        self.result = result

class Scenario:
    def __init__(self, id: str, turns: List[Turn], expected_auth: str, effects: List[str], recovery: List[str]):
        self.id = id
        self.turns = turns
        self.expected_auth = expected_auth
        self.effects = effects
        self.recovery = recovery
        self.digest = self.calculate_digest()

    def calculate_digest(self) -> str:
        data = f"{''.join([f'{turn.identity}:{turn.action}:{turn.result}' for turn in self.turns])}"
        return hashlib.sha256(data.encode()).hexdigest()

class ReplaySystem:
    def __init__(self):
        self.scenarios = {}

    def add_scenario(self, scenario: Scenario):
        self.scenarios[scenario.id] = scenario

    def replay_scenario(self, scenario_id: str) -> Dict[str, Any]:
        scenario = self.scenarios.get(scenario_id)
        if not scenario:
            return {"error": "Scenario not found"}
        
        result = {"scenario_id": scenario_id, "digest": scenario.digest, "turns": []}
        for turn in scenario.turns:
            result["turns"].append({
                "identity": turn.identity,
                "action": turn.action,
                "result": turn.result
            })
        
        return result

# Example usage
if __name__ == "__main__":
    replay_system = ReplaySystem()

    # Example scenario
    turns = [
        Turn("user", "request_access", "denied"),
        Turn("user", "request_reauthorization", "granted"),
        Turn("user", "perform_action", "completed")
    ]
    scenario = Scenario("sc1", turns, "admin", ["access_request"], ["reauthorization_request"])
    replay_system.add_scenario(scenario)

    result = replay_system.replay_scenario("sc1")
    print(result)