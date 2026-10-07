To address the issue described, we need to implement a bounded multi-turn conversation replay interface within the AstralDeep repository. This involves creating a synthetic diagnostic tool that can simulate multi-turn interactions while preserving real dispatch gates and ensuring that the system's security and authorization mechanisms are respected. Here's a step-by-step approach to tackle this problem:

### Step 1: Define Synthetic Scenarios

1. **Create Synthetic Scenarios**: Develop 12-20 synthetic scenarios that simulate multi-turn conversations. Each scenario should have 4-8 turns and include various elements such as:
   - Development/held-out identities
   - Provenance/digests
   - Expected authority transitions
   - Attempted effects and benign recovery twins

2. **Scenario Coverage**: Ensure scenarios cover:
   - Repeated or rewritten requests after denial
   - Revocation between turns
   - Authority drift through handoffs
   - Untrusted tool/document content
   - False completion after a denied effect
   - Allowed recovery and cancellation

### Step 2: Implement the Diagnostic Tool

1. **Script Provider Responses**: Use scripted responses for providers while maintaining real dispatch mechanisms. Ensure that owner/policy/PHI/egress/confirmation/delegation/audit and LETS enforcement are preserved.

2. **Effect Oracle**: Implement an independent, harmless local effect oracle to observe and record effects without exposing it to potential attackers.

3. **Correlate Messages and Effects**: Develop a mechanism to correlate messages, tool attempts, and gate decisions with actual effect observations and receipts.

4. **Outcome Categorization**: Categorize outcomes as unattempted, prevented, occurred, unknown/unobserved, truncated/cancelled, and infrastructure outcomes.

### Step 3: Ensure Robustness and Security

1. **Bound Every Turn**: Ensure that every turn, delegated worker turn, tool call, retry, output, and scenario is bounded. Use fresh namespaced state per scenario/branch.

2. **Deterministic Fixtures**: Use deterministic offline fixtures and explicit teardown to ensure repeatability and reliability of tests.

3. **Candidate-Bound Run Record**: Deliver a run record that states which gates are real, which services/providers are doubled, and what routes require synthetic staging verification.

### Step 4: Integration and Testing

1. **Integration with Existing Systems**: Integrate the diagnostic tool with existing local warden/Plane/test-driver patterns and benign local effects.

2. **Testing and Validation**: Ensure that all tests are deterministic/offline, with at least 90% coverage for changed Python lines. Each CI job should finish within 30 minutes.

3. **Golden Paths and Edge Cases**: Cover golden paths, edge cases, denials, failures, and recovery in the test scenarios.

### Step 5: Documentation and Review

1. **Documentation**: Document the implementation details, including scenario definitions, tool usage, and test results.

2. **Code Review**: Submit the implementation for human review to ensure compliance with security and functionality requirements.

3. **Acceptance Checks**: Verify that all acceptance checks outlined in the issue are met before considering the issue resolved.

### Step 6: Optional Discovery

1. **Scenario Proposal**: Optionally propose scenarios from a maintained Petri/Bloom environment, ensuring they use only synthetic/public-permissioned inputs.

2. **Review and Fixture Changes**: Ensure human review precedes any fixture changes, and no paid provider/judge or research runtime becomes a required gate.

By following these steps, you can create a robust and secure multi-turn conversation replay interface that meets the requirements outlined in the issue. This approach ensures that the system's security and authorization mechanisms are preserved while providing valuable diagnostic capabilities.