# 🧩 AURA Profiles

**Version-specific acquisition settings and manufacturer System UI controls**

Profiles describe the data to identify, the acquisition methods and environments, the UI elements to use, and the criteria for evaluating results. Application collectors execute those settings; System UI profiles support device preparation and OS-owned controls.

[File layout](#layout) · [Version catalogue](#catalogue) · [Application fields](#application-fields) · [Selectors](#selectors) · [System UI](#system-ui) · [Validate a profile](#authoring)

[← Back to AURA](../README.md)

<a id="layout"></a>

## 📁 File layout

There are two kinds of profiles:

| Kind | Location | Purpose |
| --- | --- | --- |
| Application | `apps/<app-id>/<app-version>.json` | App-version-specific acquisition settings |
| System UI | `system_ui/<profile-name>.json` | Manufacturer-matched candidates for device settings and OS-owned UI |

```text
profiles/
├── apps/
│   ├── telegram/
│   │   ├── 12.9.0.json
│   │   └── 12.9.2.json
│   ├── chrome/
│   │   ├── 138.0.7204.179.json
│   │   └── 150.0.7871.124.json
│   └── …/
└── system_ui/
    ├── generic.json
    ├── huawei.json
    └── samsung.json
```

**The app version is the JSON filename, not a directory.** AURA no longer uses `<app-version>/<profile-version>.json` or an independent `profile_version` field.

<a id="catalogue"></a>

## 📚 Application-version catalogue

| Application | Profile file | Android filter |
| --- | --- | --- |
| Google Chrome | [138.0.7204.179.json](apps/chrome/138.0.7204.179.json) | 9 |
| Google Chrome | [150.0.7871.124.json](apps/chrome/150.0.7871.124.json) | 10+ |
| Google Chrome | [156.0.8078.79.json](apps/chrome/156.0.8078.79.json) | 10+ |
| Google Chrome | [157.0.8103.48.json](apps/chrome/157.0.8103.48.json) | 10+ |
| Google Drive | [2.26.147.5.all.alldpi.json](apps/google_drive/2.26.147.5.all.alldpi.json) | — |
| Google Drive | [2.26.297.3.all.alldpi.json](apps/google_drive/2.26.297.3.all.alldpi.json) | — |
| Google Drive | [2.26.337.0.all.alldpi.json](apps/google_drive/2.26.337.0.all.alldpi.json) | — |
| Google Drive | [2.26.437.0.all.alldpi.json](apps/google_drive/2.26.437.0.all.alldpi.json) | — |
| Google Drive | [2.26.447.0.all.alldpi.json](apps/google_drive/2.26.447.0.all.alldpi.json) | — |
| Google Drive | [2.26.447.1.all.alldpi.json](apps/google_drive/2.26.447.1.all.alldpi.json) | — |
| Notesnook | [3.4.15.json](apps/notesnook/3.4.15.json) | — |
| Notesnook | [3.4.5.json](apps/notesnook/3.4.5.json) | — |
| Notion | [0.6.4030.json](apps/notion/0.6.4030.json) | — |
| Notion | [0.6.4280.json](apps/notion/0.6.4280.json) | — |
| Notion | [0.6.4304.json](apps/notion/0.6.4304.json) | — |
| Samsung Internet | [28.0.5.9.json](apps/samsung_browser/28.0.5.9.json) | — |
| Samsung Internet | [30.0.0.67.json](apps/samsung_browser/30.0.0.67.json) | — |
| Samsung Internet | [30.0.5.27.json](apps/samsung_browser/30.0.5.27.json) | — |
| Telegram | [12.9.0.json](apps/telegram/12.9.0.json) | — |
| Telegram | [12.9.2.json](apps/telegram/12.9.2.json) | — |
| Telegram | [13.1.1.json](apps/telegram/13.1.1.json) | — |
| Telegram | [13.2.2.json](apps/telegram/13.2.2.json) | — |
| WhatsApp | [2.26.27.85.json](apps/whatsapp/2.26.27.85.json) | — |
| WhatsApp | [2.26.43.74.json](apps/whatsapp/2.26.43.74.json) | — |
| WhatsApp | [2.26.44.90.json](apps/whatsapp/2.26.44.90.json) | — |

A dash indicates a profile with no additional Android-version filter. The device and app versions used in the repeated-acquisition study are listed in [runs.csv](../public-evaluation/runs.csv).

<a id="application-fields"></a>

## 📝 Application profile fields

### Top-level fields

| Field | Purpose |
| --- | --- |
| `app_id` | Supported application identifier; matches the parent directory |
| `package_name` | Android application package |
| `app_version` | Target app version; matches the JSON filename |
| `acquisition_methods` | Available methods: `materialize`, `export`, or both |
| `targets` | Target types, method/environment assignments and acquisition rules |
| `selectors` | Named UI element selectors used by the collector |
| `timings` | Timeouts, polling intervals and settling times |
| `parameters` | Optional collector-specific settings, such as output locations |
| `android_versions` | Optional Android major-version bounds |

For an Android-restricted profile, `min` is required and `max` is optional and inclusive. For example, Chrome 138 uses:

```json
{
  "android_versions": {
    "min": 9,
    "max": 9
  }
}
```

Both exact and nearest app-version selection respect these bounds. A restricted profile requires a parseable, compatible Android version.

### Required profiles and acquisition waits

All app collectors and app-specific parsers require an explicitly selected,
validated profile. Required `timings` and parser parameter sections are checked
when loading the profile; missing settings are not replaced with Python defaults.
Pure XML helpers receive an explicit field map, which may be empty when callers
already supply concrete resource names.

Acquisition waits use the profile's `timings`, grouped by purpose:

| Setting | Purpose |
| --- | --- |
| `default_timeout`, `poll_interval` | Wait for a UI condition and control how often it is checked |
| `application_start_settle`, `transition_settle` | Settle after application launch or navigation |
| `scroll_settle` | Settle after Chrome or Samsung Browser list scrolling |
| `history_load_timeout` | Wait for Chrome history loading |
| `history_group_expand_timeout`, `history_group_expand_settle` | Wait for and settle after Samsung history group expansion |
| `inventory_probe_interval` | Poll for file inventory changes in apps that acquire files |
| `inventory_stable_for` | Require Telegram's changed file inventory to remain stable |
| `application_return_settle` | Settle after returning to Notesnook |
| `share_target_wait`, `windows_receiver_timeout` | Wait for WhatsApp export sharing and the Windows receiver |
| `export_receipt_timeout`, `export_receipt_poll_interval` | Wait for and poll received WhatsApp export files |

Only the settings used by an app are required. Values must be finite,
nonnegative numbers; zero remains supported. Immediate observations (`timeout=0`)
are not delays. Existing condition-based waits remain condition-based; short
settling sleeps use the selected profile's value.

The 2026-11-10 migration preserves every previous effective wait value. New
browser scroll/group settling values are `0.4` seconds, and Telegram inventory
stability is `1.0` second. OS/Bluetooth-specific preparation logic is unchanged.
Chrome, Samsung Browser and Telegram profile hashes changed: regenerate their
session configuration JSON with `aura configure` before the next acquisition. Previously
preserved evidence packages and confirmed authority documents are not rewritten.

### Browser XML parsing rules

Chrome and Samsung Browser keep their version-specific extraction rules in
`parameters.list_parsing` in each app-version JSON profile. There is no Python
copy or fallback rule set. Shared functions in `aura.parsing` execute the rules;
the registry validates only the output fields and parts required by callers.
Profile loading rejects missing views, invalid syntax and incompatible outputs
before acquisition starts.

Direct parser calls also require an explicitly selected profile:

```python
from pathlib import Path
from aura.profiles import ProfileStore
from aura.apps.chrome.collector import parse_history

profile = ProfileStore(Path("profiles")).load_app("chrome", "156.0.8078.79")
records = parse_history(xml, profile=profile)
```

For legacy XML, explicitly select a compatible version JSON profile, or use its
preserved profile if it contains the required rules. Parsers never infer the
newest version or fill missing rules silently. Keep the original evidence package
unchanged and record which profile was used for re-analysis. Normal `aura acquire`
already supplies its selected profile; acquisition steps and waits are unchanged.

### Target definitions

Each entry in `targets` declares:

| Field | Purpose |
| --- | --- |
| `target_id` | Stable target-type identifier |
| `item_types` | Types of data or collection records represented by the target |
| `method` | One method declared in `acquisition_methods` |
| `acquisition_environments` | `device_only`, `controlled_online`, or both |
| `environment_rationale` | Analyst rationale for both environments, including any excluded environment |
| `expected_artifacts` | Required groups of acquisition evidence or output |
| `rules` | Identification, completion, result and error-handling criteria |

The current profiles assign environments per target:

| App / target scope | Device-only | Controlled-online |
| --- | --- | --- |
| Chrome / account, history, bookmarks, download entries, recent tabs | Execute | Execute |
| Samsung Browser / history, bookmarks, download and saved-page entries | Execute | Execute |
| Drive / inventory and basic metadata (`google_drive.inventory`) | Execute | Execute |
| Drive / activity (`google_drive.activity`) and originals (`google_drive.files`) | Exclude | Execute |
| Notesnook / notes, checklist state, revisions, attachments, trash records and Export | Execute | Execute |
| Notion / account, workspace and home-page inventory (`notion.inventory`) | Execute | Execute |
| Notion / members, page/subpage/DB content, attachments and Export | Exclude | Execute |
| Telegram / account, conversations, messages and implemented originals | Execute | Execute |
| WhatsApp / messages, photo/video originals and Export | Execute | Execute |

“Execute” means eligible when selected in the session configuration, not mandatory execution or guaranteed success. Browser download/saved-page entries are lists; WhatsApp Materialize document originals remain outside the implemented scope.

Each target records the analyst's reasons in `environment_rationale`. Both keys are required for current distributed profiles, even when only one environment is enabled. For example, `google_drive.files` includes:

```json
"environment_rationale": {
  "device_only": "An offline Download request may be queued and execute after connectivity returns, obscuring attribution. Exclude Download actions even when a cached copy might exist.",
  "controlled_online": "Request originals with connectivity available and verify resulting files. Retain the pending-request attribution check for unresolved earlier requests."
}
```

The field explains the decision; `acquisition_environments` remains the execution control. Rationale text is retained with the selected profile in `acquisition.json` and included in the canonical profile hash. Updating the reasons therefore requires a newly confirmed session configuration. The rationale records the pre-analysis decision; execution records retain the observed outcome for each attempt.

The loader and result validator reject malformed reasons when supplied. The profile audit also rejects missing reasons in current profiles. Historical profiles/packages without the field remain readable; reasons are not inserted retrospectively into preserved evidence.

For Notion, the device-only exclusion has an observed page-loading basis. In the retained S21 / Notion 0.6.4280 pilot, the examiner confirmed that pages did not load and could not be opened; Materialize records seven `notion_page_entry_unavailable` targets. Attachment identification/saving could not proceed through the unopened pages. The offline Export run likewise stopped at its first page-entry prerequisite (`notion_export_page_unavailable`), before testing the native Export operation. The Export exclusion is based on the failed page-entry prerequisite. Membership settings separately showed an explicit online-required message. These observations differ from Drive's deferred-download side-effect concern.

This policy has a coverage limitation: Notion may contain locally stored offline page bodies, but the current device-only profile retains listings and does not acquire those bodies. Offline-page cache states were not exhaustively tested. Later online content must not be treated as a verified snapshot of the preceding device-only body state. The maintained 0.6.4030 profile adopts the policy based on the cited 0.6.4280 observation.

The analyst selects `acquisition_environments` during version-specific pre-analysis, considering cached data, UI availability and deferred side effects. Current Drive policy avoids creating offline download requests that may execute when Wi-Fi returns. Current Notion policy preserves offline home inventory without entering members or page bodies; the cited S21 observation found that page bodies did not load or open in the tested device-only state. Reassess and test these assignments when updating a profile.

Session environment selection and target eligibility are separate. `configure` offers only nonempty method/environment scopes, and each confirmed execution scope covers the complete eligible target set. Policy-excluded targets are stored separately as `not_attempted` / null acquisition status with reason `profile_environment_excluded`. They are not failed attempts or evidence that data is absent. Drive activity has its own outcome, independent of basic metadata.

The November 11 policy update changes Drive and Notion target scopes; the subsequent rationale addition changes hashes for all 16 distributed profiles. Generate fresh session configuration JSON with `aura configure`; preserve historical packages and authority documents unchanged.

### Rule groups

| Rule | What it determines |
| --- | --- |
| `identification` | Data types to count and UI criteria used to identify them |
| `traversal_completion` | Recognized boundaries that establish completion of enumeration |
| `result` | Artifact groups required to establish acquisition |
| `error_handling` | Partial-result preservation and supported fallback behavior |

Every rule contains an `id` and `parameters`. The identifiers and parameters must match behavior implemented in the corresponding collector. Changing a JSON rule does not create a new acquisition algorithm.

For example, `ui_observation` requires both a screenshot and UI XML. Those records cannot satisfy a requirement for an original file. A supported display-only fallback can preserve UI information while recording that the original was not acquired.

<a id="selectors"></a>

## 🎯 UI selectors

Application selectors can use `resource_id`, `text` or `content_description`. Optional constraints refine the match, for example by class or clickability.

This is the `whatsapp.chat-row` entry from the WhatsApp profile's `selectors` object:

```json
{
  "whatsapp.chat-row": {
    "selector": {
      "kind": "resource_id",
      "owner_package": "com.whatsapp",
      "selector_id": "whatsapp.chat-row.resource-id",
      "value": "com.whatsapp:id/contact_row_container"
    },
    "constraints": {
      "clickable": true
    }
  }
}
```

The outer name is the collector's lookup key. Inside `selector`, `kind` chooses the matching method, `value` identifies the UI element, `owner_package` specifies its package, and `selector_id` names the selector.

Profiles do not store fixed tap coordinates or bounds. When a collector uses element bounds, it obtains them from the current UI hierarchy after identifying the element.

<a id="system-ui"></a>

## 📲 System UI profiles

System UI varies by manufacturer and OS version. These profiles supply known candidates for device settings and OS-owned interfaces; the runtime checks the resulting state before accepting an operation.

| Profile | Manufacturer matching | Candidate set |
| --- | --- | --- |
| [samsung.json](system_ui/samsung.json) | `samsung` | Samsung settings and System UI |
| [huawei.json](system_ui/huawei.json) | `huawei` | Huawei EMUI candidates |
| [generic.json](system_ui/generic.json) | `google`, `generic` | Android / Google Pixel-like candidates |

`generic` is not a catch-all fallback for every manufacturer. The CLI requires exactly one matching System UI profile.

Each file declares `name`, `description`, `manufacturer_match` and `operations`. Operations specify recognized strategies and candidates, such as settings intents, UI labels or commands for the existing runtime helpers. Required state checks are enforced: when notification hiding is required, failing to verify it prevents preparation from succeeding.

Device-specific selectors and navigation belong in the System UI profile; the common runtime performs and checks the declared operations.

Samsung, generic Android/Pixel and Huawei profiles declare Bluetooth power, pairing and cleanup operations for the same runtime. Pixel pairing can enter `Pair new device`; saved-device lookup can open `See all` and scroll within the profile's limits. Huawei device-settings buttons are resolved within the named receiver's row. The generic/Huawei `settings_navigation` fallback follows declared label steps from Settings when direct intents cannot reach Bluetooth; intermediate Settings rows are not accepted as destination page titles. Phone confirmation matches the receiver and dialog; the operator confirms the matching code on Windows. Existing pairings are reused. Cleanup removes only a phone-side pairing created for that execution, then restores the preceding Bluetooth power state.

<a id="authoring"></a>

## ✅ Update and validate a profile

1. **Analyse the intended app version in a separate test environment.** Confirm the data types, available UI controls, traversal boundaries and result checks.
2. **Use the flat filename.** Save `apps/<app-id>/<app-version>.json` with matching `app_id` and `app_version` values.
3. **Check collector support.** Keep rule IDs, selector keys and parameters aligned with the [app collector](../src/aura/apps). A new app or a changed UI procedure may also require collector code and tests.
4. **Validate without a device.** Load the profile and run the profile tests.
5. **Verify on the intended device/app combination.** Use `inspect` to confirm selection, then test acquisition and inspect the retained records. Retain the tested device/app versions and their acquisition results when adapting a profile.

From the repository root, after installing AURA:

```python
from aura.profiles import ProfileStore

store = ProfileStore("profiles")
app = store.load_app("telegram", "12.9.2")
system_ui = store.load_system_ui("samsung")

print(app.app_id, app.app_version)
print(system_ui.name)
```

```bash
python3 -m pytest tests/test_profiles.py -q
```

The loader checks path/identity agreement, declared methods and targets, rule contracts, coordinate-free profile data, timings and Android bounds. Loading successfully does not prove that the UI will match on a physical device.

<details>
<summary>How profile settings appear in acquisition records</summary>

The preserved schema 2 profile uses `routes` for the source file's `acquisition_methods`. It retains the selected app version and Android bounds; the installed app version is recorded separately.

Targets use `acquisition_environments`; a run or attempt uses `acquisition_environment`, and sessions record `environment_transitions`. Hyphenated names are used only in display text and run identifiers.

Profiles contain reusable acquisition settings. Actual items, attempts, timestamps, device state and acquisition outcomes belong in the execution records, not in the static profile.

</details>

## Review implementation additions (2026-11-05)

Each shipped target declares `remote_access_environments`: `[]` for device-only targets, or `["controlled_online"]` when that environment is available. This describes possible remote access, not proof that a particular run fetched server data. Case-specific authority references stay in the session.

Run `python3 tools/audit_aura_profiles.py --json profile-audit.json` for static checks against the actual collector contracts and literal selector references, plus version-to-version differences. Its `profile_sha256` and `execution_scopes` are the values needed for confirmed session execution scopes. Each scope covers the full target set for that method/environment; the current collectors do not enforce account/date/item filters. Device testing follows the acquisition-and-review step above.

## v9 Samsung history geometry

Samsung Browser profiles declare `ui_fields.history_divider` for clipped-row XML alignment. The shared matcher still requires two independent rows and consistent motion; row descriptions are not visit IDs. The parser digests and profile hashes changed, so regenerate session configurations containing Samsung Browser.

### Forward traversal swipe distance

Notesnook requires `parameters.note_list_scroll_ratio`; Notion requires
`parameters.page_scroll_ratio`, both in `(0, 1]`. Current profiles use 0.30;
previous-version profiles explicitly retain 0.20. These values apply only to
forward note-list traversal and forward page-content traversal, respectively.
Rewinds, attachment/trash/home lists, database boards and Actions menus retain
the existing 20% gestures. Notesnook's `history_scroll_ratio` remains 0.35;
horizontal and bounded gestures are unchanged.

The ratio controls centered gesture length relative to screen height, not
actual content displacement or guaranteed overlap. Telegram remains at 20%
after the S21 comparison exposed a continuity failure at 30%.
