To address the issue of implementing a bounded multi-turn conversation replay interface in the AstralDeep repository, we need to create a synthetic diagnostic that integrates with existing systems while ensuring security and compliance with the specified requirements. Here's a step-by-step approach to tackle this problem:

### Step 1: Understand the Existing Infrastructure

1. **Review Existing Code**: Familiarize yourself with the existing benchmark driver and LETS driver to understand how they handle real dispatch gates and security enforcement.
   - [Existing benchmark driver](https://github.com/AstralDeep/AstralDeep/blob/f89ba2ae9c0e8decd271544e05a93f6c8a202a66/backend/security_benchmark/drivers/inprocess.py)
   - [Existing narrow real-gate LETS driver](https://github.com/AstralDeep/AstralDeep/blob/f89ba2ae9c0e8decd271544e05a93f6c8a202a66/backend/tests/lets_case_study_driver.py)

2. **Understand Security and Compliance Requirements**: Ensure that all security measures, such as Keycloak IAM, LETS enforcement, and hash-chained audit, are preserved.

### Step 2: Design the Synthetic Multi-Turn Diagnostic

1. **Scenario Design**: Create 12–20 synthetic scenarios with 4–8 turns each. These scenarios should cover various cases such as repeated requests, authority drift, and recovery after denial.
   
2. **State Management**: Use fresh namespaced state per scenario to ensure isolation and prevent cross-contamination of state between scenarios.

3. **Effect Oracle**: Implement an independent harmless local effect oracle to simulate the effects of actions without causing real changes.

### Step 3: Implement the Diagnostic

1. **Scripted Provider Responses**: Develop scripted responses for providers while maintaining real dispatch logic. This ensures that the diagnostic remains realistic and adheres to existing security policies.

2. **Bounded Execution**: Ensure that each turn, tool call, and scenario is bounded. Use fake clocks and deterministic offline fixtures to control timing and execution flow.

3. **Correlation and Logging**: Implement logging to correlate messages, tool attempts, and gate decisions with actual effect observations. This will help in analyzing the outcomes of each scenario.

### Step 4: Testing and Validation

1. **Acceptance Checks**: Implement the acceptance checks outlined in the issue to ensure that all scenarios are covered and that the diagnostic meets the specified requirements.

2. **CI Integration**: Integrate the diagnostic with the existing CI pipeline, ensuring that all tests are deterministic and complete within the specified time limits.

3. **Coverage and Compliance**: Ensure that the changes maintain at least 90% test coverage and comply with Python 3.11 standards.

### Step 5: Documentation and Submission

1. **Documentation**: Document the design and implementation of the synthetic diagnostic, including the scenarios, state management, and correlation logic.

2. **Pull Request**: Submit a pull request with the changes, ensuring that it is well-documented and adheres to the repository's contribution guidelines.

3. **Review and Iterate**: Address any feedback from the code review process and make necessary adjustments to ensure the solution meets all requirements.

By following these steps, you can implement a bounded multi-turn conversation replay interface that integrates with existing systems while ensuring security and compliance.