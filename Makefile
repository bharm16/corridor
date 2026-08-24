.PHONY: boot up down psql test test-full test-slow test-serial corpus demo ingest docs queue agreements extract active-run revision-process milestones exceptions eval gold ledger-archive carry-forward m8-acceptance sh99-admission-acceptance event-admission-acceptance sh99-coordinator-rehearsal evidence-investigator evidence-shadow evidence-shadow-eval minutes report

TEST_WORKERS ?= 4

# One command from a clean clone.
boot:
	uv sync
	docker compose up -d --wait
	uv run alembic upgrade head

up:
	docker compose up -d --wait

down:
	docker compose down

psql:
	docker compose exec postgres psql -U corridor -d corridor

# Fast PostgreSQL-backed developer loop. Exhaustive rehearsals stay in test-full.
test:
	uv run pytest -n $(TEST_WORKERS) --dist loadfile -m "not slow"

# Release/CI gate: behavior, real-corpus geometry, acceptance, and migrations.
test-full:
	uv run pytest -n $(TEST_WORKERS) --dist loadfile

# Exhaustive tests only, for changes inside those seams.
test-slow:
	uv run pytest -n $(TEST_WORKERS) --dist loadfile -m slow

# Single-process fallback for debugger use and scheduler diagnosis.
test-serial:
	uv run pytest

# Resolve corpus/manifest.yaml to files on disk. Re-running is a no-op for
# unchanged sources; a source whose bytes changed keeps both revisions.
corpus:
	uv run python -m corridor.corpus

# Raw files -> cited report. Pass N to accept only the first N candidates:
#   make demo LIMIT=25
demo:
	uv run python -m corridor.demo $(LIMIT)

# Load every lockfile opted into bulk project materialization. Pass
# `ARGS="<slug>"` to ingest one explicit project, including opted-out ones.
ingest:
	uv run python -m corridor.docs ingest

# Browse the evidence store: `make docs` or `make docs ARGS="page 167 1"`.
docs:
	uv run python -m corridor.docs $(ARGS)

# Adjudication queue at http://localhost:8412
queue:
	uv run uvicorn corridor.web.app:app --port 8412 --reload

# LLM extraction over the executed agreements. Needs OPENAI_API_KEY in .env.
agreements:
	uv run python -m corridor.extract_agreement $(ARGS)

# Extract every matrix in a project. Skips documents already extracted at
# this prompt version; --redo replaces their pending candidates and leaves
# adjudicated ones alone. Reads the page images with a model, so it
# needs OPENAI_API_KEY:
#   make extract ARGS="nhhip-3c2"
#   make extract ARGS="nhhip-3c2 --redo"
extract:
	uv run python -m corridor.extract_project $(ARGS)

# Load a project onto the record (ADR-0029). `make extract` and
# `make minutes` do this on their way out, so this is the backfill for a
# project read before that was true, and the way to finish a load after
# declaring the Active Run of a document that held several readings:
#   make admission ARGS="load sh99-grand-parkway"
admission:
	uv run python -m corridor.admission_cli $(ARGS)

# Explicitly select reviewer work; extraction never makes "newest" active:
#   make active-run ARGS="<document-id> <extraction-run-id>"
# Bulk form for the unambiguous case only — declares the single completed
# run of every undeclared document, refusing whole if any document holds
# several (docs/sh99-date-rehearsal.md):
#   make active-run ARGS="--single-run-documents sh99-grand-parkway"
active-run:
	uv run python -m corridor.extraction_runs $(ARGS)

# Run one exact predecessor-successor pair through comparison readback and
# already-authorized Carry-Forward only. Never infers runs or accepts policy
# identity flags:
#   make revision-process ARGS="<predecessor-extraction-run-id> <successor-extraction-run-id>"
revision-process:
	uv run python -m corridor.revision_processing_cli $(ARGS)

# Import a milestone CSV: make milestones ARGS="sh99-grand-parkway corpus/sh99-milestones.csv RELO-CONSTR"
milestones:
	uv run python -m corridor.milestones $(ARGS)

# Extraction Measurement over exact completed run receipts. Repeat
# --extraction-run once per matrix. A machine reference must travel with its
# author-time scope manifest; prompt/document flags are assertions only:
#   make eval ARGS="wsdot-9424 gold/wsdot-9424.machine.csv --reference-manifest=gold/wsdot-9424.machine.scope.json --extraction-run=123"
eval:
	uv run python -m corridor.eval $(ARGS)

exceptions:
	uv run python -m corridor.exceptions $(ARGS)

# Prepare a diagnostic disagreement report, or author the explicitly
# semi-independent machine reference plus its required scope manifest:
#   make gold ARGS="wsdot-9424 --author"
gold:
	uv run python -m corridor.gold $(ARGS)

# One-time Development Ledger retirement. Always run `plan` first; `retire`
# requires the exact digest and Dependency count printed by that plan:
#   make ledger-archive ARGS="plan nhhip-3c2"
#   make ledger-archive ARGS="retire nhhip-3c2 --expected-sha256=<sha> --expected-dependency-count=141"
ledger-archive:
	uv run python -m corridor.legacy_ledger_archive_cli $(ARGS)

