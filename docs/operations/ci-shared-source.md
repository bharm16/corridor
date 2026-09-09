# Shared corpus tests and the CI database

CI prepares PostgreSQL and the locked Python environments without separately
rebuilding the application schema in the configured empty database. The xdist
harness still migrates one fresh template through the real Alembic path.

After successful runner provisioning, `ci_environment.sh` exports
`CORRIDOR_CI_EMPTY_SHARED_SOURCE=1` through GitHub's environment file. Only with
that marker and GitHub Actions execution may `shared_source_database_url`
substitute the configured local `corridor` database on port 5433, and only when
it has no user relations. An existing or partially populated schema retains its
original URL: available corpus remains tested and a broken schema remains a
failure. The harness never drops, recreates, migrates or writes that source.

The fixture lazily creates one separate empty clone of the migrated template.
Workers and the shared source use the same coordination lock and migration
readiness marker. The source clone never inherits worker fixture rows. Failure
is retained and refused by sibling workers. Controller cleanup and abandoned-run
reclamation cover its guarded `_source` identity along with worker databases.

The NHHIP/SH99 corpus tests keep their original availability skips and query the
real configured corpus outside this explicit empty-CI case. The executable
`test_the_shared_state_fixture_reaches_a_migrated_database` guard still proves
that corpus queries receive a schema. Fresh-baseline and supported-transition
migration tests continue to provision and validate their own databases.

The change removes a duplicate setup migration; no test, budget, runner,
corpus-availability condition, or supported migration transition is removed.

Removing the serial setup migration also exposes initially missing cluster-wide
roles to independent worker/runtime/migration database bootstraps. The baseline
now rolls back a losing CREATE ROLE savepoint and checks that exact role again
before continuing through the unchanged capability-attribute checks. Only
PostgreSQL duplicate-role/role-name-index races and concurrent catalog updates
are handled; unrelated permission or DDL failures remain failures. No passwords,
role attributes, grants, schema objects or migration revisions change.
