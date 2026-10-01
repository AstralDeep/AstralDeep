"""Checks source-claim authority, immutable code, and serialized recovery."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "claim-reply.yml"
ACTION = "AstralDeep/astraldeep.github.io/actions/claim-reply@3d2d31aa243fdfde35be30ce46de0065430ea4cf"


def assert_contract(text):
    assert re.search(r"(?m)^permissions: \{\}$", text)
    guard = (
        "    if: (github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main')"
        " && (github.ref == 'refs/heads/main'"
        " && (github.event_name != 'issue_comment' || github.event.comment.user.type == 'User'))"
    )
    assert re.search(r"(?m)^" + re.escape(guard) + r"$", text)
    assert re.findall(r"(?m)^  ([\w-]+):$", text.split("jobs:\n", 1)[1]) == ["reply"]
    assert re.findall(r"(?m)^    timeout-minutes: (\d+)$", text) == ["5"]
    assert re.findall(r"(?m)^      (\S+): (read|write)$", text) == [("issues", "write")]
    assert re.findall(r"(?m)^      - uses: (\S+)$", text) == [ACTION]
    assert not re.search(r"(?m)^\s*(?:- )?(?:run|secrets|env|id-token|contents):", text)
    assert re.search(r"(?m)^  group: bounty-claims$", text)
    assert re.search(r"(?m)^  cancel-in-progress: false$", text)
    triggers = text.split("on:\n", 1)[1].split("permissions:", 1)[0]
    assert re.findall(r"(?m)^  ([\w-]+):$", triggers) == [
        "issue_comment",
        "schedule",
        "workflow_dispatch",
    ]
    assert "    types: [created, edited]\n" in triggers
    assert "    - cron: '4-59/5 * * * *'\n" in triggers
    assert "        required: false\n        type: string\n" in triggers
    assert "        type: boolean\n        default: true\n" in triggers
    assert "          comment-id: ${{ inputs.comment_id || '' }}\n" in text
    assert (
        "          dry-run: ${{ github.event_name == 'workflow_dispatch'"
        " && inputs.dry_run || false }}\n"
    ) in text


class ClaimReplyWorkflowTests(unittest.TestCase):
    def test_reviewed_workflow_has_only_issue_authority(self):
        assert_contract(WORKFLOW.read_text(encoding="utf-8"))

    def test_privilege_and_trigger_mutations_are_rejected(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        mutations = [
            ("issues: write", "contents: write"),
            (
                ACTION,
                ACTION.replace("3d2d31aa243fdfde35be30ce46de0065430ea4cf", "main"),
            ),
            ("refs/heads/main", "refs/heads/candidate"),
            ("timeout-minutes: 5", "timeout-minutes: 31"),
            ("permissions: {}", "permissions: write-all"),
            ("cancel-in-progress: false", "cancel-in-progress: true"),
            ("group: bounty-claims", "group: bounty-claims-${{ github.event.comment.id }}"),
            ("types: [created, edited]", "types: [created, edited, deleted]"),
            ("        default: true", "        default: false"),
            ("    steps:", "    env:\n      SECRET: value\n    steps:"),
            ("    steps:", "    steps:\n      - run: echo unsafe"),
            ("  issue_comment:", "  pull_request_target:"),
            ("4-59/5 * * * *", "0 * * * *"),
            ("user.type == 'User'", "user.type == 'Bot'"),
            ("required: false", "required: true"),
        ]
        for old, new in mutations:
            with self.subTest(change=old):
                self.assertNotEqual(text, text.replace(old, new))
                with self.assertRaises(AssertionError):
                    assert_contract(text.replace(old, new))


if __name__ == "__main__":
    unittest.main()