# Inspect, authorize, disable, or run project-level Automatic Carry-Forward.
# Authorization requires an explicit stable HumanPrincipal subject:
#   make carry-forward ARGS="authorize nhhip-3c2 --principal=local:<subject>"
carry-forward:
	uv run python -m corridor.automatic_carry_forward_cli $(ARGS)

# Capture, replay, or verify the isolated mechanical M8 acceptance bundle.
# Ordinary replay is model-free and requires exact fixture/transformation pins:
#   make m8-acceptance ARGS="replay --fixture=<path> --transformations=<path> --output-dir=<path> --postgres-admin-url=<url> --expected-fixture-sha256=<sha> --expected-transformations-sha256=<sha>"
#   make m8-acceptance ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
m8-acceptance:
	uv run python -m corridor.m8_acceptance_cli $(ARGS)

# Capture the real, pinned SH 99 state read-only, clone it into a newly created,
# disposable PostgreSQL database, then run the exact shared Admission command twice.
# Verify checks the emitted receipt without opening a database. This never authorizes
# or performs shared SH 99 mutation:
#   make sh99-admission-acceptance ARGS="replay --project-slug=sh99-grand-parkway --source-database-url=<url> --expected-clean-git-revision=<sha> --output-dir=<new-dir> --postgres-admin-url=<url>"
#   make sh99-admission-acceptance ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
# Seal the current post-activation shared-operation plan on a disposable clone;
# repeat --expected-active-run once per approved Document/Extraction Run pair:
#   make sh99-admission-acceptance ARGS="seal --project-slug=sh99-grand-parkway --source-database-url=<url> --expected-clean-git-revision=<sha> --output-dir=<new-dir> --postgres-admin-url=<url> --expected-acceptance-receipt-id=<id> --expected-acceptance-receipt-sha256=<sha> --expected-activation-id=<id> --expected-active-run=1435:193811 --expected-active-run=1438:193812 --expected-candidate-id=405519"
#   make sh99-admission-acceptance ARGS="verify-seal <bundle-dir> --expected-manifest-sha256=<sha>"
sh99-admission-acceptance:
	uv run python -m corridor.sh99_admission_acceptance_cli $(ARGS)

# Compare predecessor and unknown-scope Event Admission on two disposable clones.
# A failed receipt never activates; a passing receipt activates normal processing.
#   make event-admission-acceptance ARGS="replay --project-slug=sh99-grand-parkway --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha>"
# Suspension is append-only and restores the predecessor policy:
#   make event-admission-acceptance ARGS="suspend --project-slug=sh99-grand-parkway --database-url=<url> --reason=<reason> --recorded-by=local:<subject>"
event-admission-acceptance:
	uv run python -m corridor.event_admission_acceptance_cli $(ARGS)

# Run the bounded coordinator exercise only on a disposable clone. It verifies the
# prior Admission bundle first, separates shared operations/backfill time from the
# timed coordinator flow, upgrades only the clone between explicit migration pins,
# and records assistance or failure honestly:
#   make sh99-coordinator-rehearsal ARGS="replay --project-slug=sh99-grand-parkway --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha> --expected-source-migration-head=e255a7c4d9e2 --expected-target-migration-head=f255b7c4d9e3 --shared-admission-receipt-path=<validation-passed.json> --expected-shared-admission-receipt-sha256=<sha> --approved-shared-state-receipt=<immutable-url> --shared-backfill-elapsed-seconds=291 --output-dir=<new-dir>"
#   make sh99-coordinator-rehearsal ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
sh99-coordinator-rehearsal:
	uv run python -m corridor.sh99_coordinator_rehearsal_cli $(ARGS)

# Run one current Unplaced Statement Candidate through the hidden, read-only
# Evidence Investigator and append a terminal local receipt. Needs OPENAI_API_KEY:
#   make evidence-investigator ARGS="<candidate-id>"
evidence-investigator:
	uv run python -m corridor.evidence_investigator_cli $(ARGS)

# Freeze and invisibly run current Unplaced Statement work, or capture one
# later independent human outcome. Needs OPENAI_API_KEY for `run`:
#   make evidence-shadow ARGS="run <project-slug> --candidate-id=<id> --selection-rule=operator-declared:<rule> --manifest-path=<new-json>"
#   make evidence-shadow ARGS="capture <shadow-case-id>"
evidence-shadow:
	uv run python -m corridor.evidence_investigator_shadow_cli $(ARGS)

# Grade an explicit shadow run set into immutable JSON and Markdown receipts:
#   make evidence-shadow-eval ARGS="--run=<run-id> --human-scores=<json> --output-dir=<new-dir>"
evidence-shadow-eval:
	uv run python -m corridor.evidence_investigator_evaluation_cli $(ARGS)

# LLM extraction over coordination meeting notes. Needs OPENAI_API_KEY.
# Bound a run to exact registered notes by repeating --document-id:
#   make minutes ARGS="sh99-grand-parkway --document-id 123 --document-id 456"
minutes:
	uv run python -m corridor.extract_minutes $(ARGS)

# Build the weekly report without re-running the pipeline:
#   make report ARGS="nhhip-3c2"
report:
	uv run python -m corridor.report $(ARGS)
