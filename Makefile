.PHONY: boot up down psql check test-focused test test-full test-slow test-timing test-slow-timing test-migrations test-serial corpus demo ingest docs queue agreements extract active-run revision-process milestones exceptions eval candidate-model gold storage-baseline storage retention ledger-archive carry-forward due-work location-discovery m8-acceptance sh99-admission-acceptance event-admission-acceptance sh99-coordinator-rehearsal product-proving evidence-investigator evidence-shadow evidence-shadow-eval pdf-eval page-inventory-eval minutes report

TEST_WORKERS ?= 4

# One command from a clean clone.
boot:
	uv sync
	uv sync --project workers/render --frozen
	docker compose up -d --wait
	CORRIDOR_LEGACY_DEV_LOGIN=1 uv run alembic upgrade head

up:
	docker compose up -d --wait

down:
	docker compose down

psql:
	docker compose exec postgres psql -U corridor -d corridor

# Fast source and architecture checks with no model or external service calls.
check:
	uv run ruff check src/corridor
	uv run python -m compileall -q src/corridor
	uv run pytest tests/test_architecture.py -q

# Tight PostgreSQL-backed loop for the exact seam being changed.
# Example: make test-focused ARGS="tests/test_work_list.py::test_name"
test-focused:
	@if [ -z "$(strip $(ARGS))" ]; then echo 'ARGS must name at least one test seam' >&2; exit 2; fi
	uv run pytest -n 1 --dist loadfile $(ARGS)

# Broad developer gate. Run after a broad change, not after every edit.
# This is the non-slow subset of test-full; do not run both on one revision.
test:
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not slow"

# Complete manual/scheduled gate. PR workflows use the scoped gates below.
test-full:
	uv run pytest -n $(TEST_WORKERS) --dist worksteal

# Exhaustive non-migration complement to test.
test-slow:
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "slow and not migration"

# One balanced slice of the non-slow suite. CI runs the slices as a matrix so
# each lands on its own runner: the gate is CPU-bound on a four-core runner,
# so redistributing between workers on one machine cannot help and more
# actual CPU can (#548).
test-shard:
	@if [ -z "$(strip $(SHARD))" ] || [ -z "$(strip $(SHARDS))" ]; then \
	  echo 'SHARD and SHARDS are required' >&2; exit 2; fi
	@files=$$(uv run python scripts/test_shard.py --shards $(SHARDS) --shard $(SHARD)); \
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not slow" $$files; \
	status=$$?; \
	if [ $$status -eq 5 ]; then \
	  echo "shard $(SHARD) holds no matching tests"; exit 0; fi; \
	exit $$status

# One balanced slice of the exhaustive non-migration complement, sharded for
# the same reason as test-shard.
test-slow-shard:
	@if [ -z "$(strip $(SHARD))" ] || [ -z "$(strip $(SHARDS))" ]; then \
	  echo 'SHARD and SHARDS are required' >&2; exit 2; fi
	@files=$$(uv run python scripts/test_shard.py --shards $(SHARDS) --shard $(SHARD) --slow); \
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "slow and not migration" $$files; \
	status=$$?; \
	if [ $$status -eq 5 ]; then \
	  echo "shard $(SHARD) holds no slow tests"; exit 0; fi; \
	exit $$status

# Per-file timing for the feedback budget (#548). Writes a JUnit report so a
# revision can be compared against its base branch before any test is cut, and
# so the shard partition is recomputed from measured seconds:
#   make test-timing
#   uv run python scripts/test_timing.py out/timing/non-slow.xml
#   uv run python scripts/test_timing.py out/timing/non-slow.xml --write tests/durations.json
test-timing:
	@mkdir -p out/timing
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not slow" \
	  --durations=50 --durations-min=0.5 --junitxml=out/timing/non-slow.xml

# The same measurement for the slow gate. `tests/durations-slow.json` had no
# producer, so it went stale and the balancer counted ten unrecorded files as
# imaginary average work — which is how one slow shard ran no tests (#548):
#   make test-slow-timing
#   uv run python scripts/test_timing.py out/timing/slow.xml --write tests/durations-slow.json
test-slow-timing:
	@mkdir -p out/timing
	uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "slow and not migration" \
	  --durations=50 --durations-min=0.5 --junitxml=out/timing/slow.xml

# Database upgrade tests. Run for migration-sensitive changes, not ordinary PRs.
test-migrations:
	uv run pytest -n 1 --dist loadfile -m migration

