.PHONY: clean-test-databases boot up down psql check test-engine-absent image-engine-audit retained-citation-inventory pdf-reader-inspect pdf-reader-node pdf-reader-reproduce pdf-pairs-measure pdf-reader-gold-eval native-matrix-replay textract-replay link-deliveries test-focused test test-full test-slow test-shard test-slow-shard test-timing test-slow-timing test-migrations test-serial corpus demo ingest docs queue agreements extract active-run revision-process milestones exceptions eval candidate-model gold storage-baseline storage identity-audit retention ledger-archive carry-forward due-work location-discovery m8-acceptance sh99-admission-acceptance event-admission-acceptance sh99-coordinator-rehearsal product-proving evidence-investigator evidence-shadow evidence-shadow-eval pdf-eval page-inventory-eval page-inventory-routing-replay render-rasterizer-compare minutes report

TEST_WORKERS ?= 4
TEST_TIMEOUT_SECONDS ?= 600
FOCUSED_TEST_TIMEOUT_SECONDS ?= 30
LOCAL_BROAD_REASON ?=

# Which engine-absent proof `make test-engine-absent` runs: imports, collect
# or the complete suite.
MODE ?= suite

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
	uv run ruff check src/corridor_pdf_reader
	uv run mypy src/corridor_pdf_reader
	uv run python -m compileall -q src/corridor
	uv run pytest tests/test_architecture.py tests/test_source_scan_support.py infra/tests/test_workflow_ordering.py -q

# Tight loop for the exact seam; PostgreSQL starts only on a database connection.
# Example: make test-focused ARGS="tests/test_work_list.py::test_name"
test-focused:
	$(if $(strip $(ARGS)),,$(error ARGS must name at least one test seam))
	uv run python scripts/run_local_tests.py --suite focused --timeout-seconds $(FOCUSED_TEST_TIMEOUT_SECONDS) $(if $(LOCAL_BROAD_REASON),--diagnostic-reason $(LOCAL_BROAD_REASON),) -- -n 1 --dist loadfile $(ARGS)

.PHONY: control-plane
# Separate PostgreSQL operations registry and external receipts (#656).
# Requires explicit role-specific URLs; never uses a default customer URL.
#   make control-plane ARGS="initialize"
#   make control-plane ARGS="register --file environment-registration.json"
# Full input/custody contract: docs/operations/customer-environments.md.
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
test-timing:
	@mkdir -p out/timing
	CORRIDOR_LOCAL_BROAD_REASON=performance-investigation uv run pytest -n $(TEST_WORKERS) --dist worksteal -m "not slow" \
	  --durations=50 --durations-min=0.5 --junitxml=out/timing/non-slow.xml

# Optional measurement of the slow complement, with its actual scheduler.
test-slow-timing:
	@mkdir -p out/timing
	CORRIDOR_LOCAL_BROAD_REASON=performance-investigation uv run pytest -n $(TEST_WORKERS) --dist loadfile -m "slow and not migration" \
	  --durations=50 --durations-min=0.5 --junitxml=out/timing/slow.xml

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
#   make gold ARGS="<new-unspent-project> --author"
# Existing first-write references refuse re-authoring. For a justified new
# unspent PDF scope, select the independent native-cell recipe explicitly:
#   make gold ARGS="<project> --author --method=native-pdf-cell-grid --directory=out/references"
# Read-only regeneration verifies its recorded native source/configuration:
#   make gold ARGS="<project> --replay <native-reference.csv>"
# Archived CSV evaluation does not regenerate it; see docs/operations/machine-reference-methods.md.
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
# Dry run by default; --apply drops. Never touches the configured development
# database, a database with an open connection, or a name it does not recognise.
clean-test-databases:
	uv run python scripts/clean_test_databases.py $(ARGS)

storage:
	uv run python -m corridor.storage_cli $(ARGS)

# The identity and authorization export (#531): every enrollment, sign-in,
# sign-out, designation change, and deprovisioning act, oldest first. Resume a
# previous export with the id it ended on; nothing else narrows it.
#   make identity-audit ARGS="--format=csv --after-id=9100"
identity-audit:
	uv run python -m corridor.identity_audit_cli $(ARGS)

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

# Render corpus pages under both rasterizers and record the comparison with
# its declared tolerances (#735). An explicit experiment outside pytest and
# CI; it reads the corpus content store and takes minutes:
#   make render-rasterizer-compare ARGS="--output artifacts/render-rasterizer-comparison/735-corpus-render-comparison.json"
render-rasterizer-compare:
	uv run python -m corridor.render_rasterizer_comparison $(ARGS)

# Decide the frozen Stage 1 routing pages again from the reader-backed Page
# Inventory and record every difference from the incumbent run and the gold
# labels (#734). An experiment runner, not a pytest alias; the holdout family
# needs an actor and a reason, appended to gold/pdf/v1/holdout-access.jsonl:
#   make page-inventory-routing-replay ARGS="--output-dir artifacts/pdf-reader-page-inventory --holdout-actor <actor> --holdout-reason <reason>"
page-inventory-routing-replay:
	uv run python scripts/page_inventory_routing_replay.py $(ARGS)

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

