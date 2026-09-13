# Testing and validation

Validation covers automated tests, the four-device evaluation reported in the
accompanying manuscript, and a separate single-device execution check. Results
apply to the versions, data and acquisition conditions used in each evaluation.

## Evaluation reported in the paper

The paper covers seven applications and ten application–method combinations on
Samsung Galaxy S8, Samsung Galaxy S21 5G, Google Pixel 5 and Huawei P30 Lite.
The final acquired data were compared with the prepared evaluation data.
The completion status below refers to the evaluation, not to every individual
attempt or acquisition environment.

| Application | Evaluated version | Methods evaluated | Evaluation performed |
| --- | --- | --- | --- |
| Telegram | 12.9.2 | Materialize | Completed |
| WhatsApp | 2.26.27.85 | Materialize, Export | Completed |
| Notion | 0.6.4030 | Materialize, Export | Completed |
| Notesnook | 3.4.5 | Materialize, Export | Completed |
| Chrome | 138.0.7204.179 on Android 9; 150.0.7871.124 on Android 14 | Materialize | Completed |
| Samsung Internet | 30.0.0.67 | Materialize | Completed |
| Google Drive | 2.26.337.0.all.alldpi | Materialize | Completed |

The paper also examines condition-specific non-acquisition, including attachment
information retained without an original file. Completion of the evaluation
therefore does not mean that every file was acquired under every condition.
See the [manuscript citation](../README.md#citation) for the accompanying paper.

## Included automated tests

Run the device-free suite from the repository root:

```bash
python3 -m pip install -e ".[test]"
python3 -m pytest -q
```

The September 13, 2026 check of the public distribution passed **787 tests**.
The suite in `tests/` uses fake devices and temporary directories and does not
start a physical-device acquisition. It checks profile rules, application
collectors, recording, packaging and independent validation, including regression
cases for failures and incomplete acquisitions. Public test data use placeholder
personal identifiers and synthetic or reduced UI fixtures.

The distribution also supports [profile loading checks](../profiles/README.md#authoring)
and [validation of acquired session records](../README.md#validation).

## Single-device execution check — September 13, 2026

This separate check used one Samsung SM-G991N running Android 14 with the
installed versions and selected execution outcomes below.

| Application | Installed version | Selected execution outcomes |
| --- | --- | --- |
| Chrome | 150.0.7871.124 | Device-only Materialize complete |
| Samsung Browser | 30.0.0.67 | Device-only Materialize complete |
| Telegram | 12.9.0 | Materialize complete in both environments |
| Google Drive | 2.26.337.0.all.alldpi | Materialize complete in both environments |
| Notesnook | 3.4.5 | Materialize complete in both environments; device-only Export complete |
| Notion | 0.6.4030 | Materialize partial in both environments; online Export complete |
| WhatsApp | 2.26.27.85 | Materialize partial in both environments; Export not tested |

The 14 selected executions came from multiple sessions: ten complete and four
partial. All selected packages passed integrity and record-consistency checks.
App data and caches were not reset between sessions, so the sessions are not
repetitions from identical initial states. WhatsApp Export was excluded from
this check; its completion in the paper evaluation is reported separately above.

Telegram retained 30 original files in each selected execution.
Google Drive retained 25 files online and metadata for 25 files and nine folders.
Notion encountered unavailable page-entry or workspace UI information during
Materialize; Export retained seven ZIP files across three
workspaces. WhatsApp preserved a download failure asking for the photo to be
resent; no message or resend request was sent.

Drive's bounded row-readiness wait and obscured-row recovery are covered by
regression tests. Neither recovery branch was triggered in this device check.

Record-consistency validation does not certify the truth of the recorded
content or complete acquisition of every target. Direct observation references
are available where recorded; otherwise related screen records can be inspected
through the acquisition-attempt identifier.

### Version coverage

- Telegram 12.9.2 shares acquisition content with 12.9.0.
- Chrome 138.0.7204.179 is restricted to Android 9 and shares acquisition content
  with Chrome 150.0.7871.124, which is restricted to Android 10 and newer.
- Those variant combinations were checked for content equality, loading and
  applicability, but were not physically tested in this single-device check.

Manufacturer System UI profiles contain candidate controls; compatibility
depends on the device and operating-system version.