# Single-process fallback for debugger use and scheduler diagnosis.
test-serial:
	uv run pytest

# Resolve corpus/manifest.yaml to files on disk. Re-running is a no-op for
# unchanged sources; a source whose bytes changed keeps both revisions.
corpus:
	uv run python -m corridor.corpus

# Raw files -> cited Coordination Report. Pass N to accept only the first N
# Extracted Proposals:
#   make demo LIMIT=25
demo:
	uv run python -m corridor.demo $(LIMIT)

# Load every lockfile opted into bulk project materialization. Pass
# `ARGS="<slug>"` to ingest one explicit project, including opted-out ones.
ingest:
	uv run python -m corridor.docs ingest $(ARGS)

# Browse source documents: `make docs` or `make docs ARGS="page 167 1"`.
docs:
	uv run python -m corridor.docs $(ARGS)

# Regenerate docs/adr/INDEX.md from ADR frontmatter (make check fails when stale).
adr-index:
	uv run python scripts/adr_index.py

# Coordination review UI at http://localhost:8412
queue:
	uv run uvicorn corridor.web.app:app --port 8412 --reload

# LLM extraction over the executed agreements. Needs OPENAI_API_KEY in .env.
agreements:
	uv run python -m corridor.extract_agreement $(ARGS)

# Extract every matrix in a project. Skips documents already extracted at
# this prompt version; --redo replaces their pending Extracted Proposals and
# leaves those with recorded decisions alone. Reads page images with a model, so it
# needs OPENAI_API_KEY:
#   make extract ARGS="nhhip-3c2"
#   make extract ARGS="nhhip-3c2 --redo"
#   make extract ARGS="sh99-grand-parkway --document-sha256=8b93d8b934b8501b5464ff33db9f2e83c2f716b2910c7eb5385dde9d45075a30 --redo"
extract:
	uv run python -m corridor.extract_project $(ARGS)

# Record Inclusion: load a project onto the Project Record (ADR-0029). `make extract` and
# `make minutes` do this on their way out, so this is the backfill for a
# project read before that was true, and the way to finish a load after
# declaring the Current Production Run of a document that held several readings:
#   make admission ARGS="load sh99-grand-parkway"
admission:
	uv run python -m corridor.admission_cli $(ARGS)

# Explicitly select the Current Production Run; extraction never selects "newest":
#   make active-run ARGS="<document-id> <extraction-run-id>"
# Bulk form for the unambiguous case only — declares the single completed
# run of every undeclared document, refusing whole if any document holds
# several (docs/sh99-date-rehearsal.md):
#   make active-run ARGS="--single-run-documents sh99-grand-parkway"
active-run:
	uv run python -m corridor.extraction_runs $(ARGS)

# Run one exact predecessor-successor pair through comparison readback and
# released Automatic Support Update Rules only. Never infers runs or accepts policy
# identity flags:
#   make revision-process ARGS="<predecessor-extraction-run-id> <successor-extraction-run-id>"
revision-process:
	uv run python -m corridor.revision_processing_cli $(ARGS)

# Import key dates from a schedule CSV (technical command remains milestones):
#   make milestones ARGS="sh99-grand-parkway corpus/sh99-milestones.csv RELO-CONSTR"
milestones:
	uv run python -m corridor.milestones $(ARGS)

# Extraction Measurement over exact completed run receipts. Repeat
# --extraction-run once per matrix. A machine reference must travel with its
# author-time scope manifest; prompt/document flags are assertions only. The
# named database must be a disposable copy and is refused if it is production:
#   make eval ARGS="wsdot-9424 gold/wsdot-9424.machine.csv --database-url=<disposable-url> --reference-manifest=gold/wsdot-9424.machine.scope.json --extraction-run=123"
eval:
	uv run python -m corridor.eval $(ARGS)

# Measure a named candidate model against the current one, repeatably. Runs the
# candidate reader over exactly the documents the current runs read, scores both
# against one Reference Dataset, and writes one comparison receipt (current vs
# candidate, per reference). Name the current reading with --current-extraction-run
# once per matrix; the named database must be a disposable copy and is refused if
# it is production. Adoption on a measured win is a human read of the receipt
# (docs/operations/candidate-model-comparison.md):
#   make candidate-model ARGS="wsdot-9424 gold/wsdot-9424.machine.csv --candidate-model=gpt-5.1-vision --database-url=<disposable-url> --reference-manifest=gold/wsdot-9424.machine.scope.json --current-extraction-run=123"
candidate-model:
	uv run python -m corridor.candidate_model $(ARGS)

