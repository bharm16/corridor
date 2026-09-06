# PDF extraction evaluation — corridor_pdf_reader frozen-reader commit c39363e, pypdfium2 5.13.0 (PDFium 153.0.7999.0), pypdf 6.17.0, engine tagged at 36 dpi

Dataset: `2026-08-31.2`
Configuration: `9e04839879161682b6f1839ba45e57903bdb10477e0e39340301dfe138f5722d`
Result: **FAIL**

## Source-citation safety

Wrong source-cited Extracted Proposals avoided: 7/7; wrong proposals emitted: 0.

## Per page class

| Page class | Pages | Coverage F1 | Class accuracy | Table F1 | Row F1 | Column F1 | Cell F1 | Exact text | Row disposition | Cell spans | Cell topology | Header relationships | Canonical mapping | Cell state | Page-scoped values F1 | Abstention | Failure | Latency ms | Peak memory bytes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ambiguous-retirement-matrix | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 864 | 110247936 |
| image-only-agreement | 1 | 1.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 358 | 106643456 |
| marked-resolution-matrix | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 864 | 110247936 |
| matrix-body | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.087 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 2039 | 181092352 |
| matrix-continuation | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 908 | 120668160 |
| matrix-cover | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 2039 | 181092352 |
| mixed-native-scanned-agreement | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 390 | 89145344 |
| page-scoped-owner-matrix | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 847 | 115376128 |

## Per document

| SHA-256 | Pages | Coverage F1 | Class accuracy | Table F1 | Row F1 | Column F1 | Cell F1 | Exact text | Row disposition | Cell spans | Cell topology | Header relationships | Canonical mapping | Cell state | Page-scoped values F1 | Abstention | Failure | Latency ms | Peak memory bytes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `0462b167206f1b2cee0f79d3c37388e511be138825e4b6018811f78dca541611` | 1 | 1.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 358 | 106643456 |
| `5b9c39bf570447c23e4e4ad2e02629576005697d39b8e52f1083b5fe10e2c1be` | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 847 | 115376128 |
| `6cf9abd1e4c0f6071c2eb1dffcac75da1045d3074718212caf7b5fe67c1988a2` | 1 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 390 | 89145344 |
| `8b93d8b934b8501b5464ff33db9f2e83c2f716b2910c7eb5385dde9d45075a30` | 1 | 1.000 | 0.000 | 1.000 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 908 | 120668160 |
| `cfded966dd3395e4a1d87370c8da496844e05df499ac786440459fd926ae0db8` | 2 | 1.000 | 0.000 | 0.667 | 0.000 | 0.048 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 2039 | 181092352 |
| `e619a4abf6044ee17415d4f6933ec9ef674c67735befb172a8d6da5a691e4b9a` | 2 | 1.000 | 0.000 | 0.667 | 0.000 | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 864 | 110247936 |

## Acceptance ceilings

- [x] page_coverage
- [ ] page_class_accuracy
- [ ] table_f1
- [ ] row_f1
- [ ] column_f1
- [ ] cell_f1
- [x] cell_text_exact
- [x] row_disposition_exact
- [x] cell_span_exact
- [x] cell_topology_exact
- [x] header_relationships_exact
- [x] canonical_mapping_exact
- [x] cell_state_exact
- [ ] page_scoped_values
- [x] wrong_source_cited_proposals
- [x] failure_rate
- [x] abstention_rate
- [x] latency
- [x] memory
