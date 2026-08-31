# Inert released-policy sources

Alembic does not load this directory. `alembic.ini` points executable migration
history at `migrations/baseline_versions`.

The 13 Python files here retain exact source bytes used by released policy
fingerprints. Their revision metadata is historical data, not an executable
upgrade graph. Do not edit them. A policy change adds a newly named source and
a new policy version.
