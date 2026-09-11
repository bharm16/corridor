---
status: accepted
domain: retention
scope: current product
amends:
  - ADR-0080
migration: the pass exists and is proved, but nothing runs it on a schedule and its receipt is an `audit_log` entry rather than a row in a receipt family of its own; this ADR specifies both changes and builds neither. The `sign_in_attempts` period is the one number here that a customer contract or agency records schedule may override, and no such schedule has been read.
---

# A sign-in record is deleted once it can no longer authorize anyone, and an attempt log is kept longer

**Amends ADR-0080.** It adds one class of data that ADR-0080's four retention
classes never covered, and one expiry regime for it. Everything ADR-0080 says
about the Project Record, about Class B intermediaries, and about governed
disposition is unchanged, including its rule that a legal hold suspends *every*
deletion path — the pass this ADR authorizes is suspended by one too.

## Context

`web_sessions`, `sign_in_tokens` and `sign_in_attempts` are the three relations
the sign-in system writes (#331). All three evaluate expiry inline in the
`WHERE` clause against an injectable clock, which is correct for authorization:
`resolve_web_session` will not return an expired or revoked session, the
consuming `UPDATE` will not spend an expired or already-spent link, and
`count_recent_attempts` counts only inside `ATTEMPT_WINDOW`. An expired row is
never *read* as valid.

But nothing deleted them. Expired sessions, consumed and unconsumed links, and
every recorded attempt to reach the product accumulated for the life of the
customer database (#907). These are personal data — an email address, a
principal subject, a client address — held with no stated period.

ADR-0080 does not cover them. Its four classes are the Project Record (A),
assistant and processing intermediaries (B), rebuildable indexes (C) and cost
records (D). A session record is none of those: it is not the customer's
record, it is not an intermediary of any processing run, it cannot be rebuilt,
and it is not a cost. ADR-0080's disposition apparatus does not fit it either.
That apparatus is per project — every Class B candidate carries a `project_id`,
a hold is placed on a project, a dry-run manifest lists a content digest per
row, and execution *nulls the content columns* so the receipt keeps its
identity. A sign-in record has no project, has no content to null because the
whole row is the credential, and is keyed to a person or, for an attempt, to a
normalized email or client address that may belong to nobody. So this is not a
case of an existing sweep having missed three tables. There was no mechanism
they could be added to.

Published practice, researched before any number was proposed
([note](../research/sign-in-record-retention-2026-09-11.md)), turns out to be
uneven in a way that shapes this decision:

- **It is emphatic that a period must exist.** NIST SP 800-63B-4 §2.4.2
  (July 2025): a verifier retaining records "in the absence of mandatory
  requirements" SHALL run a risk process "to determine how long records should
  be retained" and SHALL inform the subscriber. OWASP ASVS 5.0 requirement
  16.1.1 requires the log inventory to document "for how long logs are kept".
- **It says nothing about deleting a session record or a spent link.** Every
  requirement found — NIST SP 800-63B-4 §5.1, NIST SP 800-63A-4 §3.8, ASVS
  7.4.1 — says *invalidate*, never *delete*. There is abundant guidance on when
  a session must stop working and none on when its row must go.
- **It does give numbers for an authentication log.** PCI DSS v4.0.1 10.5.1:
  at least twelve months, three immediately available. CIS Critical Security
  Controls v8 Safeguard 8.10: a minimum of ninety days. CNIL Délibération
  n° 2021-122 ¶8: between six months and one year, explicitly balancing the
  need to detect attacks against "la nécessité de ne pas conserver un volume de
  données trop important".

And no source draws the distinction #907 assumes — that an attempt record is a
security log while a session and a token are operational credentials. The
research note says so plainly rather than assembling support out of fragments.
The argument for that distinction has to be made from Corridor's own facts, and
it can be: `audit.SIGN_IN` is written at session creation, so a **successful**
sign-in leaves a durable `audit_log` entry that `identity_audit` exports. A
**failed or unknown-email** attempt leaves no audit entry at all.

## Decision

### 1. The three relations are one class of their own, and a row in it is deleted, not emptied

They are **sign-in records**: the machinery that establishes and bounds access,
keyed to a person rather than to a project. A sign-in record carries no
authority, supports no accepted value, and is cited by nothing. When its period
ends the whole row goes. There is no manifest of digests, because there is no
content whose identity survives its deletion; there is no attributable human
approval, because there is no customer record being disposed of and nothing for
a person to review; and there is no export obligation, because everything
durable about the act is already in `audit_log`.

### 2. The three periods, each measured from the moment the row could last authorize something

| Relation | Period | Measured from |
|---|---|---|
| `web_sessions` | **30 days** | the earlier of `expires_at` and `revoked_at` |
| `sign_in_tokens` | **7 days** | the earlier of `expires_at` and `consumed_at` |
| `sign_in_attempts` | **365 days** | `occurred_at` |

The anchor is deliberate. A period measured from creation would be the "N years
after ingest" schedule ADR-0080 rejects; a period measured from the moment the
row stopped being able to authorize anything is a triggering event. It also
handles the case the code actually produces: `deprovision_principal` revokes
every unrevoked session without checking expiry, so a `revoked_at` later than
`expires_at` exists, and reading the *earlier* of the two stops an offboarding
from extending the life of a session that had already died.

### 3. Why the three differ

**A sign-in link: 7 days, the shortest, because it is the shortest-lived secret
and the least informative row.** It is usable for fifteen minutes
(`SIGN_IN_TOKEN_TTL`), once, and only as a hash. After that the row answers
exactly one question — "was the link I was sent spent, or did it run out?" —
which is worth answering for a support request and worth nothing after. NIST SP
800-63B-4 §3.1.3.2 requires an out-of-band authentication to complete within ten
minutes and to be accepted once; NIST SP 800-63A-4 §3.8 allows an emailed
confirmation code at most twenty-four hours. Corridor's fifteen minutes is
inside both. **Neither says when the spent row goes, so seven days is a
judgement**, chosen as long enough for a support question to arrive and short
enough that a hash of an emailed secret is not sitting in the database a month
later.

**A session record: 30 days, longer, because a dead session is still the shape
of somebody's access and is the one dead row the product reads.** Two facts set
it. First, `access.expired_web_session` deliberately reads a session that
expired and was not revoked, so #844 can hand a coordinator back what they had
typed; that reader needs the row only while a held draft could exist, and
`form_drafts.DRAFT_TTL` is thirty minutes, so any period in days is clear of it
— but a period in minutes would break #844. Second, after that the row's only
remaining use is reconstructing an incident. **This too is a judgement**: no
source says when a session record goes. Thirty days is the number this product
already uses for non-authoritative operational data (`retention.CLASS_B_DAYS`),
and it is also the longest a session may live under any NIST assurance level
(SP 800-63B-4 §2.1.3, AAL1), so a session record is never kept for longer after
it dies than a session could have lived.

**A sign-in attempt: 365 days, the longest, and the only one with a published
basis.** Two separate reasons, and they compound:

1. *It is the only record that an attempt happened.* A successful sign-in is in
   `audit_log` as `SIGN_IN` and survives this sweep untouched. An attempt from
   an address bound to nobody, or one that never reached consumption, is
   recorded **only** in `sign_in_attempts`. Deleting a dead session or a spent
   link destroys no history. Deleting an attempt row destroys the only evidence
   that somebody tried and did not get in — which is precisely the pattern an
   access review or an incident investigation is looking for.
2. *Published practice for an authentication log is measured in months, and
   365 days is the only value that clears every cited figure at once.* PCI DSS
   10.5.1's twelve-month floor and CIS Safeguard 8.10's ninety-day floor are
   minimums; CNIL ¶8's six-months-to-a-year is a band whose upper end is
   twelve months. 365 days satisfies both floors and stays inside the band.
   180 days clears CIS and sits mid-band but falls well under PCI's floor.

The operational need is far shorter than either: the backoff counter stops
mattering after `ATTEMPT_WINDOW`, fifteen minutes, and NIST SP 800-63B-4 §3.2.2
says a verifier SHOULD reset the retry count on successful authentication.
Everything past fifteen minutes is retained as a security log, not as a
throttle.

### 4. What the maintainer is being asked to settle, and what the alternatives are

Three of the numbers above are open in different ways, and this ADR states
which rather than presenting all three as equally settled.

- **`sign_in_attempts` = 365 days is the one that depends on something not yet
  known.** It is the widest compliant envelope in the absence of a customer
  contract or agency records schedule. If Corridor should instead lead with
  minimisation — CNIL ¶8's own reason for a ceiling is that a large log is
  itself something to attack — **180 days** is the alternative: mid-band under
  CNIL, above CIS's floor, under PCI's. If a customer's own schedule turns up,
  it governs and this number changes to match it.
- **`web_sessions` = 30 days rests on judgement.** **7 days** is the
  minimisation-leaning alternative and still 336 times the #844 draft TTL.
  **90 days** is the alternative if session records should be treated as part
  of the security log rather than as spent machinery, which would put them at
  CIS Safeguard 8.10's floor.
- **`sign_in_tokens` = 7 days rests on judgement.** **24 hours** is the
  alternative, and is the only other figure with anything published behind it
  (NIST SP 800-63A-4 §3.8's ceiling for an emailed confirmation code, borrowed
  from proofing rather than authentication).

Any of the three can be changed by editing one constant in
`corridor.sign_in_retention` and the corresponding row in the table above.
Changing one does not disturb the other two, and the reasons above are written
so that one can be reconsidered without rereading the rest.

### 5. A hold stops this pass, for the reason it stops an unattributable object deletion

An active `RetentionHold` anywhere — on any project — refuses this pass, and it
deletes nothing. This is not a new rule: `retention.permit_deletion_unreferenced`
already decided what a project hold means for a deletion that cannot be
attributed to a project, and decided "any active hold anywhere refuses it",
because the thing being deleted might be the held project's. A sign-in record is
in exactly that position — the person whose session it was may be the person a
litigation hold is about — so it takes the same answer. ADR-0080's "legal holds
suspend every deletion path" therefore remains literally true with this pass
added.

The cost, stated rather than hidden: a hold on one project suspends sign-in
record expiry for the whole customer environment, which over-retains. That is
the correct direction for a hold, which exists to override minimisation, and it
is bounded by holds being rare and lifted by a separate attributable act.

## Considered and rejected

- **Adding the three relations to the Class B apparatus.** Rejected in the
  Context above. Every load-bearing part of it — the project scope, the
  completion-shaped anchor, the digest manifest, the null-the-content execution
  — is either absent or meaningless for a credential row.
- **Deleting a session or a link the moment it stops working.** It is what
  minimisation argues for and it is wrong twice over: it would break #844's
  draft hand-back, which reads exactly that row, and it would leave an incident
  with no trace of a session that existed an hour ago.
- **Keeping attempts only as long as the throttle needs them (15 minutes).**
  That treats the relation as only a counter. It is also the product's only
  record of an access attempt that did not succeed, and PCI, CIS and CNIL all
  measure that in months.
- **Sweeping on the sign-in path, the way #844 discards drafts where they are
  kept.** Attractive, because growth would then be self-limiting by
  construction. Rejected: the sweep would run on an unauthenticated request
  path, and a delete over `sign_in_attempts` by time alone cannot use
  `ix_sign_in_attempt_scope`, so every unauthenticated attempt would pay for a
  scan. Worse, an attacker who never succeeds never triggers the pass that
  would remove their own rows.
- **Declaring this to the Due Work runtime now.** Rejected as unbuildable
  today, not as wrong; see below.
- **Making this an operator command and stopping there.** Rejected as the
  answer. #488 already established what happens to a retention boundary whose
  only caller is a command: "intermediary content expired when somebody
  remembered to run a command". The command exists as the recovery entry point,
  as `retention_cli` is for Class B, but it is not the mechanism, and this ADR
  does not pretend otherwise.

## Consequences

**What is built.** `corridor.sign_in_retention` holds the three periods and one
pass, `sweep_sign_in_records`, which checks the hold, deletes what is past its
period from the three relations and nothing else, writes one `audit_log` entry
recording what it removed, and returns a schema-versioned reading whether it
deleted, declined, or found nothing due. `tests/test_sign_in_retention.py`
proves an expired row is actually removed, that a live one is not, that the
period runs from revocation rather than a later expiry, that #844's expired
session outlives a pass, and that a hold deletes nothing.
`make retention ARGS="expire-sign-in-records --as-of <iso>"` runs it.

**What is not built, and exactly what each would need.** Both are honest gaps,
recorded in the frontmatter's `migration` key rather than half-implemented.

1. **A recurring trigger.** Due Work is the product's one supervised runtime
   (#332), and it is per project by schema: `due_work_schedules.project_id`,
   `due_work_receipts.project_id` are `NOT NULL` foreign keys to `projects`,
   and `DueWorkScheduling.project_id` is a required field. A
   customer-environment-wide pass cannot be declared to it as it stands.
   Declaring one schedule per project is not a workaround — the relations are
   customer-wide, so N projects would run the same global delete N times. The
   change is to let a schedule name the customer environment instead of a
   project: `project_id` nullable on `due_work_schedules` and
   `due_work_receipts`, the `uq_due_work_schedule_identity` unique constraint
   made null-safe, and a check constraint requiring a scope kind that says
   which of the two a row is. Until that exists, expiry depends on somebody
   running the command, which is the defect #488 named.
2. **A receipt of its own.** The pass writes its receipt to `audit_log`,
   which takes it without a schema change: `access.CUSTOMER_WIDE_RELATIONS`
   already classifies `audit_log` as "the append-only attribution ledger of the
   whole customer database", it already carries sign-in, sign-out and
   offboarding, `corridor_worker` already holds `INSERT` on it, and
   `UNBOUND_IDENTITY` already exists for an act that names no identity row. It
   is a fair home and not the best one: the entry names no subject, and a
   sweep is not a ledger mutation. The better home is
   `sign_in_record_expiry_receipts` in `models/operations.py` — `id` bigserial,
   `public_id` varchar(64) unique, `as_of` timestamptz, the three period
   lengths in days, the three deleted counts, a bounded `refusal` string, and
   `created_at`; `SELECT, INSERT` to `corridor_worker` and nothing to
   `corridor_web`. It carries no personal data, so it needs no partition
   policy. Neither the relation nor the census entries it implies were built
   here: this change did not hold the schema slot.

**The fourth per-person store, and whether this covers it.** #844 holds what a
coordinator had typed across an expired session. It is deliberately not a
relation: the web process holds it, bounded by a thirty-minute expiry and a
fixed count of 256, and it is discarded where it is kept rather than by a job
that may never run. Nothing here changes that, and nothing here should: a store
with no rows needs no sweep, and #844's own docstring is right that enforcing
the bound at the point of keeping is stronger than enforcing it from a
schedule.

If it ever becomes a relation — the specification is the last paragraph of
`form_drafts`: "a table of its own, with a person, a customer, a project, an
occurrence, the input and an expiry" — this class covers it and this pass would
extend to it, because a draft is the same kind of thing these three are:
permitted input that is never a record, keyed to a person, with an expiry and
no authority. Three things would have to be true first. Its period would have
to be stated here beside the other three, and it would be the shortest of them,
because a draft that is not handed back within its TTL has no further use at
all. The anchor would be the row's expiry, the same "moment it stopped being
usable" the three above use. And the sweep's closed relation list —
`SWEPT_RELATIONS` — would have to name it, which is the deliberate gate that
stops this pass reaching anything it was not given. None of that is built
speculatively; the relation does not exist.

**What this ADR does not touch.** `SESSION_TTL`, `SIGN_IN_TOKEN_TTL`,
`ATTEMPT_WINDOW` and the three attempt limits are unchanged; how long a session
*works* is a different question from how long its record is *kept*, and #907
asked only the second. `audit_log` itself has no stated retention and does not
get one here — it is the attribution ledger, it is read by the identity export,
and ADR-0080's posture on record history governs it.

**What remains open.** The research note records one design observation that
#907 did not ask for and this ADR does not settle: `sign_in_attempts` is doing
two jobs, a fifteen-minute throttle counter and a year-long security log, over
the same rows keyed by an email address or a client address. CNIL ¶22 and the
OWASP Logging Cheat Sheet both point toward pseudonymizing the scope once the
counter has reset. Neither prescribes it, and separating the two is a change to
the sign-in path rather than to its retention.
