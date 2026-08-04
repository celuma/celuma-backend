# Report fixtures (Céluma 1.3, Phase 1 — Workstream 5)

Anonymized JSON fixtures that represent a report body as persisted in S3
(`reports/{tenant}/{branch}/{report}/versions/{n}/report.json`). None contain
real patient data, identifiable diagnoses, or real medical files — all names,
license numbers, and image URLs are synthetic (`https://cdn.example.invalid/...`
is never resolved or downloaded in tests).

Each file carries a `_fixture_meta` field (ignored by production code, only
for this document) indicating which Workstream 5 matrix case(s) it covers.

## Mapping to the `Céluma1.3-Fase1.md` matrix

| Required case | Fixture(s) |
|---|---|
| 1. One sample | `draft_single_sample_no_images.json` |
| 2. Multiple samples | `published_multi_sample_with_images_all_sections.json` |
| 3. Report with images | `published_multi_sample_with_images_all_sections.json` |
| 4. Report without images | `draft_single_sample_no_images.json` |
| 5. Short content | `draft_single_sample_no_images.json` |
| 6. Multi-page content | `long_content_multipage.json` |
| 7. Empty optional sections | `empty_optional_sections.json` |
| 8. All sections complete | `published_multi_sample_with_images_all_sections.json` |
| 9. Report without patient | `no_patient_report.json` |
| 10. Released report | `published_multi_sample_with_images_all_sections.json` (`status: PUBLISHED`) |
| 11. Draft report | `draft_single_sample_no_images.json` (`status: DRAFT`) |
| 12. Special characters and accents | `special_characters_accents.json` |
| 13. Long texts that force page breaks | `long_content_multipage.json` |
| 14. Historical report with older structure | `legacy_oldest_structure.json` |

`legacy_oldest_structure.json` is the only fixture without `base_order`,
`section_order`, or `signatureMetadata`, and without the base field
`requesting_physician` (added later — see
`LEGACY_PREDEFINED_BASE_HIDDEN` in `celuma-frontend/src/models/report.ts`).
No fixture includes `schema_version` because that field does not exist in
production today — see `report-compatibility-strategy.md`.

## Usage

- Backend: `celuma-backend/tests/test_report_json_contract.py` loads these
  files and validates they deserialize against the current Pydantic schemas
  (`app/schemas/report.py`) without needing a database.
- Frontend: equivalent copies in
  `celuma-frontend/src/test/fixtures/reports/` (same content, imported as
  TS modules) are used to test `src/models/report.ts` and the
  `ReportPreviewPages` renderer.

## Updating fixtures / snapshots

These fixtures represent **current** behavior and must not be changed just
so a test "passes" — if a test fails after a code change, first determine
whether the behavior change is intentional. If it is, update the fixture
and the test in the same commit, explaining why in the commit message. New
snapshots were not auto-approved in this phase.
