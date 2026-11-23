# AURA evaluation data and provenance review examples

This release accompanies *AURA: A Framework for UI-Based Data Acquisition from Android Applications with Reviewable Provenance*. The two CSV files summarize the four-device repeated-acquisition experiment. The two ZIP files contain separately prepared synthetic examples that readers can inspect with the AURA result validator.

| File | Contents |
| --- | --- |
| [runs.csv](runs.csv) | One row for each of 228 executions, with conditions, elapsed time, storage, recorded status, and the target and procedure counts reported in the appendix. |
| [item-counts.csv](item-counts.csv) | 792 item-type counts for the runs in Table 8. Join the two CSVs on the public identifier `run_id`. |
| [Drive example](examples/drive-preserved-file.zip) | Start with a preserved text file and trace its source folder and download action. |
| [Telegram example](examples/telegram-partial-acquisition.zip) | Start with an attachment record and examine why the procedure completed with only a partial acquisition. |

## Reading the run summaries

Each configuration is one device, application, method, and environment. The 76 configurations were each run three times. Each device contributed 57 runs, for 108 device-only and 120 controlled-online runs overall. The separate cache, scale, reviewer, and device-change experiments are outside this dataset.

Public run identifiers are local to this release. The CSVs contain numeric results and experiment conditions. Original conversations, page contents, attachments, filenames, host and device paths, account information, and original session and run identifiers remain private.

| Fields in `runs.csv` | How to read them |
| --- | --- |
| `device`, `android_version`, `application`, `application_version`, `method`, `environment`, `repetition` | Published device label and conditions of the run. Repetition is 1, 2, or 3 within the configuration. |
| `logged_run_status` | Overall status emitted by AURA for the archived execution. |
| `logged_procedure_completed`, `logged_procedure_interrupted`, `logged_procedure_not_attempted` | Tally of the procedure statuses of **all direct attempt records** in the archive, including operational collection attempts. |
| `reported_targets_expected`, `reported_targets_identified` | Reference targets within the evaluation scope and targets identified in the run (Tables A.1 and A.2). |
| `reported_targets_acquired`, `reported_targets_partial`, `reported_targets_not_acquired`, `reported_targets_not_attempted` | Outcomes of individually assessed targets, following the published appendix. An empty cell means that individual assessment does not apply. Zero is a count. |
| `reported_targets_assessed_via_list_or_chat` | Targets assessed through their parent list or chat. Adding this to the four individual outcome counts gives the number identified. |
| `reported_procedures_completed`, `reported_procedures_interrupted`, `reported_procedures_not_attempted` | Procedures in the published evaluation scope. The unit is a procedure, and the scope can differ from the direct attempts counted under `logged_`. |
| `elapsed_seconds` | Execution including run packaging. Shared session preparation, transitions and restoration are reported separately in the paper. Group by configuration to reproduce the run-time cells of Table 9. |
| `acquired_data_bytes`, `screen_evidence_bytes`, `other_record_bytes`, `total_bytes` | Uncompressed package-member sizes. Screen evidence includes PNG and UI XML. Native exports count once at their retained size. The first three columns sum to the total. To reproduce Table 10, average each column by application, method and environment and divide by 1,048,576. |

### Comparing item counts

In `item-counts.csv`, `reported_count` gives the final Table 8 value for the specified run and item type. Compare the complete set of these counts across repetitions to obtain the published result of **71 of 76 configurations with identical counts**. An explicit zero means that no item of an in-scope type was acquired. An out-of-scope type has no row.

`archived_summary_count` preserves the corresponding value from the earlier per-run evaluation worksheet, including its in-scope export file count. The two columns agree on 787 of 792 rows. Five Pixel 5 Telegram attachment rows differ. The earlier worksheet lists 27 for device-only repetitions 2 and 3 and 28 for all three controlled-online repetitions. The final evaluation reports 30 for each of those five runs. The archived worksheet is itself a scoped summary, and the public files do not contain the private underlying attachments.

The unit matters when comparing the two CSVs. Telegram's Table 8 measures include 30 reference attachments and 135 comparison-set messages. The appendix counts one account, eight chats and 30 attachments as 39 individually assessed targets, with the 135 messages assessed through their chats. For Export, the appendix assesses the requested archives, while Table 8 counts their in-scope contents. For WhatsApp these comprise chat transcripts, photos, and videos. Chrome bookmarks include both links and folders. Notion's exported database count refers to CSV data rows.

## Using the examples

Extract either example ZIP into its own folder and open its README. It provides a starting record, a validator command, a manual route through the records, and the expected result. Each example contains one native schema-version-2 session and one run archive, built with the AURA recording and packaging code against a simulated device. The example filenames, source labels, screen illustrations, UI XML, and file contents were created for public use. These examples are separate from the 228 measured executions. Timestamps and durations inside the examples describe their generation.
