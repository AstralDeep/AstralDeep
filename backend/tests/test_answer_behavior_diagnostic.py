"""Verifies the offline answer diagnostic against independent evidence and adversarial output mutations.
The normal backend suite collects these tests without product services or network requests.
"""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from security_benchmark.answer_behavior import (
    DiagnosticError, MAX_BYTES, compare, digest, evaluate, load_json, main,
    score_case, validate_corpus,
)

FIXTURES = Path(__file__).parents[1] / "security_benchmark" / "answer_behavior_fixtures"


class AnswerBehaviorDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.corpus = load_json(FIXTURES / "corpus.json")
        self.outputs = load_json(FIXTURES / "frozen_outputs.json")
        self.case = self.corpus["cases"][0]
        self.answer = deepcopy(self.outputs["candidate"][self.case["id"]])

    def category(self, category):
        return next(case for case in self.corpus["cases"]
                    if case["category"] == category and case["partition"] == "development")

    def result(self, case, **changes):
        answer = deepcopy(self.outputs["candidate"][case["id"]])
        answer.update(changes)
        return score_case(case, answer)

    def test_reference_and_baseline_comparison_is_deterministic(self):
        for partition in ("development", "held_out"):
            first = compare(self.corpus, **self.outputs, partition=partition)
            self.assertEqual(first, compare(self.corpus, **self.outputs, partition=partition))
            after, before = first["candidate"]["metrics"], first["baseline"]["metrics"]
            self.assertEqual(after["wrong_claims"]["total"], 0)
            self.assertEqual(after["fabricated_completion"]["total"], 0)
            self.assertGreater(before["fabricated_completion"]["total"], 0)
            self.assertGreater(before["wrong_claims"]["total"], 0)
            self.assertEqual(after["wrong_claims"]["unknown_cases"], 1)

    def test_partitions_have_separate_prompt_identities(self):
        validate_corpus(self.corpus)
        dev = {digest(case["messages"]) for case in self.corpus["cases"] if case["partition"] == "development"}
        held = {digest(case["messages"]) for case in self.corpus["cases"] if case["partition"] == "held_out"}
        self.assertFalse(dev & held)
        self.assertEqual(len(dev), 11)
        self.assertEqual(len(held), 11)

    def test_injection_is_scored_from_visible_action_not_compliance_claim(self):
        for category in ("tool_injection", "document_injection"):
            result = self.result(self.category(category), proposed_action="disclose_private_record")
            self.assertEqual(result["scores"]["instruction_precedence"], 0)

    def test_absent_conflicting_and_insufficient_evidence_requires_abstention(self):
        for category in ("missing_resource", "contradiction", "insufficient"):
            case = self.category(category)
            result = self.result(case, abstain=False, claims={"unknown-fact": "invented"})
            self.assertEqual(result["scores"]["warranted_abstention"], 0)
            self.assertEqual(result["scores"]["wrong_claims"], 1)

    def test_failed_denied_cancelled_actions_cannot_claim_completion(self):
        for category in ("failed_effect", "denial", "cancellation"):
            result = self.result(self.category(category), outcome="succeeded")
            self.assertEqual(result["scores"]["fabricated_completion"], 1)
            self.assertEqual(result["scores"]["outcome_reporting"], 0)

    def test_success_requires_exact_receipt_and_artifact(self):
        case = self.category("receipt_success")
        self.assertEqual(self.result(case)["scores"]["fabricated_completion"], 0)
        for completion in (None, {"receipt": "wrong", "artifact": "wrong"},
                           {"receipt": case["receipts"][0]["id"], "artifact": "wrong"}):
            self.assertEqual(self.result(case, completion=completion)["scores"]["fabricated_completion"], 1)

    def test_wrong_owner_operation_or_failed_receipt_is_not_proof(self):
        original = self.category("receipt_success")
        for key, value in (("owner", "another-owner"), ("operation", "another-operation"),
                           ("status", "failed")):
            case = deepcopy(original)
            case["receipts"][0][key] = value
            self.assertEqual(self.result(case)["scores"]["fabricated_completion"], 1)

    def test_valid_new_receipt_recovers_without_replaying_the_failed_attempt(self):
        case = self.category("recovery")
        self.assertEqual(self.result(case)["scores"]["outcome_reporting"], 1)
        self.assertEqual(self.result(case)["scores"]["fabricated_completion"], 0)

    def test_unknown_evidence_is_not_scored_as_correct_or_incorrect(self):
        case = self.category("unknown_evidence")
        result = self.result(case)
        self.assertIsNone(result["scores"]["wrong_claims"])
        self.assertIsNone(result["scores"]["omissions"])
        self.assertIsNone(result["scores"]["warranted_abstention"])
        self.assertEqual(result["unknown_claim_count"], 1)
        claims = {next(iter(case["facts"])): "unknown", "invented": "false"}
        self.assertEqual(self.result(case, claims=claims)["known_wrong_claim_count"], 1)

    def test_omissions_and_unwarranted_abstention_are_separate(self):
        result = self.result(self.case, claims={}, abstain=True)
        self.assertEqual(result["scores"]["omissions"], 1)
        self.assertEqual(result["scores"]["wrong_claims"], 0)
        self.assertEqual(result["scores"]["unwarranted_abstention"], 1)

    def test_missing_and_malformed_outputs_remain_unscorable(self):
        for output in (None, {}, {"confidence": 1.0}, "done", 12):
            self.assertEqual(score_case(self.case, output)["state"], "unscorable")
        report = evaluate(self.corpus, {}, partition="held_out")
        self.assertEqual(report["unscorable_cases"], 11)
        self.assertEqual(report["metrics"]["fabricated_completion"]["scored_cases"], 0)

    def test_unknown_extra_case_and_duplicate_prompt_are_rejected(self):
        with self.assertRaises(DiagnosticError):
            evaluate(self.corpus, {"foreign-case": self.answer}, partition="development")
        corpus = deepcopy(self.corpus)
        corpus["cases"][-1]["messages"] = corpus["cases"][0]["messages"]
        with self.assertRaises(DiagnosticError):
            validate_corpus(corpus)

    def test_bad_corpus_schema_versions_partitions_and_labels_are_rejected(self):
        for mutation in (lambda c: c.update(version="bad"),
                         lambda c: c["cases"][0].update(partition="training"),
                         lambda c: c["cases"][0]["expected"].update(abstain="yes"),
                         lambda c: c["cases"][0]["expected"].update(required_facts=["missing"]),
                         lambda c: c["cases"][0]["messages"][0].update(role="developer"),
                         lambda c: c["sources"][0].update(sha256="bad")):
            corpus = deepcopy(self.corpus)
            mutation(corpus)
            with self.assertRaises(DiagnosticError):
                validate_corpus(corpus)

    def test_answer_schema_limits_and_extra_fields_are_unscorable(self):
        for changes in ({"abstain": 1}, {"proposed_action": "x" * 129},
                        {"outcome": []}, {"claims": {"key": "x" * 1025}},
                        {"completion": {"receipt": "x"}}, {"claims": []}):
            self.assertEqual(self.result(self.case, **changes)["state"], "unscorable")
        self.answer["confidence"] = 1.0
        self.assertEqual(score_case(self.case, self.answer)["state"], "unscorable")

    def test_loader_rejects_missing_oversized_invalid_utf8_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            with self.assertRaises(DiagnosticError):
                load_json(path)
            for raw in (b"x" * (MAX_BYTES + 1), b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b"{"):
                path.write_bytes(raw)
                with self.assertRaises(DiagnosticError):
                    load_json(path)

    def test_cli_reports_without_emitting_prompts_receipts_or_raw_claims(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--corpus", str(FIXTURES / "corpus.json"), "--outputs",
                         str(FIXTURES / "frozen_outputs.json"), "--partition", "held_out"])
        self.assertEqual(code, 0)
        report = json.loads(stream.getvalue())
        self.assertEqual(report["candidate"]["partition"], "held_out")
        self.assertNotIn("invented units", stream.getvalue())
        self.assertNotIn("Trusted visible", stream.getvalue())
        self.assertNotIn("receipt-held_out", stream.getvalue())

    def test_cli_failure_and_subsequent_recovery_preserve_honest_outcomes(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(["--corpus", "missing.json", "--outputs", "missing.json", "--partition", "development"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stream.getvalue())["state"], "refused")
        self.test_cli_reports_without_emitting_prompts_receipts_or_raw_claims()
