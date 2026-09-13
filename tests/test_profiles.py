import json
from pathlib import Path

import pytest

from aura.models import Route
from aura.profiles import ProfileError, ProfileStore


@pytest.mark.parametrize("mutation", ["missing", "unknown", "inapplicable", "unknown_boundary", "wrong_app_boundary"])
def test_rejects_rules_that_cannot_be_applied(tmp_path, mutation):
    source = Path(__file__).resolve().parents[1] / "profiles/apps/telegram/12.9.2.json"
    document = json.loads(source.read_text())
    target = document["targets"][0]
    if mutation == "missing":
        target.pop("rules", None)
    elif mutation == "unknown":
        target["rules"] = {"identification": {"id": "invented_rule", "parameters": {}}}
    elif mutation == "inapplicable":
        target["rules"] = {"identification": {"id": "registered_ui_items", "parameters": {"counted_item_types": ["file"]}}}
    else:
        target["rules"]["traversal_completion"]["parameters"]["completion_conditions"] = [
            "invented_boundary" if mutation == "unknown_boundary" else "recursive_listing_exhausted"
        ]
    destination = tmp_path / "apps/telegram/12.9.2.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(document))
    with pytest.raises(ProfileError, match="rules"):
        ProfileStore(tmp_path).load_app("telegram", "12.9.2")


@pytest.mark.parametrize("mutation", ["screen_as_file", "audit_as_data", "unknown_kind", "unknown_fallback", "empty_conditions", "empty_evidence", "fallback_acquired", "missing_ui_pair"])
def test_rejects_unsupported_result_and_fallback_rules(tmp_path, mutation):
    source = Path(__file__).resolve().parents[1] / "profiles/apps/telegram/12.9.2.json"
    document = json.loads(source.read_text())
    target = next(target for target in document["targets"] if target["target_id"] == "telegram.attachments")
    required = target["rules"]["result"]["parameters"]["required_artifacts"]
    fallback = target["rules"]["error_handling"]["parameters"]["fallbacks"]
    if mutation == "screen_as_file":
        required["downloaded_file"] = ["screen_image"]
    elif mutation == "audit_as_data":
        required["structured_data"] = ["audit_record"]
    elif mutation == "unknown_kind":
        required["downloaded_file"] = ["invented_data"]
    elif mutation == "unknown_fallback":
        fallback["invented_fallback"] = {}
    elif mutation == "empty_conditions":
        fallback["display_fallback"]["conditions"] = []
    elif mutation == "empty_evidence":
        fallback["display_fallback"]["required_artifacts"] = []
    elif mutation == "fallback_acquired":
        fallback["display_fallback"]["acquisition_status"] = "acquired"
    else:
        target["expected_artifacts"].append("ui_observation")
        required["ui_observation"] = ["ui_hierarchy"]
    destination = tmp_path / "apps/telegram/12.9.2.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(document))
    with pytest.raises(ProfileError, match="rules"):
        ProfileStore(tmp_path).load_app("telegram", "12.9.2")


