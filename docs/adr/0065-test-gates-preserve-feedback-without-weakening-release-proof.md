---
status: accepted
---

# Test gates preserve feedback without weakening release proof

Corridor's real-PostgreSQL tests protect transaction boundaries, database constraints, migration paths, provenance, and fail-closed authority that mocks cannot prove. The suite had nevertheless become an indiscriminate developer loop: the normal gate was followed by its full-suite superset, one exhaustive 453-row replay remained in the normal gate, and every committed-transaction test replayed the complete Alembic chain in a function-scoped database. The result was multi-minute feedback without additional evidence proportional to the delay.

## Decision

Testing is tiered by feedback purpose. A revision obtains complete evidence either from one `make test-full` run or from the two non-overlapping `make test` and `make test-slow` selections.

- The implementation loop is `make check` plus `make test-focused ARGS="..."` at the changed public seam.
- `make test` is the broad non-slow developer gate. It is not an edit-by-edit ritual.
- `make test-full` is the release and CI gate and is a strict superset of `make test`. A revision runs `make test-full` directly rather than running `make test` first.
- When `make test` already passed on the unchanged revision, `make test-slow` is its exhaustive complement. Any code or test change invalidates both receipts.
- Volume, real-corpus, acceptance, and migration-rehearsal tests remain in the suite but carry `slow`; speed alone never justifies deleting a behavioral contract.

Tests that need independent committed transactions keep a fresh database per test. At `head`, each process migrates one private template and creates test databases by PostgreSQL template copy; the copied schema is still the result of the real migration chain. Exact historical migration rehearsals never use the template because replaying that chain is the behavior under test. Reusable-template copies, templates, and xdist worker databases carry their owning process identity, so a later run may reclaim them only after proving that owner no longer exists; live sibling databases and legacy names are never swept automatically.

## Considered options

**Delete tests until the suite is fast.** Rejected: the audit found no exact duplicate bodies and no test whose behavioral contract was proven unnecessary. The measured waste was repeated execution and database setup.

**Replace PostgreSQL with SQLite or mocked persistence.** Rejected: those substitutes cannot prove PostgreSQL constraints, isolation, locking, durable commits, or Alembic behavior.

**Share one mutable database across committed-transaction tests.** Rejected: independently committed workers and recovery scenarios leak state across tests and previously deadlocked under parallel execution.

## Consequences

Focused development remains isolated and fast, CI still proves the complete suite, and migration rehearsals retain their original strength. A first reusable database in each process still pays one real migration cost; subsequent committed-transaction tests pay only the template-copy cost. Test additions must select the cheapest gate that preserves their actual behavioral seam.
