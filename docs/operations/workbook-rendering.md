# Native UCM rendering

The renderer writes the accepted Project Record into the registered output
template using the registered field mapping. It preserves unrelated package
parts byte-for-byte and reports each changed part.

`workbook_render_v2` corrects table ownership during declared row insertion.
When `extend_table_ref` is enabled, worksheet relationships must identify one
table covering the matrix header, existing rows and appended columns. Tables
beside the matrix or on another worksheet remain unchanged. The owning table's
filter follows its range. Missing or ambiguous ownership, totals rows, and
inconsistent table/filter ranges refuse rendering instead of guessing.

New Issue Profile registrations must name `workbook_render_v2` for the updated
UCM artifact. A profile pinned to v1 does not silently select v2: declare the
new profile through the existing profile-registration act before preparing a
new issue. Existing sealed Release Packages retain their original renderer
identity and bytes; this change does not regenerate them or re-adopt a baseline.