# Compute dated Constraint Alerts; JSON and diagnostic identifiers stay unchanged.
exceptions:
	uv run python -m corridor.exceptions $(ARGS)

# Prepare a diagnostic disagreement report, or author the explicitly
# semi-independent machine reference plus its required scope manifest:
#   make gold ARGS="wsdot-9424 --author"
gold:
	uv run python -m corridor.gold $(ARGS)

# Measure the known permanent copy chains on the current development corpus and
# freeze representative Report, release, and Extraction Run semantics. Optional
# exact row identities keep a rerun pinned as the database grows:
#   make storage-baseline ARGS="--report-run-id=1 --release-id=1 --extraction-run-id=1"
# Already-sealed representative outputs may be pinned by file when the current
# development database has no retained Report Run or Report Approved for Release.
storage-baseline:
	uv run python -m corridor.storage_baseline_cli $(ARGS)

# Operate the content-addressed store (ADR-0079). `migrate` puts every local
# file under its own digest into the configured backend, idempotently and
# digest-verified; `reconcile` compares the PostgreSQL manifests with the store
# and reports orphans in both directions. Repairs are opt-in:
#   make storage ARGS="migrate"
#   make storage ARGS="reconcile --repair"
#   make storage ARGS="reconcile --remove-unreferenced"
storage:
	uv run python -m corridor.storage_cli $(ARGS)

# Plan first; execute requires the exact manifest digest. Holds and lifts are
# separate attributable commands through CORRIDOR_HUMAN_PRINCIPAL.
retention:
	uv run python -m corridor.retention_cli $(ARGS)

# One-time retirement of legacy development constraint records. Always run `plan`
# first; `retire` requires the exact digest and constraint count (counts.dependencies):
#   make ledger-archive ARGS="plan nhhip-3c2"
#   make ledger-archive ARGS="retire nhhip-3c2 --expected-sha256=<sha> --expected-dependency-count=141"
ledger-archive:
	uv run python -m corridor.legacy_ledger_archive_cli $(ARGS)

# Inspect or run project-level Automatic Support Update under the released rules:
#   make carry-forward ARGS="status nhhip-3c2"
#   make carry-forward ARGS="run nhhip-3c2"
carry-forward:
	uv run python -m corridor.automatic_carry_forward_cli $(ARGS)

# One supervised runtime owns production schedules and recovery. Configure every
# gate-7 field explicitly, then run the supervisor separately from the web app:
#   make due-work ARGS="configure-health <project-slug> --configuration-version=processing-health-v1 --starts-at=2026-08-29T07:00:00+00:00 --cadence=hourly --timezone=UTC --missed-run-policy=latest_only --retention-days=3650 --max-attempts=3 --backoff-seconds=60 --claim-ttl-seconds=300 --deadline-seconds=120 --concurrency-limit=1 --model-token-budget=0 --notification-budget=0"
# New-assignment notification delivery is gate-7 too: nothing is delivered until an
# authorized operator records this, and completing the code enables no real sends.
#   make due-work ARGS="configure-notifications <project-slug> --configuration-version=assignment-notification-v1 --channel=email --starts-at=2026-08-29T07:00:00+00:00 --cadence=hourly --timezone=UTC --missed-run-policy=latest_only --retention-days=3650 --max-attempts=3 --backoff-seconds=60 --claim-ttl-seconds=300 --deadline-seconds=120 --concurrency-limit=1 --model-token-budget=0 --notification-budget=500"
#   make due-work ARGS="configure-publication <project-slug> --configuration-version=report-publication-v1 --provenance-mode=all-supported-sources --prepare-external-pdf --starts-at=2026-08-31T07:00:00+00:00 --cadence=weekly --timezone=UTC --missed-run-policy=latest_only --comparison-window-policy=since_last_released --retention-days=3650 --max-attempts=3 --backoff-seconds=120 --claim-ttl-seconds=1800 --deadline-seconds=1800 --concurrency-limit=1 --model-token-budget=0 --notification-budget=0"
#   make due-work ARGS="supervise --owner=runtime:<worker-id> --poll-seconds=5"
# Bounded operational commands use the same durable interfaces:
#   make due-work ARGS="run-once --owner=runtime:<worker-id>"
#   make due-work ARGS="recover --owner=runtime:<worker-id>"
#   make due-work ARGS="status --project-slug=<project-slug>"
# Enable one connected TxDOT RID/Box source (#350). Every gate-7 field is
# explicit; a sealed rehearsal location is refused before any fetch:
#   make due-work ARGS="configure-discovery <project-slug> --configuration-version=txdot-rid-box-v1 --location-id=txdot-nhhip-3c2-utilities --adapter-identity=txdot-rid-box-v1 --source-manifest-id=nhhip-3c2 --index-url=https://www.txdot.gov/business/road-bridge-maintenance/alternative-delivery/nhhip-3c2/rid.html --rid-link-text=Utilities --authorized-host=www.txdot.gov --authorized-host=txdot.box.com --authorized-host=txdot.app.box.com --authorized-host=app.box.com --authorized-host=public.boxcloud.com --starts-at=2026-08-29T07:00:00+00:00 --cadence=hourly --timezone=UTC --missed-run-policy=latest_only --retention-days=3650 --max-attempts=3 --backoff-seconds=120 --claim-ttl-seconds=600 --deadline-seconds=300 --concurrency-limit=1 --model-token-budget=0 --notification-budget=0"
due-work:
	uv run python -m corridor.due_work_cli $(ARGS)

