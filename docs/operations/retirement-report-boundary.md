# Report comparison after legacy retirement

`retire_legacy_ledger` records the maximum Report Run ID for the project in
`retirement_report_run_watermark_id`, in the same transaction as retirement.
Zero means the project had no reports. The report-table lock serializes the
boundary with every report insertion; it is taken before the project lock.
The existing `retired_at` remains the human-readable act time.

`diff_since_last` selects reports above that ID, then chooses the greatest
eligible ID for the requested document-only mode. Timestamps never decide
eligibility or predecessor order.

The supported historical archive format retained neither report membership
nor a Report Run watermark. Those existing boundaries stay NULL, explicitly
unknown; no timestamp-derived backfill occurs. New Report Runs are bound by a
database trigger to the existing project archive. After an unknown boundary,
only those explicitly bound reports can serve as predecessors. The first
report explains that the prior comparison boundary could not be established;
subsequent reports resume ID-based comparison independently for each report
mode. The archive relationship, project, and Report Run ID cannot be rewritten.

No archive content, original author, or historical report is rewritten. A
downgrade refuses once either new boundary field carries retained evidence.
