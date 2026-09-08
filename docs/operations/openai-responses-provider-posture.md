# OpenAI Responses provider posture — native matrix structure mapping

This document is the provider posture the native matrix outbound boundary
binds to. `src/corridor/native_provider_boundary.py` carries the SHA-256 of
these bytes; changing a word here changes the digest, and every authorization
record that names the old digest stops covering a request until it is
re-recorded. That is the point: the posture is what the provider is approved
to do at all, and a record cannot accept terms the code does not implement.

The decision itself was made on 2026-09-03 and is recorded on issue #557. This
file restates it in the exact fields the boundary matches. It does not approve
anything on its own and does not select a production configuration.

## Identity

`openai-responses-gpt-5.6-luna-structured-matrix-posture-1`

## Provider and request configuration

| Field | Value |
|---|---|
| Provider | OpenAI |
| API surface | Responses API |
| Endpoint | `https://api.openai.com/v1` |
| Model | `gpt-5.6-luna`, exactly as pinned in this repository |
| Reasoning effort | `none` |
| Structured output | strict JSON schema |
| `store` | `false` |
| Service tier | standard (no flex) |
| Image detail | `original` |
| Model context DPI | 110 |
| Training opt-in | disabled |

Only the minimum pages a purpose requires are sent. No project material is
uploaded into provider-hosted Files, vector stores, assistants, code
execution, or any other persistent provider resource.

## Permitted purposes

- `native-matrix-structure-mapping` — mapping a utility-matrix page's table
  structure and row identities from the page image plus the document's own
  native cell listing.
- `extraction-measurement` — a qualification or evaluation campaign over that
  same source class.

No other purpose is permitted by this posture, and no purpose here covers
Minutes prose, scanned pages, or mixed OCR regions.

## Permitted source classes

`native_matrix` only.

## Retention, stated accurately

OpenAI states API data is not used to train models by default unless an
organization opts in. Standard abuse-monitoring logs may nevertheless be
retained for up to 30 days. Eligible customers can seek Modified Abuse
Monitoring or Zero Data Retention.

`store: false` prevents application-level response storage. It must not be
represented as eliminating all abuse-monitoring retention.

| Field | State |
|---|---|
| `retention` | `standard-abuse-monitoring-up-to-30-days` |
| `training_opt_out` | `default-not-trained` |
| `zero_data_retention` | `not-enabled` |

## Customer processing is not yet open under this posture

Two conditions are unmet and are recorded here as unmet, not worked around:

| Field | State | Why |
|---|---|---|
| `customer_processing` | `blocked` | #522's signed customer authorization must disclose the abuse-monitoring retention above, and no such signed record exists yet. |
| `pdf_licensing` | `unresolved` | #557 conditions PDF image and table extraction on the PyMuPDF licensing resolution (#461), which is open. |

The boundary therefore refuses every customer-sourced request with zero
outbound calls, and names both fields in the refusal. Public and synthetic
experiment material is unaffected: an experiment scope authorizes the
`experiment` stage and nothing else, so no fictional customer agreement is
ever written to cover reference material.

## No-model paths do not wait on this

Deterministic processing — adopted UCM workbooks, later workbook revisions,
declared structured exports, the native reader, the page inventory, rendering,
cropping, segmentation and materialization — calls no model and is outside
this posture entirely.

## Change authority

A provider or model change requires a named maintainer approver, a candidate
model evaluation on the pinned corpus, a new posture identity and digest, a
clean measurement boundary, and customer reauthorization whenever the
provider, retention, region, subprocessor set or contractually named model
changes.

## Status

`approved` for the experiment stage on public and synthetic material.
`blocked` for every customer stage until the two fields above are recorded
otherwise, with evidence, in this document.
