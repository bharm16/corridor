# WSDOT 9424 reference validity for the native challenger

Decision: **reused**. No new enumeration was justified or authored for #738.
The reference remains method `pymupdf-table-grid`, version `1`; the native
extractor does not relabel its authorship.

The unchanged files are bound by these SHA-256 values:

| File | SHA-256 |
|---|---|
| `wsdot-9424.machine.csv` | `94720ed31bc66616479a5de0d2448774569c50b22cbdb4768d31090c1566a111` |
| `wsdot-9424.machine.md` | `59ef121cc1404010606d66016b888869b330825c6c6e66f7f4425f3b3e1fa0e5` |
| `wsdot-9424.machine.scope.json` | `bac9b49ae4e6f7b67b02583eb60fa2a8865199c048d58412b47fee2a3ba01d62` |

The scope binds original PDF
`e619a4abf6044ee17415d4f6933ec9ef674c67735befb172a8d6da5a691e4b9a`.
The CSV contains 162 source-reference rows: 82 critical, 78 noncritical and
two blank criticality labels. Its existing backfill provenance remains intact.

#737's [retained native replay](native-matrix/v1/receipts/2026-09-08-selection.json)
reproduces the 162 source-reference entries from this CSV. That supports reuse
of its enumeration. Field values, source references, row dispositions and
Fact materialization are separately compared against retained native readings;
the CSV is not independent field or physical-occurrence gold.

The historical sidecar's **97 retired rows and two empty slots** describe its
old authoring population. The native semantic reading's 265 body rows include
162 extracted, 101 retired and two insufficient rows. These are different
populations and recipes; the native count does not correct or replace the old
sidecar. No historic reference, limitation or receipt was rewritten.

The original shared-PyMuPDF caveat describes the reference's authoring
context. The native extractor now uses a different parser, but the reference
still cannot reveal regions its own authoring method omitted. Shared source
bytes and classification vocabulary do not establish independent completeness
or semantic truth. Keep the semi-independent ceiling qualification.
