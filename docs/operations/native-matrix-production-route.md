# Production Matrix extraction (#766)

`make extract` and the scheduled processing pass route PDF matrices through
`run_selected_native_matrix`. A spreadsheet still uses the deterministic
workbook reader. The native PDF reader records `matrix_structure_ids_v1` and
`native-matrix-source-fields-v1`; the retired `matrix_tiered_v4` receipt is
never used for a new native reading.

## Missing selection is a Processing Failure

An absent, disabled, stale, foreign-deployment or out-of-scope selection
produces a failed Extraction Run naming the selection refusal, with no
rendering or outbound request. There is no fallback. Selection is checked
before resume too: a completed earlier run cannot hide a subsequently disabled
selection. Resume requires the same complete pipeline configuration, not just
the same prompt version. `--redo` appends a new run with a new attempt key.

The native pipeline owns the atomic Source Fact append and its Extraction Run.
The outer project, single-document and acceptance adapters carry that exact
run through; they do not append it again. Successful zero-row readings retain
the same ownership. Selection authorizes source capture only. Both the old
challenger marker and the deployed native marker remain excluded from legacy
Current Production Run selection and automatic Record Inclusion.

## Supply the deployment inputs

Set `CORRIDOR_ENVIRONMENT` to the deployment named by its selection, and
`CORRIDOR_NATIVE_MATRIX_RUNTIME_FILE` to an operator-provided JSON file. Its
outer fields are:

| Field | Meaning |
|---|---|
| `deployment` | Must equal `CORRIDOR_ENVIRONMENT`. |
| `authorization` | The existing `ExperimentScope.as_dict()` or `CustomerAuthorization.as_dict()` record, including its `kind`. This adapter creates neither. |
| `request` | The `RequestBoundary.as_dict()` fields, including the exact project slug, source class, purpose, stage and measured model controls. |
| `budget` | `max_calls`, `max_pages`, `max_total_tokens`, within the authorization's ceilings. |
| `source_sha256s` | The exact source allowlist, matching the selected scope. |
| `campaign` | The operator's identity for this processing invocation. |
| `qualification_policy_sha256` | Optional registered measurement-policy identity when the accepted configuration names one. |

The record and request shapes are defined by
[`native_provider_boundary.py`](../../src/corridor/native_provider_boundary.py).
The existing outbound boundary validates them before any request. Its project
must also match the actual Document's project. `OPENAI_API_KEY` and
`OPENAI_BASE_URL` supply the transport, with the endpoint checked against the
provider posture. Raw clients passed for the old extractor cannot replace this
boundary or silently change the measured model.

`CORRIDOR_NATIVE_MATRIX_OUTPUT_DIR` defaults to `out/native-matrix`. Each
attempt uses a new directory and idempotency key. A project pass shares one
provider boundary and budget across its documents, and closes its transport on
exit. These existing counters are process-local; they are not a durable budget
across restarts or separate operator invocations.

To prepare the exact configuration for the existing acceptance/selection
commands, in the deployment that will run extraction:

```bash
make pipeline-qualification ARGS="configuration --output out/native-configuration.json"
```

This writes a new file, prints its digest and implementation revision, and
sends no model request or database write. It refuses overwriting an existing
file. The maintainer's separate `accept` and `select` commands must name these
exact bytes and their declared scope (ADR-0095). Old acceptances and measured
receipts stay unchanged. This implementation does not create a real deployment
selection or grant customer processing authorization.

The container records its build commit through `CORRIDOR_CODE_REVISION` and
ships the reader prompt, render profiles and provider-posture document. It
does not require Git at runtime. Configuration identities include platform and
package versions, so a selection made on a different runtime does not silently
authorize the container.

## Retirement proof

The legacy geometry and structure/transcription flow are removed.
`artifacts/pdf-engine-retirement/legacy-prompts/manifest.json` retains all four
old prompt files byte for byte, under non-executable archival paths. Old runs,
Source Segments, reference CSVs and released artifacts are unchanged. Legacy
reference *reading* remains supported; new authoring uses the native method.

`make test-engine-absent MODE=suite` is the explicit #766 acceptance, separate
from routine local broad testing. It prepares both isolated environments,
verifies engine/import/executable absence, and runs the complete suite. Its
dedicated pytest flag is refused in the ordinary environment and rechecks
both isolated environments before entering the suite. The regular local broad
guard remains in force.

`tests/test_matrix_retirement_e2e.py` carries the public WSDOT 9540 gas PDF and
its frozen mapping answer, so CI can ingest, extract and cite it without an
external corpus directory. Only HTTP is doubled, and the test runs extraction
as `corridor_worker`. A seeded legacy citation retains its exact quotation and
passes retained-reading integrity with the original document digest. This is
not fresh reconstruction by the retired reader or an inventory of customer
history. The actual retained-population inventory from #741 remains separate.

`make image-engine-audit` builds the image and checks both Python environments,
the executable and OS package inventory, PDFium notices, and native runtime
configuration. Its JSON and Markdown receipts are the basis for #461's
retirement decision and the corresponding #535 criterion.