# The two managed connected-location acts that need a person, plus a read-only
# operations view (#350). Attribution comes from CORRIDOR_HUMAN_PRINCIPAL:
#   make location-discovery ARGS="authorize <project-slug> --reference-key=<key> --doc-type=matrix --registry-id=<id>"
#   make location-discovery ARGS="recover-parse <project-slug> --document-id=<id>"
#   make location-discovery ARGS="view <project-slug>"
location-discovery:
	uv run python -m corridor.location_discovery_cli $(ARGS)

# Capture, replay, or verify the isolated mechanical M8 acceptance-test bundle.
# This tests software behavior; it is not Contract Acceptance of construction.
# Ordinary replay is model-free and requires exact fixture/transformation pins:
#   make m8-acceptance ARGS="replay --fixture=<path> --transformations=<path> --output-dir=<path> --postgres-admin-url=<url> --expected-fixture-sha256=<sha> --expected-transformations-sha256=<sha>"
#   make m8-acceptance ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
m8-acceptance:
	uv run python -m corridor.m8_acceptance_cli $(ARGS)

# Capture the real, pinned SH 99 state read-only, clone it into a newly created,
# disposable PostgreSQL database, then run the exact Record Inclusion command twice.
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

# Compare predecessor and statement Record Inclusion where Applies To is not yet
# known, on two disposable clones.
# A failed receipt never activates; a passing receipt activates normal processing.
#   make event-admission-acceptance ARGS="replay --project-slug=sh99-grand-parkway --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha>"
# Read the effective proof, policy, authority, and permitted operations:
#   make event-admission-acceptance ARGS="status --project-slug=sh99-grand-parkway --database-url=<url>"
# Suspension is append-only and restores the predecessor policy:
#   make event-admission-acceptance ARGS="suspend --project-slug=sh99-grand-parkway --database-url=<url> --reason=<reason> --recorded-by=local:<subject>"
# A lift is a separate attributable human act and still requires current proof:
#   make event-admission-acceptance ARGS="lift --project-slug=sh99-grand-parkway --database-url=<url> --recorded-by=local:<subject>"
event-admission-acceptance:
	uv run python -m corridor.event_admission_acceptance_cli $(ARGS)

# Run the bounded coordinator exercise only on a disposable clone. It verifies the
# prior Record Inclusion bundle first, separates shared operations/backfill time from the
# timed coordinator flow, upgrades only the clone between explicit migration pins,
# and records assistance or failure honestly:
#   make sh99-coordinator-rehearsal ARGS="replay --project-slug=sh99-grand-parkway --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha> --expected-source-migration-head=<released-head> --expected-target-migration-head=<direct-successor-head> --shared-admission-receipt-path=<validation-passed.json> --expected-shared-admission-receipt-sha256=<sha> --approved-shared-state-receipt=<immutable-url> --shared-backfill-elapsed-seconds=291 --output-dir=<new-dir>"
#   make sh99-coordinator-rehearsal ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
sh99-coordinator-rehearsal:
	uv run python -m corridor.sh99_coordinator_rehearsal_cli $(ARGS)

