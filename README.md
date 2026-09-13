# 📱 AURA

**UI-based Android data acquisition with reviewable records**

*Acquisition through User Interfaces with Reviewable Artifacts and Outcomes*

AURA is a research tool for acquiring application data through the UI of an authorized, unlocked Android device. Application-version profiles guide acquisition, while UI actions, screenshots, UI XML, acquired files and non-acquisition reasons are preserved for later review.

[Quick start](#quick-start) · [Supported apps](#supported-apps) · [Profiles](profiles/README.md) · [Results](#results) · [Validation](#validation) · [Tests](#tests) · [Citation](#citation) · [License](#license)

> ⚠️ **Acquisition changes device state.** AURA starts with airplane mode ON, Wi-Fi OFF and Do Not Disturb ON. Planned online acquisition enables Wi-Fi. These settings are **not restored** when the session ends, including after an error or interruption. Use only devices and data you are authorized to examine.

<a id="quick-start"></a>

## 🚀 Quick start

### 1. Prepare the host and device

You need Python **3.13 or newer** (tested with 3.13), ADB, and exactly one connected Android device. Enable USB debugging, authorize the host's ADB key, unlock the device, and sign in to the target applications before acquisition.

From the repository root, install AURA and its runtime dependencies:

```bash
python3 -m pip install -e .
adb devices -l
```

Keep the repository's `profiles/` directory alongside the source and run the commands below from the repository root.

### 2. Check the installed applications

```bash
python3 -m aura inspect
```

This reports the connected device, selected System UI profile, installed application versions and profile matches without starting acquisition. Check the target applications before proceeding.

- **`exact`** — an eligible profile matches the installed app version.
- **`nearest`** — an eligible nearby-version profile was selected; verify its UI compatibility separately.
- **`no_profile`** — no eligible profile is available for that application.

### 3. Acquire data

Start with one application:

```bash
python3 -m aura acquire telegram
```

Or select several applications for the same session:

```bash
python3 -m aura acquire telegram notion google_drive
```

The selected profiles determine the acquisition methods and environments. AURA finishes the planned device-only work before proceeding to planned online work.

> **WhatsApp Export:** selecting `whatsapp` also schedules the Export method in its profile. Prepare Bluetooth pairing and file reception on the analysis computer before running it. See [Bluetooth export delivery](#bluetooth-export) for the transfer path and environment requirements.

<details>
<summary>Windows Bluetooth receiver dependency</summary>

Install the optional Windows dependency when using the Windows Bluetooth File Transfer receiver:

```bash
python3 -m pip install -e ".[windows]"
```

Ensure the receiver is paired, discoverable as required for the transfer, and ready to receive files.

</details>

Do not operate the phone manually or change its network state while acquisition is running.

<a id="supported-apps"></a>

## 🧩 Supported applications

**Materialize** records UI-displayed information and acquires individual files through the application's save or download controls. **Export** uses the application's own export or backup function to obtain its output.

| Application | CLI identifier | Methods | Acquisition scope |
| --- | --- | --- | --- |
| Telegram | `telegram` | Materialize | Account information, chats, messages, media and files |
| WhatsApp | `whatsapp` | Materialize, Export | Chats, messages, media and chat exports |
| Notion | `notion` | Materialize, Export | Accounts, workspaces, pages, database entries, attachments and exports |
| Notesnook | `notesnook` | Materialize, Export | Notes, revisions, attachments, trash and note exports |
| Google Chrome | `chrome` | Materialize | Account information, history, download entries, bookmarks and recent tabs |
| Samsung Browser | `samsung_browser` | Materialize | History, download entries, saved-page entries and bookmarks |
| Google Drive | `google_drive` | Materialize | My Drive folders, file information, activity and downloadable files |

WhatsApp document information is retained with message records; document originals are outside its Materialize scope. Browser download entries are list records, not a claim that the corresponding original files were acquired. Google Drive collects inventory and metadata device-only, then adds activity and file downloads online.

See the [profile catalogue](profiles/README.md#catalogue) for exact app versions and Android constraints. An available profile does not by itself establish compatibility with every device or UI state.

<a id="acquisition-flow"></a>

## ⚙️ Acquisition flow

UI actions and evidence are recorded throughout the session.

1. **Prepare:** record the initial device state and verify airplane mode ON, Wi-Fi OFF and Do Not Disturb ON.
2. **Plan:** identify installed applications, select profiles and build the execution plan.
3. **Acquire device-only:** perform the planned procedures, check their results and package each run.
4. **Acquire online when planned:** retain airplane mode and Do Not Disturb, enable Wi-Fi, verify connectivity and execute the online procedures.
5. **Finalize:** record the final device state and preserve the session record and its hash without restoring settings.

Online acquisition is planned work, not an automatic retry triggered by an individual item's failure. AURA controls device connectivity but does not configure an AP or restrict traffic to application-specific servers. Application data and cache changes are not reversed.

If acquisition stops early, completed packages and the interruption point remain recorded. If the final state cannot be read, AURA records that failure separately from the last confirmed state.

<a id="bluetooth-export"></a>

### WhatsApp Export: Bluetooth delivery

WhatsApp exports are sent through Android's share interface to the configured, paired analysis computer over Bluetooth. AURA checks the paired receiver, records Bluetooth preparation and receiver selection, waits for a received file to stabilize, and preserves it with the acquisition records. Bluetooth state is restored after the transfer run; this is separate from the session's airplane-mode, Wi-Fi and Do Not Disturb settings, which remain unchanged at session end.

Here, **device-only** means external server access is blocked, not that all local transfer channels are disabled. For a device-only export, keep Wi-Fi and cellular connectivity disabled, use the intended analysis computer as the receiver, and keep Bluetooth tethering disabled. AURA selects the configured receiver but does not implement Bluetooth traffic filtering or verify that tethering is disabled. Treat the Bluetooth transfer as an explicit part of the acquisition procedure and review its recorded actions and results.

<a id="results"></a>

## 📦 Results and records

Each executed application–method–environment combination has its own run identifier and package:

```text
runs/
├── <run-id>/
│   ├── acquisition.json
│   ├── events.jsonl
│   └── artifacts/
├── <run-id>.zip
├── <run-id>.zip.sha256
└── sessions/<session-id>/
    ├── session.json
    └── session.sha256
```

| Record | What it preserves |
| --- | --- |
| `session.json` | Device and app versions, profile selection, execution order, environment transitions, run-package references and final state |
| `acquisition.json` | Selected profile, target-identification records, items, timed attempts, procedure/acquisition states and evidence references |
| `events.jsonl` | Chronological UI actions and evidence-retention events |
| `artifacts/` | UI-extracted records, acquired files, screenshots and UI XML |
| `files.json` **inside each ZIP** | Paths, sizes and SHA-256 values of every other ZIP member |

**Procedure completion and data acquisition are separate results.** A procedure can finish without acquiring the expected file. An unstarted attempt retains its reason with null acquisition status and null actual start/end times. Screenshots and XML document the UI; they do not establish acquisition of an original file.

The session hash file uses the format `<digest>  session.json\n`. Keep the session files, run ZIPs and their hashes together when preserving or moving an acquisition.

<a id="validation"></a>

## 🔎 Validate and trace

The independent validator uses the Python standard library without importing AURA's acquisition or packaging code.

### Check a session

```bash
python3 tools/validate_aura_results.py \
  runs/sessions/<session-id>/session.json
```

Checks cover session and package hashes, member paths and sizes, profile/record correspondence, identification counts, procedure states and times, and recorded provenance references.

A schema 2 result with `"valid": true` and `"validation_scope": "schema2_record_consistency"` means those integrity and consistency checks passed. It does **not** certify the truth of the recorded content or complete acquisition of every target. Consistent partial or interrupted acquisitions can also pass.

### Review a file or a non-acquisition outcome

```bash
python3 tools/validate_aura_results.py \
  runs/sessions/<session-id>/session.json \
  --run <run-id> --trace artifact-000001

python3 tools/validate_aura_results.py \
  runs/sessions/<session-id>/session.json \
  --run <run-id> --trace outcome-000001
```

Identifiers restart within each run, so include `--run` to avoid ambiguity. The trace returns the recorded item, attempt, action chain and direct observation reference where available. Related screen records can also be inspected through the attempt identifier; an absent direct reference is not replaced by a guessed screen.

<details>
<summary>Legacy records and bulk-data references</summary>

Schema 1 records are checked under the narrower `legacy_integrity_and_references` scope; they are not treated as schema 2 records. Unsupported schema versions fail validation.

For bulk data, `associated_items` identifies validated row or message bindings while retaining the actual collection attempt. Original JSON files acquired from an app remain user data: arbitrary keys inside them are not interpreted as AURA provenance references.

</details>

<a id="tests"></a>

## 🧪 Tests and validation

Install the test dependencies and run the included device-free suite from the repository root:

```bash
python3 -m pip install -e ".[test]"
python3 -m pytest -q
```

The **September 13, 2026** check of this distribution passed **787 tests**. The suite covers profiles, collectors, recording, packaging and the independent validator using fake devices and temporary directories. Public fixtures use placeholder personal identifiers and synthetic or reduced UI data; the suite does not operate a connected phone.

See the [validation guide](docs/VALIDATION.md) for evaluated device and application configurations, execution outcomes, and the scope of automated checks.

<a id="repository-layout"></a>

## 🗂️ Repository guide

```text
README.md       Setup, acquisition and result-review guide
CITATION.cff    Software and manuscript citation metadata
LICENSE         Apache License 2.0
pyproject.toml  Package configuration and dependencies
.gitignore      Exclusions for generated data and local files
src/aura/       Acquisition runtime, UI control and app collectors
profiles/       Application-version and manufacturer System UI profiles
tools/          Independent result validator
tests/          Device-free tests and regression fixtures
docs/           Evaluation and validation scope
```

- [Profile reference](profiles/README.md) — supported versions, fields, selectors and validation.
- [Validation status](docs/VALIDATION.md) — what was tested and what remains outside that scope.

Acquisition packages can contain sensitive data. Keep generated `runs/` directories and private data out of version control.

<a id="citation"></a>

## 📖 Citation

If you use AURA in research, cite the following work and identify the repository revision used.

```bibtex
@unpublished{Kim2026AURA,
  author = {Kim, Junki and Park, Jungheum and Lee, Sangjin},
  title  = {{AURA}: A Framework for {UI}-Based Mobile Data Acquisition and Provenance Review},
  year   = {2026},
  note   = {Manuscript submitted to Forensic Science International: Digital Investigation}
}
```

Machine-readable software and manuscript metadata are available in [CITATION.cff](CITATION.cff). Source code: [JJun1207/AURA](https://github.com/JJun1207/AURA).

<a id="license"></a>

## ⚖️ License

AURA is licensed under the [Apache License 2.0](LICENSE). Third-party dependencies remain subject to their respective licenses. This software license does not grant permission to access devices, accounts or data without authorization.
