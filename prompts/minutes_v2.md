# minutes_v2

You read one page of a utility coordination meeting note between a state
highway department (or its engineering consultant) and an external party. You
report source-supported statements about conflicts that already exist in the
utility conflict matrix. Do not create new conflicts.

## Event kinds

- `commitment`: an external party states it will do something.
- `committed_date_change`: the page itself gives both a previous stated timing
  and a new stated timing. Do not infer a change from another meeting.
- `response`, `escalation`, `status_change`, or `closure`: use only when the
  page says that thing happened. A recurring project deadline is not a new
  external-party commitment.

## Rules

1. `quote` is one contiguous verbatim span from this page. Never tidy,
   abbreviate, or join spans.
2. Record `external_org` as the affected non-department party. Record
   `stated_party` only when the page identifies who actually made the
   statement. They can differ; never fill `stated_party` from `external_org`.
3. `event_date` is when the statement happened, only when this page says so.
4. For every stated timing, preserve its source wording in `text` and its
   precision. For a day, use identical ISO `start_date` and `end_date`. For a
   calendar month, use the first and last ISO day of that same month. For an
   approximate phrase, set both bounds to null. Do not invent a date for
   “about May,” “in two weeks,” or an undated deadline.
5. `committed_date_change` requires both `previous_timing` and
   `committed_date`. A normal `commitment` has `previous_timing: null`.
6. Return null where the page does not prove a value. If there is no event,
   return an empty list.

## Fields

- `event_type`: one of the event kinds above.
- `description`: one sentence explaining what the page says happened.
- `event_date`: ISO source date or null.
- `external_org`: affected external party, or null.
- `stated_party`: party who spoke, or null.
- `conflict_ref`, `station_from`, `station_to`: source identifiers or null.
- `committed_date`: a timing object or null, with `text`, `precision`,
  `start_date`, and `end_date`.
- `previous_timing`: the prior timing object for a date change, otherwise
  null.
- `quote`: supporting span, max roughly 300 characters.
- `confidence`: a number from 0 to 1.
