# Fixtures de reportes (Céluma 1.3, Fase 1 — Workstream 5)

Fixtures JSON anonimizados que representan el body de un reporte tal como se
persiste en S3 (`reports/{tenant}/{branch}/{report}/versions/{n}/report.json`).
Ninguno contiene datos reales de pacientes, diagnósticos identificables ni
archivos médicos reales — todos los nombres, cédulas y URLs de imagen son
sintéticos (`https://cdn.example.invalid/...` nunca se resuelve ni se
descarga en las pruebas).

Cada archivo trae un campo `_fixture_meta` (ignorado por el código de
producción, solo para este documento) indicando qué caso(s) de la matriz del
Workstream 5 cubre.

## Mapeo a la matriz de `Céluma1.3-Fase1.md`

| Caso requerido | Fixture(s) |
|---|---|
| 1. Una muestra | `draft_single_sample_no_images.json` |
| 2. Varias muestras | `published_multi_sample_with_images_all_sections.json` |
| 3. Reporte con imágenes | `published_multi_sample_with_images_all_sections.json` |
| 4. Reporte sin imágenes | `draft_single_sample_no_images.json` |
| 5. Contenido corto | `draft_single_sample_no_images.json` |
| 6. Contenido de varias páginas | `long_content_multipage.json` |
| 7. Secciones opcionales vacías | `empty_optional_sections.json` |
| 8. Todas las secciones completas | `published_multi_sample_with_images_all_sections.json` |
| 9. Reporte sin paciente | `no_patient_report.json` |
| 10. Reporte liberado | `published_multi_sample_with_images_all_sections.json` (`status: PUBLISHED`) |
| 11. Reporte en borrador | `draft_single_sample_no_images.json` (`status: DRAFT`) |
| 12. Caracteres especiales y acentos | `special_characters_accents.json` |
| 13. Textos largos que provocan saltos de página | `long_content_multipage.json` |
| 14. Reporte histórico con estructura más antigua | `legacy_oldest_structure.json` |

`legacy_oldest_structure.json` es el único fixture sin `base_order`,
`section_order` ni `signatureMetadata`, y sin el campo base
`requesting_physician` (agregado después — ver
`LEGACY_PREDEFINED_BASE_HIDDEN` en `celuma-frontend/src/models/report.ts`).
Ningún fixture incluye `schema_version` porque ese campo no existe hoy en
producción — ver `report-compatibility-strategy.md`.

## Uso

- Backend: `celuma-backend/tests/test_report_json_contract.py` carga estos
  archivos y valida que deserializan contra los schemas Pydantic actuales
  (`app/schemas/report.py`) sin necesitar base de datos.
- Frontend: copias equivalentes en
  `celuma-frontend/src/test/fixtures/reports/` (mismo contenido, importado
  como módulos TS) se usan para probar `src/models/report.ts` y el renderer
  `ReportPreviewPages`.

## Actualizar fixtures / snapshots

Estos fixtures representan el comportamiento **actual** y no deben
modificarse para que una prueba "pase" — si una prueba falla después de un
cambio de código, primero hay que determinar si el cambio de comportamiento
es intencional. Si lo es, actualizar el fixture y la prueba en el mismo
commit, explicando el motivo en el mensaje de commit. No se aprobaron
snapshots nuevos automáticamente en esta fase.