# ---- The imported paired-rendition reader (#729) -----------------------------
# Print a stored Document Rendition as the reader sees it: pages, tables,
# cells with their semantics-tier IDs, text outside every table, clipped runs.
# The read runs in a PDFium-isolated child process (corridor_pdf_reader.execution).
# Address the rendition by content digest through the storage interface, or by path:
#   make pdf-reader-inspect ARGS="--sha256 <sha256> --pages 1 2"
#   make pdf-reader-inspect ARGS="--file corpus/files/<sha256>.pdf --json"
pdf-reader-inspect:
	uv run python -m corridor_pdf_reader.rendition_cli $(ARGS)

# The answer-key printer's number-format library (`ssf`), from the committed
# package-lock.json. Needed by the reproduction only; CI never runs it.
pdf-reader-node:
	cd src/corridor_pdf_reader/paired_trial && npm ci --ignore-scripts --no-audit --no-fund

# The 333-pair reproduction of loop-020: verify the corpus digests, build the
# answer keys and compare them with the retained key digests, read every pair
# with the measured engine, score the development set and the spent holdout,
# tally, and write a receipt carrying the configuration identity. An explicit
# experiment outside pytest and CI (ADR-0008); needs the reference corpus at
# TRUE_PAIRS_ROOT, `make pdf-reader-node`, and tens of minutes:
#   make pdf-reader-reproduce ARGS="--output out/pdf-reader/reproduction-2026-09-06 --retain"
# `--retain` copies the receipt set into src/corridor_pdf_reader/receipts/ and
# appends the holdout access to bootstrap/LOOP-LOG.md (ADR-0008).
TRUE_PAIRS_ROOT ?= /Users/bryceharmon/Desktop/utility-conflict-matrices/PDF-Spreadsheet-Pairs/true-pairs/exact
pdf-reader-reproduce:
	mkdir -p src/corridor_pdf_reader/tmp
	TRUE_PAIRS_ROOT=$(TRUE_PAIRS_ROOT) uv run --group pdf-reader-experiment python -m corridor_pdf_reader.reproduction $(ARGS)

# The paired-rendition Extraction Measurement (#731): score a named
# configuration (`frozen-reader`, `native-segments-v1`, `drawn-grid`) against the registered
# Reference Dataset in gold/pdf-pairs/v1 and write a receipt that keeps pair,
# page and cell measures apart and development and holdout apart. The holdout
# is spent (ADR-0008): it is read only with --include-holdout, an actor and a
# reason, and every access is appended to gold/pdf-pairs/v1/holdout-access.jsonl.
# An explicit experiment outside CI; needs the corpus at TRUE_PAIRS_ROOT and
# `make pdf-reader-node`:
#   make pdf-pairs-measure ARGS="--configuration frozen-reader --output out/pdf-pairs/<run>"
#   make pdf-pairs-measure ARGS="--configuration drawn-grid --output out/pdf-pairs/<run> --keys <key> ..."
# `--retain baseline|failure-proof|measurement` copies the receipt set into
# gold/pdf-pairs/v1/receipts/<run>/ and indexes it in gold/pdf-pairs/v1/receipts.json.
pdf-pairs-measure:
	mkdir -p src/corridor_pdf_reader/tmp
	TRUE_PAIRS_ROOT=$(TRUE_PAIRS_ROOT) uv run --group pdf-reader-experiment python -m corridor_pdf_reader.measurement $(ARGS)

# The frozen reader through the existing PDF evaluation contract (#731): read
# the gold/pdf/v1 documents from the content store, write an engine run in the
# contract's shape, and evaluate it with `corridor.pdf_evaluation_cli`. The
# holdout family is refused without the ledger flags, as for `make pdf-eval`:
#   make pdf-reader-gold-eval ARGS="--output out/pdf-reader/gold-v1 --include-holdout --holdout-actor <actor> --holdout-reason <reason>"
pdf-reader-gold-eval:
	uv run python -m corridor_pdf_reader.gold_evaluation $(ARGS)

# Offline native matrix mapping regression (#737). Verifies the seven retained
# answer manifests, all 20 measured 110-dpi images, source PDFs and machine CSVs;
# replays their raw structures through the actual adapter, commits only in a
# guarded disposable database, and compares every retained field/row decision
# separately from historical source_ref multiplicity matching. WSDOT 9540 is
# spent; this is not a new generalization score. No model/AWS calls or production
# selection. The local PostgreSQL 16 admin URL provisions and drops a new DB;
# the configured shared database is never migrated. Explicit experiment, not CI:
#   make native-matrix-replay ARGS="--output <new-dir> --postgres-admin-url <local-admin-url>"
# Or pass --postgres-admin-url-env CORRIDOR_MEASUREMENT_POSTGRES_URL.
# Optional --results-root relocates the same digest-pinned retained answer sets.
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
# Replay retained Textract responses through the adapter's normalizer, twice
# each, and write a receipt of raw-response and normalized-reading digests
# (ADR-0094: exact replay is the retained response, never a fresh call). An
# explicit experiment outside pytest and CI over the 116-entry experiment
# cache, which stays in the standalone worktree and is only read; the retained
# lane reads under the same results root supply each raster's page frame. CI
# replays only the four committed fixtures (tests/test_textract_adapter_replay.py).
# No AWS call is made by this target or by anything under textract_adapter.
#   make textract-replay ARGS="--output out/textract/experiment-cache-replay-2026-09-06.json --retain"
TEXTRACT_RESULTS ?= /Users/bryceharmon/Desktop/pdf-reader-comparison-textract/results
textract-replay:
	uv run python -m corridor_pdf_reader.textract_adapter.replay --cache $(TEXTRACT_RESULTS)/textract-cache --reads $(TEXTRACT_RESULTS) $(ARGS)
