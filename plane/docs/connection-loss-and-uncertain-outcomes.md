# Connection loss and uncertain transaction outcomes

## Scope

This contract qualifies `astralplane.database.pool` and `astralplane.database.transaction` when a
PostgreSQL connection is interrupted at checkout, during a statement, or while a commit is being
acknowledged. It changes no schema, schema revision, migration digest, blob layout, recovery
contract, owner predicate, or write fence. Ordinary SQL errors remain the driver's own typed
exceptions, including the PostgreSQL errors that callers handle inside a savepoint.

A lost connection is not evidence that a write rolled back. A lost commit acknowledgement is not
evidence that it committed. Plane therefore never retries a commit as if the first attempt failed,
never reports an unknown commit as success, and never returns an unsafe connection to the pool.

## Failure contract

| Point of interruption | Typed result | Connection custody |
| --- | --- | --- |
| Driver checkout fails | `PoolCheckoutError`, code `pool_checkout_failed` | The reserved borrow slot is released; no connection is yielded. An already typed `PlaneError` or a `BaseException` interrupt is preserved unchanged after the slot is released. |
| A statement, fetch, cursor operation, begin, or savepoint control detects a lost connection | `TransactionStatementError`, code `transaction_statement_failed`, with only the bounded operation name in metadata | The transaction is aborted, the connection is marked unsafe, and the pool discards it instead of reusing it. |
| Commit raises before or after the server committed | `TransactionCommitOutcomeUnknownError`, code `transaction_commit_outcome_unknown`, a subclass of `TransactionCommitError` | The outcome is unknown. The connection is discarded, commit is not retried, and no completion value is returned. |
| Rollback fails | `TransactionRollbackError`, code `transaction_rollback_failed` | The connection is discarded. An earlier caller exception remains the reported primary failure. |
| Commit succeeds but connection cleanup fails | `TransactionCleanupError`, code `transaction_cleanup_failed` | The committed result is not reported as a failed commit, but the damaged connection is discarded and the cleanup failure is visible. |
| An ordinary PostgreSQL statement error leaves the connection usable | The original driver exception, including savepoint-recoverable errors | The transaction rolls back and the connection may be reused after a successful reset. |

`BaseException` signals such as `KeyboardInterrupt` and `SystemExit` are not converted into a
`PlaneError`. They propagate after marking the connection unsafe, so a signal is not mistaken for a
recoverable database result. All typed errors have bounded, non-sensitive metadata and keep the
driver exception only as their chained cause; driver text and connection material do not enter the
reported message.

## Caller recovery

1. Treat every `TransactionCommitOutcomeUnknownError` and interrupted transaction as unresolved.
   Do not claim that its writes committed or rolled back.
2. Re-read the durable state through a fresh connection, or repeat an operation only through its
   declared idempotency key, receipt, or compare-and-set fence. A fresh ordinary retry is not proof
   that the uncertain attempt was absent.
3. Keep admission closed when the transaction was part of migration, reconciliation, restore,
   session retirement, or another recovery gate. Follow the guarded recovery procedure and repeat
   only its declared idempotent steps.
4. Do not weaken an owner predicate, version fence, advisory lock, or audit requirement to make a
   retry succeed. A discarded connection is not a replacement for the host's authorization,
   election, fencing, or approval decisions.
5. Discard the failed process's caches and re-read any state that may have changed. A pooled
   connection that was discarded cannot carry uncommitted session state into the next borrower.

## Evidence

The real-PostgreSQL fault fixtures in
`tests/integration/test_connection_loss_transactions.py` terminate a backend during a statement,
terminate it before commit, and lose the acknowledgement of a commit that actually reached the
server. They verify the typed outcome, discarded connection, absence of an automatic retry,
presence or absence of the durable row where that outcome is knowable, and a healthy next checkout.
`tests/test_pool_transaction.py` covers checkout interruption, failed rollback/release/autocommit
cleanup, `BaseException` propagation, malformed connection state, ordinary SQL-error reuse, and
savepoint compatibility without a live server.