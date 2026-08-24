---
status: accepted
---

# A dead facility is still a Dependency; only the form's numbering is retired

Settles #128, raised by the labelling preparation for the M7 gate (#88) and decided before WSDOT 9540 — the same form — is fetched (ADR-0008).

WSDOT's Appendix U prints `Not Used` in its Notes column 102 times on contract 9424. The phrase does two different jobs, and the form never says which is which:

- **101 rows** carry at most a printed conflict number beside the phrase — 97 with a number, 4 with the phrase alone — and nothing else. The form's retired numbering: slots 72, 73, 75, 76, 77 and 80 sit visibly blank on the page-5 image between populated rows 71, 74, 79 and 81.
- **1 row** (page 9, conflict 210) holds an owner (PSE), a facility (UG Power), a location (Military Road and Veterans Dr), a construction phase and stage, a permit mark, and an `X` under `Retain and Protect` — beside the phrase.

Spelling does not discriminate: both spellings appear on retired rows — `Not Used` 79 times, `Not used` 22 — and the 23rd `Not used` is row 210 itself. Content discriminates perfectly: 101 against 1, nothing between. The maintainer confirmed against the page images that the phrase really sits in row 210's own Notes cell, and that the two id-only rows without the phrase (163, 256) are empty slots — a third category, not conflicts and not carriers of the phrase.

## Decision

**A row whose only content is a retirement phrase, at most beside an identifier, is a Retired Row** — the form's bookkeeping for numbering taken out of service. It is not a conflict. It enters neither the Ledger nor a declared reference enumeration, and the exclusion is a stated rule in every reader — `vocabulary.is_retired_row`, applied by the structure tier, the transcription tier, and the spreadsheet path alike.

**A populated conflict row carrying the phrase is a conflict.** The phrase describes the facility, not the row, and it reaches the record verbatim in `notes` for Adjudication to judge. Nobody selects a resolution strategy for a number that was never used: row 210 was analysed, resolved `Retain and Protect`, and marked for a city permit. The reading has a home in the document's own lineage — TxDOT's published template, verified in `corpus/` since #60, carries `Out of service` in its `Operational Status` controlled vocabulary. Describing a facility, the phrase maps to a value the industry defines; describing a row, it maps to nothing.

The domain already committed to this side of the line. ADR-0009 puts **abandonment on the critical side** — *"an abandoned facility is scheduled utility-owner work, not a facility that stays"* — because this industry does not treat dead facilities as gone. A rule that read `Not used` as "row does not exist" would sit one column away from a rule that reads `Abandon / Deactivate` as "critical, someone must act", about the same pipe.

## Why the rule had to be stated rather than inherited

The 101 Retired Rows were already excluded — by luck, twice over. The 97 that carry a number map an id plus a note: exactly two fields, which **passes** `MIN_ROW_FIELDS`, so they die only on the `REQUIRED` guard, and only because this layout prints an owner column they leave empty. On a layout whose owner arrives from the page header — FDOT's shape, `PAGE_FIELDS` inheritance — those 97 inherit the owner, pass both guards, and land in the queue as phantom conflicts. (The 4 phrase-only rows die on `MIN_ROW_FIELDS` and never had the problem.) A test now pins the phantom dead.

## What it does to the gate

With the maintainer's triage of 9424's non-extracted rows that print anything — 33 page furniture, 2 empty slots, 101 retired; two wholly empty grid lines print nothing and fall outside every category:

| | this ADR | the rejected reading (phrase always retires) |
|---|---|---|
| true conflicts | 162 | 161 |
| recall | 100% | 100% |
| precision | 100% | 99.4% — row 210 a phantom |

Recall was never at stake between the readings. What was: a labeller counting retired numbering as conflicts would read recall as 162/263 = 61.6%, failing the gate on bookkeeping. The labelling rule below is this ADR's real payload.

**The labelling rule, one sentence:** count a row when it names a facility, not when it merely bears a number — a Retired Row is not counted, a populated row is counted whatever its notes say.

## Considered options

**The phrase always retires the row.** Mechanically trivial — a whole-cell match, the same operation `is_retired_row` performs. What it cannot be is innocent: it makes the extractor read two words of prose as an instruction to delete a row the agency analysed and resolved, which is assigning meaning to prose — the boundary ADR-0006 draws for values and ADR-0011 for narrative, moved without anyone deciding to move it. Its one honest argument is the counter-case: someone may have filled row 210 in and retired it later, leaving the data behind. The document cannot rule that out. The answer is the architecture's, not this ADR's: the row reaches Adjudication carrying `notes: 'Not used'` beside its strategy, and a human accepts or rejects with both in view. A phantom that gets human review is a cheap failure; a silently deleted real conflict is the expensive one, and it is the one this product exists to prevent.

**Map the phrase to `operational_status` at extraction.** Premature: `Not used` is not a value in the template's vocabulary (`Out of service` is), and the extractor asserting the translation would be deciding what prose means. Left to Adjudication, where a reviewer can set the status with the note in front of them.

## Consequences

- **One phrase list, two deliberately different matchers.** `vocabulary.RETIREMENT_PHRASES` is the single home of the vocabulary; the gold-preparation report imports it rather than keeping a copy, and a test pins the sharing. The *matchers* differ on purpose: the extractor retires only on whole-cell equality, while the report flags any cell containing a phrase — over-reporting is the safe direction for a document a human is about to read, and a note like `Not used for potable supply` should reach the reviewer's eyes without retiring anything.
- The gold worksheet instruction states the labelling rule, so the human and the extractor count the same set before 9540 is fetched. The mechanical enumeration (#90) is unchanged — it is a cross-check, never the denominator, and its own coverage reporting already says what it could not count.
- The retirement vocabulary is deliberately one phrase long. It grows only from a real document, and under-matching is the safe direction: an unknown phrase surfaces its row in the preparation report for a human to read, while over-matching would hide one behind a classification nobody checked.
- **Checked corpus-wide, run today:** across every stored candidate in every project, exactly one carries a retirement phrase in any field — 9424's row 210. One instance, not a class, and the rule excludes zero stored candidates: the extractor's output is byte-identical to what it already produced, 162 rows with row 210 among them. The rule exists so that stays true when the layout stops cooperating.
