# minutes_v3

You read one page of a utility coordination meeting note between a state
highway department (or its engineering consultant) and one external party,
or about one external party. You report what the page says happened without
collapsing who spoke, who was affected, or how exact the timing was.

These are biweekly meetings about conflicts that **already exist** in a
utility conflict matrix. You are not cataloguing new conflicts; you are
recording what was said about them on this date.

## What to report

**Events.** Something that happened or was stated at this meeting:

- `commitment` — a party stated it will do something, with timing words the
  page actually supports
- `response` — a party answered a question or supplied information
- `committed_date_change` — an attributable commitment's timing moved earlier or later
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
   given. Never elide with `...`, never re-transcribe, never tidy. For a
   `commitment` or `committed_date_change`, return that exact same span as
   `description`; do not paraphrase the fact that may enter the record.

2. **Keep the speaker separate from the affected party.** `stated_party` is
   the External Party who made the statement. `external_org` is the External
   Party whose Commitment, status, or act the statement describes. A named
   subject is not automatically the affected party: in “Equistar will provide
   chain-of-title material for the agreement in DOW's name,” Equistar is both
   `stated_party` and `external_org`; DOW remains in the description. If the
   page only supports one party, use that same name in both fields. If the
   speaker is not an External Party, or the page does not support who spoke,
   return null for `stated_party`.

3. **Preserve timing wording and precision.** `committed_date` is either null
   or an object with:
   - `text` — the exact timing wording as it appears (`01/2025`, `late May`,
     `in about two weeks`)
   - `precision` — one of `day`, `month`, or `approximate`
   - `start_date` / `end_date` — ISO bounds only when the page supports them
     at that precision. For a day, both are that day. For a month, use the
     first and last day of that month. For `approximate`, both are null.

4. **Do not promote project-side acts into External Party commitments.**
   Invitations, internal follow-ups, scheduled meetings, and consultant or
   department action items are not `commitment` rows by confidence alone.

5. **Do not rewrite conditionals into exact promises.** "After TxDOT confirms
   the exhibit" is approximate conditional timing, not a day.

6. **Report only what this page says.** If a date is not stated, leave it
   null. Do not carry context from what you assume happened at earlier meetings.

7. **A recurring project deadline is not a new commitment.** These notes
   restate the project schedule at nearly every meeting. Record those as
   `status_change` at most, and only when the page shows them changing.

8. **A `committed_date_change` requires the page itself to show the movement**
   — an old timing and a new one, or explicit language that the timing moved.
   Do not infer a Committed Date Change by comparing against another meeting.

9. If the page records no event, return an empty list. Attendee tables,
   agendas, and boilerplate headers are not events.

## Fields

- `event_type` — one of the six above.
- `description` — for a Commitment or Committed Date Change, the exact same
  literal span as `quote`; otherwise what happened, in your own words.
- `event_date` — the ISO date this happened, only if the page states it.
  The meeting date at the top of the page counts. Null otherwise.
- `external_org` — the affected External Party, as a clean readable name.
- `stated_party` — the External Party who made the statement, or null when
  the page does not support one.
- `conflict_ref` — the conflict identifier this concerns (`PL41`), or null.
- `station_from` / `station_to` — stationing as written (`6685+06`), or null.
- `committed_date` — the structured timing object above, or null.
- `quote` — one contiguous verbatim span supporting this, max ~300 chars.
- `confidence` — 0 to 1.
