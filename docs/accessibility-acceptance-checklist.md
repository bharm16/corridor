# Accessibility acceptance checklist for the coordinator review screens

The acceptance criteria that #527, #528, #536, and #537 cite instead of each
restating them. Recorded by #559 together with the shared primitives that make
them cheap to satisfy: `src/corridor/web/ui_primitives.py` and
`src/corridor/web/templates/_primitives.html`.

This is a checklist, not a design system. It adds no framework, component
library, build step, or client-side rendering, and it does not pause product
work. Every item below is checkable on the rendered HTML of one screen, which
is why each is written as a property a test can assert.

## What the shared layer already guarantees

A screen that renders through the primitives inherits these; it does not retest
them. `tests/test_ui_primitives.py` owns them, and
`tests/test_architecture.py::test_no_shared_template_conveys_state_by_colour_alone`
refuses a shared template that paints a state itself.

| Primitive | What it guarantees |
|---|---|
| `ui.state(label)` | Every state and consequence prints its own words. The colour and the mark beside them are redundant, and the mark is `aria-hidden`. |
| `ui.region(id, heading)` | A region is bound to its own heading, so it is reachable by heading and by landmark. |
| `ui.record_table(caption, columns)` | A captioned table with real `scope="col"` headers. |
| `ui.before_after(caption, rows)` | The current accepted value and the incoming value read in order beside the field they belong to, with `scope="row"` on the field. An absent value prints `not recorded`, never an em dash and never a strike-through. |
| `ui.field(id, text, hint, required)` | A visible label bound by `for`, a hint with its own id for `aria-describedby`, and "(required)" as a word. |
| `ui.child_selection(name, legend, children)` | Native checkboxes inside a `fieldset`, each in its own bound label, so Tab reaches and Space toggles every child. A held-out child is `disabled` and says why in text bound by `aria-describedby`. |
| `ui.outcome(kind, heading, focus)` | A completed Save announces politely (`role="status"`); a refusal announces assertively (`role="alert"`). Both are focusable. |
| `ui.error_summary(errors, focus)` | One `role="alert"` summary that takes focus, states how many fields were refused, and links each message to the control that holds it. |
| `ui.evidence_reference(...)` | The exact source behind a value, named in words in the reading order beside the claim. |
| `ui.shortcut(key, action)` | A keyboard hint whose key is spelled out for a screen reader instead of being announced as a stray letter. |
| `ui.anchor(id, focus)` | `tabindex="-1"` on every named landing place, and `autofocus` on exactly the one `ui_primitives.focus_target` chose. |

## What each consuming screen must prove for itself

Each ticket keeps its own surface-specific tests. Cite this file and assert the
items below against the screen it builds.

### 1. State and consequence are never colour alone

- [ ] Every state, consequence, and count on the screen is rendered by
      `ui.state` or reads as ordinary prose. No new colour-only class.
- [ ] `make check` passes, including the shared-template colour check.

### 2. Semantic structure

- [ ] One `h1` naming the item, and headings that descend without skipping.
- [ ] Every list is a list, and every table has a caption and column headers.
- [ ] Landmarks are used once each: one `main`, and a `section` bound to its
      heading for each region a coordinator navigates between.

### 3. Field labels

- [ ] Every control has a visible label bound by `for`, or is inside a
      `fieldset` whose `legend` names the group.
- [ ] Hints and errors are bound with `aria-describedby`, never adjacent text
      alone.
- [ ] Required is stated in words, not only by an asterisk or a colour.

### 4. Focus after Save, stale refusal, and refresh

- [ ] Exactly one element of any response carries `autofocus`, and it is the
      element `ui_primitives.focus_target` names: a refusal, then refused
      fields, then a completed Save, then the item itself.
- [ ] That element carries `tabindex="-1"` so the keyboard can be returned to
      it later.
- [ ] A stale refusal preserves the coordinator's selections, announces what
      changed, and focuses the refusal (ADR-0039: no partial write).
- [ ] A plain refresh lands on the item, not at the top of the document.
- [ ] **A screen that performs no act moves no focus when it loads.** This
      section governs the response to a write. A pure reading — the
      cross-project portfolio (#537) is the one built so far — has no refusal,
      no refused field and no completed Save to announce, so nothing on it
      carries `autofocus`: moving focus on load can carry a screen-reader user
      past the heading and the context that says what they are looking at.
      Offer a user-activated skip link to the first thing needing attention
      instead, and keep `tabindex="-1"` on the region it moves focus to.

### 5. Keyboard-complete child selection

- [ ] Every child of a batch can be inspected, selected, and deselected with
      the keyboard alone.
- [ ] A child excluded from the batch is unselectable and says why in text.
- [ ] No hidden or disabled control is reachable by Tab while invisible.
- [ ] The primary action names the scope it will save, including the selected
      count, so it is unambiguous when read out of context.

### 6. Error summaries

- [ ] A refused Save renders one summary listing every refused field.
- [ ] Each entry links to its control, and following the link moves focus to
      that control.
- [ ] The summary states the count, so a screen-reader user knows how much
      work is ahead before reading the list.

### 7. Before-and-after values and evidence references

- [ ] The accepted value and the incoming value are announced in order, each
      identified by its column and by the field in its row header.
- [ ] Differences are never conveyed by strike-through, weight, or colour
      alone.
- [ ] Every value shows its exact source in text — document, page or row, or
      message — beside the claim, not only inside an image or a crop.
- [ ] A contradiction shows both passages; neither is collapsed into one
      incoming value.

### 8. The interaction the queue already has, preserved

Factoring the primitives out must not lose what the adjudication queue
already does well. A review screen keeps:

- [ ] split evidence panes, so two revisions are compared side by side rather
      than through a toggle (`_evidence.html`);
- [ ] before-and-after differences on the item itself;
- [ ] the source crop, scrolled to the cited row, with its quote in text;
- [ ] keyboard hints on the primary actions;
- [ ] sticky actions that stay reachable while the reading scrolls;
- [ ] an explicit statement of why this item is in front of the coordinator
      (`ui.reason_presented`).

## Out of scope here

- A design system, a component library, a CSS framework, or a build step.
- Client-side rendering. Every primitive renders on the server and works with
  no script.
- Automated conformance scoring against a published guideline. The items above
  are the contract; a broader audit is a separate decision.

## Boundary this checklist does not move

Policy-recorded `Applies To: not yet known` is current Project Record state,
not a confirmation question. A mechanically admitted unknown-scope screen shows
the known facts read-only, continues with the assigned project person and Next
Action, and keeps the unknown scope as an Attention Reason. It never renders a
scope-mode quiz and never saves an attributable no-op confirmation
([ADR-0035](adr/0035-human-work-is-attributable-guided-and-reversible.md),
[ADR-0036](adr/0036-external-party-commitments-carry-attribution-precision-and-scope.md),
[ADR-0039](adr/0039-guided-statement-adjudication-is-evidence-bound-atomic-and-reversible.md),
[ADR-0042](adr/0042-authority-follows-proof-policy-writes-exact-cases-agents-assist-and-humans-decide-ambiguity.md)).
The pending Extracted Proposal screen may still ask for scope where scope is
the actual unresolved human decision. Nothing in this checklist authorises a
confirmation control that records no decision.
