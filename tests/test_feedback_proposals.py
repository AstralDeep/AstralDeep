# Copyright AstralDeep Authors
# SPDX-License-Identifier: Apache-2.0
"""Tests for knowledge proposal generation, including NUL sanitization."""

from __future__ import annotations

import pytest

from astraldeepsdk.feedback import proposals, repository


class FakeArtifact:
    def __init__(
        self,
        key: str,
        text: str,
        proposal_text: str,
        refined_text: str | None = None,
        hash_value: str = "abc123",
    ):
        self.key = key
        self.text = text
        self.proposal_text = proposal_text
        self.refined_text = refined_text
        self.hash = hash_value


class FakeToolInfo:
    def __init__(self, tool_id: str, artifacts: list[FakeArtifact]):
        self.tool_id = tool_id
        self.artifacts = artifacts


class FakeRepositoryClient:
    def __init__(self):
        self.proposals: list[dict[str, str]] = []
        self._underperforming: list[FakeToolInfo] = []

    def list_underperforming_tools(self, *, limit: int | None = None) -> list[FakeToolInfo]:
        return self._underperforming

    def insert_proposal(
        self,
        *,
        tool_id: str,
        artifact_key: str,
        artifact_hash: str,
        diff_payload: str,
        reason: str,
    ) -> None:
        # Simulate PostgreSQL rejecting NUL characters if we receive any.
        if "\x00" in diff_payload:
            raise ValueError("invalid byte sequence for encoding \"UTF8\": \x00")
        self.proposals.append(
            {
                "tool_id": tool_id,
                "artifact_key": artifact_key,
                "artifact_hash": artifact_hash,
                "diff_payload": diff_payload,
                "reason": reason,
            }
        )


class TestStripNulls:
    def test_strips_nul_characters(self) -> None:
        assert proposals._strip_nulls("hello\x00world") == "helloworld"

    def test_leaves_valid_text_unchanged(self) -> None:
        text = "no nulls here\njust normal text"
        assert proposals._strip_nulls(text) == text

    def test_strips_multiple_nuls(self) -> None:
        assert proposals._strip_nulls("a\x00b\x00c") == "abc"

    def test_handles_empty_string(self) -> None:
        assert proposals._strip_nulls("") == ""

    def test_handles_only_nuls(self) -> None:
        assert proposals._strip_nulls("\x00\x00") == ""


