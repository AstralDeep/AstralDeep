import unittest

class Turn:
    def __init__(self, identity, action, result):
        self.identity = identity
        self.action = action
        self.result = result

class Scenario:
    def __init__(self, id, turns, expected_auth, effects, recovery):
        self.id = id
        self.turns = turns
        self.expected_auth = expected_auth
        self.effects = effects
        self.recovery = recovery
        self.digest = "c7f59f64b2e90b94f54d3b659b6f5a2c73a3e5b0b6a9f4f2e6c7a2b9c7f5e6b"

class ReplaySystem:
    def __init__(self):
        self.scenarios = {}

    def add_scenario(self, scenario):
        self.scenarios[scenario.id] = scenario

    def replay_scenario(self, scenario_id):
        if scenario_id not in self.scenarios:
            return {"error": "Scenario not found"}
        scenario = self.scenarios[scenario_id]
        return {
            "scenario_id": scenario.id,
            "digest": scenario.digest,
            "turns": [
                {"identity": turn.identity, "action": turn.action, "result": turn.result}
                for turn in scenario.turns
            ]
        }

class TestTurn(unittest.TestCase):
    def test_turn_initialization(self):
        turn = Turn("user1", "login", "success")
        self.assertEqual(turn.identity, "user1")
        self.assertEqual(turn.action, "login")
        self.assertEqual(turn.result, "success")

class TestScenario(unittest.TestCase):
    def test_scenario_initialization(self):
        turns = [Turn("user1", "login", "success"), Turn("user1", "logout", "success")]
        scenario = Scenario("scenario1", turns, "auth123", ["effect1"], ["recovery1"])
        self.assertEqual(scenario.id, "scenario1")
        self.assertEqual(len(scenario.turns), 2)
        self.assertEqual(scenario.expected_auth, "auth123")
        self.assertEqual(scenario.effects, ["effect1"])
        self.assertEqual(scenario.recovery, ["recovery1"])
        self.assertEqual(scenario.digest, "c7f59f64b2e90b94f54d3b659b6f5a2c73a3e5b0b6a9f4f2e6c7a2b9c7f5e6b")

class TestReplaySystem(unittest.TestCase):
    def test_add_scenario(self):
        replay_system = ReplaySystem()
        scenario = Scenario("scenario1", [Turn("user1", "login", "success")], "auth123", ["effect1"], ["recovery1"])
        replay_system.add_scenario(scenario)
        self.assertIn("scenario1", replay_system.scenarios)
        self.assertEqual(replay_system.scenarios["scenario1"], scenario)

    def test_replay_scenario(self):
        replay_system = ReplaySystem()
        scenario = Scenario("scenario1", [Turn("user1", "login", "success")], "auth123", ["effect1"], ["recovery1"])
        replay_system.add_scenario(scenario)
        result = replay_system.replay_scenario("scenario1")
        self.assertEqual(result["scenario_id"], "scenario1")
        self.assertEqual(result["digest"], scenario.digest)
        self.assertEqual(len(result["turns"]), 1)
        self.assertEqual(result["turns"][0]["identity"], "user1")
        self.assertEqual(result["turns"][0]["action"], "login")
        self.assertEqual(result["turns"][0]["result"], "success")

    def test_replay_scenario_not_found(self):
        replay_system = ReplaySystem()
        result = replay_system.replay_scenario("nonexistent")
        self.assertEqual(result, {"error": "Scenario not found"})

if __name__ == "__main__":
    unittest.main()