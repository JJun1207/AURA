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

**The app version is the JSON filename, not a directory.** Each application profile is identified by its application ID and version.

<a id="catalogue"></a>

## 📚 Application-version catalogue

| Application | Profile file | Android filter |
| --- | --- | --- |
| Google Chrome | [138.0.7204.179.json](apps/chrome/138.0.7204.179.json) | Android 9 only |
| Google Chrome | [150.0.7871.124.json](apps/chrome/150.0.7871.124.json) | Android 10+ |
| Google Drive | [2.26.337.0.all.alldpi.json](apps/google_drive/2.26.337.0.all.alldpi.json) | — |
| Notesnook | [3.4.5.json](apps/notesnook/3.4.5.json) | — |
| Notion | [0.6.4030.json](apps/notion/0.6.4030.json) | — |
| Samsung Browser | [30.0.0.67.json](apps/samsung_browser/30.0.0.67.json) | — |
| Telegram | [12.9.0.json](apps/telegram/12.9.0.json) | — |
| Telegram | [12.9.2.json](apps/telegram/12.9.2.json) | — |
| WhatsApp | [2.26.27.85.json](apps/whatsapp/2.26.27.85.json) | — |

A dash means the profile declares no additional Android-version filter, **not** that every Android release has been validated.

Telegram 12.9.0 and 12.9.2 share the same acquisition content. The two Chrome profiles also share acquisition content, with different app-version and Android-applicability metadata.

Android filters specify profile applicability rather than tested configurations. See the [validation guide](../docs/VALIDATION.md) for evaluated versions and device combinations.

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

### Target definitions

Each entry in `targets` declares:

| Field | Purpose |
| --- | --- |
| `target_id` | Stable target-type identifier |
| `item_types` | Types of data or collection records represented by the target |
| `method` | One method declared in `acquisition_methods` |
| `acquisition_environments` | `device_only`, `controlled_online`, or both |
| `expected_artifacts` | Required groups of acquisition evidence or output |
| `rules` | Identification, completion, result and error-handling criteria |

The current profiles assign environments as follows:

| Applications | Materialize | Export |
| --- | --- | --- |
| Chrome, Samsung Browser | Device-only | — |
| Telegram, Google Drive | Device-only and controlled-online | — |
| WhatsApp, Notesnook | Device-only and controlled-online | Device-only |
| Notion | Device-only and controlled-online | Controlled-online |

These are execution assignments, not guarantees that every requested file can be obtained in every environment.

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

A manufacturer's profile is a candidate set, not a claim that every device from that manufacturer has been tested.

<a id="authoring"></a>

## ✅ Update and validate a profile

1. **Analyse the intended app version in a separate test environment.** Confirm the data types, available UI controls, traversal boundaries and result checks.
2. **Use the flat filename.** Save `apps/<app-id>/<app-version>.json` with matching `app_id` and `app_version` values.
3. **Check collector support.** Keep rule IDs, selector keys and parameters aligned with the [app collector](../src/aura/apps). A new app or a changed UI procedure may also require changes to the collector.
4. **Validate without a device.** Load the profile with `ProfileStore` to check its structure and supported rules, then run the included profile tests.
5. **Verify on the intended device/app combination.** Use `inspect` to confirm selection, then test acquisition and inspect the retained records. Copying a profile to a new version is not live validation.

From the repository root, after installing AURA:

```python
from aura.profiles import ProfileStore

store = ProfileStore("profiles")
app = store.load_app("telegram", "12.9.2")
system_ui = store.load_system_ui("samsung")

print(app.app_id, app.app_version)
print(system_ui.name)
```

Install the test dependencies and run the profile tests from the repository root:

```bash
python3 -m pip install -e ".[test]"
python3 -m pytest tests/test_profiles.py -q
```

The loader checks path/identity agreement, declared methods and targets, rule contracts, coordinate-free profile data, timings and Android bounds. Loading successfully does not prove that the UI will match on a physical device.

<details>
<summary>How profile settings appear in acquisition records</summary>

The preserved schema 2 profile uses `routes` for the source file's `acquisition_methods`. It retains the selected app version and Android bounds; the installed app version is recorded separately.

Targets use `acquisition_environments`; a run or attempt uses `acquisition_environment`, and sessions record `environment_transitions`. Hyphenated names are used only in display text and run identifiers.

Profiles contain reusable acquisition settings. Actual items, attempts, timestamps, device state and acquisition outcomes belong in the execution records, not in the static profile.

</details>
