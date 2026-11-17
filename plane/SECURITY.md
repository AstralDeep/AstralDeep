# Security policy

Report suspected vulnerabilities privately through the repository's GitHub security advisory
workflow. Do not open a public issue containing credentials, user data, host paths, database dumps,
or exploit details.

AstralPlane accepts only neutral, owner-scoped persistence values. Callers remain responsible for
authentication, authorization, PHI policy, confirmation, egress, and tool-execution decisions.
Repository writes must include owner and version/idempotency fences in the same database statement;
no cursor or connection may escape its declared scope.

Durable roots are explicit operator configuration outside source trees. Path handling rejects
traversal and symbolic-link/reparse crossings. A failed physical purge remains visible and
retryable through a tombstone. Migration and reconciliation failure leaves application admission
closed.
