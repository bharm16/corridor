.PHONY: clean-test-databases boot up down psql check test-engine-absent image-engine-audit retained-citation-inventory pdf-reader-inspect pdf-reader-node pdf-reader-reproduce pdf-pairs-measure pdf-reader-gold-eval native-matrix-replay textract-replay link-deliveries test-focused test test-full test-slow test-shard test-slow-shard test-timing test-slow-timing test-migrations test-serial corpus demo ingest docs queue agreements extract active-run revision-process milestones exceptions eval candidate-model gold storage-baseline storage identity-audit retention ledger-archive carry-forward due-work location-discovery m8-acceptance sh99-admission-acceptance event-admission-acceptance sh99-coordinator-rehearsal product-proving evidence-investigator evidence-shadow evidence-shadow-eval pdf-eval page-inventory-eval page-inventory-routing-replay render-rasterizer-compare minutes report

TEST_WORKERS ?= 4
TEST_TIMEOUT_SECONDS ?= 600
FOCUSED_TEST_TIMEOUT_SECONDS ?= 30
LOCAL_BROAD_REASON ?=

.DEFAULT_GOAL := help

# Every target with the summary written above it; bare `make` ran whichever
# target happened to be written first. The summary says which command this
# is, and `make <target> ARGS=--help` prints that command's own contract,
# which is why the Makefile no longer carries it.
.PHONY: help
help:
	@echo 'make <target> ARGS=--help prints a command'"'"'s own arguments and examples.'
	@awk '/^# ?-{3,}/ { next } \
	  /^#/ { summary = summary (summary ? " " : "") substr($$0, 3); next } \
	  /^\./ { next } \
	  /^[A-Za-z_][A-Za-z0-9_]* *[?:+]?=/ { next } \
	  /^[A-Za-z0-9][A-Za-z0-9._-]*:/ { \
	    sub(":.*", "", $$0); \
	    if (match(summary, /\. /)) summary = substr(summary, 1, RSTART); \
	    printf "  %-30s %s\n", $$0, summary; summary = ""; next } \
	  { summary = "" }' $(MAKEFILE_LIST)

.PHONY: pilot-measurement
# Read #558 product events and immutable receipts into a governed #532 report.
pilot-measurement:
	uv run python -m corridor.pilot_measurement_cli $(ARGS)

.PHONY: pilot-report pilot-checkpoint
# Reproduce economics from native measurement receipts and predeclared evidence.
pilot-report:
	uv run python -m corridor.pilot_report_cli report $(ARGS)

# Produce one evidence-backed finding per predeclared pilot criterion.
pilot-checkpoint:
	uv run python -m corridor.pilot_report_cli checkpoint $(ARGS)

# Which engine-absent proof `make test-engine-absent` runs: imports, collect
# or the complete suite.
MODE ?= suite

# One command from a clean clone.
boot:
	uv sync
	uv sync --project workers/render --frozen
	docker compose up -d --wait
	CORRIDOR_LEGACY_DEV_LOGIN=1 uv run alembic upgrade head

# Start the stack the tests and the coordination UI connect to.
up:
	docker compose up -d --wait

# Stop the stack.
down:
	docker compose down

# A psql prompt on the configured development database.
psql:
	docker compose exec postgres psql -U corridor -d corridor

# Fast source and architecture checks with no model or external service calls.
check:
	uv run ruff check src/corridor
	uv run ruff check src/corridor_pdf_reader
	uv run mypy src/corridor_pdf_reader
	uv run python -m compileall -q src/corridor
	uv run pytest tests/test_architecture.py tests/test_source_scan_support.py infra/tests/test_workflow_ordering.py -q

# Bounded infrastructure assertions in their own locked environment (no AWS calls).
# Install once with `uv sync --project infra --frozen` before this target.
# ARGS may name one synthesized-template acceptance seam.
.PHONY: test-infra
test-infra:
	cd infra && uv run --frozen python -m pytest $(if $(strip $(ARGS)),$(ARGS),tests) -q

