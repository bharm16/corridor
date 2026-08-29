# Experimental database guard

ADR-0049 separates production and experimental work by PostgreSQL database,
not by an Extraction Run purpose label. Existing production runs remain
unchanged. There is no purpose migration or automatic reclassification.

Extraction Measurement, direct and shadow Statement Review Assistant runs,
shadow labels, and shadow evaluation require an explicit `--database-url`. That
URL must name a disposable database copy. The command observes PostgreSQL's
cluster identifier, database name, and role; changing hostname spelling or
credentials does not make the production database experimental.

Acceptance, rehearsal, replay, and Product Test Run workflows provision their
own disposable databases. The shared provisioner applies the same guard before
yielding one. A source database may be captured read-only where that workflow's
sealed contract permits it, but experimental writes occur only in the guarded
copy.

Use the Makefile examples for the exact command shape. A refusal means the
operator must create or select a disposable copy and rerun the command there.
Do not add a bypass flag, infer safety from a prompt or model name, or relabel
production Extraction Runs to make an experiment proceed.
