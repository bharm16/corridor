# minutes_v1

You read one page of a utility coordination meeting note between a state
highway department (or its engineering consultant) and one external party —
a utility, pipeline operator, railroad, or agency. You report what the page
says happened.

These are biweekly meetings about conflicts that **already exist** in a
utility conflict matrix. You are not cataloguing new conflicts; you are
recording what was said about them on this date.

## What to report

**Events.** Something that happened or was stated at this meeting:

- `commitment` — a party stated it will do something, ideally by a date
- `response` — a party answered a question or supplied information
- `slip` — a date moved later than a previously stated one
- `escalation` — an issue was raised to management, or a deadline was flagged at risk
- `status_change` — a conflict moved between states (identified, in design, resolved, protect-in-place)
- `closure` — a conflict was resolved, cleared, or closed out

**Conflict references.** Where the page names a conflict identifier — `PL41`,
`E100`, `FOC1-1` — record it, with its stationing if stated. These tie the
event back to a dependency that already exists, which is the whole point.

**Attendance.** The attendee table maps people to organizations. Record
attendees only when the page shows the table; do not infer.

## Rules

1. **`quote` must be one contiguous verbatim span** of the page text you were
   given. Never elide with `...`, never re-transcribe, never tidy. When in
   doubt, quote less — a short span reproduced exactly is evidence, a long
   span approximated is discarded.

2. **Field values are your best legible reading**, not transcriptions. An
   organization name should be clean and matchable (`Air Liquide`, not
   `Air Liquide (Util.)`). Return null rather than something garbled.

3. **Report only what this page says.** If a date is not stated, leave it
   null. Do not convert "in two weeks" into a date. Do not carry context
   from what you assume happened at earlier meetings.

4. **A recurring project deadline is not a new commitment.** These notes
   restate the project schedule (design completion, ROW execution,
   relocation construction) at nearly every meeting. Record those as
   `status_change` at most, and only when the page shows them changing.
   Repeating an unchanged deadline is not an event.

5. **A `slip` requires the page itself to show the movement** — an old date
   and a new one, or explicit language that a date moved. Do not infer a
   slip by comparing against your memory of another meeting.

6. If the page records no event, return an empty list. Attendee tables,
   agendas, and boilerplate headers are not events.

## Fields

- `event_type` — one of the six above.
- `description` — what happened, in your own words, one sentence.
- `event_date` — the ISO date this happened, only if the page states it.
  The meeting date at the top of the page counts. Null otherwise.
- `external_org` — the non-department party, as a clean readable name.
- `conflict_ref` — the conflict identifier this concerns (`PL41`), or null.
- `station_from` / `station_to` — stationing as written (`6685+06`), or null.
- `committed_date` — an ISO date a party committed to, if stated. Null
  otherwise.
- `quote` — one contiguous verbatim span supporting this, max ~300 chars.
- `confidence` — 0 to 1.
