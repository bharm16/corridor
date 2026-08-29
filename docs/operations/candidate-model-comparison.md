# Candidate-model comparison

`llm_model` and the prompt version are sealed on every Extracted Proposal, and
changing either makes readings incomparable — the reason Extraction Measurement
pins exact run receipts. A candidate-model comparison turns "should we adopt
model X" from a leap of faith into one measured afternoon: it runs a named
candidate model over exactly the documents the current model already read,
scores both readings against the same Reference Dataset, and writes one
comparison receipt — current versus candidate, per reference.

## Running it

```
make candidate-model ARGS="<project-slug> [reference.csv] \
  --candidate-model=<model> \
  --current-extraction-run=<id> [--current-extraction-run=<id> ...] \
  --database-url=<disposable-url> \
  [--reference-manifest=<scope.json>] [--case-predictions=<outputs.json>] \
  [--prompt-version=<X>]"
```

`--current-extraction-run` names the current reading exactly, one completed run
per matrix, exactly as `make eval` does; no prompt, recency, or Active Run
inference selects the population. The candidate model reads those same
documents and is scored against the same reference, so the two numbers are
comparable. Reference and case options behave as they do for `make eval`.

Both readings are ordinary Extraction Measurements. The receipt embeds each
side's full measurement artifact — its model, prompt, sealed configuration, and
reference identity — plus the per-reference deltas, and a `comparison_identity`
that excludes only the timestamp, so repeating the exact command resolves to the
same immutable file.

## Non-production by location, never by label

A candidate run is an experiment. Per ADR-0049 the separation is by database:
the command requires an explicit `--database-url` naming a disposable copy and
refuses the configured production database. It declares no Active Run, so a
candidate reading never becomes a Current Production Run and is never resumed as
production work. The candidate run exists only in the disposable copy; the
production database never holds it.

## Quarterly cadence

Run a candidate-model comparison each quarter, and before any proposed model or
reader swap. It is the standing check that the reader Corridor deploys is still
the best available on the measured corpus. The cadence is operating practice;
the command is the same whenever it runs.

## Adoption rule: switch on a measured win

Adopt the candidate only on a **measured win**: on the same reference it does
not regress against the current reading — recall, precision, coverage, critical
recall, field-token failures, and the human-ruling cases all hold or improve —
and it improves at least one. The receipt states each delta and lists any
`regressions`; a single regression is not a win.

A measured win is not:

- a **narrower** comparison. A candidate that fails to read even one measured
  document produces no comparison at all — the command fails loudly and names
  the documents, because a score over a different input scope is incomparable.
  A model that cannot read the corpus is not a win.
- a **greener** one. If the reference could not score a side, the comparison is
  unmeasurable, not a pass.

Adoption is a human read of the receipt. This command measures; it never
declares the new model in production. Switching the deployed reader remains a
separate, attributable act.