def write_profiles(
    root,
    *,
    app_id="telegram",
    acquisition_methods=None,
    targets=None,
):
    app_dir = root / "apps" / "telegram"
    system_ui_dir = root / "system_ui"
    app_dir.mkdir(parents=True)
    system_ui_dir.mkdir(parents=True)
    (app_dir / "12.8.3.json").write_text(
        json.dumps(
            {
                "app_id": app_id,
                "package_name": "org.telegram.messenger",
                "app_version": "12.8.3",
                "acquisition_methods": acquisition_methods or ["materialize"],
                "targets": targets
                if targets is not None
                else [
                    {
                        "target_id": "telegram.messages",
                        "item_types": ["message"],
                        "method": "materialize",
                        "acquisition_environments": ["device_only", "controlled_online"],
                        "expected_artifacts": ["structured_data"],
                        "rules": __import__("fakes").target_rules(("message",)),
                    }
                ],
                "selectors": {"chat_list": {"resourceId": "chat_list"}},
                "timings": {"default_timeout": 5},
                "parameters": {"test_parameter": "value"},
            }
        ),
        encoding="utf-8",
    )
    (system_ui_dir / "samsung.json").write_text(
        json.dumps(
            {
                "name": "samsung",
                "description": "Known Samsung System UI candidates.",
                "manufacturer_match": ["samsung"],
                "operations": {
                    "dnd": {
                        "main_switch_resource_ids": ["android:id/switch_widget"]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return ProfileStore(root)


def test_loads_exact_app_and_system_ui_profiles(tmp_path):
    store = write_profiles(tmp_path)

    app = store.load_app("telegram", "12.8.3")
    system_ui = store.load_system_ui("samsung")

    assert app.routes == (Route.MATERIALIZE,)
    assert app.targets[0].target_id == "telegram.messages"
    assert app.targets[0].item_types == ("message",)
    assert app.targets[0].method is Route.MATERIALIZE
    assert app.targets[0].acquisition_environments == ("device_only", "controlled_online")
    assert app.targets[0].expected_artifacts == ("structured_data",)
    assert app.package_name == "org.telegram.messenger"
    assert not hasattr(app, "profile_version")
    assert app.parameters == {"test_parameter": "value"}
    assert system_ui.profile_id == "samsung"
    assert system_ui.supports("SAMSUNG")
    assert system_ui.operations["dnd"]["main_switch_resource_ids"] == [
        "android:id/switch_widget"
    ]


@pytest.mark.parametrize("android_versions", [
    None, {}, [], {"max": 9}, {"min": 0}, {"min": -1}, {"min": True},
    {"min": "9"}, {"min": 9.0}, {"min": 10, "max": 9},
    {"min": 9, "max": None}, {"min": 9, "max": False},
    {"min": 9, "max": 10.0}, {"min": 9, "unexpected": 10},
])
def test_rejects_invalid_android_applicability(tmp_path, android_versions):
    store = write_profiles(tmp_path)
    path = tmp_path / "apps" / "telegram" / "12.8.3.json"
    document = json.loads(path.read_text())
    document["android_versions"] = android_versions
    path.write_text(json.dumps(document))

    with pytest.raises(ProfileError, match="android_versions"):
        store.load_app("telegram", "12.8.3")


def test_loads_all_flat_repository_profiles_and_paired_acquisition_content():
    root = Path(__file__).resolve().parents[1] / "profiles"
    paths = sorted((root / "apps").glob("*/*.json"))
    assert len(paths) == 9
    for path in paths:
        document = json.loads(path.read_text())
        profile = ProfileStore(root).load_app(path.parent.name, path.stem)
        assert profile.app_version == path.stem
        assert "profile_version" not in document
        assert not hasattr(profile, "profile_version")

    for app, older, newer in (
        ("telegram", "12.9.0", "12.9.2"),
        ("chrome", "138.0.7204.179", "150.0.7871.124"),
    ):
        documents = [json.loads((root / "apps" / app / f"{version}.json").read_text())
                     for version in (older, newer)]
        for document in documents:
            document.pop("app_version")
            document.pop("android_versions", None)
        assert documents[0] == documents[1]


@pytest.mark.parametrize(("version", "release", "supported"), [
    ("138.0.7204.179", "9", True), ("138.0.7204.179", "9.0.0", True),
    ("138.0.7204.179", "10", False), ("150.0.7871.124", "9", False),
    ("150.0.7871.124", "10", True), ("150.0.7871.124", "16", True),
    ("150.0.7871.124", None, False), ("150.0.7871.124", "Q", False),
    ("150.0.7871.124", "10preview", False),
])
def test_chrome_android_applicability(version, release, supported):
    root = Path(__file__).resolve().parents[1] / "profiles"
    assert ProfileStore(root).load_app("chrome", version).supports_android(release) is supported


def test_unrestricted_profile_does_not_require_android_release(tmp_path):
    profile = write_profiles(tmp_path).load_app("telegram", "12.8.3")
    assert profile.supports_android(None)
    assert profile.supports_android("unparseable")


def test_rejects_coordinate_candidates_in_system_ui_profile(tmp_path):
    store = write_profiles(tmp_path)
    path = tmp_path / "system_ui" / "samsung.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["operations"]["recent_apps"] = {
        "close_all": {"x": 540, "y": 2200}
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProfileError, match="coordinate"):
        store.load_system_ui("samsung")


def test_rejects_coordinate_selectors_in_app_profile(tmp_path):
    store = write_profiles(tmp_path)
    path = tmp_path / "apps" / "telegram" / "12.8.3.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["selectors"]["chat_list"] = {"x": 540, "y": 1200}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProfileError, match="coordinate"):
        store.load_app("telegram", "12.8.3")


def test_rejects_coordinate_parameters_in_app_profile(tmp_path):
    store = write_profiles(tmp_path)
    path = tmp_path / "apps" / "telegram" / "12.8.3.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["parameters"]["tap"] = {"coordinates": [540, 1200]}
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ProfileError, match="coordinate"):
        store.load_app("telegram", "12.8.3")


def test_repository_samsung_profile_provides_required_operations():
    root = Path(__file__).resolve().parents[1] / "profiles"

    profile = ProfileStore(root).load_system_ui("samsung")

    assert profile.supports("samsung")
    assert set(profile.operations) == {
        "dnd",
        "airplane",
        "network",
        "recent_apps",
        "bluetooth",
        "share_sheet",
    }
    assert profile.operations["network"]["wifi"]["strategy"] == (
        "adb_shell_candidates"
    )
    assert profile.operations["airplane"] == {
        "strategy": "adb_shell_candidates",
        "state_command": "cmd connectivity airplane-mode",
        "enabled_value": "enabled",
        "disabled_value": "disabled",
        "enable_commands": ["cmd connectivity airplane-mode enable"],
        "disable_commands": ["cmd connectivity airplane-mode disable"],
    }
    assert profile.operations["bluetooth"] == {
        "strategy": "adb_shell",
        "state_command": "settings get global bluetooth_on",
        "enabled_value": "1",
        "disabled_value": "0",
        "enable_command": "cmd bluetooth_manager enable",
        "disable_command": "cmd bluetooth_manager disable",
        "paired_devices_command": "dumpsys bluetooth_manager",
        "ui_fallback": {
            "direct_intents": [
                "am start -a android.settings.WIRELESS_SETTINGS"
            ],
            "row_labels": ["Bluetooth"],
            "switch_resource_ids": ["android:id/switch_widget"],
        },
    }
    hide_all = profile.operations["dnd"]["hide_notifications"]
    assert hide_all["required"] is True
    assert hide_all["click_resource_suffix"] == "switch_text"
    assert hide_all["state_resource_suffix"] == "switch_widget"
    assert "switch_background_resource_suffix" not in hide_all


def test_repository_samsung_profile_exposes_bluetooth_share_candidates():
    root = Path(__file__).resolve().parents[1] / "profiles"

    share = ProfileStore(root).load_system_ui(
        "samsung"
    ).operations["share_sheet"]

    assert share == {
        "strategy": "visible_then_expand",
        "owner_packages": [
            "android",
            "com.android.intentresolver",
        ],
        "resource_ids": [
            "com.android.intentresolver:id/text1",
            "android:id/text1",
        ],
        "bluetooth_labels": ["Bluetooth"],
        "fallback": {
            "swipe_direction": "right",
            "more_labels": ["More"],
        },
        "receiver_picker": {
            "owner_packages": ["com.android.settings"],
            "title_labels": ["Select device"],
            "receiver_resource_ids": ["android:id/title"],
        },
    }


@pytest.mark.parametrize(
    ("name", "manufacturer", "dnd_strategy"),
    [
        ("generic", "Google", "button_based"),
        ("huawei", "HUAWEI", "switch_basic"),
    ],
)
def test_repository_additional_system_ui_profiles_are_coordinate_free(
    name,
    manufacturer,
    dnd_strategy,
):
    root = Path(__file__).resolve().parents[1] / "profiles"

    profile = ProfileStore(root).load_system_ui(name)

    assert profile.supports(manufacturer)
    expected_operations = {
        "dnd",
        "airplane",
        "network",
        "recent_apps",
    }
    if name == "generic":
        expected_operations.add("bluetooth")
    assert set(profile.operations) == expected_operations
    assert profile.operations["dnd"]["strategy"] == dnd_strategy
    serialized = json.dumps(profile.operations)
    assert "control_x_ratio" not in serialized
    assert "control_y_ratio" not in serialized
    assert "main_switch_max_top_ratio" not in serialized


def test_repository_telegram_profile_is_version_bound_and_coordinate_free():
    root = Path(__file__).resolve().parents[1] / "profiles"

    profile = ProfileStore(root).load_app("telegram", "12.9.2")

    assert profile.package_name == "org.telegram.messenger"
    assert profile.routes == (Route.MATERIALIZE,)
    assert {target.target_id for target in profile.targets} == {
        "telegram.account",
        "telegram.conversations",
        "telegram.attachments",
    }
    assert "max_scrolls" not in profile.parameters
    assert "telegram.list.end.contacts" in profile.selectors
    assert profile.parameters["attachment_roots"] == {
        "file": [
            "/sdcard/Download",
            "/sdcard/Download/Telegram",
        ],
        "photo": ["/sdcard/Pictures/Telegram"],
        "video": [
            "/sdcard/Movies/Telegram",
            "/sdcard/Pictures/Telegram",
        ],
    }


def test_repository_notesnook_profile_is_version_bound_and_coordinate_free():
    root = Path(__file__).resolve().parents[1] / "profiles"

    profile = ProfileStore(root).load_app("notesnook", "3.4.5")

    assert profile.package_name == "com.streetwriters.notesnook"
    assert profile.routes == (Route.MATERIALIZE, Route.EXPORT)
    assert profile.parameters == {
        "inventory_probes": 20,
        "export_root": "/sdcard/Download/AURA",
    }
    assert set(profile.selectors) == {
        "notesnook.workspace.header",
        "notesnook.workspace.logged-out",
        "notesnook.notes-list",
        "notesnook.note-menu",
        "notesnook.side-menu.open",
        "notesnook.side-menu.settings",
        "notesnook.side-menu.notes",
        "notesnook.side-menu.trash",
        "notesnook.account.sync-now",
        "notesnook.history.open",
        "notesnook.attachments.open",
        "notesnook.trash.header",
        "notesnook.selection.header",
        "notesnook.selection.export",
        "notesnook.export.markdown-frontmatter",
        "documentsui.download-root",
        "documentsui.aura-directory",
        "documentsui.current-aura",
        "documentsui.use-folder",
        "documentsui.confirm-folder",
    }


def test_repository_whatsapp_profile_is_version_bound():
    root = Path(__file__).resolve().parents[1] / "profiles"

    profile = ProfileStore(root).load_app(
        "whatsapp", "2.26.27.85"
    )

    assert profile.package_name == "com.whatsapp"
    assert profile.routes == (Route.EXPORT, Route.MATERIALIZE)
    assert {
        "whatsapp.chat-list",
        "whatsapp.chat-row",
        "whatsapp.chat-name",
        "whatsapp.chat-screen",
        "whatsapp.message-list",
        "whatsapp.message-text",
        "whatsapp.message-time",
        "whatsapp.message-status",
        "whatsapp.date-divider",
        "whatsapp.media-container",
        "whatsapp.photo",
        "whatsapp.video",
        "whatsapp.document-content",
        "whatsapp.document-title",
        "whatsapp.document-size",
        "whatsapp.document-type",
        "whatsapp.media-viewer",
        "whatsapp.media-save",
        "whatsapp.media-controls",
        "whatsapp.scroll-bottom",
        "whatsapp.more-options",
        "whatsapp.more",
        "whatsapp.export-chat",
        "whatsapp.include-media",
    } <= set(profile.selectors)
    assert set(profile.parameters["materialize_roots"]) == {
        "photo",
        "video",
    }
    attachments = next(
        target
        for target in profile.targets
        if target.target_id == "whatsapp.attachments"
    )
    assert attachments.item_types == ("photo", "video")


def test_rejects_profile_whose_identity_does_not_match_path(tmp_path):
    store = write_profiles(tmp_path, app_id="not-telegram")

    with pytest.raises(ProfileError, match="app_id"):
        store.load_app("telegram", "12.8.3")


def test_rejects_unsafe_profile_path_segment(tmp_path):
    with pytest.raises(ProfileError, match="unsafe"):
        ProfileStore(tmp_path).load_app("../telegram", "12.8.3")


@pytest.mark.parametrize(
    "acquisition_methods",
    [["materialize", "materialize"], ["display"]],
)
def test_rejects_duplicate_or_unknown_acquisition_methods(
    tmp_path, acquisition_methods
):
    store = write_profiles(
        tmp_path, acquisition_methods=acquisition_methods
    )

    with pytest.raises(ProfileError, match="acquisition_methods"):
        store.load_app("telegram", "12.8.3")


@pytest.mark.parametrize(
    ("targets", "message"),
    [
        ([], "targets"),
        (
            [
                {
                    "target_id": "telegram.messages",
                    "item_types": [],
                    "method": "materialize",
                    "acquisition_environments": ["device_only"],
                    "expected_artifacts": ["structured_data"],
                }
            ],
            "item_types",
        ),
        (
            [
                {
                    "target_id": "telegram.messages",
                    "item_types": ["message"],
                    "method": "display",
                    "acquisition_environments": ["device_only"],
                    "expected_artifacts": ["structured_data"],
                }
            ],
            "method",
        ),
        (
            [
                {
                    "target_id": "telegram.messages",
                    "item_types": ["message"],
                    "method": "materialize",
                    "acquisition_environments": ["online"],
                    "expected_artifacts": ["structured_data"],
                }
            ],
            "acquisition_environments",
        ),
        (
            [
                {
                    "target_id": "telegram.messages",
                    "item_types": ["message"],
                    "method": "materialize",
                    "acquisition_environments": ["device_only"],
                    "expected_artifacts": [],
                }
            ],
            "expected_artifacts",
        ),
        (
            [
                {
                    "target_id": "telegram.messages",
                    "item_types": ["message"],
                    "method": "materialize",
                    "acquisition_environments": ["device_only"],
                    "expected_artifacts": ["structured_data"],
                },
                {
                    "target_id": "telegram.messages",
                    "item_types": ["attachment"],
                    "method": "materialize",
                    "acquisition_environments": ["controlled_online"],
                    "expected_artifacts": ["downloaded_file"],
                },
            ],
            "target_id",
        ),
    ],
)
def test_rejects_invalid_acquisition_targets(tmp_path, targets, message):
    store = write_profiles(tmp_path, targets=targets)

    with pytest.raises(ProfileError, match=message):
        store.load_app("telegram", "12.8.3")
