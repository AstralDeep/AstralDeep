"""Checks claim-response authority, immutable code, and retry serialization."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "claim-reply.yml"
ACTION = "AstralDeep/astraldeep.github.io/actions/claim-reply@d7ab78f98bc6a508c7b4717fbb0ea0b61abcb3ff"


def assert_contract(text):
    assert re.search(r"(?m)^permissions: \{\}$", text)
    guard = (
        "    if: (github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main')"
        " && (github.ref == 'refs/heads/main')"
    )
    assert re.search(r"(?m)^" + re.escape(guard) + r"$", text)
    assert re.findall(r"(?m)^  ([\w-]+):$", text.split("jobs:\n", 1)[1]) == ["reply"]
    assert re.findall(r"(?m)^    timeout-minutes: (\d+)$", text) == ["5"]
    assert re.findall(r"(?m)^      (\S+): (read|write)$", text) == [("issues", "write")]
    assert re.findall(r"(?m)^      - uses: (\S+)$", text) == [ACTION]
    assert not re.search(r"(?m)^\s*(?:- )?(?:run|secrets|env|id-token|contents):", text)
    assert re.search(
        r"(?m)^  group: bounty-claim-guidance-\$\{\{ "
        r"github.event.comment.id \|\| inputs.comment_id \}\}$",
        text,
    )
    assert re.search(r"(?m)^  cancel-in-progress: false$", text)
    triggers = text.split("on:\n", 1)[1].split("permissions:", 1)[0]
    assert re.findall(r"(?m)^  ([\w-]+):$", triggers) == [
        "issue_comment",
        "workflow_dispatch",
    ]
    assert "    types: [created, edited]\n" in triggers
    assert "        type: boolean\n        default: true\n" in triggers
    assert "          comment-id: ${{ inputs.comment_id || '' }}\n" in text
    assert (
        "          dry-run: ${{ github.event_name == 'workflow_dispatch'"
        " && inputs.dry_run || false }}\n"
    ) in text


class ClaimReplyWorkflowTests(unittest.TestCase):
    def test_reviewed_workflow_has_only_comment_authority(self):
        assert_contract(WORKFLOW.read_text(encoding="utf-8"))

    def test_privilege_and_trigger_mutations_are_rejected(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        mutations = [
            ("issues: write", "contents: write"),
            (
                ACTION,
                ACTION.replace("d7ab78f98bc6a508c7b4717fbb0ea0b61abcb3ff", "main"),
            ),
            ("refs/heads/main", "refs/heads/candidate"),
            ("timeout-minutes: 5", "timeout-minutes: 31"),
            ("permissions: {}", "permissions: write-all"),
            ("cancel-in-progress: false", "cancel-in-progress: true"),
            (
                "github.event.comment.id || inputs.comment_id",
                "github.event.issue.number",
            ),
            ("types: [created, edited]", "types: [created, edited, deleted]"),
            ("        default: true", "        default: false"),
            ("    steps:", "    env:\n      SECRET: value\n    steps:"),
            ("    steps:", "    steps:\n      - run: echo unsafe"),
            ("  issue_comment:", "  pull_request_target:"),
        ]
        for old, new in mutations:
            with self.subTest(change=old):
                self.assertNotEqual(text, text.replace(old, new))
                with self.assertRaises(AssertionError):
                    assert_contract(text.replace(old, new))


if __name__ == "__main__":
    unittest.main()
