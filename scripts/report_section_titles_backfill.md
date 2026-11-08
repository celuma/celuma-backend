# Description title correction

The command requires a database at revision `v1_4_1` or a compatible revision, and the bucket configured for that same instance. Publish the backend and frontend code first. Run it with an operational account, one laboratory at a time, in a window coordinated with template editing, review, signing, and cleanup. Verify the PostgreSQL backup and storage access before applying.

```sh
python -m scripts.backfill_report_section_titles --tenant-id "$TENANT_UUID"
python -m scripts.backfill_report_section_titles --tenant-id "$TENANT_UUID" --apply --actor-id "$OPERATOR_UUID"
python -m scripts.backfill_report_section_titles --tenant-id "$TENANT_UUID"
```

The first and third commands are read-only inventories. Change counts match what would be written. `--report-id` limits the command to one laboratory report and skips templates. Exit code 2 means busy reports or cases that need review. Exit code 1 means an interruption. A new run resumes from the committed state.

The command keeps custom labels and historical template revisions. On eligible reports, it changes only the two default body labels, their snapshot, and, in the original cycle, the shared copy. It includes unsigned `APPROVED` reports and keeps the approval. A version of an open amendment belongs to its own cycle. PDFs created before signing are kept and, when their origin can be verified, are removed from the active download until they are regenerated. Signed or published releases and their artifacts are preserved.

Each report is committed in one transaction. The `REPORT_VERSION.BACKFILL_DESCRIPTION_LABELS` record keeps the backup identifier, the previous and next hashes, and the previous PDF identifier. If PostgreSQL fails after an upload, the next attempt verifies and reuses the prepared copy. It may temporarily appear as an orphan during reconciliation. Keep confirmed backups for the verification window. Laboratory cleanup recognizes their relationship to the version.

To recover a report, first confirm that it is still in the same open cycle and that the hashes match the audit. Restore the storage reference and metadata under the same locks, reconcile usage, and let the normal flow regenerate the pre-signing PDF. A release that was already signed is corrected through an amendment. If a logical template is restored, apply a new revision and keep the existing history.
