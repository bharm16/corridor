# Derived cases in Extraction Measurement

When a person corrects an Extracted Proposal, settles a Source Discrepancy,
or chooses Do not add, Corridor derives a case for later Extraction
Measurement. The stored state binds the exact Document hash, page or
worksheet-row passage, ruling row, expected outcome, and attributable person.
It is measurement input, not a Project Record mutation.

The stable case key groups later corrections and reversals. Each later act
appends a successor state. PostgreSQL rejects update, delete, and truncate, so
an earlier ruling remains inspectable even when it is no longer current.

Extraction Measurement reads only the current active state whose entire source
Document set is inside the exact Extraction Run population. Applicable cases
are scored against Candidates from those runs. Cases needing a specialized
matcher stay in the receipt as `not_applicable`; they are not silently counted
as a pass. Reversed cases remain in history but do not enter the current score.
Specialized evaluators supply an immutable
`corridor.extraction-measurement-case-predictions.v1` JSON file through
`--case-predictions`. Each output names one exact case-state public id. The
measurement artifact retains the file hash and scores the output against the
human conclusion; an output for a stale or out-of-scope state fails closed.

The artifact keeps the ordinary `reference_scope` and the derived cases
separate. A machine reference remains ADR-0023's semi-independent ceiling. The
`human_ruling` value is retained implementation provenance: it says the case
came from an attributable Human Record Decision, not that every Reference
Dataset is a human-authored gold standard.
