"""Checks metadata triage and rejects candidate execution and expanded authority."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-triage.yml"
ACTION = (
    "AstralDeep/astraldeep.github.io/actions/pr-triage@9c83bac26fe9c5d594388afb70b30ed9076c90f1"
)


def assert_contract(text):
    assert re.search(r"(?m)^permissions: \{\}$", text)
    assert re.findall(r"(?m)^  ([\w-]+):$", text.split("jobs:\n", 1)[1]) == ["triage"]
    assert re.findall(r"(?m)^    if: (.*)$", text) == [
        "(github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main')"
        " && (github.ref == 'refs/heads/main' && (github.event_name != 'issue_comment'"
        " || (github.event.issue.pull_request && contains(github.event.comment.body,"
        " '/astral-triage close '))))"
    ]
    assert re.findall(r"(?m)^    timeout-minutes: (\d+)$", text) == ["5"]
    assert re.findall(r"(?m)^      (\S+): (read|write)$", text) == [
        ("issues", "write"),
        ("pull-requests", "write"),
    ]
    assert re.findall(r"(?m)^      - uses: (\S+)$", text) == [ACTION]
    assert not re.search(r"(?m)^\s*(?:- )?(?:run|secrets|env|id-token|contents|environment):", text)
    assert not re.search(r"(?m)^concurrency:", text)
    assert re.findall(r"(?m)^    concurrency:$", text) == ["    concurrency:"]
    assert re.findall(r"(?m)^      group: (.*)$", text) == ["pr-triage"]
    assert re.findall(r"(?m)^      cancel-in-progress: (.*)$", text) == ["false"]
    triggers = text.split("on:\n", 1)[1].split("permissions:", 1)[0]
    assert re.findall(r"(?m)^  ([\w-]+):$", triggers) == [
        "issue_comment",
        "schedule",
        "workflow_dispatch",
    ]
    assert "    types: [created]\n" in triggers
    assert "    - cron: '7-59/15 * * * *'\n" in triggers
    assert "        type: boolean\n        default: true\n" in triggers
    assert (
        "          dry-run: ${{ github.event_name == 'workflow_dispatch'"
        " && inputs.dry_run || false }}\n"
    ) in text


class PrTriageTests(unittest.TestCase):
    def test_reviewed_controller_has_only_triage_metadata_authority(self):
        assert_contract(WORKFLOW.read_text(encoding="utf-8"))

    def test_privilege_and_execution_mutations_are_rejected(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        mutations = [
            ("issues: write", "contents: write"),
            ("pull-requests: write", "id-token: write"),
            (ACTION, ACTION.rsplit("@", 1)[0] + "@main"),
            ("refs/heads/main", "refs/heads/candidate"),
            ("timeout-minutes: 5", "timeout-minutes: 31"),
            ("permissions: {}", "permissions: write-all"),
            ("cancel-in-progress: false", "cancel-in-progress: true"),
            ("group: pr-triage", "group: pr-triage-${{ github.run_id }}"),
            ("github.event.issue.pull_request", "true"),
            ("contains(github.event.comment.body, '/astral-triage close ')", "true"),
            (
                "    concurrency:\n      group: pr-triage\n      cancel-in-progress: false\n",
                "",
            ),
            (
                "jobs:\n",
                "concurrency:\n  group: pr-triage\n  cancel-in-progress: false\njobs:\n",
            ),
            ("types: [created]", "types: [created, edited]"),
            ("        default: true", "        default: false"),
            ("    steps:", "    env:\n      TOKEN: value\n    steps:"),
            ("    steps:", "    steps:\n      - run: echo unsafe"),
            ("  issue_comment:", "  pull_request_target:"),
            ("7-59/15 * * * *", "0 * * * *"),
        ]
        for old, new in mutations:
            with self.subTest(change=old):
                self.assertNotEqual(text, text.replace(old, new))
                with self.assertRaises(AssertionError):
                    assert_contract(text.replace(old, new))


if __name__ == "__main__":
    unittest.main()
