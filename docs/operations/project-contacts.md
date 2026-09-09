# Project contacts v1

`project-contacts-v1` imports explicit contact assignments from a bound CSV
delivery or the adopted UCM's registered `ContactMapping`. It retains source
bytes, the source Document, import/mapping identity, every row outcome and exact
CSV record/column or workbook cell locators. Required identity/organization/role
values cannot be invented. Missing person, channel or address remains unresolved;
an unsupported or malformed row is retained in import accounting.

The CSV format is demonstrated by
[the synthetic example](../../tests/fixtures/project-contacts-v1.csv). Optional
fields are blank/null. Email addresses are bare addresses; phone addresses retain
their explicit digits, country prefix and formatting. No delivery verification or
country-code inference occurs. Dates are ISO calendar dates with an inclusive
start and exclusive end; empty bounds are open. Unknown columns remain accounted.

For UCM, `MappingDeclaration.contact_mapping` names the worksheet, header row and
each normalized field's source heading. A declared constant can supply the
responsible role. The registered mapping is read back and verified, including its
digest. The importer never splits a free-text `external_org_contact` cell into a
guessed person/address. A missing required identity yields an explicit refused
row; missing optional contact details yields a role-only record.

```sh
make contacts ARGS="import-csv 12 --identity contacts-revision-1 --source-family project-directory"
make contacts ARGS="import-ucm 3 --identity adopted-contacts"
make contacts ARGS="read 3"
make contacts ARGS="read 3 --organization 'Utility A' --role 'the responsible contact' --as-of 2026-09-10T12:00:00+00:00"
make contacts ARGS="correct example-project 42 --record contact.json --reason 'Onboarding correction' --identity fix-contact-42"
```

CSV input is the already bound SourceEnvelope from the shared intake; the ID in
the command is its retained delivery ID. Give revisions of one pushed directory
the same explicit `--source-family`; delivery identity alone need not be the
directory's stable identity. Same identity/version with changed bytes or mapping
refuses. Missing rows alone never expire or delete prior contact assignments.

Corrections use the application's existing signed-in web session and CSRF check,
with project-coordination membership. The CLI reads `CORRIDOR_SESSION_TOKEN` and
`CORRIDOR_CSRF_TOKEN` from the operator's environment, never an actor display
string. The endpoint accepts only contact values, a reason and idempotency key;
the authenticated session supplies the principal. A stale predecessor refuses.
Original imports and corrections remain immutable and readable.

Resolution selects the newest recorded occurrence of each source contact at the
requested cutoff, then applies its validity interval. It does not resurrect an
older address after its replacement expires. A refused row with a readable source
contact ID also replaces that identity's prior occurrence for resolution: the
contact stays unresolved until corrected or supplied in a valid later revision.
Its raw cells and refusal reason remain in import accounting. Missing rows alone
do not replace prior occurrences. Independent active records with
different person/channel/address values remain ambiguous. Identical active
records may share one resolved recipient while retaining all evidence references.

Follow-up rule v2 uses this resolver for actual bundles and release payloads.
It carries the person, explicit channel/address and retained contact IDs when
resolved. Otherwise it keeps the responsible role and an explanation; no bundle
disappears. Recipient keys include organization, role, channel and address.
Historical rule v1 remains replayable with its original rendering and payload.

This is contact-source software, not a CRM or sender. No accepted Project Record
value changes. Actual directory configuration, account sessions, deployment and
customer/pilot verification remain operational work against this completed
contract. Validation runs through the public import/correction/resolution and
follow-up bundle seams using synthetic fixtures.