class TestGenerateProposal:
    def test_sanitizes_feedback_excerpts(self) -> None:
        """Excerpts from feedback comments may contain NUL characters."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="web-research-1",
                artifacts=[
                    FakeArtifact(
                        key="fetch_page.md",
                        text="existing content",
                        proposal_text="feedback says:\x00hello world",
                        refined_text=None,
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert len(client.proposals) == 1
        assert "\x00" not in client.proposals[0]["diff_payload"]

    def test_sanitizes_existing_artifact(self) -> None:
        """The existing artifact read from disk may contain NULs."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="web-research-1",
                artifacts=[
                    FakeArtifact(
                        key="fetch_page.md",
                        text="existing\x00content",
                        proposal_text="proposed content",
                        refined_text=None,
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert len(client.proposals) == 1
        assert "\x00" not in client.proposals[0]["diff_payload"]

    def test_sanitizes_refined_llm_text(self) -> None:
        """Optionally refined LLM text may contain NULs."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="web-research-1",
                artifacts=[
                    FakeArtifact(
                        key="fetch_page.md",
                        text="existing content",
                        proposal_text="proposed content",
                        refined_text="refined\x00with nulls",
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert len(client.proposals) == 1
        assert "\x00" not in client.proposals[0]["diff_payload"]

    def test_valid_text_stored_unchanged(self) -> None:
        """Text that is already valid must be stored unchanged."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="web-research-1",
                artifacts=[
                    FakeArtifact(
                        key="fetch_page.md",
                        text="existing line\nsecond line\n",
                        proposal_text="proposed line\nsecond line\n",
                        refined_text=None,
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        proposal = client.proposals[0]
        assert proposal["tool_id"] == "web-research-1"
        assert proposal["artifact_key"] == "fetch_page.md"
        assert "\x00" not in proposal["diff_payload"]
        # The diff should reference the original texts (minus any sanitization).
        assert "existing line" in proposal["diff_payload"]
        assert "proposed line" in proposal["diff_payload"]

    def test_insert_failure_logged_but_not_crashing(self) -> None:
        """If insert still fails, the exception is logged, not re-raised."""
        class FailingClient(FakeRepositoryClient):
            def insert_proposal(self, **kwargs: object) -> None:
                raise RuntimeError("db down")

        client = FailingClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="tool-a",
                artifacts=[
                    FakeArtifact(
                        key="file.md",
                        text="existing",
                        proposal_text="proposed",
                    )
                ],
            )
        ]
        # Should not raise.
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert len(client.proposals) == 0


class TestEdgeCases:
    def test_all_nul_input_produces_empty_diff(self) -> None:
        """When all inputs are NUL-only, diff is empty but proposal is stored."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="tool-a",
                artifacts=[
                    FakeArtifact(
                        key="file.md",
                        text="\x00\x00",
                        proposal_text="\x00\x00",
                        refined_text=None,
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert len(client.proposals) == 1
        # After stripping, both texts are empty → diff may be empty.
        assert client.proposals[0]["diff_payload"] == ""

    def test_nul_at_boundary_preserves_surrounding_text(self) -> None:
        """NUL at start/end/middle is stripped while surrounding text is kept."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="tool-a",
                artifacts=[
                    FakeArtifact(
                        key="file.md",
                        text="first\x00middle\x00last",
                        proposal_text="start\x00end",
                    )
                ],
            )
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 1
        assert "\x00" not in client.proposals[0]["diff_payload"]
        assert "firstmiddlelast" in client.proposals[0]["diff_payload"]
        assert "startend" in client.proposals[0]["diff_payload"]

    def test_multiple_tools_independent_failures(self) -> None:
        """One tool's failure must not prevent others from being processed."""
        class PickFailsClient(FakeRepositoryClient):
            call_count = 0

            def insert_proposal(self, **kwargs: object) -> None:
                PickFailsClient.call_count += 1
                if PickFailsClient.call_count == 1:
                    raise RuntimeError("first tool fails")
                super().insert_proposal(**kwargs)

        client = PickFailsClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="tool-a",
                artifacts=[FakeArtifact("f.md", "ex", "pr")],
            ),
            FakeToolInfo(
                tool_id="tool-b",
                artifacts=[FakeArtifact("g.md", "ex", "pr")],
            ),
        ]
        count = proposals.generate_for_underperforming(repository_client=client)
        assert count == 2
        assert len(client.proposals) == 1

    def test_no_nul_means_no_change(self) -> None:
        """Sanitization must be a no-op for clean input."""
        client = FakeRepositoryClient()
        client._underperforming = [
            FakeToolInfo(
                tool_id="tool-a",
                artifacts=[
                    FakeArtifact(
                        key="file.md",
                        text="clean text\nline two\n",
                        proposal_text="clean proposal\nline two\n",
                    )
                ],
            )
        ]
        proposals.generate_for_underperforming(repository_client=client)
        assert len(client.proposals) == 1
        diff = client.proposals[0]["diff_payload"]
        # Verify the diff is identical to what would be produced without sanitization.
        import io
        from difflib import unified_diff
        orig_existing = "clean text\nline two\n"
        orig_proposal = "clean proposal\nline two\n"
        expected_lines = list(unified_diff(
            orig_existing.splitlines(keepends=True),
            orig_proposal.splitlines(keepends=True),
            fromfile="a/file.md",
            tofile="b/file.md",
            n=3,
        ))
        assert diff == "".join(expected_lines)
