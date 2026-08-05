# Machine-authored gold set — wsdot-9540

Document: `U3-2021-02-05-167-1b-PE-UTL-UtilityListing-Gas.pdf, U3-2021-04-08-167-1b-PE-UTL-UtilityListing-OPL.pdf, U3-2021-04-08-167-1b-PE-UTL-UtilityListing-Power.pdf, U3-2021-04-08-167-1b-PE-UTL-UtilityListing-Sewer.pdf, U3-2021-04-08-167-1b-PE-UTL-UtilityListing-Water.pdf, U3-2021-04-09-167-1b-PE-UTL-UtilityListing-Comm.pdf`

**This number is a ceiling, not a full measurement** (#81 as
amended, 2026-08-04). The enumeration behind it shares PyMuPDF's
table detection with the extractor: a region that library drops is
invisible to both readings and cannot be missed here. What it does
not share is the part that usually errs — no model, no extractor
column mapping; the header is anchored by its own printed text and
the critical marks are grid cells read through the same vocabulary
the Ledger uses (ADR-0009, ADR-0012).

- gold rows: 192
- labelled for criticality: 176 (120 yes / 56 no); 16 blank — unsettled or unmarked, out of the ≥95% denominator
- excluded: 0 retired rows, 0 empty slots (ADR-0012)

## Strengthening the ceiling toward a measurement

One look per page image closes the shared blind spot. Optional,
any time after the run:

- [ ] page 1 — `out/page-images/94fd41adde257c8a2948b529d2c8a0547021e578462241efa7325f523f8d6c27/0001.png`
- [ ] page 1 — `out/page-images/e1c7895b56021f86317efa8074aa6f298087d8f0bd4ac8652f17721f99bb0d3b/0001.png`
- [ ] page 1 — `out/page-images/1b949a1a1e26fcdb457df7ba265888d7bfe107b1d63c14ea0b1e56082620e228/0001.png`
- [ ] page 2 — `out/page-images/1b949a1a1e26fcdb457df7ba265888d7bfe107b1d63c14ea0b1e56082620e228/0002.png`
- [ ] page 1 — `out/page-images/36acb221898e1a771f955a80e1136d3bd7117e1fd6fd4b36d542c84454bf3361/0001.png`
- [ ] page 1 — `out/page-images/3c53895423d57b61959b74d146cd0076d11245328c354540a1e509fc17cb6f1c/0001.png`
- [ ] page 1 — `out/page-images/d794938ff8c9c29004586a98ab5d7aeeb8904305527980c3dc5a74fede5b26db/0001.png`
- [ ] page 2 — `out/page-images/d794938ff8c9c29004586a98ab5d7aeeb8904305527980c3dc5a74fede5b26db/0002.png`
- [ ] page 3 — `out/page-images/d794938ff8c9c29004586a98ab5d7aeeb8904305527980c3dc5a74fede5b26db/0003.png`
