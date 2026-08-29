# I-35 NEX South intake

TxDOT's I-35 NEX South design-build publishes its Utility Conflict Matrix as
the structured original — an `.xlsx` workbook — rather than a printout of one
(ADR-0005). `corpus/i35-nex-south.yaml` registers the project and its two
verified conflict workbooks, both members of the one public utilities archive
(`i35nexso-rid-utilities.zip`, 50,875,051 bytes):

- `Utilities/I-35_NEX_SOUTH_UCM_22.02.07.xlsx` — the finalized conflict list.
- `Utilities/I-35 NEX_SOUTH_Potential_Utility_Conflicts.xlsx` — the potential
  conflict list.

Both are `doc_type: matrix`, `role: spine`, `curation_status: confirmed`.
`22.02.07` is the finalized list's published revision date (2/7/2022); the
workbook's own header records it was first developed and reviewed in April
2020. The potential list carries no date in the archive and is registered
undated. The two are a contradiction test case (potential vs finalized
conflicts), not a dated revision series, so no Supersession is declared —
Supersession is a declared RID-index relation, never inferred (ADR-0015).

## Native reading — a second published TxDOT form

Both workbooks are the earlier TxDOT **"UCM - Utility Conflict List"** form,
not the Utility Conflict Analysis Template in `corpus/cross-agency.yaml`. Its
column header sits on row 8 beneath a five-row project-identification block,
and its columns are named differently from the template. `corridor.sheets`
now recognizes this form's exact column names — read straight from the
workbook's own `Field_Column Descriptions` sheet, on the same terms the
template is read: an exact published-form column name, never a fuzzy synonym
(`vocabulary.UCM_CONFLICT_LIST_HEADINGS`, #365). The workbooks are read
natively as cells with zero model tokens; each conflict row becomes a
`Candidate(kind="dependency")` proposal, and citations verify exactly because
the page text was generated from the same cells the values came from.

Fifteen of the form's twenty-four columns map to the canonical vocabulary:

| Form column | Canonical field |
| --- | --- |
| Utility Company | external_org |
| Utility Company Contact | external_org_contact |
| Utility Conflict ID | utility_id |
| Utility Type | utility_type |
| Utility Conflict Description | conflict_description |
| Longitudinal or Crossing | orientation |
| Utility Placement in Relation to Existing TxDOT Right of Way | row_placement |
| Station Origin | baseline |
| Start Station / Start Offset | station_from / offset_from |
| End Station / End Offset | station_to / offset_to |
| Level of Utility Investigation Needed | sue_level |
| Recommended Action or Resolution | resolution_strategy (ADR-0009) |
| Comments | notes |

The other nine columns are **reported as unmapped, never guessed** — the same
discipline the page extractor follows for an unrecognized column. Two of the
nine are declined deliberately rather than for lack of a field:

- **Resolution Status** — a document's workflow state. The Ledger derives
  readiness from adjudicated evidence and nothing imports a document's status
  (ADR-0002), the same reason FDOT's "Resolved Status" is declined.
- **Estimated Resolution Date** — the project's own estimate, a different
  claim than an External Party's committed date (`committed_date`).

The remaining seven (Drawing or Sheet No., Line Style, Size and/or Material,
Base or Ultimate, Highway Alignment, Test Hole No., Test Hole Depth) have no
canonical field and are carried as unmapped. A later vocabulary extension is
the only thing that would map any of them, exactly as on the page path.

## The archive holds no printed rendition of the matrix

The utilities archive was listed by central-directory read at onboarding.
Besides the two workbooks it holds SUE material (`I-35NEX_SUE-QL-A_THs.pdf`
and `IH35_NEX_Test_Hole_SummarySheet_20200320.pdf`, plus several `.dgn` strip
maps and two nested `.zip`s), one interlocal agreement
(`I-35_NEX_Central_SAWS_TxDOT_ILA_20210330.pdf`), and a lights workflow PDF.
**None of the PDFs is a rendering of either conflict matrix.** So there is no
structured/printed rendition pair to declare here — the matrix is published
only as the structured original. That absence is a listing result, not an
inference from filenames or dates: a rendition pairing, had a matrix PDF been
present, would be a `curation_status: proposed` declaration for a maintainer
to confirm, never inferred. No measurement work happens in this intake.

The SUE PDFs and strip maps are out of scope for this ticket and are not
registered; they are candidates for a later SUE-evidence intake in the shape
of the SH 99 SUE tables (`docs/operations/structured-sue-table-intake.md`).
