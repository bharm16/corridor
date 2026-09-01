---
status: accepted
domain: testing
scope: current product
---

# Test gates preserve feedback without weakening release proof

Corridor's real-PostgreSQL tests protect transaction boundaries, database constraints, upgrade paths, provenance, and fail-closed authority that mocks cannot prove. The suite had nevertheless become indiscriminate: 112 migrations created during four weeks of development were treated as permanent release history, and 78 tests retained 33 unsupported historical starting revisions. The result was multi-minute feedback without evidence proportional to the delay.

## Decision

Testing is tiered by feedback purpose and change scope.

- The implementation loop is `make check` plus `make test-focused ARGS="..."` at the changed public seam.
- `make test` is the broad non-slow developer gate. It is not an edit-by-edit ritual.
- `make test-slow` is the exhaustive non-migration complement. Normal PR CI runs `make check`, `make test`, and `make test-slow` once. The workflow does not run again on the squash merge to `main`.
- Every database upgrade test carries the `migration` marker. A path-scoped PR workflow runs `make test-migrations` only when migrations, schema models, migration tests, or their database harness change.
- `make test-full` remains the complete gate. It runs by explicit request and on the weekly schedule, not on every PR or merge.
- The permanent migration contract contains one blank-database baseline, one supported released-head-to-current transition with representative Project Record rows, one exact schema fingerprint, and one explicit downgrade refusal.
- The supported upgrade window is the released head and its immediate successor. An upgrade test expires after every supported database has advanced past its starting revision. Current behavior remains protected by ordinary tests.
- New feature migrations do not add another blank-database history test. The one baseline test owns fresh installation.

The August 2026 development chain is consolidated into released-head schema builder `b7d3f9a1c2e5` and baseline marker `c0a1d0b5e11e`. A fresh database runs the schema builder. A database already stamped at the released head runs only the marker. The marker canonicalizes 87 equivalent check-constraint expressions so both paths produce the exact schema fingerprint `72c7ffac9606259affaf705075b6daa53c4279724a48811e926cc7076e7f6703`. The builder also preserves the no-login statement-retirement role, its ten table grants, function ownership, and public-execute refusal. Downgrade across this baseline is unsupported.

Thirteen historical revision files remain as inert bytes in `migrations/versions` because released policy fingerprints name their exact contents. Alembic loads only `migrations/baseline_versions`; those thirteen files are not executable upgrade history.

Tests that need independent committed transactions keep a fresh database per test. Reusable-template copies, templates, and xdist worker databases carry their owning process identity, so a later run may reclaim them only after proving that owner no longer exists; live sibling databases are never swept.

## Considered options

**Delete tests only because they are slow.** Rejected: retirement follows the supported upgrade window, not elapsed time. The 74 removed tests started from revisions that no supported database can hold; current behavior remains in ordinary tests and the four-test baseline contract.

**Replace PostgreSQL with SQLite or mocked persistence.** Rejected: those substitutes cannot prove PostgreSQL constraints, isolation, locking, durable commits, or Alembic behavior.

**Share one mutable database across committed-transaction tests.** Rejected: independently committed workers and recovery scenarios leak state across tests and previously deadlocked under parallel execution.

**Run the complete upgrade matrix on every PR and again after merge.** Rejected: ordinary application changes do not change a historical schema path, and a squash commit does not justify repeating a tree that already passed in its PR.

**Keep every development revision forever.** Rejected: no deployed-database inventory or support contract names those revisions. Git preserves their source, while the executable chain supports only databases that can still exist.

## Consequences

Focused development remains isolated and fast. Normal PRs prove current behavior without historical upgrade work. Migration-sensitive PRs prove the bounded upgrade window once, and the weekly/manual complete gate detects broader drift. The migration inventory is two revisions and four permanent tests until a new released successor advances the window.

This policy relies on PR-only integration to `main`. A direct push does not receive a second post-merge test run and is outside the repository delivery convention.
