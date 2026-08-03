# agreement_v1

You read one page of an executed agreement between a state highway department
and an external party (a city, county, utility, railroad, or transit agency),
and report the obligations stated on that page.

An **obligation** is a commitment by a named party to do something, permit
something, pay for something, or refrain from something. It is the kind of
thing that must be satisfied before highway construction can proceed.

## Rules

1. **Quote verbatim.** Every `quote` must be an exact, contiguous substring of
   the page text you were given. Do not fix spelling, expand abbreviations,
   re-wrap lines, or join text separated by a line break with anything other
   than the characters actually present. A quote that cannot be found on the
   page is discarded and the extraction is marked unverified.

2. **Many of these pages are OCR output from scanned paper.** They contain
   stray marks, broken words, and odd spacing. Quote the text as it actually
   appears, artifacts included. Do not clean it up.

3. **Report only what the page says.** If the responsible party, the date, or
   the notice period is not stated on this page, leave it null. Do not infer
   from the document title, from context you assume, or from what would be
   typical. A null is correct; a plausible guess is a fabrication.

4. **Skip boilerplate.** Signature blocks, notary text, recitals that merely
   identify the parties, page furniture, and headers are not obligations.

5. If the page states no obligation, return an empty list. An empty page is a
   normal and expected result — most pages of an agreement contain none.

## Fields

- `title` — a short description of the obligation, in your own words.
- `external_org` — the non-department party bound by it, exactly as named on
  the page. Null if the page does not name one.
- `obligation` — what that party must do, in your own words.
- `notice_period` — any stated notice or lead time (e.g. "30 days written
  notice"). Null if none is stated.
- `committed_date` — an ISO date, only if the page states a specific calendar
  deadline for this obligation. Null otherwise. Do not convert a duration
  into a date.
- `evidence_required` — what document or act would close this obligation, if
  the page says. Null otherwise.
- `quote` — verbatim supporting text from this page, long enough to stand on
  its own as evidence, and no longer than about 300 characters.
- `confidence` — 0 to 1, your confidence that this is a real obligation
  stated on this page.
