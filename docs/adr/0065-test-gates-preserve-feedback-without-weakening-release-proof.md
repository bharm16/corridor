---
status: accepted
---

# Test gates preserve feedback without weakening release proof

Corridor's real-PostgreSQL tests protect transaction boundaries, database constraints, upgrade paths, provenance, and fail-closed authority that mocks cannot prove. The suite had nevertheless become indiscriminate: ordinary PRs ran every database upgrade test, a squash merge ran the same tree again on `main`, and 78 upgrade tests provisioned 80 databases by applying Alembic revisions more than 8,000 times per full run. The result was multi-minute feedback without evidence proportional to the delay.

## Decision

Testing is tiered by feedback purpose and change scope.

- The implementation loop is `make check` plus `make test-focused ARGS="..."` at the changed public seam.
- `make test` is the broad non-slow developer gate. It is not an edit-by-edit ritual.
- `make test-slow` is the exhaustive non-migration complement. Normal PR CI runs `make check`, `make test`, and `make test-slow` once. The workflow does not run again on the squash merge to `main`.
- Every database upgrade test carries the `migration` marker. A path-scoped PR workflow runs `make test-migrations` only when migrations, schema models, migration tests, or their database harness change.
- `make test-full` remains the complete gate. It runs by explicit request and on the weekly schedule, not on every PR or merge.
- A migration-bearing change tests one fresh database at current `head` and the supported predecessor-to-head path with representative existing rows. New feature migrations do not each add another independent full-history installation merely to prove current schema fields that ordinary head-schema tests can prove.

Tests that need independent committed transactions keep a fresh database per test. Each process builds one template for each requested revision by running the real Alembic path once. Tests clone that exact revision, seed their own rows, and run the upgrade under test. Reapplying base-to-predecessor for every clone proves nothing new. Reusable-template copies, templates, and xdist worker databases carry their owning process identity, so a later run may reclaim them only after proving that owner no longer exists; live sibling databases and legacy names are never swept automatically.

## Considered options

**Delete tests until the suite is fast.** Rejected: the audit found no exact duplicate bodies and no test whose behavioral contract was proven unnecessary. The measured waste was repeated execution and database setup.

**Replace PostgreSQL with SQLite or mocked persistence.** Rejected: those substitutes cannot prove PostgreSQL constraints, isolation, locking, durable commits, or Alembic behavior.

**Share one mutable database across committed-transaction tests.** Rejected: independently committed workers and recovery scenarios leak state across tests and previously deadlocked under parallel execution.

**Run the complete upgrade matrix on every PR and again after merge.** Rejected: ordinary application changes do not change a historical schema path, and a squash commit does not justify repeating a tree that already passed in its PR.

## Consequences

Focused development remains isolated and fast. Normal PRs prove current behavior without historical upgrade work. Migration-sensitive PRs prove database upgrades once, and the weekly/manual complete gate detects broader drift. The first request for a revision in each process still pays the real Alembic cost; later tests at that revision pay only the template-copy cost. Test additions must select the cheapest gate that preserves their actual behavioral seam.

This policy relies on PR-only integration to `main`. A direct push does not receive a second post-merge test run and is outside the repository delivery convention.