# Publish a Product Test Run only from two independently sealed live-frontend pass bundles and the
# exact restored database baseline; arbitrary success capture JSON is not accepted:
#   make product-proving ARGS="publish-observed --database-baseline-dir=<dir> --database-baseline-manifest-sha256=<sha> --pass-1-dir=<dir> --pass-1-manifest-sha256=<sha> --restore-1-dir=<dir> --restore-1-manifest-sha256=<sha> --pass-2-dir=<dir> --pass-2-manifest-sha256=<sha> --restore-2-dir=<dir> --restore-2-manifest-sha256=<sha> --source-database-url=<url> --output-dir=<new-dir>"
#   make product-proving ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
# Capture and clone-verify the exact local development database before a pass;
# restore requires an explicit exact-target opt-in and re-verifies every public
# schema object, table, and sequence after replacing the database:
#   make product-proving ARGS="database-capture --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha> --expected-migration-head=<head> --output-dir=<new-dir>"
#   make product-proving ARGS="database-restore <bundle-dir> --source-database-url=<url> --postgres-admin-url=<url> --expected-source-database-name=corridor --expected-clean-git-revision=<sha> --expected-migration-head=<head> --expected-manifest-sha256=<sha> --pass-bundle-dir=<dir> --pass-bundle-manifest-sha256=<sha> --restore-receipt-output-dir=<new-dir> --allow-shared-development-restore"
# A terminal failure uses `publish-failure` and `verify-failure`; it can never
# be read through the successful two-pass verifier.
product-proving:
	uv run python -m corridor.product_proving_run_cli $(ARGS)

# Run one current Statement Needing Clarification through the hidden, read-only
# Statement Review Assistant and append a terminal receipt only in a disposable
# database. Needs OPENAI_API_KEY:
#   make evidence-investigator ARGS="<candidate-id> --database-url=<disposable-url>"
evidence-investigator:
	uv run python -m corridor.evidence_investigator_cli $(ARGS)

# Freeze and invisibly run current Statements Needing Clarification, or capture one
# later independent human outcome. Both remain in one explicit disposable
# database. Needs OPENAI_API_KEY for `run`:
#   make evidence-shadow ARGS="--database-url=<disposable-url> run <project-slug> --candidate-id=<id> --selection-rule=operator-declared:<rule> --manifest-path=<new-json>"
#   make evidence-shadow ARGS="--database-url=<disposable-url> capture <shadow-case-id>"
evidence-shadow:
	uv run python -m corridor.evidence_investigator_shadow_cli $(ARGS)

# Grade an explicit shadow run set into immutable JSON and Markdown receipts:
#   make evidence-shadow-eval ARGS="--database-url=<disposable-url> --run=<run-id> --human-scores=<json> --output-dir=<new-dir>"
evidence-shadow-eval:
	uv run python -m corridor.evidence_investigator_evaluation_cli $(ARGS)

# Compare a PDF engine JSON artifact with the frozen page/cell gold contract.
# Holdout runs also require an explicit access log, actor, and reason; see
# gold/pdf/v1/README.md. This is the experiment runner, not a pytest alias:
#   make pdf-eval ARGS="evaluate --gold gold/pdf/v1/dataset.json --predictions=<run.json> --output-json=<metrics.json> --output-report=<metrics.md>"
pdf-eval:
	uv run python -m corridor.pdf_evaluation_cli $(ARGS)

# Record Stage 1 page-routing confusion and OCR error rates against the frozen
# gold membership, alongside the retired character-count comparator:
#   make page-inventory-eval ARGS="--gold=<stage1-gold.json> --run=<routing-run.json> --output=<receipt.json>"
page-inventory-eval:
	uv run python -m corridor.page_inventory_evaluation_cli $(ARGS)

# LLM extraction over coordination meeting notes. Needs OPENAI_API_KEY.
# Bound a run to exact registered notes by repeating --document-id; --redo
# appends a fresh attempt without changing the declared Current Production Run:
#   make minutes ARGS="sh99-grand-parkway --document-id 1435 --document-id 1438 --redo"
minutes:
	uv run python -m corridor.extract_minutes_v5 $(ARGS)

# Build the weekly Coordination Report without re-running document processing:
#   make report ARGS="nhhip-3c2"
report:
	uv run python -m corridor.report $(ARGS)
