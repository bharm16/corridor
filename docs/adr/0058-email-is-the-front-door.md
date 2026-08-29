---
status: accepted
---

# Email is the front door

Coordination work lives in email: promises arrive in message bodies, approval letters and revised matrices as attachments, minutes as forwards. Nobody's real workflow includes uploading documents to a tracking tool. Decided 2026-08-28: the primary way new information enters Corridor is a project email address.

## Decision

- **Each project gets an inbound address.** The one adoption habit is CC or forward. Mail sent to the address enters the project; nothing else does — the person is the filter, and that is a feature: deliberate disclosure into a legal-grade record, not a crawler's guess.
- **Attachments become Documents** through the existing intake pipeline — same storage, hashing, validation, reading, verification, and row-matching as any other source.
- **The email body is written evidence.** "We'll have the gas main relocated by March 15 — Dan, CenterPoint" in an email is an External Party Statement with the provenance problem already solved: exact wording, sender, timestamp. The raw message is stored byte-exact like any document, and statements extract from it through the ordinary prose path with quotes verified against the stored message.
- **The sender's address is attribution evidence.** The conflict matrix already carries each organization's contact email; a sender matching a registered contact is deterministic row-matching feeding party resolution. It is evidence for the existing rules, never a new authority.
- **Upload stays as the rare fallback** (paper handed over at a meeting). The connected-archive watcher stays for the big published sets. Reports already leave by email; now information arrives by it too.

## What was rejected

- **Reading personal mailboxes natively** (Graph/Gmail pull of a person's inbox): rejected. It makes relevance filtering the system's job — a new error class in both directions — puts confidential unrelated mail at risk of entering a discoverable record, and requires tenant-admin consent nobody grants a young product. Not a sequencing choice; personal-inbox crawling is not wanted at any stage.
- **The future middle step is allowed:** connecting a *shared project mailbox* (real programs already run correspondence through one) is native pull with a small blast radius — project mail by definition, one grant. That is a later decision with its own ticket when a real mailbox exists.

## Consequences

A new intake ticket implements the project inbox on the existing pipeline. #349 (upload) becomes the explicit fallback path. The trust boundary is unchanged: email content is untrusted input producing proposals with quotes; code verifies; rules write; nothing in a message can instruct the system.
