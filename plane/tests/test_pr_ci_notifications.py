"""Checks metadata-only PR notifications, immutable code and serialized recovery."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-ci-notifications.yml"
ACTION = "AstralDeep/astraldeep.github.io/actions/pr-ci@460a3ed56c3c1512e489607417d071f6aa174e82"
NAMES = ["AstralPlane CI"]

def assert_contract(text):
    assert re.search(r"(?m)^permissions: \{\}$", text)
    assert re.findall(r"(?m)^  ([\w-]+):$", text.split("jobs:\n", 1)[1]) == ["notify"]
    assert re.findall(r"(?m)^    if: (.*)$", text) == ["github.ref == 'refs/heads/main'"]
    assert re.findall(r"(?m)^    timeout-minutes: (\d+)$", text) == ["5"]
    assert re.findall(r"(?m)^      (\S+): (read|write)$", text) == [
        ("actions", "read"), ("issues", "write"), ("pull-requests", "write")
    ]
    assert re.findall(r"(?m)^      - uses: (\S+)$", text) == [ACTION]
    assert not re.search(r"(?m)^\s*(?:- )?(?:run|secrets|env|id-token|contents|environment):", text)
    assert "  group: pr-ci-notifications\n" in text
    assert "  cancel-in-progress: false\n" in text
    triggers = text.split("on:\n", 1)[1].split("permissions:", 1)[0]
    assert re.findall(r"(?m)^  ([\w-]+):$", triggers) == [
        "workflow_run", "schedule", "workflow_dispatch"
    ]
    assert "    workflows: [" + ", ".join("'" + name + "'" for name in NAMES) + "]\n" in triggers
    assert "    types: [completed]\n" in triggers
    assert "    - cron: '11-59/15 * * * *'\n" in triggers
    assert "        type: boolean\n        default: true\n" in triggers
    assert (
        "          dry-run: ${{ github.event_name == 'workflow_dispatch'"
        " && inputs.dry_run || false }}\n"
    ) in text

class PrCiNotificationsTests(unittest.TestCase):
    def test_reviewed_controller_has_only_notification_authority(self):
        assert_contract(WORKFLOW.read_text(encoding="utf-8"))

    def test_privilege_and_execution_mutations_are_rejected(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        mutations = [
            ("actions: read", "actions: write"),
            ("issues: write", "contents: write"),
            ("pull-requests: write", "id-token: write"),
            (ACTION, ACTION.rsplit("@", 1)[0] + "@main"),
            ("refs/heads/main", "refs/heads/candidate"),
            ("timeout-minutes: 5", "timeout-minutes: 31"),
            ("permissions: {}", "permissions: write-all"),
            ("cancel-in-progress: false", "cancel-in-progress: true"),
            ("group: pr-ci-notifications", "group: pr-ci-${{ github.run_id }}"),
            ("types: [completed]", "types: [requested, completed]"),
            ("        default: true", "        default: false"),
            ("    steps:", "    env:\n      TOKEN: value\n    steps:"),
            ("    steps:", "    steps:\n      - run: echo unsafe"),
            ("  workflow_run:", "  pull_request_target:"),
            ("11-59/15 * * * *", "0 * * * *"),
        ]
        for old, new in mutations:
            with self.subTest(change=old):
                self.assertNotEqual(text, text.replace(old, new))
                with self.assertRaises(AssertionError):
                    assert_contract(text.replace(old, new))

if __name__ == "__main__":
    unittest.main()
