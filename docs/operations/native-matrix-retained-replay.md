# Native matrix retained-answer replay (#737)

`make native-matrix-replay` reuses the seven preserved semantics runs through
the native matrix adapter. It verifies the committed manifest digests before
parsing the answers, then verifies every answer file, all 20 page images, the
seven source PDFs, and both machine CSVs. The membership and hashes are in
[`gold/native-matrix/v1/dataset.json`](../../gold/native-matrix/v1/dataset.json).
The preserved experiment directories can be relocated with `--results-root`;
their bytes must remain identical. Source PDFs must exist at their recorded
paths and match their pinned digests; a missing source fails by filename.

The recorded client checks the unchanged `matrix_structure_ids_v1` prompt,
strict schema, exact ID listing, and retained 110-dpi image before returning
each raw structure answer. The configuration remains `gpt-5.6-luna`, effort
`none`, image detail `original`, with native reconstruction at `tagged`/36 dpi.
It makes zero new model or AWS calls. Historical call and token counts remain
under `historical_usage`; replayed answers and zero new provider usage are
reported separately. No historical provider cost is inferred from token counts.

Run it with a local PostgreSQL 16 admin URL and a new output directory:

```bash
make native-matrix-replay ARGS="--output <new-receipt-directory> --postgres-admin-url <local-admin-url>"
```

`--postgres-admin-url-env CORRIDOR_MEASUREMENT_POSTGRES_URL` instead reads the
URL from that environment variable, keeping its value out of the command log.

The common disposable-database provisioner creates, migrates and drops the
measurement database. It does not migrate a shared development database.
The actual adapter appends Source Facts and reference-based Extracted Proposals;
the driver commits them and reads them back in a new session. Every stored Fact
is replayed from its ordered source links using the sealed native reading.
The output retains per-document results and a receipt with exact source,
manifest, implementation, configuration, reading and output digests.

The receipt keeps these measures separate:

- Every retained page structure, listing, mapping, refusal and confidence,
  and every row's fields, local references, disposition and reason must match.
  Extra provenance at the page envelope does not change that comparison.
- Every mapped row field has one Fact materialization outcome. Extracting a
  row does not imply that all of its fields can become typed Facts. Persisted
  proposal fields and their ordered source bindings are checked independently.
- The machine CSV comparison counts `source_ref` multiplicities: 162 for
  WSDOT 9424 and 192 for the pooled WSDOT 9540 listings. It does not establish
  scoped document/page/row occurrence identity, independent field accuracy,
  or correctness of `critical`. Repeated identifiers remain repeated entries.

`required_outcomes_pass` and `full_reading_parity` are separate. The command
can return `passed_with_diagnostic_differences` only when the required rows,
stored values and bindings match and the sole full-reading difference is
additional adjacent copies of an already-retained unmapped heading. A new
heading, removal, reordering, changed field, source reference, raw structure,
listing, refusal, confidence, table or header metadata still fails. The full
comparison remains false with its exact differences and current header spans
retained. This narrowly classified diagnostic is not a production selection.

WSDOT 9540 is already spent. This is historical retained-answer/reference
replay, not a new generalization score. Passing it does not select production;
that remains with #447/#739. No evaluation guard or current prompt path changes.

## Retained result, 2026-09-08

The [retained receipt](../../gold/native-matrix/v1/receipts/2026-09-08.json)
records `passed_with_diagnostic_differences`, `required_outcomes_pass: true`,
and `full_reading_parity: false`. All seven actual adapter runs committed in a
disposable database and passed fresh-session proposal, source-binding, Fact
replay and stored row-receipt checks. The database was dropped afterward.

| Reference population | Body rows matched exactly | Extracted rows | Facts replayed | Non-extracted field outcomes | Non-ISO date refusals |
|---|---:|---:|---:|---:|---:|
| WSDOT 9424 retained native reading | 265 | 162 | 933 | 200 | 11 |
| WSDOT 9540 retained native readings | 201 | 192 | 1,533 | 24 | 0 |

The two historical source-reference counters match 162/162 and 192/192.
All 2,701 mapped field outcomes remain separately accounted for: 2,466
materialized, 224 on non-extracted rows, and 11 refused. The raw structures,
exact listings and measured images matched the recorded client's assertions;
the page/table/header choices, confidence, refusals, row fields, local
references, dispositions and reasons also matched.

The gas, OPL, sewer and water readings each emit one extra adjacent copy of
the unmapped `FRANCHISE (F) ...` heading. The current native header cell spans
two columns, so the unchanged semantics code encounters that wording for both
unmapped columns. The receipt preserves the full exact differences and each
current header cell's text, row, column and spans. The historical header
geometry was not retained, so this does **not** establish what its earlier
span was. Identical listing/image bytes do not prove identical full table
topology. This difference and its limits remain evidence for #447's selection
decision; no retained answer was normalized or rewritten to hide it.

## Evidence clarification, 2026-09-08

The preserved [`SEMANTICS-RESULTS.md`](../../src/corridor_pdf_reader/receipts/notes/SEMANTICS-RESULTS.md)
narrative says the second 9424 run left `Relocation Estimated Date` unmapped.
The retained `semantics-9424-v2/document.json`, SHA-256
`945021e2cb6b1aeedf4715b2bb9213c5d0ae02353754574095439a66c8051c20`,
contradicts that statement. Its raw structures map column 19 on pages 2 and 7
to `committed_date`, and the checked reading includes these 11 extracted rows:

| Page | Local row ID suffix | Cell | Preserved text |
|---:|---|---|---|
| 2 | `table:0:row:1` | `t0r7c19` | Summer 2019 |
| 2 | `table:0:row:2` | `t0r8c19` | Summer 2019 |
| 2 | `table:0:row:3` | `t0r9c19` | Jun-19 |
| 2 | `table:0:row:4` | `t0r10c19` | Spring 2019 |
| 2 | `table:0:row:8` | `t0r14c19` | N/A |
| 2 | `table:0:row:10` | `t0r16c19` | Summer 2020 |
| 2 | `table:0:row:11` | `t0r17c19` | Summer 2020 |
| 2 | `table:0:row:12` | `t0r18c19` | Summer 2020 |
| 2 | `table:0:row:13` | `t0r19c19` | Summer 2020 |
| 7 | `table:0:row:11` | `t0r17c19` | Apr-20 |
| 7 | `table:0:row:12` | `t0r18c19` | Apr-20 |

The complete local row IDs begin with `structure:<page>:`. These are local
references within their identified document and page, not global addresses.
The new adapter preserves the source text and source-bound proposal fields,
and records `non_iso_date` refusals of ISO-date Fact materialization. It does
not invent dates, broaden the date contract, or treat an estimate as an
attributed External Party Statement. The raw answers and earlier narrative
remain unchanged; this clarification records their disagreement.

The retained native population is 265 body rows for 9424: 162 extracted,
101 retired, and two with insufficient mapped fields. The pooled 9540
population is 201: 192 extracted and nine missing required fields. These
counts belong to these exact native readings. The frozen 9424 scope sidecar's
97 retired rows belong to a different population; the 101 count does not
correct or replace that historical sidecar.