.PHONY: deployment-bootstrap
# Configure synthetic deployment databases with the migration credential.
deployment-bootstrap:
	uv run python -m corridor.deployment_bootstrap $(ARGS)

# Tight loop for the exact seam; PostgreSQL starts only on a database connection.
# Example: make test-focused ARGS="tests/test_work_list.py::test_name"
test-focused:
	$(if $(strip $(ARGS)),,$(error ARGS must name at least one test seam))
	uv run python scripts/run_local_tests.py --suite focused --timeout-seconds $(FOCUSED_TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- -n 1 --dist loadfile $(ARGS)

.PHONY: control-plane
.PHONY: legacy-history environment-disposition
# Inventory, capture, read and reverse retained legacy history and bounded native migrations.
legacy-history:
	uv run python -m corridor.legacy_history_cli $(ARGS)

# Explicit operator inventory/export/plan/execute/rehearsal; no provider action by default.
environment-disposition:
	uv run python -m corridor.environment_disposition_cli $(ARGS)
.PHONY: shadow-comparison
.PHONY: shadow-processing activation
# Provision an isolated baseline-adopted shadow project, capture a revision, or export its sealed output.
shadow-processing:
	uv run python -m corridor.shadow_cli $(ARGS)

# Validate or freeze explicit deployment evidence; processing checks the receipt at runtime.
activation:
	uv run python -m corridor.activation_cli $(ARGS)

# Freeze predicted changes before loading the successor working reference.
shadow-comparison:
	uv run python -m corridor.shadow_comparison_cli $(ARGS)

.PHONY: m365-replay
# Replay recorded Graph pages into an existing synthetic project; never
# connects a tenant.
m365-replay:
	uv run python -m corridor.m365_replay $(ARGS)

.PHONY: email-source
.PHONY: contacts
# Retained contact imports/resolution; corrections use an authenticated web
# session.
contacts:
	uv run python -m corridor.project_contacts_cli $(ARGS)

# Inspect retained project-bound MIME or replay a strict response without a
# model call.
email-source:
	uv run python -m corridor.email_source_cli $(ARGS)

# Separate PostgreSQL operations registry and external receipts (#656).
control-plane:
	uv run python -m corridor.control_plane_cli $(ARGS)

# Broad developer gate. Run after a broad change, not after every edit.
# This is the non-slow subset of test-full; do not run both on one revision.
test:
	uv run python scripts/run_local_tests.py --suite test --timeout-seconds $(TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- -n $(TEST_WORKERS) --dist worksteal -m "not slow"

# Complete manual/scheduled gate. PR workflows use the scoped gates below.
test-full:
	uv run python scripts/run_local_tests.py --suite full --timeout-seconds $(TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- -n $(TEST_WORKERS) --dist worksteal

# Exhaustive non-migration complement to test.
test-slow:
	uv run python scripts/run_local_tests.py --suite slow --timeout-seconds $(TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- -n $(TEST_WORKERS) --dist loadfile -m "slow and not migration"

# One balanced slice of the non-slow suite. Private Linux CI runners have
# two cores; CI sets TEST_WORKERS=2 while local machines may use more.
# Every file belongs to one shard. The wrapper records JUnit and elapsed-time
# receipts under out/test-feedback and uses the CI-provided timing weights.
test-shard:
	@if [ -z "$(strip $(SHARD))" ] || [ -z "$(strip $(SHARDS))" ]; then \
	  echo 'SHARD and SHARDS are required' >&2; exit 2; fi
	uv run python scripts/run_test_gate.py --suite pytest --shards $(SHARDS) --shard $(SHARD) --workers $(TEST_WORKERS)

# Keep each slow module on one worker so expensive module fixtures run once.
# The ordinary suite retains worksteal to rebalance independently cheap tests.
test-slow-shard:
	@if [ -z "$(strip $(SHARD))" ] || [ -z "$(strip $(SHARDS))" ]; then \
	  echo 'SHARD and SHARDS are required' >&2; exit 2; fi
	uv run python scripts/run_test_gate.py --suite slow --shards $(SHARDS) --shard $(SHARD) --workers $(TEST_WORKERS)

# Optional local diagnosis. Required CI already publishes measured per-file
# timings and feeds them into the next run; routine changes need no separate
# whole-suite timing pass or duration-only follow-up PR (ADR-0096).
#   make test-timing
#   uv run python scripts/test_timing.py out/timing/non-slow.xml
# Bootstrap weights can still be refreshed explicitly with --write.
# This is a broad local run and goes through the wrapper for its timeout,
# process-group cleanup and receipt; `--maxfail=0` keeps measuring past a
# failure, because partial durations and a partial JUnit file measure nothing.
test-timing:
	@mkdir -p out/timing
	uv run python scripts/run_local_tests.py --suite test --timeout-seconds $(TEST_TIMEOUT_SECONDS) --diagnostic-reason performance-investigation -- -n $(TEST_WORKERS) --dist worksteal -m "not slow" \
	  --maxfail=0 --durations=50 --durations-min=0.5 --junitxml=out/timing/non-slow.xml

# Optional measurement of the slow complement, with its actual scheduler.
test-slow-timing:
	@mkdir -p out/timing
	uv run python scripts/run_local_tests.py --suite slow --timeout-seconds $(TEST_TIMEOUT_SECONDS) --diagnostic-reason performance-investigation -- -n $(TEST_WORKERS) --dist loadfile -m "slow and not migration" \
	  --maxfail=0 --durations=50 --durations-min=0.5 --junitxml=out/timing/slow.xml

# Prove the retirement, not merely describe it: build a second environment
# that never receives PyMuPDF (and therefore neither the `pymupdf` nor the
# `fitz` import name) or pytesseract, strip every PATH entry offering the
# `tesseract` executable, verify that absence, and only then run the tests in
# it (#741, ADR-0094). MODE=imports names the modules that must still move,
# MODE=collect adds conftest imports, MODE=suite is the acceptance criterion.
# The receipt lands under artifacts/pdf-engine-retirement/.
test-engine-absent:
	uv run python scripts/engine_absent_suite.py --mode $(MODE) --workers $(TEST_WORKERS)

# Build the deployable image and prove neither retired engine is in it: no
# pymupdf/fitz/pytesseract in either environment, no `tesseract` on PATH, no
# tesseract-ocr apt package. Writes the audit receipt #461 closes against.
# This contract stays here rather than in the module's `--help`: the auditor
# deliberately spells neither engine's name in its own source, and
# `test_only_allowlisted_modules_still_use_pymupdf_or_tesseract` reads it.
image-engine-audit:
	uv run python scripts/audit_image_engines.py

# Inventory the persisted citations that retained accepted decisions and
# released artifacts actually reference, with each one's original document
# digest, reader and configuration identity, and locator scheme (#741).
retained-citation-inventory:
	uv run python scripts/retained_citation_inventory.py

# Collect only the migration contract and run independent cases on two workers.
# The existing experimental-database identity guard remains intact.
test-migrations:
	uv run python scripts/run_test_gate.py --suite migration --shards 1 --shard 1 --workers 2

# Single-process fallback for debugger use and scheduler diagnosis.
test-serial:
	uv run python scripts/run_local_tests.py --suite full --timeout-seconds $(TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- $(ARGS)

# Resolve corpus/manifest.yaml to files on disk.
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
# The coordination UI. It connects as `corridor_web`, which #680 restricted to
# the live-pilot route surface: the frozen legacy screens (/ledger, /queue,
# /statements, /operations, /reports) can no longer read their tables through
# it. `make boot` creates the opt-in legacy development login, so a clone that
# needs those screens runs them as that capability instead:
#   WEB_DATABASE_URL=postgresql+psycopg://corridor_legacy_dev:corridor_legacy_dev@localhost:5433/corridor make queue
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

# Fill `documents.source_delivery_id` for Documents registered before every
# intake path wrote it (#687). Every intake path that holds a delivery now
# writes the link in the same transaction, so this is only for the history:
# it links what a retained inbound-message record or a capture receipt proves,
# and leaves every other document unknown rather than guessing a delivery from
# a time. Idempotent; omit the slug to sweep every project:
#   make link-deliveries ARGS="link sh99-grand-parkway"
link-deliveries:
	uv run python -m corridor.document_delivery_backfill $(ARGS)

# Explicitly select the Current Production Run; extraction never selects "newest":
#   make active-run ARGS="<document-id> <extraction-run-id>"
# Bulk form for the unambiguous case only — declares the single completed
# run of every undeclared document, refusing whole if any document holds
# several (docs/sh99-date-rehearsal.md):
#   make active-run ARGS="--single-run-documents sh99-grand-parkway"
active-run:
	uv run python -m corridor.extraction_runs $(ARGS)

# Run one exact predecessor-successor pair through comparison readback and
# released Automatic Support Update Rules only.
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
# semi-independent machine reference plus its required scope manifest.
gold:
	uv run python -m corridor.gold $(ARGS)

# Measure the known permanent copy chains on the current development corpus
# and freeze representative Report, release, and Extraction Run semantics.
storage-baseline:
	uv run python -m corridor.storage_baseline_cli $(ARGS)

# Drop the scratch PostgreSQL databases a development machine accumulates.
clean-test-databases:
	uv run python scripts/clean_test_databases.py $(ARGS)

# Operate the content-addressed store (ADR-0079).
storage:
	uv run python -m corridor.storage_cli $(ARGS)

# The identity and authorization export (#531).
identity-audit:
	uv run python -m corridor.identity_audit_cli $(ARGS)

# Plan first; execute requires the exact manifest digest.
retention:
	uv run python -m corridor.retention_cli $(ARGS)

# One-time retirement of legacy development constraint records.
ledger-archive:
	uv run python -m corridor.legacy_ledger_archive_cli $(ARGS)

# Inspect or run project-level Automatic Support Update under the released
# rules.
carry-forward:
	uv run python -m corridor.automatic_carry_forward_cli $(ARGS)

# One supervised runtime owns production schedules and recovery.
due-work:
	uv run python -m corridor.due_work_cli $(ARGS)

# The two managed connected-location acts that need a person, plus a read-only
# operations view (#350).
location-discovery:
	uv run python -m corridor.location_discovery_cli $(ARGS)

# Capture, replay, or verify the isolated mechanical M8 acceptance-test
# bundle.
m8-acceptance:
	uv run python -m corridor.m8_acceptance_cli $(ARGS)

# Capture the real, pinned SH 99 state read-only, clone it into a newly
# created, disposable PostgreSQL database, then run the exact Record Inclusion
# command twice.
sh99-admission-acceptance:
	uv run python -m corridor.sh99_admission_acceptance_cli $(ARGS)

# Compare predecessor and statement Record Inclusion where Applies To is not
# yet known, on two disposable clones.
event-admission-acceptance:
	uv run python -m corridor.event_admission_acceptance_cli $(ARGS)

# Run the bounded coordinator exercise only on a disposable clone.
sh99-coordinator-rehearsal:
	uv run python -m corridor.sh99_coordinator_rehearsal_cli $(ARGS)

# Publish a Product Test Run only from two independently sealed live-frontend
# pass bundles and the exact restored database baseline.
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
pdf-eval:
	uv run python -m corridor.pdf_evaluation_cli $(ARGS)

# Record Stage 1 page-routing confusion and OCR error rates against the frozen
# gold membership.
page-inventory-eval:
	uv run python -m corridor.page_inventory_evaluation $(ARGS)

# Render corpus pages under both rasterizers and record the comparison with
# its declared tolerances (#735).
render-rasterizer-compare:
	uv run python -m corridor.render_rasterizer_comparison $(ARGS)

# Decide the frozen Stage 1 routing pages again from the reader-backed Page
# Inventory and record every difference from the incumbent run and the gold
# labels (#734).
page-inventory-routing-replay:
	uv run python scripts/page_inventory_routing_replay.py $(ARGS)

# LLM extraction over coordination meeting notes. Needs OPENAI_API_KEY.
minutes:
	uv run python -m corridor.minutes_source_cli project $(ARGS)

.PHONY: minutes-source
# Inspect exact minutes references or replay a fixture/provider response.
minutes-source:
	uv run python -m corridor.minutes_source_cli $(ARGS)

# Build the weekly Coordination Report without re-running document processing:
#   make report ARGS="nhhip-3c2"
report:
	uv run python -m corridor.report $(ARGS)

# ---- The imported paired-rendition reader (#729) -----------------------------
# Print a stored Document Rendition as the reader sees it: pages, tables,
# cells with their semantics-tier IDs, text outside every table, clipped runs.
pdf-reader-inspect:
	uv run python -m corridor_pdf_reader.rendition_cli $(ARGS)

# The answer-key printer's number-format library (`ssf`), from the committed
# package-lock.json. Needed by the reproduction only; CI never runs it.
pdf-reader-node:
	cd src/corridor_pdf_reader/paired_trial && npm ci --ignore-scripts --no-audit --no-fund

TRUE_PAIRS_ROOT ?= /Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs/exact
# The 333-pair reproduction of loop-020.
pdf-reader-reproduce:
	mkdir -p src/corridor_pdf_reader/tmp
	TRUE_PAIRS_ROOT=$(TRUE_PAIRS_ROOT) uv run --group pdf-reader-experiment python -m corridor_pdf_reader.reproduction $(ARGS)

# The paired-rendition Extraction Measurement (#731).
pdf-pairs-measure:
	mkdir -p src/corridor_pdf_reader/tmp
	TRUE_PAIRS_ROOT=$(TRUE_PAIRS_ROOT) uv run --group pdf-reader-experiment python -m corridor_pdf_reader.measurement $(ARGS)

# The frozen reader through the existing PDF evaluation contract (#731).
pdf-reader-gold-eval:
	uv run python -m corridor_pdf_reader.gold_evaluation $(ARGS)

# Offline native matrix mapping regression (#737).
native-matrix-replay:
	uv run python -m corridor.native_matrix_measurement $(ARGS)

# Explicit pipeline maintenance. `shadow` uses only the seven historical
# retained-answer cases and a new disposable database. `accept` records the
# maintainer's own acceptance as a selection basis (ADR-0095); it is not a gate
# result and selects nothing. `select` is a separate human maintenance act on
# exactly one basis, a passing `gate` or a recorded `accept`; no measurement
# invokes it and none of these changes a default.
# Example: make pipeline-qualification ARGS="shadow --output <new-dir> --postgres-admin-url-env <name> --actor local:<human>"
# Example: make pipeline-qualification ARGS="accept --acceptance docs/operations/native-matrix-maintainer-acceptance.json --project <slug> --configuration <run>/configuration.json --actor local:<human>"
# Example: make pipeline-qualification ARGS="select --acceptance <id> --initial --actor local:<human> --reason '<why>'"
.PHONY: pipeline-qualification
pipeline-qualification:
	uv run python scripts/pipeline_qualification.py $(ARGS)


# ---- The Textract adapter (#732) ---------------------------------------------
TEXTRACT_RESULTS ?= /Users/bryceharmon/Desktop/pdf-reader-comparison-textract/results
# Replay retained Textract responses through the adapter's normalizer, twice
# each, and write a receipt of raw-response and normalized-reading digests.
textract-replay:
	uv run python -m corridor_pdf_reader.textract_adapter.replay --cache $(TEXTRACT_RESULTS)/textract-cache --reads $(TEXTRACT_RESULTS) $(ARGS)
