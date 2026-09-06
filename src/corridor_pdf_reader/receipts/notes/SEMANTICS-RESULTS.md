# Semantics tier — first runs against Corridor's machine gold (2026-09-05)

The tier (`replacement/semantics.py`, prompt `matrix_structure_ids_v1`,
model `gpt-5.6-luna`, reasoning effort `none`, page image at 110 dpi) ran over
the reader's cells (`replacement.reader --engine tagged`) on the two WSDOT
listings Corridor keeps machine gold for. The gold names one row per conflict
as `source_ref,page`; a gold row counts as found when an extracted row on that
page carries its identifier in `utility_id`.

| Document | Pages | Gold rows | Extracted | Matched | Recall | Precision | Prompt tokens (cached) | Completion tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| WSDOT 9424 Appendix U2 existing utility listing | 11 | 162 | 162 | 162 | 100% | 100% | 71,207 (26,334) | 3,422 |
| WSDOT 9540 Appendix U3 listings (gas, OPL, power, sewer, water, comm) | 9 | 192 | 192 | 192 | 100% | 100% | 57,550 (18,977) | 1,751 |

What the model mapped on 9424: `Owner` → external_org, `Conflict ID` →
utility_id, `Facility Type (...)` → utility_type, `Location` →
location_start, `Notes` → notes, and the four columns beneath `RECOMMENDED
RESOLUTION` (`509 Relocation Needed`, `ST Relocation Needed`, `Retain and
Protect`, `Abandon / Deactivate`) as one marked resolution_strategy group; 19
columns (sheet references, construction phase and stage, permits, easements,
schedule status, agreement status) reported unmapped for a human. The first
run mapped `Relocation Estimated Date` to committed_date; the prompt now says
a project's estimate is not an owner's commitment, and the second run left it
null. On 9540 the marked group is `RELOCATION` / `PROTECTION IN PLACE` /
`ABANDON/ DEACTIVATE/ REMOVE`, with `ALIGNMENT`, `FUNCTION` and the CU sheet
columns read as the vocabulary says.

Rows the tier declined, all by rule: 101 retired rows on 9424 (`Not Used`),
two rows carrying only a conflict number, and on 9540 the plot stamp
(`c:\pw_work\...`, `PRINTED ON: ...`) and legend lines that the page prints
inside the table's frame, skipped for lacking required fields. The model's
only refused answers were column indexes one past the table's width, recorded
and ignored.

Reproduce:

```bash
make semantics ARGS="--pdf <corridor>/corpus/files/e6/e619a4ab...pdf --output results/semantics-9424 --env-file <corridor>/.env"
make semantics-eval ARGS="--reading results/semantics-9424/document.json --gold <corridor>/gold/wsdot-9424.machine.csv"
```
