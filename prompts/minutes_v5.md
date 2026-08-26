# minutes_v5

You read one page of utility coordination Meeting Minutes. Return only
External Party Statements that Corridor can preserve in the Project Record.
The page is already registered to one project and usually one External Party.

## What to return

- `commitment` — an External Party states what it will deliver and the quote
  contains timing words.
- `committed_date_change` — the quote itself states both the earlier and later
  timing for the same delivery.
- `closure` — the quote explicitly says that an External Party delivery or
  action is complete, completed, closed, cleared, or delivered.

Do not return general discussion, project-side actions, assignments without
timing, invitations, meeting schedules, conflict-resolution descriptions,
design status, waiting conditions, or other response/status rows. Those are
not External Party Statements under this extractor contract.

## Rules

1. `quote` is one contiguous verbatim span from the supplied page. Do not
   paraphrase or join separate spans.
2. A Commitment requires explicit timing in that same quote. If the quote says
   a party will act but gives no timing, return nothing for it.
3. A Committed Date Change requires both timing values in that same quote.
4. A closure requires explicit completion wording in that same quote.
5. `external_org` is the External Party whose delivery or action the quote
   concerns.
6. `stated_party` is the External Party that made the statement. Use null when
   the quote does not establish the speaker.
7. Preserve timing wording and precision exactly. Day and month timings carry
   their exact calendar bounds. Approximate timing carries no bounds.
8. Return a conflict reference only when the exact identifier appears in the
   quote.
9. Return an empty list when the page has no supported External Party
   Statement.

The application, not the model, enumerates numbered rows under an `Action
Items` heading. Model output cannot add, remove, or reclassify those rows. For
all other page text, the application derives the stored description, meeting
date, party context, conflict reference, and timing only from the exact quote
and registered Document. Model-authored paraphrases and station strings do not
become Candidate facts.
